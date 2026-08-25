"""Tests for aic_dc.claude_code.service — conversion phase 1.

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
- **Slash commands reach the CLI, which dispatches its own.** Only the
  handful this deployment answers differently — routed to a surface, or
  refused because the thing it reaches for is not here — are intercepted.
- **Failures are returned, not raised.** An RPC exception reaches the
  browser as a generic transport error instead of an actionable message.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from aic_dc.claude_code.engine_config import EngineConfig
from aic_dc.claude_code.health import EngineHealth, EngineStartupError
from aic_dc.claude_code.messages import Event
from aic_dc.claude_code.service import (
    SLASH_DENIED,
    SLASH_ROUTES,
    ClaudeCodeService,
)
from aic_dc.claude_code.session import (
    EngineNotReadyError,
    SessionLostError,
    TurnInProgressError,
)

REQUEST_ID = "1736956800000-a1b2c3"
PNG = "data:image/png;base64,aGk="


class FakeConfig:
    def __init__(self, repo_root, config_dir=None, aic_dc_dir=None):
        self.repo_root = repo_root
        self.config_dir = config_dir
        # Present so the session store is built on the production path in
        # every service test rather than only where one asks for it. Nothing
        # here appends, so no file is written — but a wiring change that
        # dropped the store would now have somewhere to show up.
        # None without a repo root, matching ConfigManager.aic_dc_dir.
        if aic_dc_dir is None and repo_root is not None:
            aic_dc_dir = Path(repo_root) / ".aic-dc"
        self.aic_dc_dir = aic_dc_dir

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
        # False the way the real session reports it whenever the transcript
        # is mirrored — which is every run with a repo, this fixture
        # included. Tests about undo turn it on deliberately.
        self.file_checkpointing = False
        # The turn in flight, which is what a mirrored entry is attributed
        # to. `None` means no turn, the way the real property reports it.
        self.active_request_id = None
        # Tool use IDs the broker recorded against the turn in flight.
        self.permission_prompts: list[str | None] = []

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
        # The initialize handshake's payload. A bare string entry, which is
        # not the shape the real CLI sends — `list_commands` tests replace
        # it with command dicts, and the ones that do not are asserting that
        # a malformed entry is skipped rather than crashing the palette.
        self.server_info: dict | None = {"commands": ["review"]}
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
        # The real session's `_active_turn` lives exactly this long, which is
        # also the window in which the CLI mirrors the turn's entries.
        self.active_request_id = turn.request_id
        try:
            for event in self.turn_events:
                if isinstance(event, BaseException):
                    raise event
                if emit is not None:
                    await emit(event)
        finally:
            self.streaming_active = False
            self.active_request_id = None
        return {"response": "Hi"}

    async def interrupt(self, request_id=None):
        self.control_calls.append(("interrupt", (request_id,)))
        if self.control_error is not None:
            raise self.control_error
        return self.interrupt_result

    def note_permission_prompt(self, tool_use_id=None):
        """What the real session returns: the turn the dialog belongs to.

        ``None`` when nothing is running, which is a request raised outside
        a turn. Present here because the attribution is what
        ``cancel_for_turn`` sweeps on — a fake that always answered ``None``
        would make every one of those tests pass vacuously.
        """
        self.permission_prompts.append(tool_use_id)
        return self.active_request_id

    async def set_permission_mode(self, mode):
        from aic_dc.claude_code.engine_config import PERMISSION_MODES

        self.control_calls.append(("set_permission_mode", (mode,)))
        if self.control_error is not None:
            raise self.control_error
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode {mode!r}.")
        self.permission_mode = mode
        return mode

    def note_permission_mode(self, mode):
        """The CLI moved the mode itself; record where it landed.

        No control call, like the real session: the change already
        happened on the permission result that carried it.
        """
        self.control_calls.append(("note_permission_mode", (mode,)))
        self.permission_mode = mode

    def prefer_permission_mode(self, mode):
        from aic_dc.claude_code.engine_config import PERMISSION_MODES

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
        return self.server_info


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


async def seed_image(service, session_id, *, source=None, tag="img"):
    """A prompt carrying an image block, the way a pasted screenshot lands.

    Returns the entry uuid, which is half of the pointer ``history_load``
    renders in place of the bytes.
    """
    entry_uuid = f"{session_id}-{tag}"
    await service.session_store.append(
        {
            "project_key": service._session_project_key(),
            "session_id": session_id,
        },
        [
            {
                "type": "user",
                "uuid": entry_uuid,
                "parentUuid": f"{session_id}-a1",
                "timestamp": "2026-08-16T00:00:04.000Z",
                "sessionId": session_id,
                "cwd": str(service._repo_root),
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look at this"},
                        {
                            "type": "image",
                            "source": source
                            or {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGk=",
                            },
                        },
                    ],
                },
            }
        ],
    )
    return entry_uuid


async def seed_subagent(
    service,
    session_id,
    agent_id,
    *,
    prompt="check the tests",
    reply="they pass",
    subpath=None,
    metadata=None,
):
    """A subagent transcript under a session, the way the mirror routes one.

    ``subpath`` defaults to the flat ``subagents/agent-<id>``; passing the
    nested ``subagents/workflows/<run>/agent-<id>`` form is how a workflow's
    subagent is stored. ``metadata`` is the ``.meta.json`` sidecar, which
    reaches a live mirror as a synthetic ``agent_metadata`` entry and never
    appears in the on-disk transcript.
    """
    entries = [
        {
            "type": "user",
            "uuid": f"{agent_id}-u1",
            "parentUuid": None,
            "timestamp": "2026-08-16T00:01:00.000Z",
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "cwd": str(service._repo_root),
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "uuid": f"{agent_id}-a1",
            "parentUuid": f"{agent_id}-u1",
            "timestamp": "2026-08-16T00:01:03.000Z",
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "cwd": str(service._repo_root),
            "message": {
                "id": f"msg_{agent_id}",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": reply}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        },
    ]
    if metadata is not None:
        entries.append({"type": "agent_metadata", **metadata})
    await service.session_store.append(
        {
            "project_key": service._session_project_key(),
            "session_id": session_id,
            "subpath": subpath or f"subagents/agent-{agent_id}",
        },
        entries,
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

    def test_the_phase_five_methods_have_landed(self, service):
        """Asserted absent since phase 1, on the reasoning that a stub
        reporting success would be worse than a missing method. This is the
        phase they exist in."""
        for name in (
            "history_list",
            "history_load",
            "history_search",
            "history_delete",
            "history_image",
            "new_session",
            "resume_session",
        ):
            assert callable(getattr(service, name, None)), name


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
    @pytest.mark.parametrize("command", sorted(SLASH_ROUTES))
    async def test_a_routed_command_names_its_surface(self, service, command):
        answer = await service.chat_streaming(REQUEST_ID, f"/{command}")
        assert answer["status"] == "routed"
        assert answer["command"] == command
        assert answer["target"] == SLASH_ROUTES[command]["target"]
        assert SLASH_ROUTES[command]["surface"] in answer["message"]

    @pytest.mark.parametrize("command", sorted(SLASH_DENIED))
    async def test_a_denied_command_says_why(self, service, command):
        answer = await service.chat_streaming(REQUEST_ID, f"/{command}")
        assert answer["status"] == "unsupported"
        assert answer["command"] == command
        assert SLASH_DENIED[command] in answer["message"]

    async def test_an_intercepted_command_never_starts_the_engine(
        self, service, events
    ):
        """Answered locally: no connect, no turn, no broadcast."""
        await service.chat_streaming(REQUEST_ID, "/clear")
        assert service.session.connect_calls == []
        assert service.session.turns == []
        assert events.calls == []

    async def test_arguments_do_not_hide_the_command(self, service):
        answer = await service.chat_streaming(REQUEST_ID, "/clear now")
        assert answer["command"] == "clear"

    async def test_case_and_whitespace_do_not_hide_the_command(self, service):
        answer = await service.chat_streaming(REQUEST_ID, "  /CLEAR  ")
        assert answer["command"] == "clear"

    async def test_a_builtin_reaches_the_engine(self, service):
        """The CLI dispatches its own built-ins, locally and for free.

        ``/compact`` was in the refusal table this replaced, on the premise
        that forwarding it would ask the model to summarise as prose. It
        does not: the CLI recognises the command before a turn is billed,
        so intercepting it withheld a working answer.
        """
        await send(service, "/compact")
        assert service.session.turns[0].message == "/compact"

    async def test_an_unknown_command_reaches_the_engine(self, service):
        """A typo is the CLI's to answer.

        It replies ``Unknown command: /contxt``, which names the mistake.
        Guessing on its behalf is how the table this replaced came to
        refuse commands that had since shipped and would have answered.
        """
        await send(service, "/contxt")
        assert service.session.turns[0].message == "/contxt"

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

    def test_no_command_is_both_routed_and_denied(self):
        """Two tables, one lookup order: an overlap would be unreadable."""
        assert not set(SLASH_ROUTES) & set(SLASH_DENIED)

    @pytest.mark.parametrize("command", sorted(SLASH_ROUTES))
    def test_every_route_has_a_palette_description(self, command):
        """``list_commands`` falls back to it for a route the CLI omits."""
        assert SLASH_ROUTES[command]["palette"]

    @pytest.mark.parametrize("command", sorted(SLASH_ROUTES))
    def test_every_route_answers_whether_it_survives_a_turn(self, command):
        """A missing flag would default to False and silently withhold a
        command that works perfectly well beside a streaming turn — the
        failure this whole field exists to correct."""
        assert isinstance(SLASH_ROUTES[command]["during_turn"], bool)

    def test_only_the_session_swaps_are_withheld_mid_turn(self):
        """The mid-turn question is "does answering this need a model turn?",
        and for a routed command the answer is no — it never reaches
        ``query()``. So the exceptions are not about cost or concurrency:
        they are the two commands that would swap the session out from under
        the stream the user is watching.
        """
        withheld = {
            name for name, route in SLASH_ROUTES.items() if not route["during_turn"]
        }
        assert withheld == {"clear", "resume"}


# ---------------------------------------------------------------------------
# The `/` palette's command list
# ---------------------------------------------------------------------------


def _commands_by_name(answer):
    return {command["name"]: command for command in answer["commands"]}


class TestListCommands:
    async def test_it_reports_what_the_cli_advertises(self, service):
        service.session.server_info = {
            "commands": [
                {
                    "name": "code-review",
                    "aliases": ["review"],
                    "argumentHint": "<pr>",
                    "description": "Review a diff",
                }
            ]
        }
        commands = _commands_by_name(await service.list_commands())
        assert commands["code-review"] == {
            "name": "code-review",
            "aliases": ["review"],
            "argument_hint": "<pr>",
            "description": "Review a diff",
            "action": "send",
            "target": "",
            "during_turn": False,
        }

    async def test_a_routed_command_carries_its_target(self, service):
        service.session.server_info = {
            "commands": [{"name": "context", "description": "Visualize context"}]
        }
        commands = _commands_by_name(await service.list_commands())
        assert commands["context"]["action"] == "route"
        assert commands["context"]["target"] == "tab:context"

    async def test_a_routed_row_is_described_by_where_it_goes(self, service):
        """Our description wins over the CLI's for a routed command.

        The CLI describes what *its* version does, and selecting the row
        does not do that — it opens a surface, and the row says so with an
        "opens UI" badge. "Show total cost and duration of the current
        session" beside that badge is the row contradicting itself.
        """
        service.session.server_info = {
            "commands": [
                {"name": "cost", "description": "Show total cost and duration"}
            ]
        }
        commands = _commands_by_name(await service.list_commands())
        assert commands["cost"]["description"] == SLASH_ROUTES["cost"]["palette"]

    async def test_a_routed_row_keeps_the_clis_aliases(self, service):
        """Only the description is overridden — the CLI still knows the
        names and argument shape better than this module does."""
        service.session.server_info = {
            "commands": [
                {"name": "context", "aliases": ["ctx"], "argumentHint": "<n>"}
            ]
        }
        commands = _commands_by_name(await service.list_commands())
        assert commands["context"]["aliases"] == ["ctx"]
        assert commands["context"]["argument_hint"] == "<n>"

    async def test_a_passthrough_command_is_never_offered_mid_turn(self, service):
        """A ``send`` entry is a turn, and the guard allows one.

        True however cheaply the CLI answers it: ``/compact`` costs no model
        call, but it still arrives as a prompt on an input stream the CLI
        reads serially, so it could at best answer after the turn rather
        than beside it.
        """
        service.session.server_info = {"commands": [{"name": "compact"}]}
        commands = _commands_by_name(await service.list_commands())
        assert commands["compact"]["during_turn"] is False

    async def test_a_routed_command_reports_the_route_tables_answer(self, service):
        """The palette holds no second copy of the mid-turn rule."""
        service.session.server_info = {
            "commands": [{"name": "mcp"}, {"name": "clear"}]
        }
        commands = _commands_by_name(await service.list_commands())
        assert commands["mcp"]["during_turn"] is True
        assert commands["clear"]["during_turn"] is False

    async def test_a_denied_command_is_not_offered(self, service):
        """Offering one and then refusing it is worse than never showing it."""
        service.session.server_info = {
            "commands": [{"name": "rewind"}, {"name": "compact"}]
        }
        commands = _commands_by_name(await service.list_commands())
        assert "rewind" not in commands
        assert "compact" in commands

    async def test_a_plumbing_command_is_not_offered(self, service):
        service.session.server_info = {
            "commands": [{"name": "_internal"}, {"name": "compact"}]
        }
        assert "_internal" not in _commands_by_name(await service.list_commands())

    async def test_a_route_the_cli_omits_is_added(self, service):
        """``/permissions`` and ``/resume`` are not in the CLI's list at all."""
        service.session.server_info = {"commands": [{"name": "compact"}]}
        commands = _commands_by_name(await service.list_commands())
        for name in SLASH_ROUTES:
            assert commands[name]["action"] == "route"
        assert commands["resume"]["description"] == SLASH_ROUTES["resume"]["palette"]

    async def test_an_advertised_route_is_not_duplicated(self, service):
        service.session.server_info = {"commands": [{"name": "context"}]}
        answer = await service.list_commands()
        names = [command["name"] for command in answer["commands"]]
        assert names.count("context") == 1

    async def test_the_list_is_sorted_by_name(self, service):
        service.session.server_info = {
            "commands": [{"name": "usage"}, {"name": "agents"}]
        }
        names = [command["name"] for command in (await service.list_commands())["commands"]]
        assert names == sorted(names)

    async def test_a_malformed_entry_is_skipped(self, service):
        """The default fixture payload: a bare string where a dict belongs."""
        answer = await service.list_commands()
        assert [command["name"] for command in answer["commands"]] == sorted(
            SLASH_ROUTES
        )

    async def test_an_absent_payload_still_lists_the_routes(self, service):
        """The routes are this deployment's own, not the CLI's to supply."""
        service.session.server_info = None
        answer = await service.list_commands()
        assert [command["name"] for command in answer["commands"]] == sorted(
            SLASH_ROUTES
        )

    async def test_a_disconnected_engine_still_offers_the_routes(self, service):
        """The engine connects on the first turn, which is the turn the user
        is composing when they open the palette. Answering with an error
        there means the palette never opens on a fresh start — which is what
        it was built for. The routes need no CLI to describe."""

        async def _boom():
            raise EngineNotReadyError("no engine")

        service.session.get_server_info = _boom
        answer = await service.list_commands()
        assert answer["partial"] is True
        assert [command["name"] for command in answer["commands"]] == sorted(
            SLASH_ROUTES
        )
        assert all(command["action"] == "route" for command in answer["commands"])

    async def test_a_complete_list_is_not_marked_partial(self, service):
        """The webapp caches on this flag; a false positive would re-fetch
        for the rest of the session."""
        service.session.server_info = {"commands": [{"name": "compact"}]}
        assert "partial" not in await service.list_commands()

    async def test_a_lost_session_is_an_error(self, service):
        """Unlike a disconnected engine: the session went away mid-flight,
        which the health banner is already saying, and a palette listing four
        commands would suggest the other thirty had been withdrawn."""

        async def _boom():
            raise SessionLostError("session lost")

        service.session.get_server_info = _boom
        assert await service.list_commands() == {"error": "session lost"}

    async def test_an_unexpected_failure_is_an_error_too(self, service, caplog):
        async def _boom():
            raise RuntimeError("transport exploded")

        service.session.get_server_info = _boom
        with caplog.at_level(logging.ERROR):
            answer = await service.list_commands()
        assert "transport exploded" in answer["error"]
        assert "list_commands failed" in caplog.text


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

    async def test_a_turn_carries_no_file_list(self, service):
        """A turn has no file channel to default from: the user names files
        in the prompt (``specs5/plan/decisions.md`` CC-21)."""
        await send(service, "hello")
        assert not hasattr(service.session.turns[0], "files")

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
# Image pointers, after the fact
# ---------------------------------------------------------------------------


class TestUserMessageImages:
    """The addresses a pasted image only gets once the CLI has written it.

    ``userMessage`` goes out before the turn starts, so its ``image_refs`` is
    empty by necessity: a pointer is ``(session_id, entry_uuid, block)`` and
    the entry does not exist yet. The store's append observer is where those
    three become available, and this is what it announces
    (``specs5/4-features/images.md`` § Engine Service Integration).
    """

    @pytest.fixture
    def mirroring(self, service):
        """A service with a turn in flight, the way the mirror sees one."""
        service.session.active_request_id = REQUEST_ID
        return service

    async def test_a_mirrored_image_becomes_a_pointer(
        self, mirroring, events, tmp_path
    ):
        entry_uuid = await seed_image(mirroring, "sess-1")
        await finish_turns(mirroring)

        payload = events.payload_of("userMessageImages")
        assert payload["image_refs"] == [
            {
                "session_id": "sess-1",
                "entry_uuid": entry_uuid,
                # Index 1, not 0: the pasted text is the first block, and
                # `history_image` seeks by position in the stored list.
                "block": 1,
                "media_type": "image/png",
            }
        ]

    async def test_it_is_turn_scoped(self, mirroring, events):
        """So the browser knows which message the pointers belong to."""
        await seed_image(mirroring, "sess-1")
        await finish_turns(mirroring)

        name, request_id, payload = events.call_of("userMessageImages")
        assert name == "userMessageImages"
        assert request_id == REQUEST_ID
        assert isinstance(payload, dict)

    async def test_no_bytes_travel_with_the_pointer(self, mirroring, events):
        await seed_image(mirroring, "sess-1")
        await finish_turns(mirroring)
        assert "aGk=" not in str(events.payload_of("userMessageImages"))

    async def test_every_image_in_the_prompt_is_pointed_at(self, mirroring, events):
        await mirroring.session_store.append(
            {"project_key": mirroring._session_project_key(), "session_id": "sess-1"},
            [
                {
                    "type": "user",
                    "uuid": "u-two",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "compare these"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png"},
                            },
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/webp"},
                            },
                        ],
                    },
                }
            ],
        )
        await finish_turns(mirroring)

        refs = events.payload_of("userMessageImages")["image_refs"]
        assert [(r["block"], r["media_type"]) for r in refs] == [
            (1, "image/png"),
            (2, "image/webp"),
        ]

    async def test_a_prompt_without_images_says_nothing(self, mirroring, events):
        """Every turn's entries pass through here; almost none carry an image."""
        await seed_transcript(mirroring, "sess-1")
        await finish_turns(mirroring)
        assert "userMessageImages" not in events.names()

    async def test_nothing_is_announced_outside_a_turn(self, service, events):
        """A resume or a re-import replaying history is not a new paste."""
        service.session.active_request_id = None
        await seed_image(service, "sess-1")
        await finish_turns(service)
        assert events.names() == []

    async def test_a_subagent_prompt_is_not_a_user_message(self, mirroring, events):
        """It is nobody's chat message, so it belongs in no transcript here."""
        await mirroring.session_store.append(
            {
                "project_key": mirroring._session_project_key(),
                "session_id": "sess-1",
                "subpath": "subagents/agent-1",
            },
            [
                {
                    "type": "user",
                    "uuid": "sub-img",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png"},
                            }
                        ],
                    },
                }
            ],
        )
        await finish_turns(mirroring)
        assert "userMessageImages" not in events.names()

    async def test_an_image_inside_a_tool_result_is_not_a_paste(
        self, mirroring, events
    ):
        """A tool result is a user entry too, and its screenshots are not the user's."""
        await mirroring.session_store.append(
            {"project_key": mirroring._session_project_key(), "session_id": "sess-1"},
            [
                {
                    "type": "user",
                    "uuid": "u-tool",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        )
        await finish_turns(mirroring)
        assert "userMessageImages" not in events.names()

    async def test_an_assistant_entry_is_never_a_source(self, mirroring, events):
        await mirroring.session_store.append(
            {"project_key": mirroring._session_project_key(), "session_id": "sess-1"},
            [
                {
                    "type": "assistant",
                    "uuid": "a-img",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png"},
                            }
                        ],
                    },
                }
            ],
        )
        await finish_turns(mirroring)
        assert "userMessageImages" not in events.names()

    async def test_a_retried_mirror_batch_announces_once(self, mirroring, events):
        """Post-dedup entries, so the browser is not handed the same tile twice."""
        await seed_image(mirroring, "sess-1")
        await seed_image(mirroring, "sess-1")
        await finish_turns(mirroring)
        assert events.names().count("userMessageImages") == 1

    async def test_an_entry_with_no_uuid_is_not_pointed_at(self, mirroring, events):
        """A pointer that cannot resolve is worse than a missing tile."""
        await mirroring.session_store.append(
            {"project_key": mirroring._session_project_key(), "session_id": "sess-1"},
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png"},
                            }
                        ],
                    },
                }
            ],
        )
        await finish_turns(mirroring)
        assert "userMessageImages" not in events.names()

    async def test_a_broken_event_callback_does_not_fail_the_mirror(
        self, mirroring, events
    ):
        """The mirror is the storage; a dropped broadcast costs a thumbnail."""
        events.error = RuntimeError("socket closed")
        await seed_image(mirroring, "sess-1")
        await finish_turns(mirroring)
        assert await mirroring.session_store.load(
            {"project_key": mirroring._session_project_key(), "session_id": "sess-1"}
        )


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

    async def test_background_work_ending_runs_the_housekeeping_again(
        self, service, events
    ):
        """The first run happens when `run_turn` returns, which is the turn's
        first result — but a background subagent outlives that, and the drain
        follows it. Without a second run the Context tab and the file tree
        described the session as it was before the background work until the
        next turn moved them on."""
        service.session.turn_events = [
            Event("streamComplete", {"response": "spawned"}),
            # The drain's own continuations, as `_drain_background` emits
            # them: one per result, only the last ending the run.
            Event(
                "streamComplete",
                {"response": "still going", "continuation": True,
                 "background_finished": False},
            ),
            Event(
                "streamComplete",
                {"response": "all done", "continuation": True,
                 "background_finished": True},
            ),
        ]
        await send(service)
        # Twice: once for the turn, once for the run.
        assert events.names().count("postResponseComplete") == 2

    async def test_a_continuation_that_is_not_the_last_does_not_repeat_it(
        self, service, events
    ):
        """A turn that fans out produces a continuation per result, and
        refetching the context on each would cost a round trip apiece for a
        run that is not over."""
        service.session.turn_events = [
            Event("streamComplete", {"response": "spawned"}),
            Event(
                "streamComplete",
                {"response": "one down", "continuation": True,
                 "background_finished": False},
            ),
        ]
        await send(service)
        assert events.names().count("postResponseComplete") == 1

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

    async def test_stop_denies_the_dialog_the_turn_was_waiting_on(self, service):
        """Stop is the way out of a dialog nobody wants to answer.

        The request has no deadline of its own while a localhost client is
        connected, so if Stop did not resolve it the turn would stay open
        indefinitely behind a dialog the user had already dismissed.
        """
        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool(
                "Bash", {"command": "rm -rf build"}, FakePermissionContext()
            )
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await service.cancel_streaming(REQUEST_ID)

        result = await asyncio.wait_for(gate, timeout=1)
        assert type(result).__name__ == "PermissionResultDeny"
        assert "stopped this turn" in result.message
        assert service.permissions.pending() == []

    async def test_the_deny_reaches_the_cli_before_the_interrupt(self, service):
        """Order is the point, not a detail.

        The CLI is blocked awaiting the permission control response, so the
        interrupt is only actionable once that response has gone out. Doing
        it the other way round is what let ``_watch_drain`` expire and take
        the session down over an unanswered dialog.
        """
        resolved_when_interrupted: list[bool] = []
        real_interrupt = service.session.interrupt

        async def recording_interrupt(request_id=None):
            resolved_when_interrupted.append(not service.permissions.pending())
            return await real_interrupt(request_id)

        service.session.interrupt = recording_interrupt

        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool(
                "Bash", {"command": "ls"}, FakePermissionContext()
            )
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await service.cancel_streaming(REQUEST_ID)
        await asyncio.wait_for(gate, timeout=1)

        assert resolved_when_interrupted == [True]

    async def test_a_turn_that_ends_sweeps_a_dialog_left_open(self, service):
        """The backstop, for the ways a turn ends that are not Stop.

        A lost session, an engine crash, or a drain that timed out all end
        the turn without going through ``cancel_streaming``. Without the
        sweep the dialog stayed on screen for a turn that was over.
        """
        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool(
                "Bash", {"command": "ls"}, FakePermissionContext()
            )
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await send(service)
        await finish_turns(service)

        result = await asyncio.wait_for(gate, timeout=1)
        assert type(result).__name__ == "PermissionResultDeny"
        assert "The turn ended" in result.message
        assert service.permissions.pending() == []

    async def test_a_background_subagents_dialog_survives_the_turn(self, service):
        """The turn-end sweep must not strand a subagent that is still working.

        Main can finish while a background subagent is mid-task — the SDK
        keeps stdin open and says so ("Result received with 1 task(s) in
        flight"). Sweeping the subagent's dialog then denied a call it was
        still blocked on, so its feed froze on a padlocked card and its tab's
        LED stuck at "status unknown at turn end".
        """
        context = FakePermissionContext()
        context.agent_id = "agent-7"
        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool("Bash", {"command": "ls"}, context)
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await send(service)
        await finish_turns(service)

        assert not gate.done()
        assert len(service.permissions.pending()) == 1

        gate.cancel()
        await asyncio.gather(gate, return_exceptions=True)

    async def test_a_terminal_subagent_event_closes_its_dialog(self, service):
        """What closes a spared dialog once the subagent stops working.

        ``stop_task()`` reports ``status="killed"``, which reaches the service
        as a terminal ``subagentEvent`` — the only signal that a subagent
        blocked on a permission is not coming back.
        """
        context = FakePermissionContext()
        context.agent_id = "agent-7"
        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool("Bash", {"command": "ls"}, context)
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await service._dispatch(
            Event("subagentEvent", {"agent_id": "agent-7", "terminal": True}),
            REQUEST_ID,
        )

        result = await asyncio.wait_for(gate, timeout=1)
        assert type(result).__name__ == "PermissionResultDeny"
        assert "subagent that made this call ended" in result.message
        assert service.permissions.pending() == []

    async def test_a_live_subagent_event_leaves_the_dialog_alone(self, service):
        """Only a terminal status sweeps. Progress is not an ending."""
        context = FakePermissionContext()
        context.agent_id = "agent-7"
        service.session.active_request_id = REQUEST_ID
        gate = asyncio.create_task(
            service.permissions.can_use_tool("Bash", {"command": "ls"}, context)
        )
        for _ in range(500):
            if service.permissions.pending():
                break
            await asyncio.sleep(0.002)

        await service._dispatch(
            Event("subagentEvent", {"agent_id": "agent-7", "terminal": False}),
            REQUEST_ID,
        )

        assert not gate.done()
        assert len(service.permissions.pending()) == 1

        gate.cancel()
        await asyncio.gather(gate, return_exceptions=True)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestState:
    async def test_current_state_has_every_key_the_frontend_reads(self, service):
        state = await service.get_current_state()
        assert set(state) == {
            "messages",
            "denied_read_files",
            "session_id",
            "repo_name",
            "repo_root",
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
            "disk_warning",
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

    async def test_the_snapshot_carries_the_absolute_root(self, tmp_path, events):
        """The browser cannot relativise a tool's path without it."""
        root = tmp_path / "my-project"
        root.mkdir()
        svc = ClaudeCodeService(FakeConfig(root), event_callback=events)
        state = await svc.get_current_state()
        assert state["repo_root"] == str(root)
        assert Path(state["repo_root"]).is_absolute()

    async def test_the_root_falls_back_to_cwd_with_the_name(self, events):
        svc = ClaudeCodeService(FakeConfig(None), event_callback=events)
        state = await svc.get_current_state()
        assert state["repo_root"] == str(Path.cwd())


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
        service.session.file_checkpointing = True
        answer = await service.rewind_files("msg-uuid-1")
        assert answer == {"restored": [], "user_message_id": "msg-uuid-1"}
        assert ("rewind_files", ("msg-uuid-1",)) in service.session.control_calls

    async def test_a_rewind_failure_is_returned(self, service):
        service.session.file_checkpointing = True
        service.session.control_error = RuntimeError("no checkpoint")
        assert "no checkpoint" in (await service.rewind_files("m"))["error"]

    async def test_a_mirrored_session_refuses_to_rewind(self, service):
        """No checkpoints exist to rewind to: the engine will not keep them
        alongside a session store, and the mirror is what history is built
        on. Says so, and names what to use instead."""
        answer = await service.rewind_files("msg-uuid-1")
        assert "mirrored" in answer["error"]
        assert "git" in answer["error"]
        assert service.session.control_calls == []

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

    async def test_a_memory_file_inside_the_repo_gets_a_relative_path(self, service):
        """The Context tab can only open what the repo layer will read.

        Every repo read takes a path relative to the root and rejects
        absolute ones, so a clickable absolute path would be a row that
        does nothing.
        """
        root = service._repo_root
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "CLAUDE.md").write_text("hi", encoding="utf-8")
        service.session.context_usage = {
            "memoryFiles": [
                {"path": str(root / ".claude" / "CLAUDE.md"), "tokens": 27},
            ],
        }
        answer = await service.get_context_usage()
        assert answer["usage"]["memoryFiles"] == [
            {
                "path": str(root / ".claude" / "CLAUDE.md"),
                "tokens": 27,
                "relPath": ".claude/CLAUDE.md",
            },
        ]

    async def test_a_memory_file_outside_the_repo_is_left_unmarked(self, service):
        """A user-level CLAUDE.md is genuinely unopenable from here."""
        service.session.context_usage = {
            "memoryFiles": [{"path": "/home/someone/.claude/CLAUDE.md", "tokens": 3}],
        }
        answer = await service.get_context_usage()
        assert answer["usage"]["memoryFiles"] == [
            {"path": "/home/someone/.claude/CLAUDE.md", "tokens": 3},
        ]

    async def test_memory_file_marking_survives_junk(self, service):
        service.session.context_usage = {
            "memoryFiles": [{"tokens": 1}, "nonsense", {"path": ""}, {"path": 7}],
        }
        answer = await service.get_context_usage()
        assert answer["usage"]["memoryFiles"] == [
            {"tokens": 1},
            "nonsense",
            {"path": ""},
            {"path": 7},
        ]

    async def test_a_payload_without_memory_files_passes_through(self, service):
        service.session.context_usage = {"totalTokens": 5, "memoryFiles": None}
        answer = await service.get_context_usage()
        assert answer["usage"] == {"totalTokens": 5, "memoryFiles": None}

    async def test_mcp_status_passes_through(self, service):
        assert await service.get_mcp_status() == {"servers": []}

    async def test_mcp_controls_report_what_they_did(self, service):
        assert await service.reconnect_mcp_server("aic-dc") == {
            "status": "reconnecting",
            "name": "aic-dc",
        }
        assert await service.toggle_mcp_server("aic-dc", False) == {
            "status": "ok",
            "name": "aic-dc",
            "enabled": False,
        }

    async def test_server_info_passes_through(self, service):
        assert await service.get_server_info() == {"commands": ["review"]}

    async def test_absent_server_info_is_an_empty_dict(self, service):
        service.session.get_server_info = lambda: _none()
        assert await service.get_server_info() == {}

    async def test_sdk_surface_reports_static_and_live_halves(self, service):
        report = await service.get_sdk_surface()
        assert report["sdk_available"] is True
        assert report["sections"]["options"]["entries"]
        # The live half arrives from the same payload get_server_info returns.
        assert report["cli"]["available"] is True
        assert report["cli"]["commands"] == ["review"]

    async def test_sdk_surface_survives_a_dead_engine(self, service):
        """The static half is the half somebody reads when things are broken.

        Refusing the whole report because the CLI is not answering would
        withhold reflection over the installed wheel, which does not depend
        on the CLI at all — see ``get_sdk_surface``'s docstring.
        """

        async def _boom():
            raise EngineNotReadyError("no engine")

        service.session.get_server_info = _boom
        report = await service.get_sdk_surface()
        assert "error" not in report
        assert report["sections"]["options"]["entries"]
        assert report["cli"]["available"] is False

    async def test_sdk_surface_survives_an_unexpected_failure(self, service):
        """An unexpected error degrades the same way, loudly in the log."""

        async def _boom():
            raise RuntimeError("transport exploded")

        service.session.get_server_info = _boom
        report = await service.get_sdk_surface()
        assert "error" not in report
        assert report["cli"]["available"] is False


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
    "chat_streaming": (REQUEST_ID, "hello"),
    "cancel_streaming": (REQUEST_ID,),
    "resolve_permission": ("perm-1", {"action": "allow"}),
    "set_denied_read_files": ([".env"],),
    "set_permission_mode": ("bypassPermissions",),
    "set_model": ("claude-opus-5",),
    "rewind_files": ("msg-uuid-1",),
    "stop_task": ("task-1",),
    "reconnect_mcp_server": ("aic-dc",),
    "toggle_mcp_server": ("aic-dc", True),
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
    # Destroys history every client can see — including the record of a turn
    # a participant might be there to review.
    "history_delete": ("sess-1",),
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
    "get_denied_read_files": (),
    "get_context_usage": (),
    "get_mcp_status": (),
    "get_server_info": (),
    # What the `/` palette lists. Reads the handshake payload and nothing
    # else; a participant who could not see the commands would be typing
    # into a composer whose autocomplete never opened.
    "list_commands": (),
    # Reflection over the installed wheel and this package's own source.
    # It reads no session state, changes nothing, and reveals nothing about
    # the repo — a participant asking which SDK features this build wired
    # up is asking about the software, not about the host's work.
    "get_sdk_surface": (),
    # Reading the review, the graph and a diff is the reviewing part of
    # collaboration; withholding it would leave a participant unable to see
    # what they were invited to look at.
    "check_review_ready": (),
    "get_commit_graph": (),
    "get_review_state": (),
    "get_review_file_diff": ("a.py",),
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
    "history_image": ("sess-1", "sess-1-u1", 0),
    "history_search": ("parser",),
    "list_subagent_transcripts": (),
    "get_subagent_transcript": ("a1",),
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
        """`mcp__aic-dc__symbol_map` is the name the CLI and the permission
        classifier both spell out, so the key is interface."""
        from aic_dc.claude_code.mcp_server import SERVER_NAME

        assert list(wired.session._mcp_servers) == [SERVER_NAME]
        assert SERVER_NAME == "aic-dc"

    def test_the_two_observational_hooks_are_the_whole_subscription(self, wired):
        """Nothing that could decide anything — see hooks.py's invariant."""
        assert sorted(wired.session._hooks) == ["PostToolUse", "PreCompact"]

    def test_the_bridge_and_the_reindex_share_one_flush(self, wired):
        """A tool that flushed a different queue than the hook fills would
        answer from the pre-write index while reporting itself fresh."""
        assert wired.mcp_bridge._flush == wired.reindexer.flush

    def test_a_bridge_that_will_not_build_still_leaves_a_session(
        self, tmp_path, events, monkeypatch, caplog
    ):
        """Without the bridge the agent loses two tools and keeps every
        built-in; refusing to construct would trade that for a dead editor."""
        from aic_dc.claude_code import mcp_server as mcp_module

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
        assert sorted(svc.session._hooks) == ["PostToolUse", "PreCompact"]
        assert "fall back to Glob/Grep/Read" in caplog.text
        # And the browser is told, not just the log. `mcp-bridge.md`
        # § Availability and Degradation: "the session continues without it
        # and a banner reports the loss — otherwise the agent simply appears
        # inexplicably worse at repo-wide questions."
        assert svc.session.health.degradations == [
            "The aic-dc repo tools did not start, so the agent has no symbol "
            "map, no document outlines and no reference graph — it will fall "
            "back to Glob, Grep and Read, which answer repo-wide questions "
            "less well."
        ]
        assert svc.get_engine_health()["degradations"] == (
            svc.session.health.degradations
        )

    def test_a_hook_that_will_not_build_still_leaves_a_session(
        self, tmp_path, events, monkeypatch, caplog
    ):
        from aic_dc.claude_code import service as service_module

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
        assert svc.session.health.degradations == [
            "The post-write re-index hook did not start, so the file tree and "
            "the symbol map will not follow the agent's writes — refresh them "
            "by hand after it edits files."
        ]

    def test_both_halves_failing_reports_both_losses(
        self, tmp_path, events, monkeypatch, caplog
    ):
        """One sentence per capability, not one per session: the two
        failures have different remedies, and a reader told only the first
        would go looking for a symbol map that is also gone."""
        from aic_dc.claude_code import mcp_server as mcp_module
        from aic_dc.claude_code import service as service_module

        def boom_hook(reindexer, broadcast=None):
            raise RuntimeError("no sdk")

        def boom_server(self):
            raise RuntimeError("no sdk")

        monkeypatch.setattr(service_module, "build_hook_matchers", boom_hook)
        monkeypatch.setattr(mcp_module.McpBridge, "build_server", boom_server)
        with caplog.at_level(logging.WARNING):
            svc = ClaudeCodeService(
                FakeConfig(tmp_path),
                event_callback=events,
                engine_config=EngineConfig(),
            )
        assert len(svc.session.health.degradations) == 2
        assert "re-index hook" in svc.session.health.degradations[0]
        assert "repo tools" in svc.session.health.degradations[1]

    def test_a_session_that_started_whole_reports_no_loss(self, wired):
        """The banner is silent otherwise, so an empty list is the normal
        state and not a missing report."""
        assert wired.session.health.degradations == []
        assert wired.get_engine_health()["degradations"] == []


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
        """`.aic-dc/sessions/` is the whole point: the CLI already has a copy
        under ~/.claude/projects/, and that is the one that expires."""
        assert wired.session_store.root == tmp_path / ".aic-dc" / "sessions"

    def test_building_the_service_writes_nothing(self, wired, tmp_path):
        """A directory that exists is not the same signal as a session that
        was mirrored, so the store makes its own on first append."""
        assert not (tmp_path / ".aic-dc" / "sessions").exists()

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
                SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
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


class TestTheDiskWarning:
    """One sentence, once, about the one thing under `.aic-dc/` that does
    not rebuild.

    Transcripts hold pasted images verbatim as base64, so an image-heavy
    week is measured in gigabytes. Nothing is deleted and nothing is
    refused — the user decides — which is exactly why repeating the warning
    every turn would be worse than saying nothing
    (``specs5/3-engine/history.md`` § Subagent Transcripts;
    ``specs-reference/3-engine/history.md`` § Numeric constants).
    """

    @pytest.fixture
    def over_threshold(self, service, monkeypatch):
        from aic_dc.claude_code import service as service_mod

        monkeypatch.setattr(
            service.session_store,
            "total_bytes",
            lambda: service_mod.DISK_WARNING_BYTES + 1,
        )
        return service

    async def test_a_small_directory_says_nothing(self, service):
        assert await service._disk_warning() is None

    async def test_a_directory_over_the_threshold_says_what_and_where(
        self, over_threshold
    ):
        warning = await over_threshold._disk_warning()
        assert "1.0 GiB" in warning
        assert ".aic-dc/sessions/" in warning
        assert "history browser" in warning

    async def test_it_fires_at_most_once_per_server_lifetime(self, over_threshold):
        assert await over_threshold._disk_warning() is not None
        assert await over_threshold._disk_warning() is None
        assert await over_threshold._disk_warning() is None

    async def test_the_turn_footer_and_the_first_paint_share_the_one_shot(
        self, over_threshold, events
    ):
        """Whichever notices first is the one that says it, and the other
        stays quiet — two channels, not two warnings."""
        state = await over_threshold.get_current_state()
        assert state["disk_warning"] is not None
        await send(over_threshold)
        assert events.payload_of("postResponseComplete")["disk_warning"] is None

    async def test_a_turn_can_be_the_one_that_notices(self, over_threshold, events):
        await send(over_threshold)
        assert events.payload_of("postResponseComplete")["disk_warning"] is not None
        assert (await over_threshold.get_current_state())["disk_warning"] is None

    async def test_a_size_that_cannot_be_read_is_not_a_failed_turn(
        self, service, monkeypatch
    ):
        def boom():
            raise OSError("the filesystem went away")

        monkeypatch.setattr(service.session_store, "total_bytes", boom)
        assert await service._disk_warning() is None
        assert service._disk_warned is False

    async def test_without_a_store_there_is_nothing_to_measure(self, tmp_path, events):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert await svc._disk_warning() is None

    async def test_the_threshold_comes_from_app_json(self, service, monkeypatch):
        """A gigabyte is the default, not the rule
        (``specs5/1-foundation/configuration.md`` § App Config)."""
        monkeypatch.setattr(service.session_store, "total_bytes", lambda: 5_000)
        service._config.history_config = {"session_dir_warning_bytes": 4_000}
        assert await service._disk_warning() is not None

    async def test_a_raised_threshold_keeps_a_big_directory_quiet(
        self, over_threshold
    ):
        """The knob works in the direction that matters: a user who has
        decided ten gigabytes is fine can say so."""
        over_threshold._config.history_config = {
            "session_dir_warning_bytes": 10 * 1024 * 1024 * 1024
        }
        assert await over_threshold._disk_warning() is None

    async def test_a_config_without_the_section_falls_back(self, over_threshold):
        """Every stub config in these tests is such a config, which is why
        the module constant stays."""
        assert not hasattr(over_threshold._config, "history_config")
        assert await over_threshold._disk_warning() is not None

    async def test_the_reload_takes_without_a_restart(self, service, monkeypatch):
        monkeypatch.setattr(service.session_store, "total_bytes", lambda: 5_000)
        service._config.history_config = {"session_dir_warning_bytes": 6_000}
        assert await service._disk_warning() is None
        service._config.history_config = {"session_dir_warning_bytes": 4_000}
        assert await service._disk_warning() is not None


class TestTheMirrorGapTolerance:
    """The service owns `app.json`; the session owns `engine.json`. The
    threshold crosses that line as a callable so an edited file takes on the
    next broadcast rather than at the next restart.

    Built on the real session rather than the shared fixture's fake, because
    the wiring under test is the one line that hands the real one its
    threshold.
    """

    @pytest.fixture
    def wired(self, tmp_path, events):
        return ClaudeCodeService(
            FakeConfig(tmp_path),
            event_callback=events,
            engine_config=EngineConfig(),
        )

    def test_the_session_health_is_handed_the_configured_number(self, wired):
        wired._config.history_config = {"mirror_gap_tolerance": 7}
        assert wired.session.health.mirror_gap_tolerance() == 7

    def test_a_config_without_the_section_uses_the_default(self, wired):
        from aic_dc.claude_code.health import DEFAULT_MIRROR_GAP_TOLERANCE

        assert not hasattr(wired._config, "history_config")
        assert (
            wired.session.health.mirror_gap_tolerance() == DEFAULT_MIRROR_GAP_TOLERANCE
        )

    def test_it_reaches_the_health_rpc(self, wired):
        wired._config.history_config = {"mirror_gap_tolerance": 0}
        wired.session.health.note_mirror_gap()
        assert wired.get_engine_health()["mirror_gaps_escalated"] is True


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
        assert messages[1]["turn"]["turn_model_usage"] == {
            "claude-opus-5": {"input_tokens": 40, "output_tokens": 9}
        }
        # Prompt at :00, reply at :02 — the wait the user actually had.
        assert messages[1]["turn"]["duration_ms"] == 2000

    async def test_this_session_events_are_interleaved(self, wired, session_id):
        """The half of a browsed transcript the engine never wrote."""
        from aic_dc.claude_code.events_log import commit_content

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
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
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
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
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
        assert wired.events_log.path == tmp_path / "repo" / ".aic-dc" / "events.jsonl"


class TestTheModeSwitchRecord:
    """Who moved the permission posture, and from where.

    The posture governs every later tool call, so a browsed session that
    shows the agent editing files without asking has to say where the
    permission came from. ``source`` distinguishes the two producers: the
    selector, and "accept edits from now on" checked in a permission
    dialog — which is the one a reader would otherwise have no record of.
    """

    @pytest.fixture
    def service(self, service):
        service.session.session_id = "33333333-3333-4333-8333-333333333333"
        return service

    async def records(self, service) -> list[dict]:
        loaded = await service.events_log.load(service.session.session_id)
        return [r for r in loaded if r["event"] == "permission_mode"]

    async def test_the_users_own_switch_records_both_ends(self, service):
        await service.set_permission_mode("plan")
        (record,) = await self.records(service)
        assert record["payload"] == {
            "from": "default",
            "to": "plan",
            "source": "user",
        }
        assert record["content"] == "Permission mode set to **plan**."

    async def test_a_refused_switch_records_nothing(self, service):
        service._collab = FakeCollab(is_localhost=False)
        await service.set_permission_mode("bypassPermissions")
        assert await self.records(service) == []

    async def test_a_failed_switch_records_nothing(self, service):
        service.session.control_error = RuntimeError("no response")
        await service.set_permission_mode("plan")
        assert await self.records(service) == []

    async def test_a_mode_the_dialog_moved_is_attributed_to_the_engine(
        self, service
    ):
        await service._note_permission_mode("acceptEdits")
        (record,) = await self.records(service)
        assert record["payload"] == {
            "from": "default",
            "to": "acceptEdits",
            "source": "engine",
        }

    async def test_the_previous_mode_is_read_before_the_switch(self, service):
        """Both producers apply the mode to the session, so a record built
        afterwards would report ``from`` and ``to`` as the same mode."""
        await service.set_permission_mode("plan")
        await service._note_permission_mode("acceptEdits")
        moves = [(r["payload"]["from"], r["payload"]["to"]) for r in await self.records(service)]
        assert moves == [("default", "plan"), ("plan", "acceptEdits")]


class TestSearch:
    """The same rows whether the index is warm, cold or corrupt."""

    @pytest.fixture
    def old(self):
        return "55555555-5555-4555-8555-555555555555"

    @pytest.fixture
    def newer(self):
        return "66666666-6666-4666-8666-666666666666"

    @pytest.fixture
    async def wired(self, tmp_path, events, old, newer):
        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        await seed_transcript(
            svc, old, prompt="the parser is broken", reply="fixed the parser"
        )
        await seed_transcript(
            svc, newer, prompt="rename the widget", reply="renamed it"
        )
        return svc

    async def test_a_hit_carries_where_it_was_and_why_it_matched(self, wired, old):
        rows = await wired.history_search("parser")
        assert {r["session_id"] for r in rows} == {old}
        assert {r["role"] for r in rows} == {"user", "assistant"}
        user = next(r for r in rows if r["role"] == "user")
        assert user["entry_uuid"] == f"{old}-u1"
        assert user["content_preview"] == "the parser is broken"
        assert user["timestamp"] == "2026-08-16T00:00:00.000Z"

    async def test_the_newest_session_comes_first(self, wired, old, newer):
        rows = await wired.history_search("the")
        assert [r["session_id"] for r in rows][0] == newer
        assert old in {r["session_id"] for r in rows}

    async def test_a_cold_index_answers_the_same_as_a_warm_one(self, wired):
        """The invariant the whole arrangement exists for: an index is a
        speed-up, so deleting it must not change an answer."""
        warm = await wired.history_search("parser")
        assert wired.history_index.path.exists()
        wired.history_index.path.unlink()
        wired.history_index._loaded = False
        wired.history_index._postings = {}
        wired.history_index._sessions = {}
        assert await wired.history_search("parser") == warm

    async def test_no_index_at_all_answers_the_same(self, wired):
        warm = await wired.history_search("parser")
        wired.history_index = None
        assert await wired.history_search("parser") == warm

    async def test_a_corrupt_index_is_discarded_not_trusted(self, wired):
        expected = await wired.history_search("parser")
        wired.history_index.path.write_text("{not json", encoding="utf-8")
        wired.history_index._loaded = False
        assert await wired.history_search("parser") == expected

    async def test_a_word_internal_match_is_found_either_way(self, wired):
        """A term index that only matched whole words would answer
        differently from the fallback scan."""
        with_index = await wired.history_search("arser")
        wired.history_index = None
        assert with_index == await wired.history_search("arser")
        assert with_index != []

    async def test_a_query_of_punctuation_falls_back_to_the_scan(self, wired):
        """Nothing tokenises, so there is nothing to look up — and the
        answer still has to be right."""
        rows = await wired.history_search("!!")
        assert rows == []

    async def test_the_role_filter_narrows_to_one_side(self, wired):
        rows = await wired.history_search("parser", role="assistant")
        assert [r["role"] for r in rows] == ["assistant"]
        assert rows[0]["content_preview"] == "fixed the parser"

    async def test_an_unknown_role_is_refused_rather_than_silently_empty(self, wired):
        answer = await wired.history_search("parser", role="robot")
        assert "error" in answer

    async def test_a_tool_call_is_searchable_by_its_input(self, wired, newer):
        """What these searches are usually for: a path, a command, a pattern
        the agent used. All of it is in the input."""
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": newer,
            },
            [
                {
                    "type": "assistant",
                    "uuid": f"{newer}-a2",
                    "parentUuid": f"{newer}-a1",
                    "timestamp": "2026-08-16T00:00:05.000Z",
                    "message": {
                        "id": f"msg_{newer}_2",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "ruff check src/aic_dc"},
                            }
                        ],
                    },
                }
            ],
        )
        rows = await wired.history_search("ruff check")
        assert [r["role"] for r in rows] == ["tool"]
        assert "ruff check src/aic_dc" in rows[0]["content_preview"]

    async def test_a_tool_result_is_never_searched(self, wired, newer):
        """The transcript holds results verbatim because it must; searching
        them would return file contents, which is what ``Grep`` is for."""
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": newer,
            },
            [
                {
                    "type": "user",
                    "uuid": f"{newer}-r1",
                    "parentUuid": f"{newer}-a1",
                    "timestamp": "2026-08-16T00:00:06.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_1",
                                "content": "a distinctive haystack string",
                            }
                        ],
                    },
                }
            ],
        )
        assert await wired.history_search("distinctive haystack") == []

    async def test_the_framing_is_not_searchable(self, wired, newer):
        """The context block is ours, not the user's, and a search for a
        file we injected would hit every prompt."""
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": newer,
            },
            [
                {
                    "type": "user",
                    "uuid": f"{newer}-u2",
                    "parentUuid": f"{newer}-a1",
                    "timestamp": "2026-08-16T00:00:07.000Z",
                    "message": {
                        "role": "user",
                        "content": (
                            "<aic-dc-ui-context>selected: zzzunique.py"
                            "</aic-dc-ui-context>\nwhat does it do?"
                        ),
                    },
                }
            ],
        )
        assert await wired.history_search("zzzunique") == []
        assert len(await wired.history_search("what does it do")) == 1

    async def test_the_limit_caps_the_rows(self, wired):
        assert len(await wired.history_search("the", limit=1)) == 1

    async def test_an_empty_query_is_not_an_error(self, wired):
        assert await wired.history_search("") == []

    async def test_without_a_store_there_is_nothing_to_search(self, tmp_path, events):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert svc.history_index is None
        assert await svc.history_search("anything") == []

    async def test_a_search_failure_is_reported_not_swallowed(self, wired):
        async def boom(*args, **kwargs):
            raise OSError("disk is gone")

        wired.session_store.list_sessions = boom
        assert "error" in await wired.history_search("parser")


class TestTheDerivedIndex:
    """It caches and it narrows; it never becomes the answer."""

    @pytest.fixture
    def session_id(self):
        return "77777777-7777-4777-8777-777777777777"

    @pytest.fixture
    async def wired(self, tmp_path, events, session_id):
        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        await seed_transcript(svc, session_id, prompt="index me")
        return svc

    def test_it_lands_under_the_project_key(self, wired, tmp_path):
        assert wired.history_index.path.parent == (
            tmp_path / "repo" / ".aic-dc" / "index"
        )
        assert wired.history_index.path.name.endswith(".json")

    async def test_the_session_list_is_cached_by_mtime(self, wired, session_id):
        """A listing that reparsed every session would grow with history."""
        first = await wired.history_list()
        parses = []
        import claude_agent_sdk

        original = claude_agent_sdk.get_session_messages_from_store

        async def counting(*args, **kwargs):
            parses.append(args[1])
            return await original(*args, **kwargs)

        claude_agent_sdk.get_session_messages_from_store = counting
        try:
            assert await wired.history_list() == first
        finally:
            claude_agent_sdk.get_session_messages_from_store = original
        assert parses == []

    async def test_a_changed_session_is_reparsed(self, wired, session_id):
        before = await wired.history_list()
        assert before[0]["message_count"] == 2
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": session_id,
            },
            [
                {
                    "type": "user",
                    "uuid": f"{session_id}-u2",
                    "parentUuid": f"{session_id}-a1",
                    "timestamp": "2026-08-16T00:00:09.000Z",
                    "message": {"role": "user", "content": "and again"},
                }
            ],
        )
        after = await wired.history_list()
        assert after[0]["message_count"] == 3

    async def test_a_grown_session_is_indexed_from_where_it_stopped(
        self, wired, session_id
    ):
        """Transcripts are append-only, so a turn costs indexing one turn."""
        await wired.history_search("index")
        assert wired.history_index._sessions[session_id]["entries"] == 2
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": session_id,
            },
            [
                {
                    "type": "user",
                    "uuid": f"{session_id}-u2",
                    "parentUuid": f"{session_id}-a1",
                    "timestamp": "2026-08-16T00:00:09.000Z",
                    "message": {"role": "user", "content": "a second question"},
                }
            ],
        )
        rows = await wired.history_search("second question")
        assert [r["entry_uuid"] for r in rows] == [f"{session_id}-u2"]
        assert wired.history_index._sessions[session_id]["entries"] == 3

    async def test_an_oversized_text_makes_its_session_always_scanned(
        self, wired, session_id
    ):
        """A truncated term list would make a real hit findable only after
        someone deleted the index."""
        from aic_dc.claude_code.history_index import _TEXT_CAP

        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": session_id,
            },
            [
                {
                    "type": "user",
                    "uuid": f"{session_id}-big",
                    "parentUuid": f"{session_id}-a1",
                    "timestamp": "2026-08-16T00:00:10.000Z",
                    "message": {
                        "role": "user",
                        "content": "x" * (_TEXT_CAP + 1) + " needle",
                    },
                }
            ],
        )
        rows = await wired.history_search("needle")
        assert [r["entry_uuid"] for r in rows] == [f"{session_id}-big"]
        assert wired.history_index._sessions[session_id]["scan"] is True

    async def test_a_deleted_session_leaves_no_postings(self, wired, session_id):
        await wired.history_search("index")
        assert wired.history_index._postings
        await wired.session_store.delete(
            {
                "project_key": wired._session_project_key(),
                "session_id": session_id,
            }
        )
        await wired.history_index.refresh()
        assert wired.history_index._postings == {}
        assert wired.history_index._sessions == {}

    async def test_forgetting_a_session_is_persisted(self, wired, session_id):
        await wired.history_search("index")
        await wired.history_index.forget(session_id)
        assert wired.history_index._sessions == {}
        import json

        saved = json.loads(wired.history_index.path.read_text())
        assert saved["sessions"] == {}
        assert saved["postings"] == {}

    async def test_forgetting_a_session_it_never_saw_writes_nothing(self, wired):
        await wired.history_index.forget("no-such-session")
        assert not wired.history_index.path.exists()

    async def test_an_index_from_another_version_is_discarded(self, wired):
        import json

        wired.history_index.path.parent.mkdir(parents=True, exist_ok=True)
        wired.history_index.path.write_text(
            json.dumps({"version": 99, "sessions": {"x": {}}, "postings": {"y": []}}),
            encoding="utf-8",
        )
        rows = await wired.history_search("index me")
        assert len(rows) == 1
        assert "x" not in wired.history_index._sessions

    async def test_a_session_it_could_not_read_is_scanned_not_skipped(
        self, wired, session_id
    ):
        original = wired.session_store.load
        calls = {"n": 0}

        async def flaky(key):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return await original(key)

        wired.session_store.load = flaky
        rows = await wired.history_search("index me")
        assert [r["session_id"] for r in rows] == [session_id]
        assert wired.history_index._sessions[session_id]["scan"] is True


class TestImagesAreFetchedOneAtATime:
    """``history_load`` renders pointers; ``history_image`` resolves one."""

    @pytest.fixture
    def session_id(self):
        return "33333333-3333-4333-8333-333333333333"

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

    async def test_a_rendered_pointer_resolves_to_the_bytes(self, wired, session_id):
        """The round trip: what the browser is handed is what it can fetch."""
        await seed_image(wired, session_id)
        messages = await wired.history_load(session_id)
        pointer = next(m for m in messages if m.get("image_refs"))["image_refs"][0]
        assert pointer["media_type"] == "image/png"
        answer = await wired.history_image(
            pointer["session_id"], pointer["entry_uuid"], pointer["block"]
        )
        assert answer == {"data_uri": "data:image/png;base64,aGk="}

    async def test_a_url_source_is_handed_back_as_the_url(self, wired, session_id):
        uuid = await seed_image(
            wired,
            session_id,
            source={"type": "url", "url": "https://example.test/a.png"},
        )
        answer = await wired.history_image(session_id, uuid, 1)
        assert answer == {"data_uri": "https://example.test/a.png"}

    async def test_an_image_in_a_subagents_prompt_is_found(self, wired, session_id):
        """A pointer carries no subpath, so the search cannot stop at the
        main transcript or subagent images would be unfetchable."""
        await wired.session_store.append(
            {
                "project_key": wired._session_project_key(),
                "session_id": session_id,
                "subpath": "subagents/agent-a1",
            },
            [
                {
                    "type": "user",
                    "uuid": "a1-u1",
                    "parentUuid": None,
                    "timestamp": "2026-08-16T00:01:00.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/webp",
                                    "data": "d2VicA==",
                                },
                            }
                        ],
                    },
                }
            ],
        )
        answer = await wired.history_image(session_id, "a1-u1", 0)
        assert answer == {"data_uri": "data:image/webp;base64,d2VicA=="}

    async def test_a_block_that_is_not_an_image_says_so(self, wired, session_id):
        uuid = await seed_image(wired, session_id)
        answer = await wired.history_image(session_id, uuid, 0)
        assert answer["error"] == "That block is not an image"

    async def test_a_block_past_the_end_is_an_error_not_a_crash(
        self, wired, session_id
    ):
        uuid = await seed_image(wired, session_id)
        assert "error" in await wired.history_image(session_id, uuid, 7)

    async def test_a_vanished_entry_is_reported(self, wired, session_id):
        answer = await wired.history_image(session_id, "no-such-uuid", 0)
        assert "no longer in the transcript" in answer["error"]

    async def test_an_image_with_no_data_is_an_error(self, wired, session_id):
        uuid = await seed_image(
            wired,
            session_id,
            source={"type": "base64", "media_type": "image/png", "data": ""},
        )
        assert "error" in await wired.history_image(session_id, uuid, 1)

    async def test_the_arguments_are_required(self, wired, session_id):
        assert "error" in await wired.history_image("", "u1", 0)
        assert "error" in await wired.history_image(session_id, "", 0)

    async def test_without_a_repo_it_says_why(self, tmp_path, events):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert "error" in await svc.history_image("s1", "u1", 0)


class TestSubagentTranscripts:
    """The transcripts a session's delegated work wrote, read back."""

    @pytest.fixture
    def session_id(self):
        return "44444444-4444-4444-8444-444444444444"

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

    async def test_a_subagent_is_listed_with_what_it_was_asked(
        self, wired, session_id
    ):
        await seed_subagent(wired, session_id, "a1", prompt="check the tests")
        listed = await wired.list_subagent_transcripts(session_id)
        assert listed == [
            {
                "agent_id": "a1",
                "subpath": "subagents/agent-a1",
                "message_count": 2,
                "preview": "check the tests",
            }
        ]

    async def test_the_mirrored_sidecar_supplies_the_description(
        self, wired, session_id
    ):
        """The CLI sends ``.meta.json`` to a live mirror as an
        ``agent_metadata`` entry, so a session we mirrored has the fields the
        reference shape lists as optional."""
        await seed_subagent(
            wired,
            session_id,
            "a1",
            metadata={
                "agentType": "general-purpose",
                "description": "Check latest test run",
                "toolUseId": "toolu_123",
                "spawnDepth": 1,
            },
        )
        row = (await wired.list_subagent_transcripts(session_id))[0]
        assert row["description"] == "Check latest test run"
        assert row["task_id"] == "toolu_123"
        assert row["agent_type"] == "general-purpose"
        # The metadata entry is not a message.
        assert row["message_count"] == 2

    async def test_a_session_without_the_sidecar_omits_those_fields(
        self, wired, session_id
    ):
        """An imported session has no ``agent_metadata`` entry, and a made-up
        description would read as the CLI's own."""
        await seed_subagent(wired, session_id, "a1")
        row = (await wired.list_subagent_transcripts(session_id))[0]
        assert "description" not in row
        assert "task_id" not in row

    async def test_a_workflow_subagent_is_listed_at_its_real_path(
        self, wired, session_id
    ):
        await seed_subagent(
            wired,
            session_id,
            "a2",
            subpath="subagents/workflows/wf_run1/agent-a2",
        )
        listed = await wired.list_subagent_transcripts(session_id)
        assert listed[0]["subpath"] == "subagents/workflows/wf_run1/agent-a2"
        assert listed[0]["agent_id"] == "a2"

    async def test_a_workflow_subagent_loads_from_its_nested_path(
        self, wired, session_id
    ):
        """Guessing the flat path would read nothing for exactly these."""
        await seed_subagent(
            wired,
            session_id,
            "a2",
            subpath="subagents/workflows/wf_run1/agent-a2",
            reply="the nested one",
        )
        messages = await wired.get_subagent_transcript("a2", session_id)
        assert messages[1]["blocks"][0]["content"] == "the nested one"

    async def test_the_transcript_comes_back_rendered_not_raw(
        self, wired, session_id
    ):
        """Rendered messages rather than the reference shape's raw entries —
        the subagent tab draws through the same panel code."""
        await seed_subagent(wired, session_id, "a1")
        messages = await wired.get_subagent_transcript("a1", session_id)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "check the tests"
        assert messages[0]["timestamp"] == "2026-08-16T00:01:00.000Z"
        assert messages[1]["turn"]["duration_ms"] == 3000

    async def test_the_sessions_events_are_not_attributed_to_a_subagent(
        self, wired, session_id
    ):
        """``events.jsonl`` records belong to the session; interleaving them
        here would credit a commit to whichever subagent was running."""
        from aic_dc.claude_code.events_log import commit_content

        await wired._record_event("commit", commit_content("abc1234", "fix: it"))
        await seed_subagent(wired, session_id, "a1")
        messages = await wired.get_subagent_transcript("a1", session_id)
        assert [m for m in messages if m.get("event")] == []

    async def test_the_default_is_the_session_on_screen(self, wired, session_id):
        """The common question is "what did *this* conversation delegate?"."""
        await seed_subagent(wired, session_id, "a1")
        assert await wired.list_subagent_transcripts() == (
            await wired.list_subagent_transcripts(session_id)
        )
        assert await wired.get_subagent_transcript("a1") == (
            await wired.get_subagent_transcript("a1", session_id)
        )

    async def test_a_session_with_no_subagents_lists_nothing(self, wired):
        assert await wired.list_subagent_transcripts() == []

    async def test_an_unknown_agent_is_an_error_not_an_empty_conversation(
        self, wired, session_id
    ):
        answer = await wired.get_subagent_transcript("nope", session_id)
        assert "error" in answer

    async def test_an_agent_id_is_required(self, wired):
        assert "error" in await wired.get_subagent_transcript("")

    async def test_with_no_session_at_all_there_is_nothing_to_list(
        self, tmp_path, events
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        assert await svc.list_subagent_transcripts() == []
        assert "error" in await svc.get_subagent_transcript("a1")

    async def test_a_read_failure_is_reported_not_swallowed(self, wired, session_id):
        async def boom(*args, **kwargs):
            raise OSError("disk is gone")

        wired.session_store.list_subkeys = boom
        assert "error" in await wired.list_subagent_transcripts(session_id)


class TestDeletingASession:
    """One operation over three files, and the one session it will not touch."""

    @pytest.fixture
    def old(self):
        return "88888888-8888-4888-8888-888888888888"

    @pytest.fixture
    def live(self):
        return "99999999-9999-4999-8999-999999999999"

    @pytest.fixture
    async def wired(self, tmp_path, events, old, live):
        """A past session with everything hanging off it, plus a live one."""
        from aic_dc.claude_code.events_log import commit_content

        repo = tmp_path / "repo"
        repo.mkdir()
        svc = ClaudeCodeService(
            FakeConfig(repo), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        await seed_transcript(svc, old, prompt="the old conversation")
        await seed_subagent(svc, old, "a1")
        await svc._record_event(
            "commit", commit_content("abc1234", "fix: it"), session_id=old
        )
        await seed_transcript(svc, live, prompt="the live one")
        await svc._record_event(
            "commit", commit_content("def5678", "fix: more"), session_id=live
        )
        # The engine is attached elsewhere, because the session on screen is
        # the one delete refuses.
        svc.session.session_id = live
        return svc

    async def test_the_transcript_its_sidecar_and_its_subagents_all_go(
        self, wired, old
    ):
        assert await wired.history_delete(old) == {
            "session_id": old,
            "status": "deleted",
        }
        assert [s["session_id"] for s in await wired.history_list()] != [old]
        assert "error" in await wired.history_load(old)
        assert await wired.list_subagent_transcripts(old) == []

    async def test_the_sessions_events_go_with_it(self, wired, old, live):
        """An archived commit outliving its session would render as history
        for a session that no longer exists."""
        assert await wired.events_log.load(old)
        await wired.history_delete(old)
        assert await wired.events_log.load(old) == []
        assert len(await wired.events_log.load(live)) == 1

    async def test_the_index_forgets_it(self, wired, old):
        """A warm index that kept answering would name a session that
        resolves to nothing."""
        assert [r["session_id"] for r in await wired.history_search("old")] == [old]
        await wired.history_delete(old)
        assert old not in wired.history_index._sessions
        assert await wired.history_search("old") == []

    async def test_the_other_session_is_untouched(self, wired, old, live):
        await wired.history_delete(old)
        listed = await wired.history_list()
        assert [s["session_id"] for s in listed] == [live]
        messages = await wired.history_load(live)
        assert any(m.get("content") == "the live one" for m in messages)
        assert any(m.get("event") == "commit" for m in messages)

    async def test_the_current_conversation_is_refused(self, wired, live):
        """The mirror is live: the CLI would write the transcript straight
        back, and the next connect would resume an empty ID."""
        answer = await wired.history_delete(live)
        assert answer["reason"] == "session_live"
        assert await wired.history_load(live)

    async def test_the_session_the_next_connect_would_resume_is_refused(
        self, wired, old, live
    ):
        """Nothing is attached yet, so "on screen" is whatever auto-resume
        will pick up — deleting that is the same rug-pull one restart later."""
        wired.session.session_id = None
        answer = await wired.history_delete(live)
        assert answer["reason"] == "session_live"
        # And the older one is still deletable while that is true.
        assert (await wired.history_delete(old))["status"] == "deleted"

    async def test_every_client_is_told(self, wired, old):
        await wired.history_delete(old)
        deleted = [c for c in events_of(wired) if c[0] == "sessionDeleted"]
        assert deleted == [("sessionDeleted", {"session_id": old})]

    async def test_deleting_twice_is_not_an_error(self, wired, old):
        assert (await wired.history_delete(old))["status"] == "deleted"
        assert (await wired.history_delete(old))["status"] == "deleted"

    async def test_a_session_id_is_required(self, wired):
        assert "error" in await wired.history_delete("")

    async def test_without_a_repo_it_says_why(self, tmp_path, events):
        svc = ClaudeCodeService(
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
            event_callback=events,
            engine_config=EngineConfig(),
        )
        svc.session = FakeSession()
        assert "error" in await svc.history_delete("whatever")

    async def test_a_failed_delete_leaves_the_events_alone(self, wired, old):
        """The transcript goes first, so a delete that could not start has
        not already thrown the session's events away."""

        async def boom(*args, **kwargs):
            raise OSError("disk is read-only")

        wired.session_store.delete = boom
        assert "error" in await wired.history_delete(old)
        assert len(await wired.events_log.load(old)) == 1
        assert events_of(wired) == []


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
            SimpleNamespace(repo_root=tmp_path, config_dir=None, aic_dc_dir=None),
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

    def test_it_carries_the_three_facts_the_agent_cannot_read_itself(
        self, service
    ):
        snapshot = service._ui_state_snapshot()
        assert set(snapshot) == {
            "viewer",
            "review_state",
            "permission_mode",
        }

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
        assert service._viewer_state == {"path": "src/a.py"}

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
