"""Tests for ac_dc.claude_code.session — conversion phase 1.

Offline: ``ClaudeSDKClient`` is replaced with a fake, so no CLI is spawned
and no tokens are spent. What is under test is *timing* — the behaviours
the module docstring calls load-bearing:

- **One client, never silently re-created.** A lost session stays lost and
  offers resume; reconnecting behind the user's back produces a
  conversation with amnesia.
- **The pump always runs to a result.** Cancel is a flag plus
  ``interrupt()``; an engine crash still emits a synthetic
  ``streamComplete`` so the UI can clear its spinner.
- **A turn outlives its consumer.** An ``emit`` that raises must not
  truncate a turn the engine is still running.

Plus the two turn-input rules that are cheap to lose: framing carries
paths and never file content (CC-14), and images go through ``query()``'s
verbatim dict path.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from dataclasses import fields
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskStartedMessage,
    TextBlock,
)

from ac_dc.claude_code import session as session_module
from ac_dc.claude_code.engine_config import EngineConfig
from ac_dc.claude_code.health import CliResolution, EngineStartupError
from ac_dc.claude_code.messages import Event
from ac_dc.claude_code.session import (
    CONNECT_TIMEOUT,
    INTERRUPT_DRAIN_TIMEOUT,
    EngineNotReadyError,
    EngineSession,
    SessionLostError,
    Turn,
    TurnInProgressError,
    ViewerFraming,
    build_content_blocks,
    build_framing,
    compose_prompt,
)

REQUEST_ID = "1736956800000-a1b2c3"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


def result_message(**overrides):
    fields = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sess-1",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def text_turn(message="hello", **kwargs):
    return Turn(request_id=REQUEST_ID, message=message, **kwargs)


DEFAULT_MESSAGES = [
    SystemMessage(subtype="init", data={"session_id": "sess-1", "model": "m"}),
    AssistantMessage(content=[TextBlock(text="Hi")], model="m", message_id="msg_1"),
    result_message(),
]


class FakeClient:
    """Stands in for ``ClaudeSDKClient``: records calls, replays messages.

    ``messages`` may hold SDK message objects or callables; a callable is
    invoked when the pump reaches it, which is how a mid-stream failure or
    a mid-stream assertion is injected.
    """

    instances: list[FakeClient] = []

    def __init__(self, options=None):
        self.options = options
        self.messages = list(DEFAULT_MESSAGES)
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.interrupt_calls = 0
        self.queries: list[object] = []
        self.connect_error: BaseException | None = None
        self.connect_delay = 0.0
        self.interrupt_error: BaseException | None = None
        self.control_calls: list[tuple[str, tuple]] = []
        self.context_usage = {"total_tokens": 1000}
        self.mcp_status = {"servers": []}
        self.server_info = {"commands": []}
        # The SDK's own initial value. Set on connect() when a resume
        # materialises a temp CLAUDE_CONFIG_DIR — see resume_cleanup.
        self._materialized = None
        FakeClient.instances.append(self)

    async def connect(self):
        self.connect_calls += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error is not None:
            raise self.connect_error

    async def disconnect(self):
        self.disconnect_calls += 1

    async def query(self, prompt, session_id="default"):
        if hasattr(prompt, "__aiter__"):
            self.queries.append([item async for item in prompt])
        else:
            self.queries.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            if callable(message):
                produced = message()
                if inspect.isawaitable(produced):
                    produced = await produced
                if produced is not None:
                    yield produced
                continue
            yield message

    async def interrupt(self):
        self.interrupt_calls += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error

    async def set_permission_mode(self, mode):
        self.control_calls.append(("set_permission_mode", (mode,)))

    async def set_model(self, model):
        self.control_calls.append(("set_model", (model,)))

    async def rewind_files(self, user_message_id):
        self.control_calls.append(("rewind_files", (user_message_id,)))

    async def stop_task(self, task_id):
        self.control_calls.append(("stop_task", (task_id,)))

    async def get_context_usage(self):
        return self.context_usage

    async def get_mcp_status(self):
        return self.mcp_status

    async def reconnect_mcp_server(self, name):
        self.control_calls.append(("reconnect_mcp_server", (name,)))

    async def toggle_mcp_server(self, name, enabled):
        self.control_calls.append(("toggle_mcp_server", (name, enabled)))

    async def get_server_info(self):
        return self.server_info


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    """Replace CLI resolution and the SDK client for every test here."""
    FakeClient.instances.clear()
    monkeypatch.setattr(
        session_module,
        "resolve_cli",
        lambda cli_path: CliResolution(
            path=cli_path or "/fake/claude", source="bundled", version="2.1.229"
        ),
    )
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    return FakeClient


@pytest.fixture
async def engine(tmp_path):
    """A connected session on a fake client, disconnected afterwards."""
    session = EngineSession(tmp_path, EngineConfig(), clock=lambda: "2026-08-14T00:00:00Z")
    await session.connect()
    yield session
    await session.disconnect()


def client_of(engine):
    return FakeClient.instances[-1]


async def collect(engine, turn=None):
    """Run a turn, returning (events, result)."""
    events: list[Event] = []

    async def emit(event):
        events.append(event)

    result = await engine.run_turn(turn or text_turn(), emit)
    return events, result


# ---------------------------------------------------------------------------
# Turn framing
# ---------------------------------------------------------------------------


class TestFraming:
    def test_a_plain_turn_is_sent_verbatim(self):
        """No framing means the prompt is exactly what the user typed."""
        assert build_framing(text_turn()) == ""
        assert compose_prompt(text_turn("hello")) == "hello"

    def test_a_turn_has_no_file_list_to_frame(self):
        """CC-21: the picker inserts a path into the prompt rather than
        handing us a set to describe out here."""
        assert "files" not in {f.name for f in fields(Turn)}

    def test_framing_is_wrapped_so_it_is_distinguishable(self):
        """The model must be able to tell our words from the user's."""
        framing = build_framing(text_turn(viewer=ViewerFraming("a.py")))
        assert framing.startswith("<ac-dc-ui-context>")
        assert framing.endswith("</ac-dc-ui-context>")

    def test_the_viewer_contributes_a_path_and_a_range(self):
        framing = build_framing(
            text_turn(viewer=ViewerFraming("src/a.py", start_line=10, end_line=20))
        )
        assert "src/a.py (lines 10-20 selected)" in framing

    def test_a_single_line_reads_as_a_cursor(self):
        framing = build_framing(
            text_turn(viewer=ViewerFraming("src/a.py", start_line=10, end_line=10))
        )
        assert "cursor on line 10" in framing

    def test_a_viewer_without_a_range_is_just_the_path(self):
        framing = build_framing(text_turn(viewer=ViewerFraming("src/a.py")))
        assert "- src/a.py\n" in framing + "\n"
        assert "line" not in framing.split("editor pane:")[1]

    def test_review_facts_are_included_when_review_is_active(self):
        framing = build_framing(
            text_turn(
                review={
                    "active": True,
                    "branch": "feature",
                    "base_branch": "main",
                    "merge_base": "abc123",
                }
            )
        )
        assert "Code review is active" in framing
        assert "base branch: main" in framing
        assert "merge base: abc123" in framing

    def test_inactive_review_contributes_nothing(self):
        assert build_framing(text_turn(review={"active": False, "branch": "x"})) == ""

    def test_framing_never_carries_file_content(self):
        """CC-14: the agent reads files with its own tools, not through us."""
        framing = build_framing(
            text_turn(
                viewer=ViewerFraming("src/a.py", start_line=1, end_line=999),
                review={"active": True, "branch": "feature"},
            )
        )
        # Every list item is a path and a range, never a file body: the
        # only thing that could smuggle content in is a `- ` line.
        items = [line for line in framing.splitlines() if line.startswith("- ")]
        assert items == ["- src/a.py (lines 1-999 selected)", "- branch: feature"]

    def test_framing_precedes_the_users_words(self):
        prompt = compose_prompt(
            text_turn("fix this", viewer=ViewerFraming("a.py"))
        )
        assert prompt.index("<ac-dc-ui-context>") < prompt.index("fix this")
        assert prompt.endswith("fix this")


class TestViewerFraming:
    def test_from_dict_reads_the_rpc_payload(self):
        viewer = ViewerFraming.from_dict(
            {"path": "src/a.py", "start_line": 3, "end_line": 9}
        )
        assert viewer == ViewerFraming("src/a.py", 3, 9)

    def test_string_line_numbers_are_coerced(self):
        """They arrive from JavaScript, which is loose about this."""
        assert ViewerFraming.from_dict({"path": "a", "start_line": "3"}).start_line == 3

    @pytest.mark.parametrize(
        "payload", [None, {}, {"path": ""}, {"path": 42}, "src/a.py", []]
    )
    def test_a_bad_shape_yields_no_framing(self, payload):
        """A malformed viewer must not fail the turn."""
        assert ViewerFraming.from_dict(payload) is None

    def test_bad_line_numbers_drop_to_null(self):
        viewer = ViewerFraming.from_dict(
            {"path": "a", "start_line": True, "end_line": "nope"}
        )
        assert viewer.start_line is None
        assert viewer.end_line is None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class TestContentBlocks:
    def test_images_precede_the_text_block(self):
        blocks = build_content_blocks(text_turn("what is this?", images=[PNG]))
        assert [b["type"] for b in blocks] == ["image", "text"]
        assert blocks[0]["source"]["media_type"] == "image/png"
        assert blocks[0]["source"]["type"] == "base64"
        assert blocks[1]["text"] == "what is this?"

    def test_the_base64_payload_is_passed_through_unchanged(self):
        blocks = build_content_blocks(text_turn(images=[PNG]))
        assert blocks[0]["source"]["data"] == PNG.split(",", 1)[1]

    def test_text_only_turns_still_get_a_text_block(self):
        assert build_content_blocks(text_turn("hi")) == [{"type": "text", "text": "hi"}]

    @pytest.mark.parametrize(
        "bad",
        [
            "https://example.com/a.png",
            "data:image/png;base64",
            "data:text/plain;base64,aGk=",
            "data:image/png;base64,",
            42,
        ],
    )
    def test_an_unusable_image_is_dropped_not_fatal(self, bad):
        """A bad paste must lose the image, not the turn."""
        blocks = build_content_blocks(text_turn("hi", images=[bad]))
        assert [b["type"] for b in blocks] == ["text"]

    def test_framing_still_applies_to_an_image_turn(self):
        blocks = build_content_blocks(
            text_turn("hi", images=[PNG], viewer=ViewerFraming("a.py"))
        )
        assert "<ac-dc-ui-context>" in blocks[-1]["text"]

    async def test_an_image_turn_uses_the_verbatim_dict_path(self, engine):
        """query() JSON-encodes each dict as given, so blocks survive."""
        await collect(engine, text_turn("look", images=[PNG]))
        sent = client_of(engine).queries[0]
        assert isinstance(sent, list) and len(sent) == 1
        message = sent[0]
        assert message["type"] == "user"
        assert message["message"]["role"] == "user"
        assert [b["type"] for b in message["message"]["content"]] == ["image", "text"]
        assert message["parent_tool_use_id"] is None

    async def test_a_text_turn_uses_the_string_path(self, engine):
        await collect(engine, text_turn("look"))
        assert client_of(engine).queries == ["look"]


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


class TestConnect:
    async def test_connect_records_health_and_reports_ready(self, tmp_path):
        session = EngineSession(tmp_path, EngineConfig())
        assert session.ready is False
        await session.connect()
        assert session.connected is True
        assert session.ready is True
        assert session.health.cli_version == "2.1.229"
        assert session.health.cli_source == "bundled"
        assert session.health.last_error is None

    async def test_session_id_is_null_until_the_init_message(self, engine):
        """connect() completes the handshake; the ID comes with the turn."""
        assert engine.session_id is None
        await collect(engine)
        assert engine.session_id == "sess-1"

    async def test_the_probed_binary_is_the_one_configured_to_run(self, engine):
        assert client_of(engine).options.cli_path == "/fake/claude"

    async def test_a_repoless_session_gets_checkpointing(self, engine):
        """Nothing to mirror into, so undo is free to be on."""
        assert client_of(engine).options.enable_file_checkpointing is True
        assert engine.file_checkpointing is True

    async def test_a_mirrored_session_connects_without_checkpointing(self, tmp_path):
        """The SDK refuses the pair, and refuses it *at connect* — keeping
        both would cost the session rather than only the undo."""
        session = EngineSession(tmp_path, EngineConfig(), session_store=object())
        await session.connect()
        assert client_of(session).options.enable_file_checkpointing is False
        assert session.file_checkpointing is False
        await session.disconnect()

    async def test_checkpointing_is_answerable_before_connect(self, tmp_path):
        """The RPC refusal has to work on a cold engine too."""
        assert EngineSession(tmp_path, EngineConfig()).file_checkpointing is True
        assert (
            EngineSession(tmp_path, EngineConfig(), session_store=object())
        ).file_checkpointing is False

    async def test_a_second_connect_does_not_replace_the_client(self, engine):
        """One client, never silently re-created."""
        await engine.connect()
        assert len(FakeClient.instances) == 1

    async def test_a_connect_failure_is_a_startup_error(self, tmp_path, monkeypatch):
        def boom(options=None):
            client = FakeClient(options)
            client.connect_error = RuntimeError("no such file")
            return client

        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", boom)
        session = EngineSession(tmp_path, EngineConfig())
        with pytest.raises(EngineStartupError, match="no such file"):
            await session.connect()
        assert session.connected is False
        assert session.health.last_error == "no such file"
        # The half-started client is torn down rather than leaked.
        assert FakeClient.instances[-1].disconnect_calls == 1

    async def test_a_hung_connect_times_out_and_names_the_cause(
        self, tmp_path, monkeypatch
    ):
        """A first-run login prompt is the case this exists for."""
        monkeypatch.setattr(session_module, "CONNECT_TIMEOUT", 0.01)

        def slow(options=None):
            client = FakeClient(options)
            client.connect_delay = 5
            return client

        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", slow)
        session = EngineSession(tmp_path, EngineConfig())
        with pytest.raises(EngineStartupError, match="first-run login"):
            await session.connect()
        assert "timed out" in session.health.last_error

    async def test_resume_is_recorded_before_the_first_turn(self, tmp_path):
        """So get_current_state() is right immediately, not only after a turn."""
        session = EngineSession(tmp_path, EngineConfig())
        await session.connect(resume="prev-session")
        assert session.session_id == "prev-session"
        assert FakeClient.instances[-1].options.resume == "prev-session"

    async def test_a_fork_does_not_claim_the_origins_id(self, tmp_path):
        """A fork mints a *new* ID that only the init message knows. Recording
        the origin would name the wrong session and point a restart's
        auto-resume at the one the user forked away from."""
        session = EngineSession(tmp_path, EngineConfig())
        await session.connect(resume="prev-session", fork_session=True)
        assert session.session_id is None
        assert FakeClient.instances[-1].options.fork_session is True

    async def test_reset_forgets_which_session_this_was(self, engine):
        """Unlike disconnect, which keeps the ID so a lost session can be
        resumed — the opposite of what starting a fresh one means."""
        engine._last_session_id = "prev-session"
        engine._session_lost = True
        await engine.reset()
        assert engine.session_id is None
        assert engine.connected is False
        # A fresh session is not a lost one; `admit` must not refuse its
        # first turn.
        assert engine._session_lost is False

    async def test_a_resumed_connect_registers_its_temp_config_dir(
        self, tmp_path, monkeypatch
    ):
        """Otherwise Ctrl-C abandons it: the signal handler exits via
        ``os._exit`` and never reaches the ``disconnect()`` the SDK cleans
        up in. One directory per launch cycle, holding a transcript copy
        and a live access token."""
        from ac_dc.claude_code import resume_cleanup

        registry: set = set()
        monkeypatch.setattr(resume_cleanup, "_DIRS", registry)
        config_dir = tmp_path / "claude-resume-xyz"
        materialized = SimpleNamespace(config_dir=config_dir)
        original = FakeClient.connect

        async def connect(self):
            """Materialise on connect, the way the real client does."""
            await original(self)
            self._materialized = materialized

        monkeypatch.setattr(FakeClient, "connect", connect)
        session = EngineSession(tmp_path, EngineConfig())
        await session.connect(resume="prev-session")

        assert registry == {config_dir}

    async def test_a_fresh_connect_registers_nothing(self, tmp_path, monkeypatch):
        """No resume, no materialised directory, nothing to clean up."""
        from ac_dc.claude_code import resume_cleanup

        registry: set = set()
        monkeypatch.setattr(resume_cleanup, "_DIRS", registry)
        session = EngineSession(tmp_path, EngineConfig())
        await session.connect()

        assert registry == set()

    async def test_the_connect_timeout_is_the_documented_sixty_seconds(self):
        """The bundled binary's cold first exec is the slow case."""
        assert CONNECT_TIMEOUT == 60.0

    async def test_disconnect_clears_the_client(self, engine):
        await engine.disconnect()
        assert engine.connected is False
        assert engine.ready is False
        assert client_of(engine).disconnect_calls == 1

    async def test_disconnect_is_idempotent(self, engine):
        await engine.disconnect()
        await engine.disconnect()
        assert client_of(engine).disconnect_calls == 1


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


class TestAdmission:
    def test_a_turn_before_connect_is_not_ready(self, tmp_path):
        session = EngineSession(tmp_path, EngineConfig())
        with pytest.raises(EngineNotReadyError, match="still starting"):
            session.admit(REQUEST_ID)

    async def test_a_turn_needs_a_request_id(self, engine):
        with pytest.raises(ValueError, match="request ID"):
            engine.admit("")

    async def test_a_second_turn_is_rejected_not_queued(self, engine):
        """Queuing reads as a hang; the user's intent is stop-and-replace."""
        reached = asyncio.Event()
        gate = asyncio.Event()

        async def pause():
            reached.set()
            await gate.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        with pytest.raises(TurnInProgressError, match=REQUEST_ID):
            engine.admit("1736956800001-zzzzzz")
        gate.set()
        await task
        assert engine.streaming_active is False

    async def test_a_lost_session_offers_resume_rather_than_reconnect(self, engine):
        client = client_of(engine)
        client.messages = [_raise(ConnectionResetError("broken pipe"))]
        await collect(engine)
        with pytest.raises(SessionLostError, match="resume"):
            engine.admit(REQUEST_ID)
        # Not silently re-created: a fresh session would have no context.
        assert len(FakeClient.instances) == 1

    async def test_the_active_turn_slot_is_cleared_after_a_failure(self, engine):
        client = client_of(engine)
        client.messages = [_raise(RuntimeError("boom"))]
        await collect(engine)
        assert engine.streaming_active is False


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------


class TestPump:
    async def test_a_turn_emits_its_channels_and_returns_the_result(self, engine):
        events, result = await collect(engine)
        assert [e.name for e in events] == [
            "sessionStarted",
            "streamChunk",
            "streamComplete",
        ]
        assert result["response"] == "Hi"
        assert result["is_error"] is False
        assert result["cancelled"] is False

    async def test_an_emit_failure_does_not_truncate_the_turn(self, engine):
        """A closed WebSocket must not stop work the engine is still doing."""
        seen = []

        async def emit(event):
            seen.append(event.name)
            raise RuntimeError("socket closed")

        result = await engine.run_turn(text_turn(), emit)
        assert seen == ["sessionStarted", "streamChunk", "streamComplete"]
        assert result["response"] == "Hi"

    async def test_a_turn_runs_with_no_consumer_at_all(self, engine):
        """The transcript accumulates server-side for a later reconnect."""
        result = await engine.run_turn(text_turn(), None)
        assert result["response"] == "Hi"

    async def test_mirror_errors_are_upgraded_to_the_full_health_record(self, engine):
        from claude_agent_sdk import MirrorErrorMessage

        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            MirrorErrorMessage(subtype="mirror_error", data={}, error="disk full"),
            *DEFAULT_MESSAGES[1:],
        ]
        events, result = await collect(engine)
        health = next(e for e in events if e.name == "engineHealth")
        assert health.turn_scoped is False
        # The translator only knows the turn; these are session facts.
        assert health.payload["cli_version"] == "2.1.229"
        assert health.payload["mirror_gaps"] == 1
        assert health.payload["last_error"] == "disk full"
        assert engine.health.mirror_gaps == 1
        assert result["mirror_gap"] is True

    async def test_an_engine_crash_still_completes_the_stream(self, engine):
        """Otherwise the browser keeps a spinner it can never clear."""
        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], _raise(RuntimeError("kaboom"))]
        events, result = await collect(engine)
        assert [e.name for e in events][-1] == "streamComplete"
        assert result["is_error"] is True
        assert result["terminal_reason"] == "engine_error"
        assert result["errors"] == ["kaboom"]
        # An engine error is not a lost session: the client is still usable.
        assert engine.connected is True

    async def test_a_broken_pipe_loses_the_session_and_says_so(self, engine):
        client = client_of(engine)
        client.messages = [_raise(ConnectionResetError("broken pipe"))]
        events, result = await collect(engine)
        assert result["terminal_reason"] == "session_lost"
        assert engine.connected is False
        # The health broadcast follows the completion, so the UI can show why.
        assert [e.name for e in events][-2:] == ["streamComplete", "engineHealth"]

    async def test_a_partial_response_survives_the_failure(self, engine):
        """What the user already saw is in the result, not discarded."""
        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            DEFAULT_MESSAGES[1],
            _raise(RuntimeError("boom")),
        ]
        _, result = await collect(engine)
        assert result["response"] == "Hi"

    async def test_a_stream_that_ends_without_a_result_is_a_failure(self, engine):
        """receive_response() only does this if the stream closed under it."""
        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0]]
        _, result = await collect(engine)
        assert result["is_error"] is True
        assert result["terminal_reason"] == "session_lost"
        assert "before the turn finished" in result["errors"][0]

    async def test_cancelling_the_pump_task_does_not_report_a_clean_finish(self, engine):
        """The turn is still running inside the CLI; say so."""
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(30)

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], hang, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.streaming_active is False

    async def test_the_session_id_persists_after_the_turn(self, engine):
        await collect(engine)
        assert engine.session_id == "sess-1"
        assert engine.streaming_active is False


# ---------------------------------------------------------------------------
# What the turn cost, as opposed to what the session has spent
# ---------------------------------------------------------------------------


class TestTurnCost:
    """The engine reports cost cumulatively; the browser wants this turn's.

    The difference lives here rather than in the translator because the
    baseline is session state and a ``TurnTranslator`` only ever sees one
    turn — the same reason ``engineHealth`` is folded in by the pump.
    """

    async def one_turn(self, engine, **result_fields):
        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], DEFAULT_MESSAGES[1], result_message(**result_fields)]
        events, result = await collect(engine)
        return next(e for e in events if e.name == "streamComplete").payload

    async def test_a_completion_carries_the_turns_own_cost(self, engine):
        payload = await self.one_turn(engine, total_cost_usd=0.21)
        assert payload["turn_cost_usd"] == 0.21
        assert payload["turn_cost_basis"] == "measured"

    async def test_the_engines_cumulative_figure_is_left_alone_beside_it(self, engine):
        """Both readings ship, under names that cannot be confused: the
        Session section of the Context tab wants the running total."""
        payload = await self.one_turn(engine, total_cost_usd=0.21)
        assert payload["total_cost_usd"] == 0.21

    async def test_the_second_turn_is_priced_against_the_first(self, engine):
        await self.one_turn(engine, total_cost_usd=0.21)
        payload = await self.one_turn(engine, total_cost_usd=0.30)
        assert payload["turn_cost_usd"] == pytest.approx(0.09, abs=1e-9)
        assert payload["total_cost_usd"] == 0.30

    async def test_per_model_usage_is_differenced_too(self, engine):
        """Keys are the engine's camelCase, straight off the wire schema —
        ``model_usage`` is passed through untranslated, so the ledger and
        every renderer downstream read the same spelling."""
        await self.one_turn(
            engine,
            total_cost_usd=0.10,
            model_usage={"m": {"inputTokens": 100, "outputTokens": 20, "costUSD": 0.10}},
        )
        payload = await self.one_turn(
            engine,
            total_cost_usd=0.30,
            model_usage={"m": {"inputTokens": 180, "outputTokens": 45, "costUSD": 0.30}},
        )
        assert payload["turn_model_usage"]["m"]["inputTokens"] == 80
        assert payload["turn_model_usage"]["m"]["outputTokens"] == 25
        assert payload["turn_model_usage"]["m"]["costUSD"] == pytest.approx(0.20, abs=1e-9)
        # The engine's own cumulative map is still there under its own name.
        assert payload["model_usage"]["m"]["inputTokens"] == 180

    async def test_a_reconnect_starts_the_baseline_over(self, engine):
        """The CLI's ledger is per-process and, in its own words, resumed
        sessions start fresh — so carrying ours across a connect would
        price the new session's first turn as a refund."""
        await self.one_turn(engine, total_cost_usd=0.40)
        await engine.disconnect()
        await engine.connect()
        payload = await self.one_turn(engine, total_cost_usd=0.05)
        assert payload["turn_cost_usd"] == 0.05
        assert payload["turn_cost_basis"] == "measured"

    async def test_a_turn_that_cost_nothing_extra_says_so_with_a_zero(self, engine):
        await self.one_turn(engine, total_cost_usd=0.40)
        payload = await self.one_turn(engine, total_cost_usd=0.40)
        assert payload["turn_cost_usd"] == 0.0
        assert payload["turn_cost_basis"] == "measured"

    async def test_a_crash_footer_is_unpriced_not_free(self, engine):
        """The synthetic completion the pump writes when the engine dies
        has no cost of its own, and the turn may have spent plenty before
        we lost track of it."""
        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], _raise(RuntimeError("kaboom"))]
        events, result = await collect(engine)
        assert result["turn_cost_usd"] is None
        assert result["turn_cost_basis"] == "unpriced"

    async def test_spend_lost_to_a_crash_lands_on_the_next_priced_turn(self, engine):
        await self.one_turn(engine, total_cost_usd=0.40)
        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], _raise(RuntimeError("kaboom"))]
        await collect(engine)
        payload = await self.one_turn(engine, total_cost_usd=0.60)
        assert payload["turn_cost_usd"] == pytest.approx(0.20, abs=1e-9)


# ---------------------------------------------------------------------------
# When a run of mirror gaps stops being bad luck
# ---------------------------------------------------------------------------


class TestMirrorGapEscalation:
    """The banner needs to know when to stop calling it bad luck.

    The rule lives on ``EngineHealth`` and not in the browser, for the
    reason the disk warning's one-shot does: one owner, so a second copy
    cannot disagree (``specs5/1-foundation/configuration.md`` § App
    Config — "how many mirror-append failures are tolerated before the
    health banner escalates").
    """

    def health(self, **kwargs):
        from ac_dc.claude_code.health import EngineHealth

        return EngineHealth(**kwargs)

    def test_a_clean_mirror_is_not_escalated(self):
        assert self.health().to_dict()["mirror_gaps_escalated"] is False

    def test_the_default_tolerates_a_run_of_three(self):
        from ac_dc.claude_code.health import DEFAULT_MIRROR_GAP_TOLERANCE

        h = self.health()
        for _ in range(DEFAULT_MIRROR_GAP_TOLERANCE):
            h.note_mirror_gap()
        assert h.to_dict()["mirror_gaps_escalated"] is False
        h.note_mirror_gap()
        assert h.to_dict()["mirror_gaps_escalated"] is True

    def test_a_zero_tolerance_escalates_on_the_first(self):
        """A real answer, not a broken one: "tell me about the first gap"."""
        h = self.health(mirror_gap_tolerance=lambda: 0)
        assert h.to_dict()["mirror_gaps_escalated"] is False
        h.note_mirror_gap()
        assert h.to_dict()["mirror_gaps_escalated"] is True

    def test_it_is_read_at_serialisation_not_at_construction(self):
        """`app.json` reloads without a restart, so the value cannot be
        pinned when the session is built."""
        tolerance = [10]
        h = self.health(mirror_gap_tolerance=lambda: tolerance[0])
        for _ in range(3):
            h.note_mirror_gap()
        assert h.to_dict()["mirror_gaps_escalated"] is False
        tolerance[0] = 1
        assert h.to_dict()["mirror_gaps_escalated"] is True

    def test_a_broken_tolerance_is_not_a_way_to_silence_a_broken_mirror(self):
        from ac_dc.claude_code.health import DEFAULT_MIRROR_GAP_TOLERANCE

        def boom():
            raise RuntimeError("no config")

        for source in (boom, lambda: None, lambda: "three", lambda: -5):
            h = self.health(mirror_gap_tolerance=source)
            for _ in range(DEFAULT_MIRROR_GAP_TOLERANCE + 1):
                h.note_mirror_gap()
            assert h.to_dict()["mirror_gaps_escalated"] is True, source

    async def test_the_broadcast_carries_it(self, engine):
        from claude_agent_sdk import MirrorErrorMessage

        engine.health.mirror_gap_tolerance = lambda: 0
        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            MirrorErrorMessage(subtype="mirror_error", data={}, error="disk full"),
            *DEFAULT_MESSAGES[1:],
        ]
        events, _ = await collect(engine)
        health = next(e for e in events if e.name == "engineHealth")
        assert health.payload["mirror_gaps_escalated"] is True


# ---------------------------------------------------------------------------
# Capabilities the session started without
# ---------------------------------------------------------------------------


class TestStartupDegradation:
    """What the health record says about a session that started short.

    Sentences rather than flags, for the reason the disk warning is a
    sentence: the words belong to whoever knows what was lost, and a
    browser turning a flag into prose would be a second owner of the
    meaning (``specs5/3-engine/mcp-bridge.md`` § Availability and
    Degradation).
    """

    def health(self, **kwargs):
        from ac_dc.claude_code.health import EngineHealth

        return EngineHealth(**kwargs)

    def test_a_whole_session_has_nothing_to_report(self):
        assert self.health().to_dict()["degradations"] == []

    def test_a_loss_is_carried_to_the_browser(self):
        h = self.health()
        h.note_degradation("The ac-dc repo tools did not start.")
        assert h.to_dict()["degradations"] == ["The ac-dc repo tools did not start."]

    def test_two_losses_keep_the_order_they_were_noted_in(self):
        h = self.health()
        h.note_degradation("first")
        h.note_degradation("second")
        assert h.degradations == ["first", "second"]

    def test_the_same_loss_twice_is_not_two_losses(self):
        """A standing condition, not an event: it is re-reported on every
        health push, and the banner keys its dismissal on what it showed."""
        h = self.health()
        h.note_degradation("no bridge")
        h.note_degradation("no bridge")
        assert h.degradations == ["no bridge"]

    def test_nothing_to_say_says_nothing(self):
        h = self.health()
        for empty in ("", "   ", "\n"):
            h.note_degradation(empty)
        assert h.degradations == []

    def test_the_dict_hands_over_a_copy(self):
        """The payload is serialised and broadcast; a later note must not
        edit a dict that has already gone out."""
        h = self.health()
        h.note_degradation("no bridge")
        payload = h.to_dict()
        h.note_degradation("no hook")
        assert payload["degradations"] == ["no bridge"]


# ---------------------------------------------------------------------------
# The CLI's own stderr
# ---------------------------------------------------------------------------


class TestCliStderr:
    """Diagnostics that existed and reached nobody.

    Unset, ``options.stderr`` leaves the subprocess inheriting the
    server's stderr, so a failing CLI explains itself into whatever
    terminal launched AC-DC — which for a desktop launch is nowhere.
    Registering the callback pipes it instead, which is why the callback
    both logs and records: the terminal must not *lose* what it had.
    """

    def health(self, **kwargs):
        from ac_dc.claude_code.health import EngineHealth

        return EngineHealth(**kwargs)

    def test_a_quiet_cli_has_nothing_to_report(self):
        assert self.health().to_dict()["cli_stderr"] == []

    def test_a_line_is_carried_to_the_browser(self):
        h = self.health()
        h.note_cli_stderr("node: out of memory")
        assert h.to_dict()["cli_stderr"] == ["node: out of memory"]

    def test_only_the_tail_is_kept(self):
        from ac_dc.claude_code.health import CLI_STDERR_TAIL

        h = self.health()
        for i in range(CLI_STDERR_TAIL + 5):
            h.note_cli_stderr(f"line {i}")
        lines = h.to_dict()["cli_stderr"]
        assert len(lines) == CLI_STDERR_TAIL
        # Trimmed from the front: the newest lines are the ones that
        # explain what just happened.
        assert lines[-1] == f"line {CLI_STDERR_TAIL + 4}"
        assert lines[0] == "line 5"

    def test_a_repeated_line_is_not_collapsed(self):
        """Unlike a degradation: forty repeats is the fact worth seeing."""
        h = self.health()
        for _ in range(3):
            h.note_cli_stderr("warning: retrying")
        assert h.cli_stderr == ["warning: retrying"] * 3

    def test_an_enormous_line_is_cut(self):
        """One minified bundle in a trace must not become the payload."""
        from ac_dc.claude_code.health import CLI_STDERR_LINE_CHARS

        h = self.health()
        h.note_cli_stderr("x" * (CLI_STDERR_LINE_CHARS * 3))
        line = h.cli_stderr[0]
        assert len(line) == CLI_STDERR_LINE_CHARS + 1
        assert line.endswith("…")

    def test_blank_lines_are_dropped(self):
        h = self.health()
        for empty in ("", "   ", "\n"):
            h.note_cli_stderr(empty)
        assert h.cli_stderr == []

    def test_the_dict_hands_over_a_copy(self):
        h = self.health()
        h.note_cli_stderr("first")
        payload = h.to_dict()
        h.note_cli_stderr("second")
        assert payload["cli_stderr"] == ["first"]

    async def test_the_session_registers_a_sink_with_the_sdk(self, engine):
        """Registering it is what makes the SDK pipe stderr at all."""
        assert client_of(engine).options.stderr == engine._note_cli_stderr

    async def test_a_line_from_the_cli_reaches_the_health_record(self, engine):
        engine._note_cli_stderr("Error: ENOSPC")
        assert engine.health.to_dict()["cli_stderr"] == ["Error: ENOSPC"]

    async def test_the_line_is_logged_as_the_clis_own(self, engine, caplog):
        """The terminal keeps what it had before the callback existed."""
        import logging

        with caplog.at_level(logging.INFO):
            engine._note_cli_stderr("node: bad option")
        assert "node: bad option" in caplog.text
        assert "CLI stderr" in caplog.text

    async def test_our_own_failure_here_is_not_silent(self, engine, caplog):
        """The SDK swallows exceptions from this callback at debug level.

        So a bug in it would otherwise be invisible: no log, no lines, and
        no clue why the banner is empty during exactly the failure it was
        built for.
        """
        import logging

        def boom(_line):
            raise RuntimeError("no room")

        engine.health.note_cli_stderr = boom
        with caplog.at_level(logging.WARNING):
            engine._note_cli_stderr("something")
        assert "CLI stderr" in caplog.text


# ---------------------------------------------------------------------------
# Reconnect replay
# ---------------------------------------------------------------------------


class TestActiveStreams:
    async def test_no_active_stream_when_idle(self, engine):
        assert engine.active_streams() == []

    async def test_a_mid_turn_client_replays_from_server_state(self, engine):
        """A turn's lifetime is independent of any WebSocket."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            DEFAULT_MESSAGES[1],
            pause,
            DEFAULT_MESSAGES[2],
        ]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        streams = engine.active_streams()
        assert len(streams) == 1
        assert streams[0]["request_id"] == REQUEST_ID
        assert streams[0]["session_id"] == "sess-1"
        assert streams[0]["started_at"] == "2026-08-14T00:00:00Z"
        assert streams[0]["blocks"][0]["content"] == "Hi"

        release.set()
        await task
        assert engine.active_streams() == []

    async def test_a_live_subagent_is_in_the_snapshot(self, engine):
        """Otherwise a refresh mid-fan-out loses the tab the subagent lives in."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            TaskStartedMessage(
                subtype="task_started",
                data={"agent_id": "agent-7"},
                task_id="task-1",
                description="Explore the repo",
                uuid="u",
                session_id="sess-1",
                tool_use_id="toolu_1",
                task_type="Explore",
            ),
            pause,
            *DEFAULT_MESSAGES[1:],
        ]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        subagents = engine.active_streams()[0]["subagents"]
        assert len(subagents) == 1
        assert subagents[0]["agent_id"] == "agent-7"
        assert subagents[0]["description"] == "Explore the repo"
        # The `Task` call joins the row to the blocks the subagent produced.
        assert subagents[0]["tool_use_id"] == "toolu_1"
        assert subagents[0]["terminal"] is False

        release.set()
        await task

    async def test_the_live_token_counter_is_in_the_snapshot(self, engine):
        """Why the engine accumulates the counter rather than the browser: a
        client that reloaded mid-turn has no record of the assistant messages
        already counted, and would otherwise show a blank counter until the
        next one landed — tens of seconds on a long tool call."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            AssistantMessage(
                content=[TextBlock(text="Hi")],
                model="claude-opus-5",
                message_id="msg_1",
                usage={"input_tokens": 900, "output_tokens": 100},
            ),
            pause,
            DEFAULT_MESSAGES[2],
        ]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        # The same `turn_model_usage` key the pushed event and the result
        # message use, so one reader in the browser serves all three.
        assert engine.active_streams()[0]["usage"] == {
            "turn_model_usage": {
                "claude-opus-5": {"input_tokens": 900, "output_tokens": 100}
            }
        }

        release.set()
        await task

    async def test_the_snapshot_counter_is_empty_before_any_usage(self, engine):
        """An empty map, not a missing key: the browser's replay reads the
        field unconditionally, and a turn can pause before its first message."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        assert engine.active_streams()[0]["usage"] == {"turn_model_usage": {}}

        release.set()
        await task

    async def test_a_permission_prompt_attaches_to_the_turn_in_flight(self, engine):
        """The broker knows nothing about turns; this is the whole bridge."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, DEFAULT_MESSAGES[2]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        assert engine.active_request_id == REQUEST_ID
        assert engine.note_permission_prompt("toolu_1") == REQUEST_ID

        release.set()
        await task
        assert engine.active_request_id is None

    async def test_a_prompt_with_no_turn_is_not_an_error(self, engine):
        """A control request can outlive its turn. Losing the attribution is
        acceptable; raising into the SDK's callback is not."""
        assert engine.active_request_id is None
        assert engine.note_permission_prompt("toolu_1") is None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestInterrupt:
    async def test_interrupting_nothing_is_not_an_error(self, engine):
        assert await engine.interrupt(REQUEST_ID) == {"status": "idle"}

    async def test_a_cancelled_turn_still_drains_to_a_result(self, engine):
        """Skipping the drain routes this turn's tail into the next turn's UI."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [
            DEFAULT_MESSAGES[0],
            pause,
            result_message(terminal_reason="aborted_streaming", subtype="error_during_execution"),
        ]
        events: list[Event] = []

        async def emit(event):
            events.append(event)

        task = asyncio.create_task(engine.run_turn(text_turn(), emit))
        await reached.wait()
        assert await engine.interrupt(REQUEST_ID) == {
            "status": "interrupting",
            "request_id": REQUEST_ID,
        }
        release.set()
        result = await task

        assert client.interrupt_calls == 1
        assert result["cancelled"] is True
        assert result["terminal_reason"] == "aborted_streaming"
        assert [e.name for e in events][-1] == "streamComplete"

    async def test_a_stale_stop_for_another_request_is_ignored(self, engine):
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        assert await engine.interrupt("1736956800001-zzzzzz") == {
            "status": "not_active",
            "request_id": "1736956800001-zzzzzz",
        }
        assert client.interrupt_calls == 0

        release.set()
        result = await task
        assert result["cancelled"] is False

    async def test_a_second_stop_click_is_a_no_op(self, engine):
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        await engine.interrupt(REQUEST_ID)
        await engine.interrupt(REQUEST_ID)
        assert client.interrupt_calls == 1

        release.set()
        await task

    async def test_a_failing_interrupt_is_reported_to_the_caller(self, engine):
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.interrupt_error = RuntimeError("no response to control request")
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()

        answer = await engine.interrupt(REQUEST_ID)
        assert "no response to control request" in answer["error"]

        release.set()
        await task

    async def test_a_turn_that_never_drains_loses_the_session(self, engine, monkeypatch):
        """Better a clean failure than reading the next turn over this tail."""
        monkeypatch.setattr(session_module, "INTERRUPT_DRAIN_TIMEOUT", 0.01)
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()
        await engine.interrupt(REQUEST_ID)
        await asyncio.sleep(0.05)

        assert engine.connected is False
        assert "did not stop within" in engine.health.last_error
        assert client.disconnect_calls == 1

        release.set()
        await task
        with pytest.raises(SessionLostError):
            engine.admit(REQUEST_ID)

    async def test_the_watchdog_is_cancelled_by_a_turn_that_does_drain(self, engine):
        """Otherwise it fires 30s into the *next* turn and kills the session."""
        reached = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            reached.set()
            await release.wait()

        client = client_of(engine)
        client.messages = [DEFAULT_MESSAGES[0], pause, *DEFAULT_MESSAGES[1:]]
        task = asyncio.create_task(engine.run_turn(text_turn()))
        await reached.wait()
        await engine.interrupt(REQUEST_ID)
        release.set()
        await task
        await asyncio.sleep(0)
        assert engine.connected is True

    def test_the_drain_timeout_is_the_documented_thirty_seconds(self):
        assert INTERRUPT_DRAIN_TIMEOUT == 30.0


# ---------------------------------------------------------------------------
# Live controls
# ---------------------------------------------------------------------------


class TestLiveControls:
    async def test_permission_mode_switches_without_reconnecting(self, engine):
        assert engine.permission_mode == "default"
        assert await engine.set_permission_mode("plan") == "plan"
        assert engine.permission_mode == "plan"
        assert ("set_permission_mode", ("plan",)) in client_of(engine).control_calls
        assert len(FakeClient.instances) == 1

    async def test_an_unknown_permission_mode_is_refused_locally(self, engine):
        with pytest.raises(ValueError, match="yolo"):
            await engine.set_permission_mode("yolo")
        assert client_of(engine).control_calls == []

    async def test_model_switches_and_none_restores_the_default(self, engine):
        assert await engine.set_model("claude-opus-5") == "claude-opus-5"
        assert engine.model == "claude-opus-5"
        assert await engine.set_model(None) is None
        assert engine.model is None

    async def test_context_usage_passes_through_as_a_plain_dict(self, engine):
        """It is a TypedDict, so it is already JSON-ready."""
        assert await engine.get_context_usage() == {"total_tokens": 1000}

    async def test_mcp_status_passes_through(self, engine):
        assert await engine.get_mcp_status() == {"servers": []}

    async def test_server_info_passes_through(self, engine):
        assert await engine.get_server_info() == {"commands": []}

    async def test_the_remaining_controls_reach_the_client(self, engine):
        await engine.rewind_files("msg-uuid-1")
        await engine.stop_task("task-1")
        await engine.reconnect_mcp_server("ac-dc")
        await engine.toggle_mcp_server("ac-dc", False)
        assert client_of(engine).control_calls == [
            ("rewind_files", ("msg-uuid-1",)),
            ("stop_task", ("task-1",)),
            ("reconnect_mcp_server", ("ac-dc",)),
            ("toggle_mcp_server", ("ac-dc", False)),
        ]

    def test_a_control_before_connect_is_not_ready(self, tmp_path):
        session = EngineSession(tmp_path, EngineConfig())
        with pytest.raises(EngineNotReadyError):
            asyncio.run(session.get_context_usage())

    async def test_a_control_on_a_lost_session_offers_resume(self, engine):
        client = client_of(engine)
        client.messages = [_raise(ConnectionResetError("broken pipe"))]
        await collect(engine)
        with pytest.raises(SessionLostError, match="resume"):
            await engine.get_context_usage()


# ---------------------------------------------------------------------------
# Connection-failure classification
# ---------------------------------------------------------------------------


class TestConnectionFailureClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError("reset"),
            BrokenPipeError("pipe"),
            SessionLostError("gone"),
        ],
    )
    def test_transport_failures_mean_the_subprocess_is_gone(self, exc):
        assert session_module._is_connection_failure(exc) is True

    def test_the_sdk_errors_we_name_still_exist(self):
        """Matched by name, so a rename must be noticed here, not in prod."""
        import claude_agent_sdk

        for name in ("CLIConnectionError", "ProcessError", "CLIJSONDecodeError"):
            assert hasattr(claude_agent_sdk, name), name

    def test_a_real_sdk_transport_error_is_classified(self):
        from claude_agent_sdk import CLIConnectionError

        assert session_module._is_connection_failure(CLIConnectionError("x")) is True

    @pytest.mark.parametrize("exc", [ValueError("nope"), KeyError("k")])
    def test_ordinary_errors_do_not_lose_the_session(self, exc):
        assert session_module._is_connection_failure(exc) is False


def _raise(exc):
    """A message-list entry that raises when the pump reaches it."""

    def _fail():
        raise exc

    return _fail
