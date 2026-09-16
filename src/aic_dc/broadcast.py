"""One sender task per client — ``specs5/plan-ag/risks.md`` § AG-R-19.

Server-push events used to travel down jrpc-oo's ``call`` proxy, which
gathers over every connected remote and awaits each browser's JSON-RPC
*reply*. Three things were wrong with that, and only the first is the one
that gets noticed:

1. The master turn blocked for up to ``DEFAULT_REMOTE_TIMEOUT`` (120 s) per
   event, waiting for an acknowledgement nothing reads. Every client handler
   is a thin re-dispatcher ending ``return true``, and ``call_all_remotes``
   packs those values into a ``{uuid: result}`` dict that no caller touches.
2. ``asyncio.gather`` over all remotes made the slowest browser set the pace
   for every other one.
3. Nothing propagated backpressure. ``transport.write()`` never refuses — the
   high-water mark only flips a flag — so a browser that stops reading costs
   unbounded memory *below* the application layer, where no application-level
   buffer can see it.

**Ordering is not what this module buys, and the register says so in the
entry above: it was measured.** In websockets 16.1.1 ``await ws.send(str)``
runs from task start through ``transport.write()`` with no suspension point,
and ``create_task`` is FIFO, so fire-and-forget already preserves wire order
against a peer that never reads a byte (Probe A: 2000/2000 frames in order,
1996 of them written while the transport was paused). What a serialised
sender buys is (3): a task suspended in ``drain()`` cannot issue the next
write, which forces the backlog up into a queue that can have a *policy*.
Probe B, same conditions: 4,022 peak transport bytes serialised against
8,040,846 concurrent.

The policy is AG-23 — **evict, never rehydrate**. Overflow is *defined* as
the client not draining its socket, so the channel a repair would travel down
has zero throughput at the moment it is needed. A snapshot sent into that is
not slow, it is a loop that never starts. Closing with an explicit code hands
the client to the reconnect path it already has, which re-baselines behind
AG-R-20's gate.

And the queue is a strict FIFO with nothing clever in it: no coalescing
(lossless only if every reducer is append-only, which is unverified), no
prioritising ``permissionRequest`` (a dialog about a ``toolUse`` the user has
not been shown is the causal inversion AG-R-19 corrects elsewhere), no
shedding (silent loss by definition).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)


#: Close code sent to a client whose send queue overflowed.
#:
#: In the private-use range (4000–4999) so it cannot be confused with a
#: protocol-level close. ``collab`` already uses 1008 for a denied admission;
#: this is a different thing and says so.
CLOSE_CODE_QUEUE_OVERFLOW = 4001

#: Reason string paired with the code above. Short, because the close frame's
#: reason field is capped at 123 bytes.
CLOSE_REASON_QUEUE_OVERFLOW = "event queue overflow"

#: How many frames one client may have waiting before it is evicted.
#:
#: A healthy client on loopback holds single digits — Probe B flags a stalled
#: peer within about nineteen frames — so this is three orders of magnitude
#: above normal and reaching it means the peer has stopped reading.
SEND_QUEUE_MAX_FRAMES = 4096

#: How many bytes one client may have waiting before it is evicted.
#:
#: A frame count is not a memory bound: a ``turnUsage`` payload is a few
#: hundred bytes and a tool result can be megabytes, so the byte bound is
#: what actually caps the damage a stalled client does.
SEND_QUEUE_MAX_BYTES = 16 * 1024 * 1024

#: How long to wait for the close handshake before aborting the transport.
#:
#: A peer that is not reading its socket is also not going to answer a close
#: frame, and ``ws.close()`` waits for that answer — the same wedge the send
#: path has. The timeout is the difference between evicting a client and
#: acquiring a second stuck task.
CLOSE_HANDSHAKE_TIMEOUT = 2.0


class ClientSender:
    """One websocket, one FIFO, one task that drains it.

    The task awaits ``ws.send`` before taking the next frame off the queue.
    That is the whole mechanism: while it is suspended in ``drain()`` it is
    not writing, so the backlog accumulates here — bounded, countable and
    evictable — instead of in the transport's write buffer, which is none of
    those things.

    Created attached but idle. The task starts on the first frame, so
    constructing a sender outside a running loop is legal and costs nothing.
    """

    def __init__(
        self,
        uuid: str,
        websocket: Any,
        remote: Any = None,
        *,
        max_frames: int = SEND_QUEUE_MAX_FRAMES,
        max_bytes: int = SEND_QUEUE_MAX_BYTES,
    ) -> None:
        self.uuid = uuid
        self.websocket = websocket
        self.remote = remote
        self.evicted = False
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._queue: deque[str] = deque()
        self._queued_bytes = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Introspection — the tripwire asserts on these
    # ------------------------------------------------------------------

    @property
    def depth(self) -> int:
        """Frames waiting, excluding the one currently in flight.

        A frame is taken off the queue *before* the ``send`` is awaited, so
        this counts what is genuinely still buffered rather than what has
        been handed to the transport.
        """
        return len(self._queue)

    @property
    def queued_bytes(self) -> int:
        """Bytes waiting, by the same rule as :attr:`depth`."""
        return self._queued_bytes

    @property
    def closed(self) -> bool:
        """True once the sender has stopped accepting frames."""
        return self._closed

    # ------------------------------------------------------------------
    # The producer side — synchronous, and never blocks the engine
    # ------------------------------------------------------------------

    def enqueue(self, frame: str) -> bool:
        """Queue one frame. Returns False if it was not accepted.

        Synchronous on purpose: the caller is ``service._dispatch``, in the
        middle of a turn, and the entire point of AG-R-19 is that a turn does
        not wait on a browser. Nothing here awaits, so nothing here can be
        paced by a client.

        ``len(frame)`` is the byte count as well as the character count —
        :func:`json.dumps` defaults to ``ensure_ascii=True``, which is how
        this frame was built and how jrpc-oo builds its own.
        """
        if self._closed:
            return False

        size = len(frame)
        if (
            len(self._queue) + 1 > self._max_frames
            or self._queued_bytes + size > self._max_bytes
        ):
            self._evict()
            return False

        self._queue.append(frame)
        self._queued_bytes += size
        self._ensure_task()
        self._wake.set()
        return True

    def _ensure_task(self) -> None:
        """Start the drain task if it is not already running."""
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:
            # No loop — a sender constructed in a synchronous test. The frame
            # stays queued and the task starts on the first enqueue that does
            # happen inside a loop.
            self._task = None

    # ------------------------------------------------------------------
    # The consumer side
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Drain the queue, one awaited send at a time, until closed."""
        try:
            while not self._closed:
                while not self._queue:
                    self._wake.clear()
                    # Re-checked after the clear with no await in between, so
                    # a frame enqueued during the clear cannot be missed.
                    if self._queue or self._closed:
                        break
                    await self._wake.wait()
                if self._closed:
                    return
                frame = self._queue.popleft()
                self._queued_bytes -= len(frame)
                await self.websocket.send(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A closed or broken socket is the ordinary end of this task, not
            # a fault: the connection teardown that caused it runs its own
            # cleanup through `rm_remote`.
            logger.debug("Sender for %s stopped: %s", self.uuid, exc)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _evict(self) -> None:
        """Close a client that stopped draining. AG-23.

        Loud, because the alternative is a client that is broken with nothing
        saying so. The queued frames go with it — they are not retried and
        not replayed, because the client will re-baseline on reconnect behind
        AG-R-20's gate.
        """
        if self._closed:
            return
        self.evicted = True
        logger.warning(
            "Client %s is not draining its socket: %d frames / %d bytes "
            "queued. Closing with code %d; it will reconnect and "
            "re-baseline.",
            self.uuid,
            len(self._queue),
            self._queued_bytes,
            CLOSE_CODE_QUEUE_OVERFLOW,
        )
        self.close()
        try:
            asyncio.get_running_loop().create_task(self._close_socket())
        except RuntimeError:
            pass

    async def _close_socket(self) -> None:
        """Send the close frame, and abort if the peer will not answer."""
        try:
            await asyncio.wait_for(
                self.websocket.close(
                    code=CLOSE_CODE_QUEUE_OVERFLOW,
                    reason=CLOSE_REASON_QUEUE_OVERFLOW,
                ),
                timeout=CLOSE_HANDSHAKE_TIMEOUT,
            )
        except Exception:
            transport = getattr(self.websocket, "transport", None)
            if transport is None:
                return
            try:
                transport.abort()
            except Exception:
                logger.debug("Transport abort for %s failed", self.uuid)

    def close(self) -> None:
        """Stop accepting frames and stop the drain task. Idempotent.

        The task is cancelled rather than drained. Cancelling a suspended
        ``send`` is forbidden on the consultation bridge — it cannot corrupt
        the stream, but it cannot retract the frame either, and there the
        stream continues. Here it does not: this runs only when the socket is
        being torn down, so there is no stream left to leave half-described.
        """
        self._closed = True
        self._queue.clear()
        self._queued_bytes = 0
        self._wake.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()


class Broadcaster:
    """One :class:`ClientSender` per connected client.

    Per-client queues rather than one shared ring read by per-client cursors,
    which is what this was originally specified as. Probe C killed that: a
    deque holds a *reference*, so five extra per-client deques over 1000
    frames of 10 KB cost 46,856 bytes against 10,000,000 bytes of payload —
    0.47% for complete lifecycle isolation. A shared ring would have pinned
    every entry until the slowest cursor passed it, which is the coupling
    this whole entry exists to remove.
    """

    def __init__(
        self,
        *,
        max_frames: int = SEND_QUEUE_MAX_FRAMES,
        max_bytes: int = SEND_QUEUE_MAX_BYTES,
    ) -> None:
        self._senders: dict[str, ClientSender] = {}
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._unknown_methods: set[tuple[str, str]] = set()

    @property
    def senders(self) -> dict[str, ClientSender]:
        """The attached senders, by remote UUID. Read-only by convention."""
        return self._senders

    def attach(self, remote: Any, websocket: Any) -> ClientSender:
        """Start tracking a client. Called once per JRPC remote.

        Takes the *remote* as well as the socket because a remote does not
        keep a reference to its websocket — only to the transmitter closure
        over it — and eviction needs the socket to close it.
        """
        uuid = getattr(remote, "uuid", None) or str(id(remote))
        existing = self._senders.get(uuid)
        if existing is not None:
            existing.close()
        sender = ClientSender(
            uuid,
            websocket,
            remote,
            max_frames=self._max_frames,
            max_bytes=self._max_bytes,
        )
        self._senders[uuid] = sender
        return sender

    def detach(self, uuid: str) -> None:
        """Stop tracking a client. Tolerant of a UUID that was never here."""
        sender = self._senders.pop(uuid, None)
        if sender is not None:
            sender.close()

    def broadcast(self, event_name: str, args: Sequence[Any]) -> int:
        """Queue ``AcApp.<event_name>`` to every client. Never raises.

        Returns how many clients accepted it, which is how the caller can
        tell an event that went nowhere from one that was refused.

        The frame is built once and the same string object is queued to every
        client — the reason per-client deques cost what Probe C measured.
        """
        if not self._senders:
            return 0

        method = f"AcApp.{event_name}"
        try:
            frame = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": {"args": list(args)},
                }
            )
        except (TypeError, ValueError) as exc:
            # Loud: an event that cannot be serialised is a row the browser
            # will never show, and nothing downstream would report it.
            logger.warning(
                "Event %s could not be serialised, so no client will see "
                "it: %s",
                event_name,
                exc,
            )
            return 0

        sent = 0
        for sender in list(self._senders.values()):
            self._warn_if_unexposed(sender, method)
            if sender.enqueue(frame):
                sent += 1
        return sent

    def _warn_if_unexposed(self, sender: ClientSender, method: str) -> None:
        """Warn once per client when a client cannot handle an event.

        A notification has no reply, and the browser's JSON-RPC library drops
        an unknown method silently when there is no ``id`` to answer — so
        without this, renaming a handler would stop events arriving and
        nothing anywhere would say so. Checked against the remote's ``rpcs``,
        which is filled from the client's own ``system.listComponents``.

        Skipped while ``rpcs`` is empty, which is the window before the
        handshake completes. Queuing through that window is deliberate: the
        frame waits behind the handshake rather than being dropped, which is
        what the old ``call``-proxy path did to it.
        """
        rpcs = getattr(sender.remote, "rpcs", None)
        if not rpcs or method in rpcs:
            return
        key = (sender.uuid, method)
        if key in self._unknown_methods:
            return
        self._unknown_methods.add(key)
        logger.warning(
            "Client %s does not expose %s; it will not see this event or "
            "any later one of the same name.",
            sender.uuid,
            method,
        )

    def shutdown(self) -> None:
        """Close every sender. For server teardown."""
        for sender in list(self._senders.values()):
            sender.close()
        self._senders.clear()
        self._unknown_methods.clear()
