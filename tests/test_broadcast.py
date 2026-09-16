"""Tests for the per-client sender — ``specs5/plan-ag/risks.md`` § AG-R-19.

The recorded tripwire is deliberately **not** on frame order. A test that
issues two sends fire-and-forget and asserts they arrive in order passes
today and proves nothing: Probe A measured ``websockets`` preserving wire
order even against a peer that never reads a byte, because ``send(str)``
reaches ``transport.write()`` with no suspension point and ``create_task``
is FIFO. Ordering holds for reasons unrelated to this design.

What bites is **buffer depth and eviction**, which is what the first two
classes below assert against a real socket and a real deaf peer:

- the transport write buffer stays within one frame of its low-water mark,
  rather than growing to the 8 MB the concurrent shape reached;
- a client that stops draining is closed with the documented code (AG-23),
  rather than being sent a snapshot down a channel with zero throughput;
- a second client is served at full speed while the first is stalled, which
  is the half that would have caught ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import socket

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from aic_dc import broadcast as broadcast_module
from aic_dc.broadcast import (
    CLOSE_CODE_QUEUE_OVERFLOW,
    CLOSE_REASON_QUEUE_OVERFLOW,
    Broadcaster,
    ClientSender,
)

# Big enough that a few hundred frames cannot hide in the kernel's socket
# buffers — a "deaf" peer that the OS quietly absorbs is not deaf, and a
# test built on one asserts nothing.
PAD = "x" * 4000

HIGH_WATER = 4096
LOW_WATER = 2048


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeSocket:
    """The wire, for tests that only need to see frames."""

    def __init__(self):
        self.frames: list[str] = []
        self.closed: tuple[int | None, str | None] | None = None

    async def send(self, frame):
        self.frames.append(frame)

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)


class RecordingSocket:
    """A real websocket with its ``close`` observed.

    A deaf peer never answers a close handshake, so the only place the
    documented close code is visible is at the moment it is issued.
    """

    def __init__(self, inner):
        self._inner = inner
        self.closes: list[tuple[int, str]] = []

    @property
    def transport(self):
        return self._inner.transport

    async def send(self, frame):
        await self._inner.send(frame)

    async def close(self, code=None, reason=None):
        self.closes.append((code, reason))
        await self._inner.close(code=code, reason=reason)


class FakeRemote:
    """A JRPC remote, as far as the broadcaster is concerned."""

    def __init__(self, uuid, rpcs=None):
        self.uuid = uuid
        self.rpcs = {} if rpcs is None else rpcs


async def drain(times=30):
    """Let the sender tasks run without advancing the clock."""
    for _ in range(times):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# A real server, and a client that never reads
# ---------------------------------------------------------------------------


async def deaf_client(port):
    """Complete the websocket handshake by hand, then stop reading.

    ``websockets.connect`` reads eagerly, which is the opposite of what this
    needs. The receive buffer is pinned small so the kernel cannot absorb
    the backlog on the peer's behalf.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    sock = writer.transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(
        f"GET / HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    await writer.drain()
    await reader.readuntil(b"\r\n\r\n")
    return writer


class Server:
    """A websocket server that hands each accepted connection back."""

    def __init__(self):
        self.accepted: list = []
        self._arrived = asyncio.Event()

    async def handler(self, ws):
        transport = ws.transport
        transport.set_write_buffer_limits(high=HIGH_WATER, low=LOW_WATER)
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        self.accepted.append(ws)
        self._arrived.set()
        try:
            await ws.wait_closed()
        except Exception:
            pass

    async def next_connection(self):
        await asyncio.wait_for(self._arrived.wait(), timeout=5)
        self._arrived.clear()
        return self.accepted[-1]


@pytest.fixture
async def live_server():
    state = Server()
    async with serve(state.handler, "127.0.0.1", 0) as server:
        state.port = server.sockets[0].getsockname()[1]
        yield state


# ---------------------------------------------------------------------------
# The frame on the wire
# ---------------------------------------------------------------------------


class TestTheFrameOnTheWire:
    """Events are notifications now, which is what deletes the 120s wait."""

    async def send_one(self, **kwargs):
        ws = FakeSocket()
        caster = Broadcaster(**kwargs)
        caster.attach(FakeRemote("c1"), ws)
        caster.broadcast("toolUse", ("r1", {"id": "t1"}))
        await drain()
        return ws

    async def test_an_event_carries_no_id(self):
        """No ``id`` is the whole mechanism.

        jrpc-oo's ``JRPC2.call`` stamps a request ID, registers a callback
        under it and arms a 120s ``timeout_handler`` task — per event. The
        browser replies, a future resolves, and the master turn was waiting
        on it. A notification has none of that: measured in
        ``webapp/node_modules/jrpc/jrpc.js``, an absent ``id`` becomes
        ``null``, no local timer is armed, the handler still runs through
        ``setImmediate``, and ``sendResponse`` returns before it builds a
        frame.
        """
        ws = await self.send_one()
        assert "id" not in json.loads(ws.frames[0])

    async def test_the_envelope_is_the_one_jrpc_oo_was_sending(self):
        """Byte-compatible with the old path, minus the id.

        The browser dispatches on ``request.method`` and hands
        ``request.params`` straight to the exposed handler, so the params
        shape is a contract with 37 handlers rather than a detail.
        """
        ws = await self.send_one()
        frame = json.loads(ws.frames[0])
        assert frame["jsonrpc"] == "2.0"
        assert frame["method"] == "AcApp.toolUse"
        assert frame["params"] == {"args": ["r1", {"id": "t1"}]}

    async def test_one_frame_object_is_shared_by_every_client(self):
        """Why per-client deques cost 0.47% rather than N times the payload.

        Probe C: a deque holds a *reference*, so five extra per-client
        deques over 1000 frames of 10 KB cost 46,856 bytes against
        10,000,000 bytes of payload. That is only true if the frame is
        built once, which is what this asserts.
        """
        caster = Broadcaster()
        first = caster.attach(FakeRemote("c1"), FakeSocket())
        second = caster.attach(FakeRemote("c2"), FakeSocket())
        caster.broadcast("toolUse", ("r1", {"pad": PAD}))
        assert first._queue[0] is second._queue[0]


# ---------------------------------------------------------------------------
# The recorded tripwire
# ---------------------------------------------------------------------------


class TestTheRecordedTripwire:
    """Buffer depth and eviction, against a socket that is really stalled."""

    async def test_a_stalled_client_is_evicted_and_the_transport_stays_clamped(
        self, live_server, monkeypatch, caplog
    ):
        monkeypatch.setattr(broadcast_module, "CLOSE_HANDSHAKE_TIMEOUT", 0.05)
        peer = await deaf_client(live_server.port)
        try:
            ws = RecordingSocket(await live_server.next_connection())
            caster = Broadcaster(max_frames=64, max_bytes=64 * 1024 * 1024)
            sender = caster.attach(FakeRemote("deaf"), ws)

            peak = 0
            with caplog.at_level(logging.WARNING, logger="aic_dc.broadcast"):
                for i in range(400):
                    caster.broadcast("streamChunk", ("r1", {"i": i, "pad": PAD}))
                    await drain(4)
                    peak = max(peak, ws.transport.get_write_buffer_size())
                    if sender.evicted:
                        break
                await drain()

            frame_len = len(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "AcApp.streamChunk",
                        "params": {"args": ["r1", {"i": 0, "pad": PAD}]},
                    }
                )
            )

            # Not vacuous: writes really happened. Without this, a sender
            # that never sent anything would satisfy the clamp trivially.
            assert peak > 0
            # The clamp. A single sender suspended in `drain()` cannot issue
            # the next write, so the buffer can hold at most what was under
            # the low-water mark plus the one frame that pushed it over.
            # Probe A's concurrent shape reached 8,040,846 bytes here.
            assert peak <= LOW_WATER + frame_len

            # AG-23: closed, with a code that says why, rather than sent a
            # snapshot down a channel that by construction is not moving.
            assert sender.evicted
            assert ws.closes == [
                (CLOSE_CODE_QUEUE_OVERFLOW, CLOSE_REASON_QUEUE_OVERFLOW)
            ]
            assert "not draining its socket" in caplog.text
        finally:
            peer.close()

    async def test_a_deaf_peer_really_does_stall_the_sender(self, live_server):
        """The control for the test above.

        If the OS absorbed the backlog, everything here would pass while
        measuring nothing — so this asserts the queue genuinely backs up
        under the same conditions, with a bound too high to evict.
        """
        peer = await deaf_client(live_server.port)
        try:
            ws = await live_server.next_connection()
            caster = Broadcaster(max_frames=10_000)
            sender = caster.attach(FakeRemote("deaf"), ws)
            for i in range(200):
                caster.broadcast("streamChunk", ("r1", {"i": i, "pad": PAD}))
            await drain()
            assert sender.depth > 0
            assert not sender.evicted
        finally:
            peer.close()


# ---------------------------------------------------------------------------
# The half that would have caught `gather`
# ---------------------------------------------------------------------------


class TestOneClientDoesNotPaceAnother:
    async def test_a_reading_client_is_served_while_a_deaf_one_backs_up(
        self, live_server
    ):
        """``call_all_remotes`` gathered over every remote, so the slowest
        browser set the pace for all of them. Per-client senders share
        nothing — not a cursor, not a queue, not a task.
        """
        count = 200
        peer = await deaf_client(live_server.port)
        try:
            deaf_ws = await live_server.next_connection()
            async with connect(f"ws://127.0.0.1:{live_server.port}") as client:
                live_ws = await live_server.next_connection()

                caster = Broadcaster(max_frames=10_000)
                deaf = caster.attach(FakeRemote("deaf"), deaf_ws)
                caster.attach(FakeRemote("live"), live_ws)

                for i in range(count):
                    caster.broadcast("streamChunk", ("r1", {"i": i, "pad": PAD}))

                seen = []
                async with asyncio.timeout(10):
                    while len(seen) < count:
                        seen.append(json.loads(await client.recv()))

            assert [f["params"]["args"][1]["i"] for f in seen] == list(range(count))
            # And the stalled one is still stalled, so the assertion above
            # is about concurrency rather than about a fast loopback.
            assert deaf.depth > 0
        finally:
            peer.close()


# ---------------------------------------------------------------------------
# The turn does not wait
# ---------------------------------------------------------------------------


class TestTheTurnDoesNotWait:
    def test_queuing_an_event_cannot_suspend(self):
        """Stated structurally, because a timing assertion alone can pass on
        a quiet machine for the wrong reason. ``broadcast`` is a plain
        ``def``: there is no await in it, so there is nothing a client can
        pace.
        """
        assert not inspect.iscoroutinefunction(Broadcaster.broadcast)
        assert not inspect.iscoroutinefunction(ClientSender.enqueue)

    async def test_a_wedged_client_does_not_slow_the_producer(self, live_server):
        peer = await deaf_client(live_server.port)
        try:
            ws = await live_server.next_connection()
            caster = Broadcaster(max_frames=10_000)
            sender = caster.attach(FakeRemote("deaf"), ws)

            # Wedge it first, so the measurement below is taken against a
            # sender that is genuinely suspended in `drain()`.
            for i in range(200):
                caster.broadcast("streamChunk", ("r1", {"i": i, "pad": PAD}))
            await drain()
            assert sender.depth > 0

            loop = asyncio.get_running_loop()
            started = loop.time()
            for i in range(200):
                caster.broadcast("streamChunk", ("r1", {"i": i, "pad": PAD}))
            elapsed = loop.time() - started

            # The old path could spend up to `DEFAULT_REMOTE_TIMEOUT` — 120s
            # — per event here.
            assert elapsed < 0.5
        finally:
            peer.close()


# ---------------------------------------------------------------------------
# Causal order, which the queue must not be able to invert
# ---------------------------------------------------------------------------


class SweepingPermissions:
    """The broker, reduced to the one thing that matters here.

    Engine bookkeeping is an event *producer*: the real
    ``cancel_for_agent`` denies what the subagent was waiting on and
    announces it, which re-enters the dispatch path.
    """

    def __init__(self, emit):
        self._emit = emit

    async def cancel_for_agent(self, agent_id):
        await self._emit("permissionResolved", None, {"agent_id": agent_id})
        return 1


class TestCausalOrderSurvivesTheQueue:
    async def test_a_terminal_subagent_event_precedes_the_denial_it_causes(
        self, tmp_path
    ):
        """The assertion that fails if bookkeeping moves ahead of the enqueue.

        ``_dispatch`` runs ``_sweep_ended_subagent`` *after* the broadcast,
        and an earlier revision of AG-R-19 said to move it in front "where
        it never needed a browser at all". That would have queued the denial
        ahead of the termination explaining it — a dialog denied for a
        subagent that still appears to be running. Leaving the statement
        order alone is what keeps this true once the broadcast stops
        awaiting.
        """
        from aic_dc.claude_code.engine_config import EngineConfig
        from aic_dc.claude_code.messages import Event
        from aic_dc.claude_code.service import ClaudeCodeService

        class Config:
            repo_root = tmp_path
            config_dir = None
            aic_dc_dir = tmp_path / ".aic-dc"

        ws = FakeSocket()
        caster = Broadcaster()
        caster.attach(FakeRemote("c1"), ws)

        async def callback(name, *args):
            caster.broadcast(name, args)

        svc = ClaudeCodeService(
            Config(), event_callback=callback, engine_config=EngineConfig()
        )
        svc.permissions = SweepingPermissions(callback)

        await svc._dispatch(
            Event("subagentEvent", {"agent_id": "a1", "terminal": True}), "r1"
        )
        await drain()

        methods = [json.loads(f)["method"] for f in ws.frames]
        assert methods == ["AcApp.subagentEvent", "AcApp.permissionResolved"]


# ---------------------------------------------------------------------------
# Attaching, detaching, and the things that must stay loud
# ---------------------------------------------------------------------------


class TestAttachingAndDetaching:
    async def test_detaching_stops_the_sender(self):
        ws = FakeSocket()
        caster = Broadcaster()
        sender = caster.attach(FakeRemote("c1"), ws)
        caster.detach("c1")
        assert sender.closed
        assert caster.broadcast("toolUse", ()) == 0

    async def test_detaching_an_unknown_client_is_not_an_error(self):
        Broadcaster().detach("never-here")

    async def test_reattaching_a_uuid_replaces_the_sender(self):
        caster = Broadcaster()
        first = caster.attach(FakeRemote("c1"), FakeSocket())
        second = caster.attach(FakeRemote("c1"), FakeSocket())
        assert first.closed
        assert caster.senders["c1"] is second

    async def test_an_event_nobody_can_handle_is_reported_once(self, caplog):
        """The one loud signal the old path had, kept.

        A notification has no reply, and the browser drops an unknown method
        silently when there is no ``id`` to answer with — so renaming a
        handler would stop events arriving with nothing anywhere saying so.
        """
        caster = Broadcaster()
        caster.attach(FakeRemote("c1", {"AcApp.toolUse": None}), FakeSocket())
        with caplog.at_level(logging.WARNING, logger="aic_dc.broadcast"):
            caster.broadcast("renamedAway", ())
            caster.broadcast("renamedAway", ())
        assert caplog.text.count("does not expose") == 1

    async def test_the_handshake_window_does_not_warn_or_drop(self):
        """Before ``system.listComponents`` returns there is nothing to check
        against, so the frame is queued rather than dropped. The old
        ``call``-proxy path dropped it — the key did not exist yet.
        """
        ws = FakeSocket()
        caster = Broadcaster()
        sender = caster.attach(FakeRemote("c1"), ws)
        assert caster.broadcast("toolUse", ()) == 1
        await drain()
        assert len(ws.frames) == 1
        assert not sender.evicted

    async def test_an_unserialisable_event_is_reported_rather_than_dropped(
        self, caplog
    ):
        caster = Broadcaster()
        caster.attach(FakeRemote("c1"), FakeSocket())
        with caplog.at_level(logging.WARNING, logger="aic_dc.broadcast"):
            assert caster.broadcast("toolUse", (object(),)) == 0
        assert "could not be serialised" in caplog.text

    async def test_shutdown_closes_everything(self):
        caster = Broadcaster()
        sender = caster.attach(FakeRemote("c1"), FakeSocket())
        caster.shutdown()
        assert sender.closed
        assert caster.senders == {}

    async def test_a_byte_bound_evicts_before_a_frame_bound_would(self):
        """A frame count is not a memory bound: a ``turnUsage`` payload is a
        few hundred bytes and a tool result can be megabytes.
        """
        ws = FakeSocket()
        caster = Broadcaster(max_frames=10_000, max_bytes=20_000)
        sender = caster.attach(FakeRemote("c1"), ws)
        for _ in range(10):
            caster.broadcast("toolResult", ("r1", {"pad": PAD}))
            if sender.evicted:
                break
        assert sender.evicted
        assert sender.depth == 0


# ---------------------------------------------------------------------------
# The server seam
# ---------------------------------------------------------------------------


class TestTheServerWiring:
    """One override covers a collab server and a solo one alike."""

    @pytest.fixture
    def server(self, monkeypatch):
        from aic_dc.rpc import MaxSizeJRPCServer

        # The JRPC handshake needs a live browser and arms a 120s timer.
        # Neither is what this seam is about.
        monkeypatch.setattr(
            MaxSizeJRPCServer, "setup_remote", lambda self, remote, ws: None
        )
        return MaxSizeJRPCServer(port=0)

    async def test_creating_a_remote_attaches_a_sender(self, server):
        remote = server.create_remote(FakeSocket())
        assert remote.uuid in server.broadcaster.senders

    async def test_removing_a_remote_detaches_it(self, server):
        remote = server.create_remote(FakeSocket())
        server.rm_remote(None, remote.uuid)
        assert remote.uuid not in server.broadcaster.senders

    async def test_the_facade_shares_one_broadcaster_with_its_inner_server(self):
        from aic_dc.rpc import RpcServer

        facade = RpcServer(port=0)
        assert facade.broadcaster is facade._create_inner_server().broadcaster

    async def test_a_collab_server_has_one_too(self):
        from aic_dc.collab import CollabServer

        assert isinstance(CollabServer(port=0).broadcaster, Broadcaster)
