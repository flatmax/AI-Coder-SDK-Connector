"""Tests for ac_dc.claude_code.service — conversion phase 1.

The engine is faked; what is under test is the *outward* half of a turn.
The properties that matter here are the ones a browser depends on:

- **The RPC namespace is the class name.** ``add_service`` derives it from
  ``type(instance).__name__``, so a rename breaks every frontend call site.
- **Event arity is fixed.** Turn-scoped events take ``(request_id,
  payload)``; session-wide events take ``(payload,)``. Getting this wrong
  puts a payload where the frontend expects an ID.
- **``postResponseComplete`` always fires**, after ``streamComplete``, with
  the right request ID — the Context tab and file tree wait on it.
- **The engine connects lazily**, so phase 1 adds no ``claude``
  subprocess to app startup while the native engine still serves the UI.
- **Slash commands never reach the model.** Forwarding ``/compact`` as
  prose turns a command into a question.
- **Failures are returned, not raised.** An RPC exception reaches the
  browser as a generic transport error instead of an actionable message.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from ac_dc.claude_code.engine_config import EngineConfig
from ac_dc.claude_code.health import EngineHealth, EngineStartupError
from ac_dc.claude_code.messages import Event
from ac_dc.claude_code.service import SLASH_EQUIVALENTS, ClaudeCodeService
from ac_dc.claude_code.session import (
    EngineNotReadyError,
    SessionLostError,
    TurnInProgressError,
)

REQUEST_ID = "1736956800000-a1b2c3"
PNG = "data:image/png;base64,aGk="


class FakeConfig:
    def __init__(self, repo_root, config_dir=None, ac_dc_dir=None):
        self.repo_root = repo_root
        self.config_dir = config_dir
        # Present so the session store is built on the production path in
        # every service test rather than only where one asks for it. Nothing
        # here appends, so no file is written — but a wiring change that
        # dropped the store would now have somewhere to show up.
        # None without a repo root, matching ConfigManager.ac_dc_dir.
        if ac_dc_dir is None and repo_root is not None:
            ac_dc_dir = Path(repo_root) / ".ac-dc4"
        self.ac_dc_dir = ac_dc_dir
        self.snippet_calls: list[str] = []

    def get_snippets(self, mode="code"):
        self.snippet_calls.append(mode)
        return [{"label": mode, "text": f"snippet for {mode}"}]

    def get_commit_prompt(self):
        return "Write a conventional commit message for this diff."


class FakeSession:
    """Stands in for ``EngineSession``: records calls, replays outcomes."""

    def __init__(self):
        self.health = EngineHealth(cli_version="2.1.229")
        self.ready = False
        self.session_id = None
        self.streaming_active = False
        self.permission_mode = "default"
        self.model = None

        self.connect_calls: list[str | None] = []
        # `(resume, fork_session)` per connect. Separate from
        # `connect_calls` so the many tests that only care what was
        # resumed keep reading the simpler list.
        self.connect_args: list[tuple[str | None, bool]] = []
        self.connect_error: BaseException | None = None
        self.disconnect_calls = 0
        self.reset_calls = 0
        self.turns: list[object] = []
        self.admit_error: BaseException | None = None
        self.control_calls: list[tuple[str, tuple]] = []
        self.control_error: BaseException | None = None
        self.interrupt_result = {"status": "interrupting"}
        self.context_usage = {"total_tokens": 1000}
        # Events the fake pump emits for each turn.
        self.turn_events: list[Event] = [
            Event("streamChunk", {"block_id": f"{REQUEST_ID}:b0", "content": "Hi"}),
            Event("streamComplete", {"response": "Hi", "is_error": False}),
        ]

    def active_streams(self):
        return []

    async def connect(self, resume=None, fork_session=False):
        self.connect_calls.append(resume)
        self.connect_args.append((resume, fork_session))
        if self.connect_error is not None:
            raise self.connect_error
        self.ready = True
        self.health.connected = True
        # What the real session does: a plain resume knows its ID up front,
        # a fork's is minted by the CLI and stays unknown until its first
        # turn's init message.
        if resume and not fork_session:
            self.session_id = resume

    async def disconnect(self):
        self.disconnect_calls += 1
        self.ready = False

    async def reset(self):
        self.reset_calls += 1
        await self.disconnect()
        self.session_id = None

    def admit(self, request_id):
        if self.admit_error is not None:
            raise self.admit_error

    async def run_turn(self, turn, emit=None):
        self.turns.append(turn)
        self.streaming_active = True
        try:
            for event in self.turn_events:
                if isinstance(event, BaseException):
                    raise event
                if emit is not None:
                    await emit(event)
        finally:
            self.streaming_active = False
        return {"response": "Hi"}

    async def interrupt(self, request_id=None):
        self.control_calls.append(("interrupt", (request_id,)))
        if self.control_error is not None:
            raise self.control_error
        return self.interrupt_result

    async def set_permission_mode(self, mode):
        from ac_dc.claude_code.engine_config import PERMISSION_MODES

        self.control_calls.append(("set_permission_mode", (mode,)))
        if self.control_error is not None:
            raise self.control_error
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode {mode!r}.")
        self.permission_mode = mode
        return mode

    def prefer_permission_mode(self, mode):
        from ac_dc.claude_code.engine_config import PERMISSION_MODES

        self.control_calls.append(("prefer_permission_mode", (mode,)))
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode {mode!r}.")
        self.permission_mode = mode
        return mode

    async def set_model(self, model=None):
        self.control_calls.append(("set_model", (model,)))
        if self.control_error is not None:
            raise self.control_error
        self.model = model
        return model

    async def rewind_files(self, user_message_id):
        self.control_calls.append(("rewind_files", (user_message_id,)))
        if self.control_error is not None:
            raise self.control_error

    async def stop_task(self, task_id):
        self.control_calls.append(("stop_task", (task_id,)))
        if self.control_error is not None:
            raise self.control_error

    async def get_context_usage(self):
        self.control_calls.append(("get_context_usage", ()))
        if self.control_error is not None:
            raise self.control_error
        return self.context_usage

    async def get_mcp_status(self):
        if self.control_error is not None:
            raise self.control_error
        return {"servers": []}

    async def reconnect_mcp_server(self, name):
        self.control_calls.append(("reconnect_mcp_server", (name,)))
        if self.control_error is not None:
            raise self.control_error

    async def toggle_mcp_server(self, name, enabled):
        self.control_calls.append(("toggle_mcp_server", (name, enabled)))
        if self.control_error is not None:
            raise self.control_error

    async def get_server_info(self):
        if self.control_error is not None:
            raise self.control_error
        return {"commands": ["review"]}


class Recorder:
    """The ``event_callback`` indirection, recording ``(name, *args)``."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.error: BaseException | None = None

    async def __call__(self, name, *args):
        self.calls.append((name, *args))
        if self.error is not None:
            raise self.error

    def names(self):
        return [call[0] for call in self.calls]

    def payload_of(self, name):
        return next(call[-1] for call in self.calls if call[0] == name)

    def call_of(self, name):
        return next(call for call in self.calls if call[0] == name)


class FakeCollab:
    """Stands in for ``CollabManager``: answers the authority questions."""

    def __init__(self, *, is_localhost=True, raises=False, clients=None):
        self._is_localhost = is_localhost
        self._raises = raises
        self._clients = clients if clients is not None else [{"is_localhost": True}]

    def is_caller_localhost(self):
        if self._raises:
            raise RuntimeError("registry is confused")
        return self._is_localhost

    def get_connected_clients(self):
        if self._raises:
            raise RuntimeError("registry is confused")
        return self._clients

    def get_collab_role(self):
        return {"role": "host", "client_id": "tab-1", "is_localhost": self._is_localhost}


class FakePermissionContext:
    """Stands in for ``ToolPermissionContext``."""

    tool_use_id = "toolu_01"
    suggestions = ()
    agent_id = None
    blocked_path = None
    decision_reason = None
    title = None
    display_name = None
    description = None


@pytest.fixture
def events():
    return Recorder()


@pytest.fixture
def service(tmp_path, events):
    """A service on a fake engine, with no CLI anywhere in sight."""
    svc = ClaudeCodeService(
        FakeConfig(tmp_path),
        event_callback=events,
        engine_config=EngineConfig(),
    )
    svc.session = FakeSession()
    return svc


async def finish_turns(service):
    """Await the background turn tasks a chat_streaming call spawned."""
    for _ in range(50):
        tasks = [t for t in service._turn_tasks if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)


async def connected(service, events):
    """Get the lazy connect out of the way, so a test sees only its own events."""
    await service.connect_engine()
    events.calls.clear()


async def send(service, message="hello", **kwargs):
    answer = await service.chat_streaming(REQUEST_ID, message, **kwargs)
    await finish_turns(service)
    return answer


async def seed_transcript(service, session_id, prompt="fix the parser", reply="done"):
    """Put one CLI-shaped exchange in the store, the way the mirror would.

    Entry shape copied from a real `sdk-py` transcript: camelCase keys,
    ``uuid``/``parentUuid`` chaining, per-message ``usage``. Written through
    the production store so the SDK's own parsers read it back.
    """
    await service.session_store.append(
        {
            "project_key": service._session_project_key(),
            "session_id": session_id,
        },
        [
            {
                "type": "user",
                "uuid": f"{session_id}-u1",
                "parentUuid": None,
                "timestamp": "2026-08-16T00:00:00.000Z",
                "sessionId": session_id,
                "cwd": str(service._repo_root),
                "message": {"role": "user", "content": prompt},
            },
            {
                "type": "assistant",
                "uuid": f"{session_id}-a1",
                "parentUuid": f"{session_id}-u1",
                "timestamp": "2026-08-16T00:00:02.000Z",
                "sessionId": session_id,
                "cwd": str(service._repo_root),
                "message": {
                    "id": f"msg_{session_id}",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [{"type": "text", "text": reply}],
                    "usage": {"input_tokens": 40, "output_tokens": 9},
                },
            },
        ],
    )


# ---------------------------------------------------------------------------
# The RPC surface itself
# ---------------------------------------------------------------------------


class TestRpcSurface:
    def test_the_class_name_is_the_rpc_namespace(self):
        """Renaming this class renames every RPC. It is interface."""
        assert ClaudeCodeService.__name__ == "ClaudeCodeService"

    def test_the_methods_the_frontend_calls_exist(self, service):
        for name in (
            "chat_streaming",
            "cancel_streaming",
            "get_current_state",
            "get_engine_health",
            "get_selected_files",
            "set_selected_files",
            "set_permission_mode",
            "set_model",
            "rewind_files",
            "stop_task",
            "get_context_usage",
            "get_mcp_status",
            "reconnect_mcp_server",
            "toggle_mcp_server",
            "get_server_info",
            "connect_engine",
            "resolve_permission",
            "get_denied_read_files",
            "set_denied_read_files",
        ):
            assert callable(getattr(service, name)), name

    def test_phase_five_methods_are_absent(self, service):
        """A stub that reported success would be worse than a missing method."""
        for name in ("history_delete",):
            assert not hasattr(service, name), name


# ---------------------------------------------------------------------------
# Lazy connect
# ---------------------------------------------------------------------------


class TestLazyConnect:
    def test_construction_does_not_connect(self, tmp_path, events):
        """Phase 1 must not add a CLI subprocess to every app startup."""
        svc = ClaudeCodeService(FakeConfig(tmp_path), event_callback=events)
        svc.session = FakeSession()
        assert svc.session.connect_calls == []
        assert events.calls == []

    async def test_the_first_turn_connects(self, service):
        await send(service)
        assert service.session.connect_calls == [None]

    async def test_a_second_turn_does_not_reconnect(self, service):
        await send(service)
        await send(service)
        assert len(service.session.connect_calls) == 1

    async def test_two_first_turns_at_once_connect_once(self, service):
        """Otherwise two clients sending together spawn two subprocesses."""
        await asyncio.gather(
            service.chat_streaming(REQUEST_ID, "one"),
            service.chat_streaming("1736956800001-zzzzzz", "two"),
        )
        await finish_turns(service)
        assert len(service.session.connect_calls) == 1

    async def test_connect_engine_is_idempotent(self, service):
        first = await service.connect_engine()
        second = await service.connect_engine()
        assert first["status"] == second["status"] == "ready"
        assert len(service.session.connect_calls) == 1

    async def test_connect_engine_passes_a_resume_id(self, service):
        await service.connect_engine("prev-session")
        assert service.session.connect_calls == ["prev-session"]

    async def test_connect_broadcasts_the_health_record(self, service, events):
        await service.connect_engine()
        payload = events.payload_of("engineHealth")
        assert payload["connected"] is True
        assert payload["cli_version"] == "2.1.229"

    async def test_a_startup_failure_is_returned_not_raised(self, service, events):
        """An RPC exception loses the actionable message."""
        service.session.connect_error = EngineStartupError("claude not found")
        answer = await service.connect_engine()
        assert answer == {"error": "claude not found", "reason": "startup_failed"}
        # Still broadcast, so the UI can show why it is unavailable.
        assert "engineHealth" in events.names()

    async def test_a_turn_that_cannot_connect_says_why(self, service):
        service.session.connect_error = EngineStartupError("claude not found")
        answer = await service.chat_streaming(REQUEST_ID, "hello")
        assert answer == {"error": "claude not found", "reason": "startup_failed"}
        assert service.session.turns == []

    async def test_the_startup_error_shows_up_in_health(self, service):
        service.session.connect_error = EngineStartupError("claude not found")
        await service.connect_engine()
        assert service.get_engine_health()["last_error"] == "claude not found"

    async def test_a_live_error_is_not_masked_by_the_startup_error(self, service):
        service.session.connect_error = EngineStartupError("claude not found")
        await service.connect_engine()
        service.session.health.last_error = "broken pipe"
        assert service.get_engine_health()["last_error"] == "broken pipe"


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


class TestSlashCommands:
    @pytest.mark.parametrize(
        "command", sorted(k for k, v in SLASH_EQUIVALENTS.items() if v)
    )
    async def test_a_mapped_command_names_its_equivalent(self, service, command):
        answer = await service.chat_streaming(REQUEST_ID, f"/{command}")
        assert answer["status"] == "unsupported"
        assert answer["command"] == command
        assert answer["equivalent"] in answer["message"]

    @pytest.mark.parametrize(
        "command", sorted(k for k, v in SLASH_EQUIVALENTS.items() if v is None)
    )
    async def test_an_unmappable_command_says_there_is_no_equivalent(
        self, service, command
    ):
        answer = await service.chat_streaming(REQUEST_ID, f"/{command}")
        assert answer["status"] == "unsupported"
        assert "no equivalent here" in answer["message"]
        assert "equivalent" not in answer

    async def test_a_slash_command_never_starts_the_engine(self, service, events):
        """It is answered locally: no connect, no turn, no broadcast."""
        await service.chat_streaming(REQUEST_ID, "/compact")
        assert service.session.connect_calls == []
        assert service.session.turns == []
        assert events.calls == []

    async def test_arguments_do_not_hide_the_command(self, service):
        answer = await service.chat_streaming(REQUEST_ID, "/model opus")
        assert answer["command"] == "model"

    async def test_case_and_whitespace_do_not_hide_the_command(self, service):
        answer = await service.chat_streaming(REQUEST_ID, "  /CLEAR  ")
        assert answer["command"] == "clear"

    async def test_a_custom_command_reaches_the_engine(self, service):
        """.claude/commands/ is the CLI's business, not ours."""
        await send(service, "/review the diff")
        assert service.session.turns[0].message == "/review the diff"

    async def test_a_lone_slash_reaches_the_engine(self, service):
        await send(service, "/")
        assert service.session.turns[0].message == "/"

    async def test_a_mid_message_slash_is_not_a_command(self, service):
        await send(service, "what does /compact do?")
        assert len(service.session.turns) == 1


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


class TestChatStreaming:
    async def test_it_returns_as_soon_as_the_turn_is_admitted(self, service):
        answer = await service.chat_streaming(REQUEST_ID, "hello")
        assert answer == {"status": "started"}
        await finish_turns(service)

    async def test_the_turn_carries_the_request_id_and_message(self, service):
        await send(service, "hello")
        turn = service.session.turns[0]
        assert turn.request_id == REQUEST_ID
        assert turn.message == "hello"

    async def test_files_default_to_the_picker_selection(self, service, tmp_path):
        (tmp_path / "a.py").write_text("x")
        await service.set_selected_files(["a.py"])
        await send(service)
        assert service.session.turns[0].files == ["a.py"]

    async def test_an_explicit_empty_list_means_no_files(self, service, tmp_path):
        """Distinct from omitting the argument, which means "use the picker"."""
        (tmp_path / "a.py").write_text("x")
        await service.set_selected_files(["a.py"])
        await send(service, files=[])
        assert service.session.turns[0].files == []

    async def test_the_viewer_payload_becomes_framing_input(self, service):
        await send(service, viewer={"path": "src/a.py", "start_line": 4})
        viewer = service.session.turns[0].viewer
        assert viewer.path == "src/a.py"
        assert viewer.start_line == 4

    async def test_a_malformed_viewer_payload_does_not_fail_the_turn(self, service):
        await send(service, viewer={"nonsense": True})
        assert service.session.turns[0].viewer is None

    async def test_images_reach_the_turn(self, service):
        await send(service, images=[PNG])
        assert service.session.turns[0].images == [PNG]

    async def test_the_user_message_is_broadcast_before_the_turn(self, service, events):
        """A collaborator sees the message even if the turn then fails."""
        await connected(service, events)
        await send(service, "hello")
        assert events.names()[0] == "userMessage"
        assert events.payload_of("userMessage")["content"] == "hello"

    async def test_the_user_message_is_session_wide(self, service, events):
        """Everyone's transcript gets it, so it takes no request-ID argument."""
        await send(service, "hello")
        name, payload = events.call_of("userMessage")
        assert name == "userMessage"
        assert isinstance(payload, dict)
        assert payload["request_id"] == REQUEST_ID

    async def test_data_uris_are_never_broadcast(self, service, events):
        """A handful of screenshots would be megabytes per client."""
        await send(service, "look", images=[PNG])
        payload = events.payload_of("userMessage")
        assert payload["image_refs"] == []
        assert PNG not in str(payload)

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (TurnInProgressError("already running"), "turn_in_progress"),
            (EngineNotReadyError("still starting"), "not_ready"),
            (SessionLostError("session gone"), "session_lost"),
            (ValueError("no request id"), "bad_request"),
        ],
    )
    async def test_admission_failures_carry_a_reason_code(self, service, error, reason):
        """So the frontend can distinguish retry from resume from bug."""
        service.session.admit_error = error
        answer = await service.chat_streaming(REQUEST_ID, "hello")
        assert answer["reason"] == reason
        assert answer["error"] == str(error)

    async def test_a_rejected_turn_broadcasts_nothing(self, service, events):
        await connected(service, events)
        service.session.admit_error = TurnInProgressError("already running")
        await service.chat_streaming(REQUEST_ID, "hello")
        assert events.calls == []


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


class TestEventDispatch:
    async def test_turn_scoped_events_lead_with_the_request_id(self, service, events):
        await send(service)
        name, request_id, payload = events.call_of("streamChunk")
        assert name == "streamChunk"
        assert request_id == REQUEST_ID
        assert payload["content"] == "Hi"

    async def test_session_wide_events_carry_only_a_payload(self, service, events):
        await service.connect_engine()
        assert len(events.call_of("engineHealth")) == 2

    async def test_post_response_complete_follows_stream_complete(self, service, events):
        await send(service)
        names = events.names()
        assert names.index("streamComplete") < names.index("postResponseComplete")

    async def test_post_response_complete_carries_the_request_id(self, service, events):
        """It fires after the active turn is cleared, so it cannot be looked up."""
        await send(service)
        _, request_id, payload = events.call_of("postResponseComplete")
        assert request_id == REQUEST_ID
        assert payload["files_reindexed"] == []
        assert payload["context_usage"] == {"total_tokens": 1000}
        assert payload["disk_warning"] is None

    async def test_post_response_complete_fires_even_when_the_turn_fails(
        self, service, events
    ):
        """The Context tab and file tree wait on it; skipping it leaves them stale."""
        service.session.turn_events = [RuntimeError("pump exploded")]
        await send(service)
        assert "postResponseComplete" in events.names()

    async def test_a_failure_outside_the_pump_still_completes_the_stream(
        self, service, events
    ):
        service.session.turn_events = [RuntimeError("pump exploded")]
        await send(service)
        payload = events.payload_of("streamComplete")
        assert payload["is_error"] is True
        assert payload["terminal_reason"] == "engine_error"

    async def test_a_failed_usage_refetch_is_not_a_failed_turn(self, service, events):
        service.session.context_usage = None
        service.session.control_error = RuntimeError("no response")
        await send(service)
        assert events.payload_of("postResponseComplete")["context_usage"] is None

    async def test_usage_is_not_refetched_from_a_disconnected_engine(
        self, service, events
    ):
        service.session.ready = True
        service.session.turn_events = []

        async def drop_ready(turn, emit=None):
            service.session.ready = False
            return {}

        service.session.run_turn = drop_ready
        await send(service)
        assert events.payload_of("postResponseComplete")["context_usage"] is None

    async def test_a_broadcast_failure_does_not_break_the_turn(
        self, service, events, caplog
    ):
        """A closed WebSocket must not truncate work the engine is doing."""
        events.error = RuntimeError("socket closed")
        with caplog.at_level(logging.WARNING):
            await send(service)
        assert events.names().count("streamChunk") == 1
        assert "postResponseComplete" in events.names()
        assert "socket closed" in caplog.text

    async def test_no_callback_yet_is_not_an_error(self, tmp_path):
        """It is wired after the RPC server starts, so it can be absent."""
        svc = ClaudeCodeService(FakeConfig(tmp_path), engine_config=EngineConfig())
        svc.session = FakeSession()
        await send(svc)
        assert len(svc.session.turns) == 1


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_passes_the_request_id_through(self, service):
        answer = await service.cancel_streaming(REQUEST_ID)
        assert answer == {"status": "interrupting"}
        assert ("interrupt", (REQUEST_ID,)) in service.session.control_calls

    async def test_a_cancel_failure_is_returned_not_raised(self, service):
        service.session.control_error = RuntimeError("no response")
        answer = await service.cancel_streaming(REQUEST_ID)
        assert "no response" in answer["error"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestState:
    async def test_current_state_has_every_key_the_frontend_reads(self, service):
        state = await service.get_current_state()
        assert set(state) == {
            "messages",
            "selected_files",
            "denied_read_files",
            "session_id",
            "repo_name",
            "init_complete",
            "engine_ready",
            "streaming_active",
            "active_streams",
            "permission_mode",
            "model",
            "pending_permissions",
            "doc_index_ready",
            "doc_index_building",
            "doc_index_enriched",
            "enrichment_status",
            "review_state",
            "engine_health",
            "doc_convert_available",
        }

    async def test_current_state_reports_the_engine_as_not_yet_ready(self, service):
        state = await service.get_current_state()
        assert state["engine_ready"] is False
        assert state["streaming_active"] is False
        assert state["session_id"] is None
        assert state["permission_mode"] == "default"

    async def test_later_phase_keys_are_present_but_empty(self, service):
        """So the frontend contract does not change when they populate."""
        state = await service.get_current_state()
        assert state["messages"] == []
        assert state["denied_read_files"] == []
        assert state["pending_permissions"] == []

    async def test_the_repo_name_comes_from_the_root(self, tmp_path, events):
        root = tmp_path / "my-project"
        root.mkdir()
        svc = ClaudeCodeService(FakeConfig(root), event_callback=events)
        assert (await svc.get_current_state())["repo_name"] == "my-project"

    async def test_it_falls_back_to_cwd_without_a_repo_root(self, events):
        svc = ClaudeCodeService(FakeConfig(None), event_callback=events)
        assert (await svc.get_current_state())["repo_name"] == Path.cwd().name


class TestSelectedFiles:
    async def test_existing_files_are_kept(self, service, tmp_path):
        (tmp_path / "a.py").write_text("x")
        assert await service.set_selected_files(["a.py"]) == ["a.py"]
        assert service.get_selected_files() == ["a.py"]

    async def test_a_stale_selection_is_dropped(self, service):
        """A deleted file must not frame a turn with a path that is gone."""
        assert await service.set_selected_files(["deleted.py"]) == []

    async def test_absolute_paths_are_accepted(self, service, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x")
        assert await service.set_selected_files([str(target)]) == [str(target)]

    async def test_junk_entries_are_dropped(self, service):
        assert await service.set_selected_files(["", None, 42]) == []

    async def test_none_clears_the_selection(self, service, tmp_path):
        (tmp_path / "a.py").write_text("x")
        await service.set_selected_files(["a.py"])
        assert await service.set_selected_files(None) == []

    async def test_the_returned_list_is_a_copy(self, service, tmp_path):
        (tmp_path / "a.py").write_text("x")
        returned = await service.set_selected_files(["a.py"])
        returned.append("b.py")
        assert service.get_selected_files() == ["a.py"]

    async def test_the_new_selection_is_broadcast(self, service, tmp_path, events):
        """Everyone sees the result immediately, per the collaboration spec.

        Session-wide arity: the filtered list is the only argument. A
        participant's picker drifting from the host's is the failure this
        prevents.
        """
        (tmp_path / "a.py").write_text("x")
        await service.set_selected_files(["a.py"])
        assert events.call_of("filesChanged") == ("filesChanged", ["a.py"])


# ---------------------------------------------------------------------------
# Live controls
# ---------------------------------------------------------------------------


class TestLiveControls:
    async def test_permission_mode_change_is_broadcast(self, service, events):
        assert await service.set_permission_mode("plan") == {"mode": "plan"}
        payload = events.payload_of("permissionModeChanged")
        assert payload == {"mode": "plan", "by": "user"}

    async def test_a_bad_mode_lists_the_valid_ones(self, service, events):
        answer = await service.set_permission_mode("yolo")
        assert "yolo" in answer["error"]
        assert "plan" in answer["valid_modes"]
        assert events.calls == []

    async def test_a_control_on_a_lost_session_reports_the_reason(self, service):
        service.session.control_error = SessionLostError("session gone")
        assert await service.set_permission_mode("plan") == {"error": "session gone"}

    async def test_an_unexpected_control_failure_is_wrapped(self, service):
        service.session.control_error = RuntimeError("no response")
        answer = await service.set_permission_mode("plan")
        assert "Could not change the permission mode" in answer["error"]

    async def test_model_change_returns_the_applied_model(self, service):
        assert await service.set_model("claude-opus-5") == {"model": "claude-opus-5"}
        assert await service.set_model(None) == {"model": None}

    async def test_rewind_does_not_claim_to_know_what_was_restored(self, service):
        """The SDK's rewind_files() returns nothing; refresh the tree instead."""
        answer = await service.rewind_files("msg-uuid-1")
        assert answer == {"restored": [], "user_message_id": "msg-uuid-1"}
        assert ("rewind_files", ("msg-uuid-1",)) in service.session.control_calls

    async def test_a_rewind_failure_is_returned(self, service):
        service.session.control_error = RuntimeError("no checkpoint")
        assert "no checkpoint" in (await service.rewind_files("m"))["error"]

    async def test_stop_task_reports_stopping_not_stopped(self, service):
        """The kill is asynchronous; the task reports its own terminal status."""
        answer = await service.stop_task("task-1")
        assert answer == {"status": "stopping", "task_id": "task-1"}

    async def test_context_usage_is_timestamped(self, service):
        answer = await service.get_context_usage()
        assert answer["usage"] == {"total_tokens": 1000}
        assert answer["fetched_at"].startswith("20")

    async def test_context_usage_on_a_cold_engine_reports_the_reason(self, service):
        service.session.control_error = EngineNotReadyError("not connected")
        assert await service.get_context_usage() == {"error": "not connected"}

    async def test_mcp_status_passes_through(self, service):
        assert await service.get_mcp_status() == {"servers": []}

    async def test_mcp_controls_report_what_they_did(self, service):
        assert await service.reconnect_mcp_server("ac-dc") == {
            "status": "reconnecting",
            "name": "ac-dc",
        }
        assert await service.toggle_mcp_server("ac-dc", False) == {
            "status": "ok",
            "name": "ac-dc",
            "enabled": False,
        }

    async def test_server_info_passes_through(self, service):
        assert await service.get_server_info() == {"commands": ["review"]}

    async def test_absent_server_info_is_an_empty_dict(self, service):
        service.session.get_server_info = lambda: _none()
        assert await service.get_server_info() == {}


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestPermissionRpc:
    """The service half of the gate: authority, and the settings file.

    ``resolve_permission`` is the method that authorises arbitrary
    ``Bash``. A remote participant able to call it would turn
    collaboration mode into a remote-code-execution grant, so the
    localhost gate here is the highest-stakes one in the app.
    """

    async def test_a_participant_cannot_resolve_a_permission(self, service, caplog):
        service._collab = FakeCollab(is_localhost=False)
        with caplog.at_level(logging.WARNING):
            answer = await service.resolve_permission("perm-1", {"action": "allow"})
        assert answer == {
            "error": "restricted",
            "reason": "Participants cannot perform this action",
        }
        assert "non-localhost" in caplog.text

    async def test_a_participant_cannot_set_denied_reads(self, service):
        service._collab = FakeCollab(is_localhost=False)
        assert service.set_denied_read_files(["a"])["error"] == "restricted"

    async def test_a_broken_collab_check_denies(self, service):
        """Fail closed: an unanswerable authority question is not an allow."""
        service._collab = FakeCollab(raises=True)
        answer = await service.resolve_permission("perm-1", {"action": "allow"})
        assert answer["error"] == "restricted"

    async def test_localhost_reaches_the_broker(self, service):
        service._collab = FakeCollab(is_localhost=True)
        answer = await service.resolve_permission("perm-nope", {"action": "allow"})
        assert answer["error"] == "unknown"

    async def test_without_collab_the_caller_is_local(self, service):
        answer = await service.resolve_permission("perm-nope", {"action": "allow"})
        assert answer["error"] == "unknown"

    def test_denied_reads_round_trip_through_settings(self, service, tmp_path):
        assert service.get_denied_read_files() == []
        answer = service.set_denied_read_files([".env", "secrets/**"])
        assert answer["denied_read_files"] == [".env", "secrets/**"]
        assert "next read of its settings sources" in answer["takes_effect"]
        assert service.get_denied_read_files() == [".env", "secrets/**"]
        assert (tmp_path / ".claude" / "settings.local.json").is_file()

    def test_a_malformed_settings_file_is_reported_not_clobbered(self, service, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir()
        path.write_text("{ oops")
        answer = service.set_denied_read_files(["a"])
        assert "not valid JSON" in answer["error"]
        assert path.read_text() == "{ oops"

    async def test_the_state_snapshot_carries_the_dialog_queue(self, service):
        """A client that reloads mid-request must be able to re-render it."""
        state = await service.get_current_state()
        assert state["pending_permissions"] == []
        assert state["denied_read_files"] == []

    def test_localhost_presence_needs_one_local_client(self, service):
        service._collab = FakeCollab(clients=[{"is_localhost": False}])
        assert service._localhost_available() is False
        service._collab = FakeCollab(
            clients=[{"is_localhost": False}, {"is_localhost": True}]
        )
        assert service._localhost_available() is True

    def test_presence_without_collab_is_true(self, service):
        assert service._localhost_available() is True

    async def test_the_gate_is_wired_into_the_engine(self, tmp_path, events):
        """Phase 2's whole point: no run reaches a tool without the callback."""
        svc = ClaudeCodeService(
            FakeConfig(tmp_path), event_callback=events, engine_config=EngineConfig()
        )
        assert svc.session._can_use_tool == svc.permissions.can_use_tool


# ---------------------------------------------------------------------------
# Collaboration restrictions
# ---------------------------------------------------------------------------
#
# The ``Repo`` half lives in ``test_collab_restrictions.py``, along with the
# dispatch-level test that the gate can still see who is calling. This half
# is here because it needs the fake engine.

# Every mutating RPC, with arguments good enough to reach the gate. The
# frontend moved onto this service in phase 2, and a gate that was on the
# native method but not its replacement is a restriction silently dropped —
# so the table is exhaustive rather than representative.
GATED_METHODS: dict[str, tuple] = {
    "connect_engine": (),
    "shutdown": (),
    "set_selected_files": (["a.py"],),
    "chat_streaming": (REQUEST_ID, "hello"),
    "cancel_streaming": (REQUEST_ID,),
    "resolve_permission": ("perm-1", {"action": "allow"}),
    "set_denied_read_files": ([".env"],),
    "set_permission_mode": ("bypassPermissions",),
    "set_model": ("claude-opus-5",),
    "rewind_files": ("msg-uuid-1",),
    "stop_task": ("task-1",),
    "reconnect_mcp_server": ("ac-dc",),
    "toggle_mcp_server": ("ac-dc", True),
    # Writes nothing, and is still a lever on the prompt: the viewer path
    # goes into the turn framing and into the `ui_state` tool, so a
    # participant could point the agent at a file of their choosing on
    # somebody else's turn.
    "set_viewer_state": ("src/a.py",),
    # Which conversation the host's engine is attached to. A participant
    # switching it would decide for everyone, and abandoning a session
    # discards the context every client is looking at.
    "new_session": (),
    "resume_session": ("sess-1",),
    # Git writes to the host's tree, and the review arrangement moves every
    # file in it. Same restriction the native methods carried.
    "commit_all": (),
    "reset_to_head": (),
    "start_review": ("feature", "abc123"),
    "end_review": (),
}

# Deliberately reachable by a participant, with arguments good enough to
# call. Watching a turn is the point of collaboration: a participant who can
# see less than the host cannot review what the agent did
# (``specs5/4-features/collaboration.md`` § Read-Only).
READ_ONLY_METHODS: dict[str, tuple] = {
    "get_engine_health": (),
    "get_current_state": (),
    "get_selected_files": (),
    "get_denied_read_files": (),
    "get_context_usage": (),
    "get_mcp_status": (),
    "get_server_info": (),
    # Reading the review, the graph and a diff is the reviewing part of
    # collaboration; withholding it would leave a participant unable to see
    # what they were invited to look at.
    "check_review_ready": (),
    "get_commit_graph": (),
    "get_review_state": (),
    "get_review_file_diff": ("a.py",),
    "get_snippets": (),
    # The symbol index answers questions about the tree and changes nothing.
    "lsp_get_hover": ("a.py", 1, 0),
    "lsp_get_definition": ("a.py", 1, 0),
    "lsp_get_references": ("a.py", 1, 0),
    "lsp_get_completions": ("a.py", 1, 0),
    # "Broadcast when *any* client navigates to a file"
    # (specs5/4-features/collaboration.md § File Navigation Sync) — pointing
    # everyone at a file is a participant's to do.
    "navigate_file": ("a.py",),
    # Reading past sessions changes nothing, and a participant who cannot
    # see what the agent already did cannot review the work they were
    # invited to look at. `history_delete` is the localhost-only one.
    "history_list": (),
    "history_load": ("sess-1",),
}


class TestCollabRestrictions:
    """Who may do what, once a remote participant is in the session."""

    def test_every_rpc_is_classified(self):
        """A new public method is a new RPC — jrpc-oo exposes them all.

        ``ExposeClass`` publishes every method whose name lacks a leading
        underscore, so adding one silently widens the RPC surface. This
        assertion fails on the next addition and asks the only question
        that matters: may a participant call it?
        """
        public = {
            name
            for name in dir(ClaudeCodeService)
            if not name.startswith("_")
            and callable(getattr(ClaudeCodeService, name, None))
        }
        assert public == set(GATED_METHODS) | set(READ_ONLY_METHODS)

    @pytest.mark.parametrize("method", sorted(GATED_METHODS))
    async def test_a_participant_is_refused(self, service, method):
        service._collab = FakeCollab(is_localhost=False)
        answer = getattr(service, method)(*GATED_METHODS[method])
        if asyncio.iscoroutine(answer):
            answer = await answer
        assert answer == {
            "error": "restricted",
            "reason": "Participants cannot perform this action",
        }, method

    @pytest.mark.parametrize("method", sorted(GATED_METHODS))
    async def test_a_refusal_has_no_side_effect(self, service, method):
        """Restricted calls "always return an error without side effects"."""
        service._collab = FakeCollab(is_localhost=False)
        answer = getattr(service, method)(*GATED_METHODS[method])
        if asyncio.iscoroutine(answer):
            await answer
        assert service.session.connect_calls == []
        assert service.session.disconnect_calls == 0
        assert service.session.control_calls == []
        assert service.session.turns == []
        assert service.get_selected_files() == []
        assert service.get_denied_read_files() == []
        assert events_of(service) == []

    @pytest.mark.parametrize("method", sorted(READ_ONLY_METHODS))
    async def test_a_participant_may_watch(self, service, method):
        service._collab = FakeCollab(is_localhost=False)
        answer = getattr(service, method)(*READ_ONLY_METHODS[method])
        if asyncio.iscoroutine(answer):
            answer = await answer
        if isinstance(answer, dict):
            assert answer.get("error") != "restricted", method

    @pytest.mark.parametrize("method", sorted(GATED_METHODS))
    async def test_a_broken_collab_check_refuses(self, service, method):
        """Fail closed. An unanswerable authority question is not an allow."""
        service._collab = FakeCollab(raises=True)
        answer = getattr(service, method)(*GATED_METHODS[method])
        if asyncio.iscoroutine(answer):
            answer = await answer
        assert answer["error"] == "restricted", method

    async def test_a_participant_cannot_probe_with_a_slash_command(self, service):
        """The gate is ahead of the slash reply, so ``/context`` is not an
        oracle for whether the caller is restricted."""
        service._collab = FakeCollab(is_localhost=False)
        answer = await service.chat_streaming(REQUEST_ID, "/context")
        assert answer["error"] == "restricted"

    async def test_the_host_still_works_with_a_participant_present(self, service):
        """The gate asks about the caller, not about who else is connected."""
        service._collab = FakeCollab(
            is_localhost=True, clients=[{"is_localhost": True}, {"is_localhost": False}]
        )
        assert await send(service) == {"status": "started"}


def events_of(service):
    """The events a service emitted, for the no-side-effect assertions."""
    callback = service._event_callback
    return list(getattr(callback, "calls", []))


# ---------------------------------------------------------------------------
# The bridge and the hook, as the session receives them — phase 4
# ---------------------------------------------------------------------------


class TestBridgeWiring:
    """The indexes reach the agent, or the session says why it could not.

    Both are constructor arguments to the session, like the permission
    gate, because attaching either afterwards would need a reconnect.
    """

    @pytest.fixture
    def wired(self, tmp_path, events):
        """A service with its real session object, so the wiring is visible."""
        return ClaudeCodeService(
            FakeConfig(tmp_path), event_callback=events, engine_config=EngineConfig()
        )

    def test_the_server_is_registered_under_its_own_name(self, wired):
        """`mcp__ac-dc__symbol_map` is the name the CLI and the permission
        classifier both spell out, so the key is interface."""
        from ac_dc.claude_code.mcp_server import SERVER_NAME

        assert list(wired.session._mcp_servers) == [SERVER_NAME]
        assert SERVER_NAME == "ac-dc"

    def test_the_post_write_hook_is_the_only_subscription(self, wired):
        assert list(wired.session._hooks) == ["PostToolUse"]

    def test_the_bridge_and_the_reindex_share_one_flush(self, wired):
        """A tool that flushed a different queue than the hook fills would
        answer from the pre-write index while reporting itself fresh."""
        assert wired.mcp_bridge._flush == wired.reindexer.flush

    def test_a_bridge_that_will_not_build_still_leaves_a_session(
        self, tmp_path, events, monkeypatch, caplog
    ):
        """Without the bridge the agent loses two tools and keeps every
        built-in; refusing to construct would trade that for a dead editor."""
        from ac_dc.claude_code import mcp_server as mcp_module

        def boom(self):
            raise RuntimeError("no sdk")

        monkeypatch.setattr(mcp_module.McpBridge, "build_server", boom)
        with caplog.at_level(logging.WARNING):
            svc = ClaudeCodeService(
                FakeConfig(tmp_path),
                event_callback=events,
                engine_config=EngineConfig(),
            )
        assert svc.session is not None
        assert svc.session._mcp_servers is None
        # And the hook half is unaffected: one failure is not the other's.
        assert list(svc.session._hooks) == ["PostToolUse"]
        assert "fall back to Glob/Grep/Read" in caplog.text

    def test_a_hook_that_will_not_build_still_leaves_a_session(
        self, tmp_path, events, monkeypatch, caplog
    ):
        from ac_dc.claude_code import service as service_module

        def boom(reindexer, broadcast=None):
            raise RuntimeError("no sdk")

        monkeypatch.setattr(service_module, "build_hook_matchers", boom)
        with caplog.at_level(logging.WARNING):
            svc = ClaudeCodeService(
                FakeConfig(tmp_path),
                event_callback=events,
                engine_config=EngineConfig(),
            )
        assert svc.session._hooks is None
        assert svc.session._mcp_servers is not None
        assert "will not follow the agent's writes" in caplog.text


# ---------------------------------------------------------------------------
# The session store, as the session receives it — phase 5
# ---------------------------------------------------------------------------


class TestSessionStoreWiring:
    """The mirror reaches the engine, or history quietly stops surviving.

    This wiring has no visible symptom when it breaks: the CLI keeps its
    own transcript, so a session with no store works right up until the
    CLI's retention timer expires it — days later, looking like data loss
    rather than a dropped constructor argument.
    """

    @pytest.fixture
    def wired(self, tmp_path, events):
        return ClaudeCodeService(
            FakeConfig(tmp_path), event_callback=events, engine_config=EngineConfig()
        )

    def test_the_store_the_service_built_is_the_one_the_session_got(self, wired):
        assert wired.session_store is not None
        assert wired.session._session_store is wired.session_store

    def test_the_store_points_at_the_repo_not_the_home_directory(
        self, wired, tmp_path
    ):
        """`.ac-dc4/sessions/` is the whole point: the CLI already has a copy
        under ~/.claude/projects/, and that is the one that expires."""
        assert wired.session_store.root == tmp_path / ".ac-dc4" / "sessions"

    def test_building_the_service_writes_nothing(self, wired, tmp_path):
        """A directory that exists is not the same signal as a session that
        was mirrored, so the store makes its own on first append."""
        assert not (tmp_path / ".ac-dc4" / "sessions").exists()

    def test_an_injected_store_wins(self, tmp_path, events):
        sentinel = object()
        svc = ClaudeCodeService(
            FakeConfig(tmp_path),
            event_callback=events,
            engine_config=EngineConfig(),
            session_store=sentinel,
        )
        assert svc.session._session_store is sentinel

    def test_no_repo_means_no_store_and_a_session_that_still_runs(
        self, tmp_path, events, caplog
    ):
        """Nowhere to mirror to is not a reason to refuse to start."""
        with caplog.at_level(logging.INFO):
            svc = ClaudeCodeService(
                SimpleNamespace(repo_root=tmp_path, config_dir=None, ac_dc_dir=None),
                event_callback=events,
                engine_config=EngineConfig(),
            )
        assert svc.session_store is None
        assert svc.session is not None
        assert svc.session._session_store is None
        assert "sessions are not mirrored" in caplog.text

    def test_the_project_key_comes_from_the_sdk(self, wired):
        """A hand-rolled sanitiser would point the readers at a directory
        nothing writes to."""
        from claude_agent_sdk import project_key_for_directory

        assert wired._session_project_key() == project_key_for_directory(
            str(wired._repo_root)
        )


class TestHistoryRpcs:
    """A real store, a real transcript, and the two read RPCs over it.

    The point of phase 5: "restarting the server resumes the previous
    conversation with context intact" (``specs5/plan/README.md``). These go
    through the production store and the SDK's own parsers rather than a
    fake, because the parsers are exactly what a hand-rolled fixture would
    stop testing.
    """

    @pytest.fixture
    def session_id(self):
        import uuid

        return str(uuid.uuid4())

    @pytest.fixture
    async def wired(self, tmp_path, events, session_id):
        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        svc.session.session_id = session_id
        await seed_transcript(svc, session_id)
        return svc

    async def test_a_stored_session_is_listed(self, wired, session_id):
        listed = await wired.history_list()
        assert [s["session_id"] for s in listed] == [session_id]
        assert listed[0]["preview"] == "fix the parser"
        assert listed[0]["resumable"] is True

    async def test_a_stored_session_loads_as_renderable_messages(
        self, wired, session_id
    ):
        messages = await wired.history_load(session_id)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "fix the parser"
        assert messages[1]["turn"]["model_usage"] == {
            "claude-opus-5": {"input_tokens": 40, "output_tokens": 9}
        }
        # Prompt at :00, reply at :02 — the wait the user actually had.
        assert messages[1]["turn"]["duration_ms"] == 2000

    async def test_this_session_events_are_interleaved(self, wired, session_id):
        """The half of a browsed transcript the engine never wrote."""
        from ac_dc.claude_code.events_log import commit_content

        await wired._record_event("commit", commit_content("abc1234", "fix: it"))
        messages = await wired.history_load(session_id)
        commits = [m for m in messages if m.get("event") == "commit"]
        assert len(commits) == 1
        assert commits[0]["system_event"] is True

    async def test_an_event_with_no_session_does_not_reach_disk(self, wired):
        wired.session.session_id = None
        await wired._record_event("reset", "gone")
        assert wired.events_log.dropped_without_session == 1

    async def test_an_unknown_session_is_an_error_not_an_empty_list(self, wired):
        """Empty would render as a session that happened and said nothing."""
        answer = await wired.history_load("00000000-0000-4000-8000-000000000000")
        assert "error" in answer

    async def test_no_session_id_is_refused(self, wired):
        assert "error" in await wired.history_load("")

    async def test_a_read_failure_is_reported_not_swallowed(self, wired, session_id):
        async def boom(*args, **kwargs):
            raise OSError("disk is gone")

        wired.session_store.list_session_summaries = boom
        wired.session_store.list_sessions = boom
        answer = await wired.history_list()
        assert "error" in answer

    async def test_without_a_repo_listing_is_empty_and_loading_says_why(
        self, tmp_path, events
    ):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, ac_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert svc.events_log is None
        assert await svc.history_list() == []
        assert "error" in await svc.history_load("whatever")

    async def test_recording_an_event_without_a_log_is_a_no_op(
        self, tmp_path, events
    ):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, ac_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        svc.session.session_id = "s1"
        await svc._record_event("reset", "nowhere to write this")

    async def test_a_log_failure_does_not_fail_the_action_it_records(self, wired):
        """The commit already happened; raising here would fail a completed
        action over a missing history line."""

        async def boom(*args, **kwargs):
            raise RuntimeError("log is wedged")

        wired.events_log.append = boom
        await wired._record_event("commit", "already done")

    def test_the_log_points_at_the_repo(self, wired, tmp_path):
        assert wired.events_log.path == tmp_path / "repo" / ".ac-dc4" / "events.jsonl"


class TestSessionLifecycle:
    """New, resume, fork — and the restart that is meant to be invisible."""

    @pytest.fixture
    def old(self):
        return "11111111-1111-4111-8111-111111111111"

    @pytest.fixture
    def newer(self):
        return "22222222-2222-4222-8222-222222222222"

    @pytest.fixture
    async def wired(self, tmp_path, events, old, newer):
        """Two stored sessions, `newer` mirrored last, and a cold engine."""
        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        await seed_transcript(svc, old, prompt="the older question")
        await seed_transcript(svc, newer, prompt="the newer question")
        events.calls.clear()
        return svc

    # -- Restart -----------------------------------------------------

    async def test_a_restart_resumes_the_most_recent_session(self, wired, newer):
        """The phase's exit criterion: restarting resumes the previous
        conversation, with no pointer file to say which one it was."""
        await wired.connect_engine()
        assert wired.session.connect_args == [(newer, False)]

    async def test_the_first_turn_resumes_it_too(self, wired, newer):
        """Auto-resume has to live in the lazy connect, not in a startup
        step: the connect a user actually triggers is the first turn's."""
        await send(wired)
        assert wired.session.connect_args[0] == (newer, False)

    async def test_the_snapshot_shows_the_session_a_restart_will_resume(
        self, wired
    ):
        """Otherwise the model has context the user cannot see."""
        state = await wired.get_current_state()
        assert [m["content"] for m in state["messages"] if m["role"] == "user"] == [
            "the newer question"
        ]

    async def test_the_snapshot_reads_no_engine_into_existence(self, wired):
        """A read, not a resume (``specs5/6-deployment/startup.md`` § Phase 1):
        the many launches that never chat must not spawn a CLI."""
        await wired.get_current_state()
        assert wired.session.connect_calls == []

    async def test_a_live_session_wins_over_the_store(self, wired, old):
        wired.session.session_id = old
        state = await wired.get_current_state()
        assert [m["content"] for m in state["messages"] if m["role"] == "user"] == [
            "the older question"
        ]

    async def test_a_storeless_service_still_snapshots(self, tmp_path, events):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, ac_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert (await svc.get_current_state())["messages"] == []

    async def test_an_unreadable_session_snapshots_empty_not_broken(
        self, wired, caplog
    ):
        """Every other field is still worth painting."""

        async def boom(*args, **kwargs):
            raise OSError("disk is gone")

        wired.session.session_id = "33333333-3333-4333-8333-333333333333"
        wired.session_store.load = boom
        with caplog.at_level(logging.WARNING):
            state = await wired.get_current_state()
        assert state["messages"] == []
        assert "for the state snapshot" in caplog.text
        assert state["engine_health"]["cli_version"] == "2.1.229"

    # -- New ---------------------------------------------------------

    async def test_new_session_abandons_the_old_one(self, wired):
        answer = await wired.new_session()
        assert answer == {"session_id": None, "status": "new"}
        assert wired.session.reset_calls == 1
        assert wired.session.session_id is None

    async def test_new_session_does_not_connect(self, wired):
        """A new session that never gets a turn should cost nothing."""
        await wired.new_session()
        assert wired.session.connect_calls == []

    async def test_the_next_turn_after_new_starts_blank(self, wired):
        await wired.new_session()
        await send(wired)
        assert wired.session.connect_args == [(None, False)]

    async def test_new_session_empties_the_snapshot(self, wired):
        """The store still holds the old sessions; none of them is this one."""
        await wired.new_session()
        assert (await wired.get_current_state())["messages"] == []

    async def test_new_session_tells_every_client(self, wired, events):
        await wired.new_session()
        changed = [c for c in events.calls if c[0] == "sessionChanged"]
        assert changed[0][1] == {
            "session_id": None,
            "messages": [],
            "action": "new",
        }

    async def test_new_session_waits_for_the_turn_to_finish(self, wired):
        wired.session.streaming_active = True
        answer = await wired.new_session()
        assert answer["reason"] == "turn_in_progress"
        assert wired.session.reset_calls == 0

    # -- Resume ------------------------------------------------------

    async def test_resume_attaches_the_engine_to_that_session(self, wired, old):
        answer = await wired.resume_session(old)
        assert answer == {"session_id": old}
        assert wired.session.connect_args == [(old, False)]

    async def test_a_pending_request_beats_the_auto_resume_default(self, wired, old):
        """Why the request is held rather than passed: the choice is made
        inside the connect lock, so a first turn arriving alongside the click
        connects to what the user asked for and not to the newest session."""
        wired._resume_request = (old, False)
        await send(wired)
        assert wired.session.connect_args == [(old, False)]

    async def test_a_resumed_session_survives_being_lost(self, wired, old):
        """Auto-resume comes back on after the request is consumed, so a
        session lost mid-conversation reattaches to itself rather than
        silently continuing as a blank one."""
        await wired.resume_session(old)
        assert wired._resume_request is None
        wired.session.ready = False
        await wired.connect_engine()
        assert wired.session.connect_args[-1] == (old, False)

    async def test_resume_broadcasts_the_rendered_transcript(
        self, wired, events, old
    ):
        await wired.resume_session(old)
        changed = [c for c in events.calls if c[0] == "sessionChanged"][0][1]
        assert changed["session_id"] == old
        assert changed["action"] == "resumed"
        assert changed["forked_from"] is None
        assert [m["content"] for m in changed["messages"] if m["role"] == "user"] == [
            "the older question"
        ]

    async def test_resume_is_recorded_in_the_session_it_names(self, wired, old):
        await wired.resume_session(old)
        records = await wired.events_log.load(old)
        assert [r["event"] for r in records] == ["session_switch"]
        assert old in records[0]["content"]

    async def test_an_unreadable_session_is_not_resumed(self, wired):
        """Browsable but not resumable — the CLI would fail the connect and
        the user would be looking at an engine that will not start."""
        answer = await wired.resume_session("44444444-4444-4444-8444-444444444444")
        assert answer["reason"] == "not_resumable"
        assert wired.session.connect_calls == []

    async def test_resume_needs_a_session_id(self, wired):
        assert (await wired.resume_session(""))["reason"] == "no_session_id"

    async def test_resume_waits_for_the_turn_to_finish(self, wired, old):
        wired.session.streaming_active = True
        answer = await wired.resume_session(old)
        assert answer["reason"] == "turn_in_progress"
        assert wired.session.connect_calls == []

    async def test_a_failed_connect_is_reported_not_swallowed(self, wired, old):
        wired.session.connect_error = EngineStartupError("no CLI")
        answer = await wired.resume_session(old)
        assert answer["reason"] == "startup_failed"
        assert "no CLI" in answer["error"]

    # -- Fork --------------------------------------------------------

    async def test_a_fork_leaves_the_original_alone(self, wired, old):
        answer = await wired.resume_session(old, fork=True)
        assert answer["forked_from"] == old
        assert wired.session.connect_args == [(old, True)]

    async def test_a_fork_cannot_name_itself_yet(self, wired, old):
        """The CLI mints the ID and only reports it in the first turn's init
        message, so there is nothing honest to put here."""
        answer = await wired.resume_session(old, fork=True)
        assert answer["session_id"] is None

    async def test_a_fork_is_recorded_against_the_origin(self, wired, old):
        """Where the user actually was. The fork has no ID to file it under,
        and dropping the record would lose the branch entirely."""
        await wired.resume_session(old, fork=True)
        records = await wired.events_log.load(old)
        assert records[0]["payload"] == {
            "action": "forked",
            # Unknowable until the fork's first turn, and null rather than
            # the origin's ID: naming the origin here would claim the branch
            # is the session it came from.
            "session_id": None,
            "forked_from": old,
        }

    async def test_a_fork_broadcasts_the_copied_transcript(self, wired, events, old):
        await wired.resume_session(old, fork=True)
        changed = [c for c in events.calls if c[0] == "sessionChanged"][0][1]
        assert changed["action"] == "forked"
        assert changed["forked_from"] == old
        assert [m["content"] for m in changed["messages"] if m["role"] == "user"] == [
            "the older question"
        ]


class TestIndexReadiness:
    """Absent, building, built — three answers, not two.

    A half-built map reads as "these files have no symbols" and the agent
    does not go back to check, so the middle state has to be its own.
    """

    def test_before_the_walk_the_index_is_not_ready(self, service):
        assert service._symbol_index_ready is False
        assert service._live_symbol_index() is None

    def test_a_partial_index_is_not_offered_as_a_map(self, service):
        """Monaco keeps using it for hovers; the map does not claim it."""
        service._attach_symbol_index(object())
        assert service._symbol_index_ready is False
        assert service._live_symbol_index() is not None

    def test_the_walk_finishing_makes_it_ready(self, service):
        service._attach_symbol_index(object())
        service._mark_symbol_index_ready()
        assert service._symbol_index_ready is True

    def test_a_failed_walk_reports_unavailable_not_partial(self, service):
        """The partially-built index is sitting right there, and serving it
        would be a confident lie about the repo."""
        service._attach_symbol_index(object())
        service._mark_symbol_index_failed()
        assert service._live_symbol_index() is None
        assert service._symbol_index_ready is False

    def test_a_failure_during_a_rebuild_withdraws_readiness(self, service):
        service._attach_symbol_index(object())
        service._mark_symbol_index_ready()
        service._mark_symbol_index_failed()
        assert service._symbol_index_ready is False

    def test_a_detached_index_counts_as_failed(self, service):
        service._attach_symbol_index(None)
        assert service._live_symbol_index() is None

    def test_a_failed_doc_build_is_withheld_too(self, service):
        assert service._live_doc_index() is service.doc_builder.doc_index
        service.doc_builder.failed = True
        assert service._live_doc_index() is None


class TestUiStateSnapshot:
    """What the `ui_state` tool answers with: paths and modes, never content."""

    def test_it_carries_the_four_facts_the_agent_cannot_read_itself(
        self, service
    ):
        snapshot = service._ui_state_snapshot()
        assert set(snapshot) == {
            "selected_files",
            "viewer",
            "review_state",
            "permission_mode",
        }

    async def test_the_picker_selection_shows_up(self, service, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        await service.set_selected_files(["a.py"])
        assert service._ui_state_snapshot()["selected_files"] == ["a.py"]

    def test_the_viewer_is_none_until_a_browser_says_otherwise(self, service):
        assert service._ui_state_snapshot()["viewer"] is None

    def test_a_viewer_push_reaches_the_snapshot(self, service):
        service.set_viewer_state("src/a.py", 10, 40)
        assert service._ui_state_snapshot()["viewer"] == {
            "path": "src/a.py",
            "start_line": 10,
            "end_line": 40,
        }

    def test_closing_the_pane_clears_it(self, service):
        """Rather than leaving the agent pointed at a file nobody is on."""
        service.set_viewer_state("src/a.py")
        assert service.set_viewer_state(None) == {"status": "cleared"}
        assert service._ui_state_snapshot()["viewer"] is None

    def test_junk_line_numbers_are_dropped_not_echoed(self, service):
        answer = service.set_viewer_state("src/a.py", "ten", None)
        assert answer == {"status": "ok", "path": "src/a.py"}

    def test_the_snapshot_does_not_alias_the_service_state(self, service):
        """The tool serialises it; a caller mutating the copy must not
        rewrite what the next turn is framed with."""
        service.set_viewer_state("src/a.py")
        snapshot = service._ui_state_snapshot()
        snapshot["viewer"]["path"] = "elsewhere.py"
        snapshot["selected_files"].append("b.py")
        assert service._viewer_state == {"path": "src/a.py"}
        assert service._selected_files == []

    async def test_the_last_push_frames_a_turn_that_sends_no_viewer(
        self, service
    ):
        """The browser pushes on navigation; a turn sent from elsewhere
        should still know where the user is looking."""
        service.set_viewer_state("src/a.py", 3, 9)
        await send(service)
        turn = service.session.turns[0]
        assert turn.viewer is not None
        assert turn.viewer.path == "src/a.py"

    async def test_an_explicit_viewer_payload_still_wins(self, service):
        service.set_viewer_state("stale.py")
        await send(service, viewer={"path": "fresh.py"})
        assert service.session.turns[0].viewer.path == "fresh.py"


class TestReindexReporting:
    """`files_reindexed` in the turn footer: the frontend's only evidence
    that the agent's edits reached the indexes."""

    async def test_the_footer_names_what_was_refreshed(
        self, service, tmp_path, events
    ):
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        service.reindexer._reindexed.add("a.py")
        await send(service)
        assert events.payload_of("postResponseComplete")["files_reindexed"] == [
            "a.py"
        ]

    async def test_the_tally_does_not_repeat_on_the_next_turn(
        self, service, events
    ):
        service.reindexer._reindexed.add("a.py")
        await send(service)
        events.calls.clear()
        await send(service)
        assert events.payload_of("postResponseComplete")["files_reindexed"] == []

    async def test_a_flush_that_fails_does_not_fail_the_turn(
        self, service, events, caplog
    ):
        """The turn is over and the answer is already on screen; a stale
        index is not worth an error card."""

        async def boom():
            raise RuntimeError("index is wedged")

        service.reindexer.flush = boom
        with caplog.at_level(logging.DEBUG):
            await send(service)
        assert "postResponseComplete" in events.names()


class TestShutdown:
    async def test_shutdown_disconnects_the_engine(self, service):
        await service.connect_engine()
        await service.shutdown()
        assert service.session.disconnect_calls == 1

    async def test_shutdown_denies_pending_permissions(self, service):
        """A callback still waiting on a browser would never answer the CLI."""
        broker = service.permissions
        task = asyncio.create_task(
            broker.can_use_tool("Bash", {"command": "ls"}, FakePermissionContext())
        )
        for _ in range(200):
            if broker.pending():
                break
            await asyncio.sleep(0.002)
        await service.shutdown()
        result = await task
        assert type(result).__name__ == "PermissionResultDeny"
        assert "shut down" in result.message

    async def test_shutdown_cancels_a_turn_in_flight(self, service):
        release = asyncio.Event()

        async def hang(turn, emit=None):
            service.session.turns.append(turn)
            await release.wait()

        service.session.run_turn = hang
        await service.chat_streaming(REQUEST_ID, "hello")
        await asyncio.sleep(0)
        await service.shutdown()
        await finish_turns(service)
        assert service.session.disconnect_calls == 1


async def _none():
    return None
