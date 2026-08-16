"""Tests for ac_dc.claude_code.messages — conversion phase 1.

The pump's translation layer, fed real SDK message objects. Everything
here is offline: constructing an ``AssistantMessage`` needs no CLI, which
is exactly why the translator holds no client reference.

The properties under test are the three the module docstring calls out,
plus the dispatch hazard the SDK's class hierarchy creates:

- **Subclasses before superclass.** Six message types subclass
  ``SystemMessage``; dispatching on the parent first would swallow them
  all, so each is checked to land on its own channel.
- **Nothing is dropped.** Unknown system subtypes and unknown content
  blocks reach a generic channel rather than vanishing.
- **Chunks are cumulative within a block, not across the turn**, and each
  carries a monotonic ``seq`` and a stable block identity.
- **No duplicate render.** A block that streamed as partials must not be
  re-emitted when the completed assistant message arrives.
"""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    HookEventMessage,
    MirrorErrorMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ac_dc.claude_code.messages import (
    TOOL_INPUT_SUMMARY_CHARS,
    TOOL_RESULT_PREVIEW_CHARS,
    TOOL_RESULT_PREVIEW_LINES,
    TurnTranslator,
    summarise_tool_input,
    truncate_tool_result,
)

REQUEST_ID = "1736956800000-a1b2c3"


@pytest.fixture
def translator():
    """A translator on a fake clock, so durations are assertable."""
    ticks = iter(range(0, 10_000))
    return TurnTranslator(REQUEST_ID, clock=lambda: float(next(ticks)))


def names(events):
    return [event.name for event in events]


def result_message(**overrides):
    """A minimal ``ResultMessage`` with the required fields filled in."""
    fields = {
        "subtype": "success",
        "duration_ms": 1200,
        "duration_api_ms": 900,
        "is_error": False,
        "num_turns": 3,
        "session_id": "c3f1a2b4-5d6e-4f70-8a91-b2c3d4e5f607",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def stream(event, *, parent=None):
    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=parent)


def open_stream_block(translator, index=0, kind="text", message_id="msg_1", parent=None):
    """Replay the framing the CLI sends before the first delta."""
    translator.translate(
        stream({"type": "message_start", "message": {"id": message_id}}, parent=parent)
    )
    translator.translate(
        stream(
            {"type": "content_block_start", "index": index, "content_block": {"type": kind}},
            parent=parent,
        )
    )


# ---------------------------------------------------------------------------
# Dispatch: SystemMessage subclasses must not be swallowed by the parent
# ---------------------------------------------------------------------------


class TestSubclassDispatch:
    def test_all_six_task_and_hook_types_subclass_system_message(self):
        """The hazard this whole ordering exists for, asserted directly."""
        for cls in (
            TaskStartedMessage,
            TaskProgressMessage,
            TaskUpdatedMessage,
            TaskNotificationMessage,
            HookEventMessage,
            MirrorErrorMessage,
        ):
            assert issubclass(cls, SystemMessage)

    def test_task_started_is_a_subagent_event(self, translator):
        events = translator.translate(
            TaskStartedMessage(
                subtype="task_started",
                data={"agent_id": "agent-7"},
                task_id="task-1",
                description="Explore the repo",
                uuid="u",
                session_id="s",
                tool_use_id="toolu_1",
                task_type="Explore",
            )
        )
        assert names(events) == ["subagentEvent"]
        payload = events[0].payload
        assert payload["type"] == "started"
        assert payload["task_id"] == "task-1"
        assert payload["agent_id"] == "agent-7"
        assert payload["tool_use_id"] == "toolu_1"
        assert payload["task_type"] == "Explore"
        assert payload["terminal"] is False

    def test_task_progress_carries_usage_and_last_tool(self, translator):
        events = translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="Explore",
                usage={"total_tokens": 500, "tool_uses": 2, "duration_ms": 900},
                uuid="u",
                session_id="s",
                last_tool_name="Grep",
            )
        )
        payload = events[0].payload
        assert payload["type"] == "progress"
        assert payload["last_tool_name"] == "Grep"
        assert payload["usage"]["total_tokens"] == 500

    def test_task_updated_terminal_status_is_flagged(self, translator):
        """A task killed via stop_task() reports this way, with no notification."""
        events = translator.translate(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="task-1",
                patch={"status": "killed"},
                status="killed",
            )
        )
        assert events[0].payload["status"] == "killed"
        assert events[0].payload["terminal"] is True

    def test_task_updated_reads_status_from_the_patch(self, translator):
        """The dataclass only hoists `status` when the patch carried it."""
        events = translator.translate(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="task-1",
                patch={"status": "completed", "description": "Explore"},
            )
        )
        assert events[0].payload["status"] == "completed"
        assert events[0].payload["description"] == "Explore"
        assert events[0].payload["terminal"] is True

    def test_task_updated_non_terminal_status_is_not_flagged(self, translator):
        events = translator.translate(
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="t", patch={"status": "running"}
            )
        )
        assert events[0].payload["terminal"] is False

    def test_task_notification_carries_summary_and_output_file(self, translator):
        events = translator.translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="task-1",
                status="completed",
                output_file="/tmp/out.md",
                summary="Found three call sites",
                uuid="u",
                session_id="s",
            )
        )
        payload = events[0].payload
        assert payload["type"] == "notification"
        assert payload["summary"] == "Found three call sites"
        assert payload["output_file"] == "/tmp/out.md"
        assert payload["terminal"] is True

    def test_hook_event_goes_to_the_debug_channel(self, translator):
        events = translator.translate(
            HookEventMessage(
                subtype="hook_response",
                data={"tool_name": "Edit", "outcome": "allow", "exit_code": 0},
                hook_event_name="PreToolUse",
            )
        )
        assert names(events) == ["hookEvent"]
        payload = events[0].payload
        assert payload["phase"] == "hook_response"
        assert payload["hook_event_name"] == "PreToolUse"
        assert payload["tool_name"] == "Edit"
        assert payload["exit_code"] == 0

    def test_mirror_error_is_session_scoped_health(self, translator):
        """Non-fatal: the turn continues, and the flag reaches the result."""
        events = translator.translate(
            MirrorErrorMessage(
                subtype="mirror_error",
                data={},
                key=["session", "sub"],
                error="disk full",
            )
        )
        assert names(events) == ["engineHealth"]
        assert events[0].turn_scoped is False
        assert events[0].payload["mirror_gap"] is True
        assert events[0].payload["error"] == "disk full"
        assert translator.stats.mirror_gap is True


# ---------------------------------------------------------------------------
# Generic system messages
# ---------------------------------------------------------------------------


class TestSystemMessages:
    def test_init_becomes_session_started(self, translator):
        events = translator.translate(
            SystemMessage(
                subtype="init",
                data={
                    "session_id": "sess-1",
                    "model": "claude-opus-5",
                    "cwd": "/repo",
                    "tools": ["Read", "Edit"],
                    "mcp_servers": [{"name": "ac-dc", "status": "connected"}],
                    "slash_commands": ["review"],
                    "permissionMode": "plan",
                    "somethingNew": 1,
                },
            )
        )
        assert names(events) == ["sessionStarted"]
        payload = events[0].payload
        assert payload["session_id"] == "sess-1"
        assert payload["model"] == "claude-opus-5"
        assert payload["tools"] == ["Read", "Edit"]
        assert payload["permission_mode"] == "plan"
        # Unknown keys survive for the debug view rather than being dropped.
        assert payload["raw"]["somethingNew"] == 1
        assert translator.session_id == "sess-1"

    def test_compact_boundary_reads_nested_metadata(self, translator):
        """There is no CompactBoundary class; the payload is untyped."""
        events = translator.translate(
            SystemMessage(
                subtype="compact_boundary",
                data={
                    "compact_metadata": {
                        "pre_tokens": 150_000,
                        "post_tokens": 40_000,
                        "trigger": "auto",
                    }
                },
            )
        )
        assert names(events) == ["compactionEvent"]
        payload = events[0].payload
        assert payload["stage"] == "compact_boundary"
        assert payload["pre_tokens"] == 150_000
        assert payload["post_tokens"] == 40_000
        assert payload["trigger"] == "auto"

    def test_compact_boundary_tolerates_a_flat_payload(self, translator):
        events = translator.translate(
            SystemMessage(
                subtype="compact_boundary",
                data={"pre_tokens": 10, "post_tokens": 5, "trigger": "manual"},
            )
        )
        assert events[0].payload["pre_tokens"] == 10

    def test_unknown_subtype_reaches_the_generic_channel(self, translator):
        """A CLI upgrade must degrade to shown-but-unstyled, never silent."""
        events = translator.translate(
            SystemMessage(subtype="something_new", data={"a": 1})
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload == {"subtype": "something_new", "data": {"a": 1}}

    def test_conversation_reset_is_not_dropped(self, translator):
        """In the SDK's Message union and in no spec, so it can arrive."""
        events = translator.translate(
            ConversationResetMessage(
                new_conversation_id="conv-2", uuid="u", session_id="s"
            )
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload["subtype"] == "conversation_reset"
        assert events[0].payload["data"]["new_conversation_id"] == "conv-2"

    def test_a_type_we_have_never_seen_is_not_dropped(self, translator):
        class SomethingNew:
            pass

        events = translator.translate(SomethingNew())
        assert names(events) == ["systemEvent"]
        assert events[0].payload["subtype"] == "unknown_message"

    def test_a_broken_message_does_not_abort_the_pump(self, translator):
        """An exception here would kill a turn the engine is still running."""

        class Exploding(SystemMessage):
            @property
            def subtype(self):  # type: ignore[override]
                raise RuntimeError("boom")

        events = translator.translate(Exploding.__new__(Exploding))
        assert events == []


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_fields_pass_through_in_the_sdk_units(self, translator):
        events = translator.translate(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    rate_limit_type="five_hour",
                    resets_at=1736960000,
                    utilization=0.87,
                    raw={"resetsAt": 1736960000},
                ),
                uuid="u",
                session_id="s",
            )
        )
        assert names(events) == ["rateLimit"]
        payload = events[0].payload
        assert payload["status"] == "allowed_warning"
        assert payload["rate_limit_type"] == "five_hour"
        # Unix seconds, not ISO and not milliseconds.
        assert payload["resets_at"] == 1736960000
        assert payload["utilization"] == 0.87
        assert payload["raw"] == {"resetsAt": 1736960000}


# ---------------------------------------------------------------------------
# Streaming partials and block identity
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_deltas_accumulate_within_the_block(self, translator):
        open_stream_block(translator)
        first = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hel"},
                }
            )
        )
        second = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "lo"},
                }
            )
        )
        assert names(first) == names(second) == ["streamChunk"]
        assert first[0].payload["content"] == "Hel"
        # Cumulative within the block — not a delta, not the whole turn.
        assert second[0].payload["content"] == "Hello"
        assert first[0].payload["block_id"] == second[0].payload["block_id"]

    def test_seq_starts_at_zero_and_increments(self, translator):
        open_stream_block(translator)
        seqs = []
        for text in ("a", "b", "c"):
            events = translator.translate(
                stream(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
                )
            )
            seqs.append(events[0].payload["seq"])
        assert seqs == [0, 1, 2]

    def test_block_identity_is_request_scoped_and_monotonic(self, translator):
        open_stream_block(translator, index=0)
        translator.translate(
            stream(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text"},
                }
            )
        )
        first = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "a"},
                }
            )
        )
        second = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "b"},
                }
            )
        )
        assert first[0].payload["block_id"] == f"{REQUEST_ID}:b0"
        assert second[0].payload["block_id"] == f"{REQUEST_ID}:b1"

    def test_thinking_deltas_use_the_thinking_channel(self, translator):
        open_stream_block(translator, kind="thinking")
        events = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                }
            )
        )
        assert names(events) == ["thinkingChunk"]
        assert events[0].payload["content"] == "hmm"

    def test_content_block_stop_marks_the_block_done(self, translator):
        open_stream_block(translator)
        translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                }
            )
        )
        events = translator.translate(stream({"type": "content_block_stop", "index": 0}))
        assert names(events) == ["streamChunk"]
        assert events[0].payload["done"] is True

    def test_input_json_delta_is_ignored_in_this_phase(self, translator):
        """The card carries the complete input from the assistant message."""
        open_stream_block(translator, kind="tool_use")
        events = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"a"'},
                }
            )
        )
        assert events == []

    def test_framing_events_emit_nothing(self, translator):
        for event in (
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "message_delta", "delta": {}},
            {"type": "message_stop"},
            {"type": "something_new"},
        ):
            assert translator.translate(stream(event)) == []

    def test_two_agents_streaming_at_once_do_not_collide(self, translator):
        """Partial blocks are keyed per agent scope, not per event uuid."""
        open_stream_block(translator, index=0, message_id="msg_main")
        open_stream_block(
            translator, index=0, message_id="msg_sub", parent="toolu_task_1"
        )
        main = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "main"},
                }
            )
        )
        sub = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "sub"},
                },
                parent="toolu_task_1",
            )
        )
        assert main[0].payload["block_id"] != sub[0].payload["block_id"]
        assert main[0].payload["content"] == "main"
        assert sub[0].payload["content"] == "sub"


# ---------------------------------------------------------------------------
# Assistant messages
# ---------------------------------------------------------------------------


class TestAssistantMessages:
    def test_text_block_without_partials_emits_one_chunk(self, translator):
        events = translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Hello")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert names(events) == ["streamChunk"]
        assert events[0].payload["content"] == "Hello"
        assert events[0].payload["done"] is True

    def test_a_streamed_block_is_not_rendered_twice(self, translator):
        """The browser must not draw a second copy of text it animated."""
        open_stream_block(translator, message_id="msg_1")
        translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                }
            )
        )
        translator.translate(stream({"type": "content_block_stop", "index": 0}))
        events = translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Hello")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert events == []
        assert len(translator.rendered_blocks()) == 1

    def test_a_dropped_delta_is_corrected_by_the_completed_message(self, translator):
        """The completed content is authoritative; a hole must not persist."""
        open_stream_block(translator, message_id="msg_1")
        translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hel"},
                }
            )
        )
        events = translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Hello")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert names(events) == ["streamChunk"]
        assert events[0].payload["content"] == "Hello"
        assert events[0].payload["block_id"] == f"{REQUEST_ID}:b0"

    def test_thinking_block_uses_the_thinking_channel(self, translator):
        events = translator.translate(
            AssistantMessage(
                content=[ThinkingBlock(thinking="reasoning", signature="sig")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert names(events) == ["thinkingChunk"]

    def test_mixed_blocks_keep_arrival_order(self, translator):
        events = translator.translate(
            AssistantMessage(
                content=[
                    TextBlock(text="Let me look."),
                    ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "a.py"}),
                    TextBlock(text="Done."),
                ],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert names(events) == ["streamChunk", "toolUse", "streamChunk"]

    def test_unknown_block_kind_is_rendered_not_dropped(self, translator):
        class AdvisorToolResultBlock:
            def __init__(self):
                self.advice = "consider this"

        events = translator.translate(
            AssistantMessage(
                content=[AdvisorToolResultBlock()],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert names(events) == ["streamChunk"]
        content = events[0].payload["content"]
        assert "AdvisorToolResultBlock" in content
        assert "consider this" in content

    def test_assistant_error_reaches_the_generic_channel(self, translator):
        events = translator.translate(
            AssistantMessage(
                content=[],
                model="claude-opus-5",
                message_id="msg_1",
                error="authentication_failed",
            )
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload["data"]["error"] == "authentication_failed"

    def test_response_text_concatenates_text_blocks_in_order(self, translator):
        translator.translate(
            AssistantMessage(
                content=[
                    TextBlock(text="one "),
                    ThinkingBlock(thinking="ignored", signature="s"),
                    TextBlock(text="two"),
                ],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        assert translator.response_text() == "one two"


# ---------------------------------------------------------------------------
# Tool cards
# ---------------------------------------------------------------------------


class TestToolCards:
    def _use(self, translator, **overrides):
        block = ToolUseBlock(
            id=overrides.get("id", "toolu_1"),
            name=overrides.get("name", "Edit"),
            input=overrides.get("input", {"file_path": "src/a.py", "old_string": "x"}),
        )
        return translator.translate(
            AssistantMessage(content=[block], model="m", message_id="msg_1")
        )

    def test_card_uses_the_tool_use_id_as_its_block_identity(self, translator):
        events = self._use(translator)
        assert names(events) == ["toolUse"]
        assert events[0].payload["tool_use_id"] == "toolu_1"

    def test_card_summarises_the_input_and_keeps_the_full_copy(self, translator):
        events = self._use(translator)
        payload = events[0].payload
        assert "file_path=src/a.py" in payload["input_summary"]
        assert payload["input"]["old_string"] == "x"
        assert payload["status"] == "pending"
        assert payload["gated"] is False

    def test_a_prompt_before_the_card_makes_the_card_born_gated(self, translator):
        """The control request can arrive before the assistant message.

        The SDK dispatches ``can_use_tool`` in a detached task, so ordering
        against the message stream is not guaranteed. A card that only
        learned it was gated by being patched afterwards would render
        ungated in exactly this case.
        """
        translator.note_permission_prompt("toolu_1")
        events = self._use(translator)
        assert events[0].payload["gated"] is True

    def test_a_prompt_after_the_card_patches_it(self, translator):
        self._use(translator)
        translator.note_permission_prompt("toolu_1")
        assert translator._blocks["toolu_1"].tool["gated"] is True

    def test_a_prompt_for_an_unseen_tool_does_not_gate_the_others(self, translator):
        translator.note_permission_prompt("toolu_other")
        assert self._use(translator)[0].payload["gated"] is False

    def test_mcp_tools_name_their_server(self, translator):
        events = self._use(translator, name="mcp__ac-dc__symbol_map", input={})
        assert events[0].payload["server"] == "ac-dc"

    def test_builtin_tools_have_no_server(self, translator):
        assert self._use(translator)[0].payload["server"] is None

    def test_subagent_calls_carry_their_agent_scope(self, translator):
        events = translator.translate(
            AssistantMessage(
                content=[ToolUseBlock(id="toolu_9", name="Grep", input={})],
                model="m",
                message_id="msg_2",
                parent_tool_use_id="toolu_task_1",
            )
        )
        assert events[0].payload["agent_id"] == "toolu_task_1"

    def test_a_redelivered_message_does_not_duplicate_the_card(self, translator):
        self._use(translator)
        assert self._use(translator) == []

    def test_result_attaches_by_id_and_times_the_call(self, translator):
        self._use(translator)
        events = translator.translate(
            UserMessage(
                content=[ToolResultBlock(tool_use_id="toolu_1", content="ok")],
            )
        )
        assert names(events) == ["toolResult"]
        payload = events[0].payload
        assert payload["tool_use_id"] == "toolu_1"
        assert payload["status"] == "ok"
        assert payload["preview"] == "ok"
        assert payload["truncated"] is False
        assert payload["duration_ms"] > 0

    def test_error_results_are_flagged(self, translator):
        self._use(translator)
        events = translator.translate(
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="toolu_1", content="nope", is_error=True)
                ]
            )
        )
        assert events[0].payload["status"] == "error"

    def test_mutating_tools_report_the_file_they_changed(self, translator):
        self._use(translator)
        translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok")])
        )
        assert translator.stats.files_modified == ["src/a.py"]

    def test_a_failed_write_reports_no_file_change(self, translator):
        self._use(translator)
        translator.translate(
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="toolu_1", content="no", is_error=True)
                ]
            )
        )
        assert translator.stats.files_modified == []

    def test_read_only_tools_report_no_file_change(self, translator):
        self._use(translator, name="Read", input={"file_path": "src/a.py"})
        translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok")])
        )
        assert translator.stats.files_modified == []

    def test_notebook_edit_uses_its_own_input_key(self, translator):
        self._use(
            translator, name="NotebookEdit", input={"notebook_path": "nb.ipynb"}
        )
        translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok")])
        )
        assert translator.stats.files_modified == ["nb.ipynb"]

    def test_a_result_for_an_unknown_call_is_still_emitted(self, translator, caplog):
        """It means the pump missed an assistant message; say so, don't drop."""
        events = translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_ghost")])
        )
        assert names(events) == ["toolResult"]
        assert "toolu_ghost" in caplog.text

    def test_a_repeated_result_is_emitted_once(self, translator):
        self._use(translator)
        block = ToolResultBlock(tool_use_id="toolu_1", content="ok")
        translator.translate(UserMessage(content=[block]))
        assert translator.translate(UserMessage(content=[block])) == []

    def test_list_content_is_flattened_for_the_preview(self, translator):
        self._use(translator)
        events = translator.translate(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="toolu_1",
                        content=[{"type": "text", "text": "line one"}],
                    )
                ]
            )
        )
        assert events[0].payload["preview"] == "line one"

    def test_replayed_block_list_carries_the_card(self, translator):
        """A client reconnecting after the result must not see "pending"."""
        self._use(translator)
        translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok")])
        )
        block = next(
            b for b in translator.rendered_blocks() if b["block_id"] == "toolu_1"
        )
        assert block["kind"] == "tool"
        assert block["tool"]["status"] == "ok"
        assert block["tool"]["result"]["preview"] == "ok"

    def test_tool_calls_are_counted(self, translator):
        self._use(translator, id="toolu_1")
        self._use(translator, id="toolu_2")
        assert translator.stats.tool_calls == 2


# ---------------------------------------------------------------------------
# User messages
# ---------------------------------------------------------------------------


class TestUserMessages:
    def test_a_replayed_string_message_renders_nothing(self, translator):
        """The browser drew it before the turn started."""
        events = translator.translate(UserMessage(content="hello", uuid="msg-uuid-1"))
        assert events == []
        assert translator.user_message_id == "msg-uuid-1"

    def test_a_replayed_human_message_is_recognised_by_origin(self, translator):
        events = translator.translate(
            UserMessage(
                content=[TextBlock(text="hello")],
                uuid="msg-uuid-2",
                origin={"kind": "human"},
            )
        )
        assert events == []
        assert translator.user_message_id == "msg-uuid-2"

    def test_only_the_first_user_message_id_is_kept(self, translator):
        translator.translate(UserMessage(content="one", uuid="first"))
        translator.translate(UserMessage(content="two", uuid="second"))
        assert translator.user_message_id == "first"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class TestResult:
    def test_footer_carries_usage_cost_and_pump_counts(self, translator):
        translator.translate(
            AssistantMessage(content=[TextBlock(text="Hi")], model="m", message_id="m1")
        )
        events = translator.translate(
            result_message(
                usage={"input_tokens": 100, "output_tokens": 20},
                model_usage={"claude-opus-5": {"inputTokens": 100, "costUSD": 0.01}},
                total_cost_usd=0.01,
                terminal_reason="completed",
            )
        )
        assert names(events) == ["streamComplete"]
        payload = events[0].payload
        assert payload["response"] == "Hi"
        assert payload["num_turns"] == 3
        assert payload["duration_ms"] == 1200
        assert payload["usage"]["input_tokens"] == 100
        assert payload["model_usage"]["claude-opus-5"]["costUSD"] == 0.01
        assert payload["total_cost_usd"] == 0.01
        assert payload["cancelled"] is False
        assert payload["tool_calls"] == 0
        assert translator.complete is True

    def test_a_missing_cost_stays_missing_rather_than_becoming_zero(self, translator):
        """The CLI's wire schema types ``total_cost_usd`` as a plain number
        with no null branch, so a live result always carries a figure — but
        the translator also replays turns AC⚡DC wrote itself, where cost was
        never recorded. Defaulting that to 0.0 would render as "free"."""
        events = translator.translate(result_message(total_cost_usd=None))
        assert events[0].payload["total_cost_usd"] is None

    def test_the_cumulative_reading_is_passed_through_untouched(self, translator):
        """A translator is one turn's worth of state, and this turn's share
        of the cost is a difference against the *previous* turn's result —
        so ``EngineSession`` owns that arithmetic and this figure stays the
        engine's running total, under the engine's own name."""
        events = translator.translate(result_message(total_cost_usd=0.30))
        assert events[0].payload["total_cost_usd"] == 0.30
        assert "turn_cost_usd" not in events[0].payload

    @pytest.mark.parametrize(
        "reason", ["aborted_streaming", "aborted_tools"]
    )
    def test_abort_terminal_reasons_mark_the_turn_cancelled(self, translator, reason):
        events = translator.translate(result_message(terminal_reason=reason))
        assert events[0].payload["cancelled"] is True

    def test_an_interrupt_flag_marks_the_turn_cancelled(self, translator):
        """Set by the session before the result arrives."""
        translator.cancelled = True
        events = translator.translate(result_message(terminal_reason="completed"))
        assert events[0].payload["cancelled"] is True

    def test_a_completed_turn_is_not_cancelled(self, translator):
        events = translator.translate(result_message(terminal_reason="completed"))
        assert events[0].payload["cancelled"] is False

    def test_optional_error_fields_appear_only_when_present(self, translator):
        plain = translator.translate(result_message())[0].payload
        assert "api_error_status" not in plain
        assert "permission_denials" not in plain

        other = TurnTranslator(REQUEST_ID)
        payload = other.translate(
            result_message(
                is_error=True, api_error_status=529, errors=["overloaded"]
            )
        )[0].payload
        assert payload["api_error_status"] == 529
        assert payload["errors"] == ["overloaded"]

    def test_deferred_tool_use_warns_because_our_hooks_are_observational(
        self, translator, caplog
    ):
        translator.translate(result_message(deferred_tool_use={"toolUseId": "t"}))
        assert "deferred_tool_use" in caplog.text

    def test_mirror_gap_reaches_the_footer(self, translator):
        translator.translate(
            MirrorErrorMessage(subtype="mirror_error", data={}, error="disk full")
        )
        events = translator.translate(result_message())
        assert events[0].payload["mirror_gap"] is True

    def test_user_message_id_reaches_the_footer_for_undo(self, translator):
        translator.translate(UserMessage(content="hi", uuid="msg-uuid-1"))
        events = translator.translate(result_message())
        assert events[0].payload["user_message_id"] == "msg-uuid-1"

    def test_result_text_is_used_when_no_assistant_text_arrived(self, translator):
        """Local slash-command results come back this way."""
        events = translator.translate(result_message(result="done"))
        assert events[0].payload["response"] == "done"


# ---------------------------------------------------------------------------
# Truncation helpers
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_input_summary_is_one_line(self):
        summary = summarise_tool_input({"command": "echo one\ntwo\tthree"})
        assert "\n" not in summary
        assert "\t" not in summary

    def test_input_summary_is_capped(self):
        summary = summarise_tool_input({"command": "x" * 500})
        assert len(summary) == TOOL_INPUT_SUMMARY_CHARS
        assert summary.endswith("…")

    def test_input_summary_of_nothing_is_empty(self):
        assert summarise_tool_input(None) == ""
        assert summarise_tool_input({}) == ""

    def test_non_string_values_are_json_encoded(self):
        assert summarise_tool_input({"edits": [1, 2]}) == "edits=[1, 2]"

    def test_result_preview_caps_on_characters(self):
        preview, truncated = truncate_tool_result("x" * (TOOL_RESULT_PREVIEW_CHARS + 10))
        assert truncated is True
        assert len(preview) == TOOL_RESULT_PREVIEW_CHARS

    def test_result_preview_caps_on_lines(self):
        preview, truncated = truncate_tool_result(
            "\n".join(str(i) for i in range(TOOL_RESULT_PREVIEW_LINES + 20))
        )
        assert truncated is True
        assert len(preview.splitlines()) == TOOL_RESULT_PREVIEW_LINES

    def test_short_results_are_untouched(self):
        assert truncate_tool_result("fine") == ("fine", False)

    def test_full_bytes_measures_the_untruncated_result(self, translator):
        translator.translate(
            AssistantMessage(
                content=[ToolUseBlock(id="toolu_1", name="Bash", input={})],
                model="m",
                message_id="m1",
            )
        )
        body = "é" * (TOOL_RESULT_PREVIEW_CHARS + 100)
        events = translator.translate(
            UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content=body)])
        )
        assert events[0].payload["truncated"] is True
        assert events[0].payload["full_bytes"] == len(body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Reconnect replay
# ---------------------------------------------------------------------------


class TestReplay:
    def test_rendered_blocks_describe_the_turn_so_far(self, translator):
        translator.translate(
            AssistantMessage(
                content=[
                    TextBlock(text="Looking."),
                    ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "a"}),
                ],
                model="m",
                message_id="msg_1",
            )
        )
        blocks = translator.rendered_blocks()
        assert [b["kind"] for b in blocks] == ["text", "tool"]
        assert blocks[0]["content"] == "Looking."
        assert blocks[0]["seq"] == 0
        # Serialisable, because it goes over the wire on reconnect.
        json.dumps(blocks)

    def test_permission_prompts_are_recorded_by_the_gate_not_a_message(
        self, translator
    ):
        translator.note_permission_prompt()
        translator.note_permission_prompt()
        events = translator.translate(result_message())
        assert events[0].payload["permission_prompts"] == 2
