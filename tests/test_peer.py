"""Peer conversations (CC-23): records, the wire, both roles, the lock.

The end-to-end cases run two :class:`PeerConversations` in one loop against
each other over real loopback HTTP, with a fake engine host on the responder.
Two pids are needed because a process never lists itself as a peer, and both
must be live because a dead pid's record is stale: this process and its
parent.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from aic_dc.claude_code import peer as peer_module
from aic_dc.claude_code.peer import (
    DENY_REASON,
    FIRST_TURN_FRAMING,
    PeerConversations,
    PeerRecord,
    header_of,
    read_records,
    write_record,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHost:
    """The responder's engine. Turns answer at once unless ``hold`` is set."""

    def __init__(self) -> None:
        self.available = True
        self.busy = False
        self.turns: list[str] = []
        self.begun = 0
        self.finished: list[dict[str, Any]] = []
        self.hold: asyncio.Event | None = None
        self.cancelled: list[str] = []
        self._released: asyncio.Event | None = None
        self.outcome: dict[str, Any] | None = None

    def _peer_available(self) -> bool:
        return self.available

    def _peer_busy(self) -> bool:
        return self.busy

    async def _peer_begin(self) -> dict[str, Any]:
        self.begun += 1
        return {"session_id": "old-session", "permission_mode": "acceptEdits"}

    async def _peer_turn(self, request_id: str, text: str) -> dict[str, Any]:
        self.turns.append(text)
        if self.hold is not None:
            self._released = asyncio.Event()
            self.hold.set()
            await self._released.wait()
            return {"response": "", "cancelled": True}
        if self.outcome is not None:
            return self.outcome
        return {"response": f"answer {len(self.turns)}", "cancelled": False}

    async def _peer_cancel_turn(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        if self._released is not None:
            self._released.set()

    async def _peer_finish(self, previous: dict[str, Any]) -> None:
        self.finished.append(previous)


class Events:
    def __init__(self) -> None:
        self.states: list[Any] = []

    async def __call__(self, event: Any) -> None:
        assert event.name == "peerChanged"
        self.states.append(event.payload["state"])

    def last(self) -> Any:
        return self.states[-1] if self.states else None


async def wait_for(predicate, *, ticks: int = 500) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


@pytest.fixture
async def pair(tmp_path):
    config_dir = tmp_path / "config"
    (tmp_path / "repo1").mkdir()
    (tmp_path / "repo2").mkdir()
    host = FakeHost()
    responder_events, caller_events = Events(), Events()
    responder = PeerConversations(
        host=host,
        config_dir=config_dir,
        repo_root=tmp_path / "repo2",
        broadcast=responder_events,
        pid=os.getpid(),
    )
    caller = PeerConversations(
        host=FakeHost(),
        config_dir=config_dir,
        repo_root=tmp_path / "repo1",
        broadcast=caller_events,
        pid=os.getppid(),
    )
    await responder.start()
    try:
        yield caller, responder, host, caller_events, responder_events
    finally:
        await caller.aclose()
        await responder.aclose()


async def consent(responder: PeerConversations, allow: bool = True) -> dict[str, Any]:
    await wait_for(lambda: (responder.state() or {}).get("status") == "pending")
    return await responder.answer_request(allow)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class TestRecords:
    def _record(self, pid: int) -> PeerRecord:
        return PeerRecord(
            pid=pid, repo_root="/r", repo_name="r", port=1, token="t", started_at=""
        )

    def test_written_owner_only(self, tmp_path):
        path = write_record(tmp_path, self._record(os.getpid()))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert read_records(tmp_path) == [self._record(os.getpid())]

    def test_a_dead_pid_is_stale_and_swept(self, tmp_path):
        dead = 2**22 + 12345
        path = write_record(tmp_path, self._record(dead))
        assert read_records(tmp_path) == []
        assert path.exists()
        read_records(tmp_path, sweep=True)
        assert not path.exists()

    def test_unreadable_records_are_skipped(self, tmp_path):
        directory = tmp_path / "peers"
        directory.mkdir()
        (directory / "1.json").write_text("{not json")
        assert read_records(tmp_path) == []

    async def test_a_process_never_lists_itself_or_its_own_root(self, tmp_path):
        (tmp_path / "repo").mkdir()
        me = PeerConversations(
            host=FakeHost(), config_dir=tmp_path, repo_root=tmp_path / "repo",
            broadcast=Events(), pid=os.getpid(),
        )
        await me.start()
        try:
            twin = PeerConversations(
                host=FakeHost(), config_dir=tmp_path, repo_root=tmp_path / "repo",
                broadcast=Events(), pid=os.getppid(),
            )
            assert me.peers() == []
            assert twin.peers() == []
        finally:
            await me.aclose()
        assert read_records(tmp_path) == []


class TestFixedStrings:
    def test_the_permission_gate_spells_the_server_the_same(self):
        from aic_dc.claude_code.permissions import PEER_MCP_SERVER

        assert PEER_MCP_SERVER == peer_module.SERVER_NAME == "aic-dc-peer"

    def test_the_header_round_trips(self):
        caller = {"repo_name": "repo1", "repo_root": "/home/u/repo (1)"}
        convo = PeerConversations(
            host=FakeHost(), config_dir="/nonexistent", repo_root="/x", broadcast=Events()
        )
        text = convo._framed(caller, "hello", first=True)
        assert header_of(text) == caller
        assert FIRST_TURN_FRAMING in text
        assert FIRST_TURN_FRAMING not in convo._framed(caller, "again", first=False)

    def test_an_ordinary_message_has_no_header(self):
        assert header_of("hello\n[aic-dc peer message from a (b)]") is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


class TestConversation:
    async def test_open_answer_continue_end(self, pair):
        caller, responder, host, caller_events, responder_events = pair

        ask = asyncio.create_task(caller.ask("repo2", "How do you index symbols?"))
        await wait_for(lambda: (caller.state() or {}).get("status") == "opening")
        # Nothing reaches the engine before consent.
        assert (await consent(responder))["status"] == "allowed"
        reply = await ask

        assert reply.startswith("answer 1")
        assert "is open" in reply
        assert host.begun == 1
        assert responder.locked
        assert header_of(host.turns[0])["repo_name"] == "repo1"
        assert FIRST_TURN_FRAMING in host.turns[0]

        second = await caller.ask("repo2", "Thanks. Answering yours: none.")
        assert second.startswith("answer 2")
        assert FIRST_TURN_FRAMING not in host.turns[1]
        assert caller.state()["status"] == "open"

        assert "ended" in await caller.end_calling("caller")
        await wait_for(lambda: responder.state()["status"] == "ended")
        assert not responder.locked
        assert host.finished == [{"session_id": "old-session", "permission_mode": "acceptEdits"}]
        assert responder.state()["ended_by"] == "caller"
        assert responder.state()["previous_session_id"] == "old-session"

    async def test_denied(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "hi"))
        await consent(responder, allow=False)
        assert "declined" in await ask
        assert host.turns == [] and host.begun == 0
        assert caller.state()["status"] == "ended"

    async def test_busy_is_refused_without_a_dialog(self, pair):
        caller, responder, host, *_ = pair
        host.busy = True
        reply = await caller.ask("repo2", "hi")
        assert "busy" in reply and "Nothing was sent" in reply
        assert responder.state() is None
        assert caller.state() is None

    async def test_another_engine_is_refused(self, pair):
        caller, responder, host, *_ = pair
        host.available = False
        assert "not running the Claude engine" in await caller.ask("repo2", "hi")

    async def test_the_caller_withdraws_a_pending_request(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "hi"))
        await wait_for(lambda: (responder.state() or {}).get("status") == "pending")
        await caller.end_calling("caller")
        assert "ended" in await ask
        await wait_for(lambda: responder.state()["status"] == "ended")
        assert host.begun == 0

    async def test_the_responder_ends_a_pending_request(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "hi"))
        await wait_for(lambda: (responder.state() or {}).get("status") == "pending")
        await responder.end_responding("responder")
        reply = await ask
        assert "repo2's user" in reply and "Do not retry" in reply

    async def test_ending_mid_turn_waits_for_the_turn_before_restoring(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "first"))
        await consent(responder)
        await ask

        host.hold = asyncio.Event()
        ask = asyncio.create_task(caller.ask("repo2", "second"))
        await host.hold.wait()
        assert host.finished == []
        await caller.end_calling("caller")
        assert "ended" in await ask
        await wait_for(lambda: bool(host.finished))
        assert host.cancelled

    async def test_the_responders_stop_ends_it(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "first"))
        await consent(responder)
        await ask
        host.outcome = {"response": "", "cancelled": True}
        reply = await caller.ask("repo2", "second")
        assert "repo2's user" in reply
        assert not responder.locked
        assert caller.state()["status"] == "ended"

    async def test_a_turn_error_keeps_it_open(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "first"))
        await consent(responder)
        await ask
        host.outcome = {"is_error": True, "terminal_reason": "engine_error"}
        reply = await caller.ask("repo2", "second")
        assert "engine_error" in reply and "is open" in reply
        assert responder.locked

    async def test_a_responder_that_exited(self, pair):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "first"))
        await consent(responder)
        await ask
        await responder._listener.aclose()
        reply = await caller.ask("repo2", "second")
        assert "could not be reached" in reply
        assert caller.state()["status"] == "ended"

    async def test_one_conversation_per_process(self, pair, tmp_path):
        caller, responder, host, *_ = pair
        ask = asyncio.create_task(caller.ask("repo2", "first"))
        await consent(responder)
        await ask
        # The responder cannot also call.
        assert "itself answering" in await responder.ask("repo1", "hi")

    async def test_unknown_peer_lists_the_live_ones(self, pair):
        caller, *_ = pair
        reply = await caller.ask("nope", "hi")
        assert "repo2" in reply and caller.state() is None

    async def test_bad_token_is_refused(self, pair):
        caller, responder, *_ = pair
        (record,) = caller.peers()
        forged = PeerRecord(**{**record.__dict__, "token": "wrong"})
        status, _ = await caller._client.message(
            forged, conversation_id=None, caller=caller.me, message="hi"
        )
        assert status == 401
        assert responder.state() is None


# ---------------------------------------------------------------------------
# The gate and the lock, on the real broker and service
# ---------------------------------------------------------------------------


class TestPermissionLockdown:
    async def test_every_request_is_denied_without_a_dialog(self, tmp_path):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        from aic_dc.claude_code.permissions import PermissionBroker

        broadcasts: list[Any] = []

        async def broadcast(event):
            broadcasts.append(event)

        broker = PermissionBroker(tmp_path, broadcast=broadcast)
        broker.lockdown = lambda: DENY_REASON

        class Ctx:
            tool_use_id = "t1"
            suggestions = ()
            agent_id = None

        for tool in ("Edit", "Bash", "ExitPlanMode", "AskUserQuestion", "mcp__aic-dc-peer__ask_peer"):
            result = await broker.can_use_tool(tool, {}, Ctx())
            assert isinstance(result, PermissionResultDeny), tool
            assert result.message == DENY_REASON
        assert broadcasts == []
        allowed = await broker.can_use_tool("mcp__aic-dc__symbol_map", {}, Ctx())
        assert isinstance(allowed, PermissionResultAllow)

    async def test_peer_tools_need_no_dialog_otherwise(self, tmp_path):
        from claude_agent_sdk import PermissionResultAllow

        from aic_dc.claude_code.permissions import PermissionBroker

        async def broadcast(event):
            raise AssertionError("no dialog expected")

        broker = PermissionBroker(tmp_path, broadcast=broadcast)

        class Ctx:
            tool_use_id = "t1"
            suggestions = ()
            agent_id = None

        result = await broker.can_use_tool("mcp__aic-dc-peer__ask_peer", {}, Ctx())
        assert isinstance(result, PermissionResultAllow)


class TestServiceLock:
    @pytest.fixture
    async def service(self, tmp_path):
        from aic_dc.claude_code import ClaudeCodeService
        from aic_dc.claude_code.engine_config import EngineConfig

        class Config:
            repo_root = tmp_path
            config_dir = tmp_path / "config"
            aic_dc_dir = None

        svc = ClaudeCodeService(Config(), engine_config=EngineConfig())
        loop = asyncio.get_running_loop()
        svc.peer._responding = peer_module._Responding(
            conversation_id="c",
            caller={"repo_name": "repo1", "repo_root": "/r1", "pid": 1},
            first_message="hi",
            consent=loop.create_future(),
            status="open",
        )
        yield svc
        await svc.peer.aclose()

    @pytest.mark.parametrize(
        "method,args",
        [
            ("chat_streaming", ("r1", "hello")),
            ("new_session", ()),
            ("resume_session", ("s",)),
            ("restart_session", ()),
            ("set_permission_mode", ("default",)),
            ("set_model", ("opus",)),
            ("rewind_files", ("m",)),
            ("commit_all", ()),
            ("reset_to_head", ()),
        ],
    )
    async def test_locked_methods_refuse(self, service, method, args):
        result = await getattr(service, method)(*args)
        assert result["reason"] == "peer_conversation"

    async def test_the_gate_reports_the_lock(self, service):
        assert service.permissions.lockdown() == DENY_REASON
        service.peer._responding = None
        assert service.permissions.lockdown() is None

    async def test_the_router_will_not_switch_engines(self, service):
        from aic_dc.engine_router import _engine_is_busy

        assert _engine_is_busy(service)

    async def test_host_hooks_are_not_rpc_methods(self):
        """`add_service` exposes every public method, and these bypass both gates."""
        from aic_dc.engine_router import _public_methods_of_reference

        exposed = set(_public_methods_of_reference())
        assert {"peer_answer", "peer_end", "peer_dismiss"} <= exposed
        assert not {n for n in exposed if n.startswith("peer_") and n not in {
            "peer_answer", "peer_end", "peer_dismiss"
        }}
