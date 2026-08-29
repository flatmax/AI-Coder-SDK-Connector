"""Tests for aic_dc.claude_code.messages — conversion phase 1.

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

from aic_dc.claude_code.messages import (
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
        """The payload shape, with both types as the CLI really reports them.

        `task_type` is the transport kind and `subagent_type` the agent's own;
        the field names are one letter apart in meaning and completely apart in
        value, so both are pinned here. Taken from a headless CLI capture on
        2026-08-25: `{"task_type": "local_agent", "subagent_type": "Explore"}`.
        """
        events = translator.translate(
            TaskStartedMessage(
                subtype="task_started",
                data={"agent_id": "agent-7", "subagent_type": "Explore"},
                task_id="task-1",
                description="Explore the repo",
                uuid="u",
                session_id="s",
                tool_use_id="toolu_1",
                task_type="local_agent",
            )
        )
        assert names(events) == ["subagentEvent"]
        payload = events[0].payload
        assert payload["type"] == "started"
        assert payload["task_id"] == "task-1"
        assert payload["agent_id"] == "agent-7"
        assert payload["tool_use_id"] == "toolu_1"
        assert payload["task_type"] == "local_agent"
        assert payload["subagent_type"] == "Explore"
        assert payload["terminal"] is False

    def test_subagent_type_is_read_off_the_raw_payload(self, translator):
        """It has no dataclass field, so only `data`/`patch` carry it.

        The browser labels every subagent with it — the row's chip, the tab
        strip, the Stop confirmation — and the fallback when it is missing is
        the description alone, never `task_type`: labelling with the transport
        kind put "local_agent" on every row alike.
        """
        translator.translate(
            TaskStartedMessage(
                subtype="task_started",
                data={"agent_id": "agent-7", "subagent_type": "general-purpose"},
                task_id="task-1",
                description="Write the tests",
                uuid="u",
                session_id="s",
                tool_use_id="toolu_1",
                task_type="local_agent",
            )
        )
        # And it survives the events that do not repeat it: a notification
        # carries neither type, and the row must not lose its label.
        translator.translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="task-1",
                status="completed",
                output_file="/tmp/task-1.output",
                summary="Wrote 4 tests.",
                uuid="u",
                session_id="s",
            )
        )
        (row,) = translator.rendered_subagents()
        assert row["subagent_type"] == "general-purpose"
        assert row["task_type"] == "local_agent"
        assert row["summary"] == "Wrote 4 tests."

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
                    "mcp_servers": [{"name": "aic-dc", "status": "connected"}],
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

    def test_a_compacting_status_is_the_live_start_signal(self, translator):
        """The stream's only report that a compaction is running *now*.

        The ``PreCompact`` hook fires for the CLI's speculative background
        compaction too, so it cannot be read as "the session is waiting".
        This frame can.
        """
        events = translator.translate(
            SystemMessage(subtype="status", data={"status": "compacting"})
        )
        assert names(events) == ["compactionEvent"]
        assert events[0].payload["stage"] == "compaction_started"

    def test_a_failed_compaction_is_reported_nowhere_else(self, translator):
        """No boundary is written when compaction fails, so this is it."""
        events = translator.translate(
            SystemMessage(
                subtype="status",
                data={
                    "status": None,
                    "compact_result": "failed",
                    "compact_error": "Conversation too long",
                },
            )
        )
        assert names(events) == ["compactionEvent"]
        payload = events[0].payload
        assert payload["stage"] == "compaction_ended"
        assert payload["result"] == "failed"
        assert payload["error"] == "Conversation too long"

    def test_a_failure_with_no_reason_is_still_a_failure(self, translator):
        """``compact_error`` is gated on the CLI side; the result is not."""
        events = translator.translate(
            SystemMessage(
                subtype="status", data={"status": None, "compact_result": "failed"}
            )
        )
        assert events[0].payload["result"] == "failed"
        assert events[0].payload["error"] is None

    def test_a_successful_end_retracts_the_indicator(self, translator):
        events = translator.translate(
            SystemMessage(
                subtype="status", data={"status": None, "compact_result": "success"}
            )
        )
        assert names(events) == ["compactionEvent"]
        assert events[0].payload["stage"] == "compaction_ended"
        assert events[0].payload["result"] == "success"

    def test_a_status_that_is_not_about_compaction_stays_generic(self, translator):
        """``requesting`` fires on ordinary API calls, and a permission-mode
        change rides the same subtype. Neither is the compaction channel's."""
        for data in (
            {"status": "requesting"},
            {"status": None, "permissionMode": "acceptEdits"},
            {"status": "something_new"},
        ):
            events = translator.translate(SystemMessage(subtype="status", data=data))
            assert names(events) == ["systemEvent"]
            assert events[0].payload["subtype"] == "status"

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
        # And each chunk says whose it is. A subagent narrates in text, not
        # only in tool calls, so the browser needs the scope to render that
        # text under the subagent's row and in the subagent's own tab
        # instead of at turn level.
        assert main[0].payload["agent_id"] is None
        assert sub[0].payload["agent_id"] == "toolu_task_1"

    def test_a_subagents_thinking_carries_its_scope_too(self, translator):
        open_stream_block(
            translator, kind="thinking", message_id="msg_sub", parent="toolu_task_1"
        )
        events = translator.translate(
            stream(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                },
                parent="toolu_task_1",
            )
        )
        assert events[0].payload["agent_id"] == "toolu_task_1"


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
        # The model's own trailing space already separates these, so they
        # join as written — no invented paragraph break inside one message.
        assert translator.response_text() == "one two"

    def test_two_runs_that_would_otherwise_glue_get_a_blank_line(
        self, translator
    ):
        """An agentic turn speaks once before each tool call, and those blocks
        carry no trailing whitespace to join on. Concatenating them produced
        `"I'll read the file.SPAWNED"`, which is what the copy button copies
        and the 🔊 button reads."""
        translator.translate(
            AssistantMessage(
                content=[TextBlock(text="I'll read the file.")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        translator.translate(
            AssistantMessage(
                content=[TextBlock(text="SPAWNED")],
                model="claude-opus-5",
                message_id="msg_2",
            )
        )
        assert translator.response_text() == "I'll read the file.\n\nSPAWNED"

    def test_an_empty_text_block_does_not_add_a_separator(self, translator):
        translator.translate(
            AssistantMessage(
                content=[TextBlock(text="only this")],
                model="claude-opus-5",
                message_id="msg_1",
            )
        )
        translator.translate(
            AssistantMessage(
                content=[TextBlock(text="")],
                model="claude-opus-5",
                message_id="msg_2",
            )
        )
        assert translator.response_text() == "only this"


# ---------------------------------------------------------------------------
# Live token counter
# ---------------------------------------------------------------------------


class TestTurnUsage:
    """The mid-turn counter, which is `AssistantMessage.usage` and nothing else.

    Its webapp counterparts are `webapp/src/turn-cost.test.js` (the reader)
    and the live-counter block in `webapp/src/chat-panel/streaming.test.js`
    (the renderer). Two properties carry the weight here: the payload is a
    **running total for this turn** — never a per-message delta, never the
    session's — and it is accumulated server-side, because the browser has no
    idea which messages it has already seen after a refresh.
    """

    def message(self, usage, *, message_id="msg_1", model="claude-opus-5", **kw):
        return AssistantMessage(
            content=[TextBlock(text="Hi")],
            model=model,
            message_id=message_id,
            usage=usage,
            **kw,
        )

    def usage_of(self, events):
        """The `turnUsage` payload's model map, or None if none was pushed."""
        pushed = [e for e in events if e.name == "turnUsage"]
        return pushed[-1].payload["turn_model_usage"] if pushed else None

    def test_an_assistant_message_pushes_what_it_used(self, translator):
        events = translator.translate(
            self.message({"input_tokens": 900, "output_tokens": 100})
        )
        assert "turnUsage" in names(events)
        assert self.usage_of(events) == {
            "claude-opus-5": {"input_tokens": 900, "output_tokens": 100}
        }

    def test_the_counter_lands_after_the_content_it_priced(self, translator):
        """The card is on screen before the number under it, not the reverse."""
        events = translator.translate(
            AssistantMessage(
                content=[
                    TextBlock(text="Let me look."),
                    ToolUseBlock(id="toolu_1", name="Read", input={"file_path": "a.py"}),
                ],
                model="claude-opus-5",
                message_id="msg_1",
                usage={"input_tokens": 900},
            )
        )
        assert names(events) == ["streamChunk", "toolUse", "turnUsage"]

    def test_all_four_counters_reach_the_browser(self, translator):
        """Cache reads and writes are priced differently from input, so the
        split has to survive the trip — a single total cannot be re-split."""
        events = translator.translate(
            self.message(
                {
                    "input_tokens": 300,
                    "output_tokens": 2000,
                    "cache_creation_input_tokens": 1200,
                    "cache_read_input_tokens": 40_000,
                }
            )
        )
        assert self.usage_of(events)["claude-opus-5"] == {
            "input_tokens": 300,
            "output_tokens": 2000,
            "cache_creation_input_tokens": 1200,
            "cache_read_input_tokens": 40_000,
        }

    def test_the_payload_is_the_turn_so_far_not_the_message(self, translator):
        """An agentic turn is many API calls, and the counter must climb
        across them: pushing one message's usage would make it jump about."""
        translator.translate(self.message({"input_tokens": 900, "output_tokens": 100}))
        events = translator.translate(
            self.message(
                {"input_tokens": 1000, "output_tokens": 50}, message_id="msg_2"
            )
        )
        assert self.usage_of(events) == {
            "claude-opus-5": {"input_tokens": 1900, "output_tokens": 150}
        }

    def test_a_message_seen_twice_is_counted_once(self, translator):
        """A reconnecting SDK client can re-deliver; the second copy would
        otherwise double the turn's tokens for the rest of the turn."""
        translator.translate(self.message({"input_tokens": 900}))
        events = translator.translate(self.message({"input_tokens": 900}))
        assert self.usage_of(events) is None
        assert translator.turn_usage() == {"claude-opus-5": {"input_tokens": 900}}

    def test_a_subagent_is_counted_under_its_own_model(self, translator):
        """Delegation is the expensive part of a delegating turn. A counter
        that skipped subagents would sit still through exactly that part."""
        translator.translate(self.message({"input_tokens": 900}))
        events = translator.translate(
            self.message(
                {"input_tokens": 4000},
                message_id="msg_2",
                model="claude-haiku-4-5",
                parent_tool_use_id="toolu_1",
            )
        )
        assert self.usage_of(events) == {
            "claude-opus-5": {"input_tokens": 900},
            "claude-haiku-4-5": {"input_tokens": 4000},
        }

    def test_no_event_when_the_message_reports_nothing(self, translator):
        """The payload is a running total, so an unchanged one is a repaint
        that says the same thing. Every shape here is one the CLI can send."""
        for index, usage in enumerate(
            (None, {}, "600", {"web_search_requests": 3}, {"input_tokens": 0})
        ):
            events = translator.translate(
                self.message(usage, message_id=f"msg_{index}")
            )
            assert "turnUsage" not in names(events)
        assert translator.turn_usage() == {}

    def test_junk_counters_are_dropped_rather_than_summed(self, translator):
        # `bool` is an `int` in Python; True as a token count is an upstream
        # bug, not one token.
        events = translator.translate(
            self.message(
                {
                    "input_tokens": 900,
                    "output_tokens": "100",
                    "cache_read_input_tokens": -5,
                    "cache_creation_input_tokens": True,
                }
            )
        )
        assert self.usage_of(events) == {"claude-opus-5": {"input_tokens": 900}}

    def test_a_model_the_message_does_not_name_is_still_counted(self, translator):
        """Losing the tokens because the label is missing is the worse of
        the two failures — the count is what the counter is for."""
        events = translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Hi")],
                model="",
                message_id="msg_1",
                usage={"input_tokens": 900},
            )
        )
        assert self.usage_of(events) == {"unknown": {"input_tokens": 900}}

    def test_turn_usage_hands_out_copies(self, translator):
        """`ActiveTurn.to_dict` and every pushed payload share this dict's
        contents; a caller that mutated one would corrupt the turn's count."""
        translator.translate(self.message({"input_tokens": 900}))
        snapshot = translator.turn_usage()
        snapshot["claude-opus-5"]["input_tokens"] = 0
        snapshot["invented"] = {}
        assert translator.turn_usage() == {"claude-opus-5": {"input_tokens": 900}}

    def test_a_message_with_no_id_is_counted_rather_than_skipped(self, translator):
        """Nothing to dedupe on. Under-counting a turn is worse than the
        double it would protect against, so it counts."""
        events = translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Hi")],
                model="claude-opus-5",
                usage={"input_tokens": 900},
            )
        )
        assert self.usage_of(events) == {"claude-opus-5": {"input_tokens": 900}}

    def test_the_counter_is_turn_scoped_like_every_streaming_event(self, translator):
        """Without the request ID the browser cannot tell whose turn it is,
        and a collaborator's tokens would land on this card."""
        events = translator.translate(self.message({"input_tokens": 900}))
        pushed = [e for e in events if e.name == "turnUsage"][0]
        assert pushed.turn_scoped is True


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

    def test_card_carries_the_input_and_no_summary_of_it(self, translator):
        """The header's one-liner is the browser's to render (§ C3).

        The card used to carry both the dict and a pre-joined `key=value`
        string built from it. Only the dict crosses now — the string was the
        copy that could not shorten a repo path, because the rule for that
        is in the browser.
        """
        events = self._use(translator)
        payload = events[0].payload
        assert "input_summary" not in payload
        assert payload["input"]["old_string"] == "x"
        assert payload["status"] == "pending"
        assert payload["gated"] is False

    def test_the_card_says_when_the_call_was_made(self):
        """The one time fact a *pending* card can carry.

        ``duration_ms`` arrives with the result, so until then a call that
        has hung and a call that answered in 30ms render identically — the
        gap ``invoked_at`` closes.
        """
        translator = TurnTranslator(REQUEST_ID, wall_clock=lambda: 1_772_080_327.5)
        payload = self._use(translator)[0].payload
        assert payload["invoked_at"] == "2026-02-26T04:32:07.500000+00:00"

    def test_the_invocation_time_is_utc_with_an_offset(self):
        """A collaborating browser may not be in this machine's timezone.

        A naive local string would have a participant two zones away reading
        a stall that had not happened yet.
        """
        translator = TurnTranslator(REQUEST_ID, wall_clock=lambda: 0.0)
        assert self._use(translator)[0].payload["invoked_at"] == (
            "1970-01-01T00:00:00+00:00"
        )

    def test_an_unusable_clock_reading_leaves_the_time_out(self):
        """The card is worth more with no time than with a wrong one.

        The browser already treats an absent ``invoked_at`` as "no time to
        show", so the degraded card renders without a chip rather than with a
        nonsense one.
        """
        translator = TurnTranslator(REQUEST_ID, wall_clock=lambda: float("nan"))
        payload = self._use(translator)[0].payload
        assert payload["invoked_at"] == ""
        # The rest of the card is unharmed — one bad clock is not a lost call.
        assert payload["tool_use_id"] == "toolu_1"
        assert payload["status"] == "pending"

    def test_the_wall_clock_is_not_the_duration_clock(self):
        """Two clocks, because only one of them may step.

        A duration read off a wall clock can report a result arriving before
        its own request when NTP corrects mid-call; an ``invoked_at`` read off
        a monotonic clock is a process-local number no browser can turn into
        a time of day. Neither is derivable from the other.
        """
        translator = TurnTranslator(
            REQUEST_ID, clock=lambda: 100.0, wall_clock=lambda: 1_772_080_327.0
        )
        assert self._use(translator)[0].payload["invoked_at"].startswith("2026-02-26")
        # A monotonic reading of 100.0 would have been 1970.

    def test_a_reconnecting_client_re_reads_the_time_off_the_block(self, translator):
        """The chip has to survive a refresh mid-call, which is when it matters.

        A reconnecting client renders from the block list, not from the
        ``toolUse`` event it was not there for.
        """
        self._use(translator)
        replayed = translator._blocks["toolu_1"].to_dict()
        assert replayed["tool"]["invoked_at"]

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
        events = self._use(translator, name="mcp__aic-dc__symbol_map", input={})
        assert events[0].payload["server"] == "aic-dc"

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
        the translator also replays turns AIC⚡DC wrote itself, where cost was
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
    # `summarise_tool_input` is the permission layer's one-liner now, not the
    # tool card's header — that one is built in the browser from the card's
    # `input` (§ C3). These still pin it, because `summarise_request` and
    # `build_command_payload` both fall back to it.
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

    def test_replayed_blocks_say_which_scope_produced_them(self, translator):
        """Without it a reconnected subagent tab cannot claim its own feed."""
        translator.translate(
            AssistantMessage(
                content=[TextBlock(text="Delegating.")],
                model="m",
                message_id="msg_1",
            )
        )
        translator.translate(
            AssistantMessage(
                content=[
                    TextBlock(text="reading the parser"),
                    ToolUseBlock(id="toolu_9", name="Grep", input={}),
                ],
                model="m",
                message_id="msg_2",
                parent_tool_use_id="toolu_task_1",
            )
        )
        scopes = {b["content"] or b["block_id"]: b["agent_id"]
                  for b in translator.rendered_blocks()}
        assert scopes["Delegating."] is None
        assert scopes["reading the parser"] == "toolu_task_1"
        assert scopes["toolu_9"] == "toolu_task_1"

    def test_rendered_subagents_accumulate_across_the_task_messages(
        self, translator
    ):
        """The snapshot is the row, not the last event: fields come from all four."""
        translator.translate(
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
        translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="",
                usage={"total_tokens": 500, "tool_uses": 2, "duration_ms": 900},
                uuid="u",
                session_id="s",
                last_tool_name="Grep",
            )
        )
        rows = translator.rendered_subagents()
        assert len(rows) == 1
        row = rows[0]
        assert row["key"] == "task-1"
        # The progress message carried no description; the label survives it.
        assert row["description"] == "Explore the repo"
        assert row["agent_id"] == "agent-7"
        assert row["tool_use_id"] == "toolu_1"
        assert row["last_tool_name"] == "Grep"
        assert row["usage"]["tool_uses"] == 2
        assert row["terminal"] is False
        # Goes over the wire inside `active_streams`.
        json.dumps(rows)

    def test_the_row_keeps_the_task_not_the_activity(self, translator):
        """`description` is two things on the wire, and only one is the task.

        `task_started` names it; every `task_progress` overwrites it with what
        the subagent is doing right now. Patching left a reconnecting browser
        headed with whatever it happened to be doing when the socket came
        back — and the activity is already reported as `last_tool_name`.
        """
        translator.translate(
            TaskStartedMessage(
                subtype="task_started",
                data={"agent_id": "agent-7", "subagent_type": "Explore"},
                task_id="task-1",
                description="Find magic word in README",
                uuid="u",
                session_id="s",
                tool_use_id="toolu_1",
                task_type="local_agent",
            )
        )
        translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="Reading README.md",
                usage={"total_tokens": 500, "tool_uses": 1, "duration_ms": 900},
                uuid="u",
                session_id="s",
                last_tool_name="Read",
            )
        )
        (row,) = translator.rendered_subagents()
        assert row["description"] == "Find magic word in README"
        assert row["last_tool_name"] == "Read"

    def test_a_row_opened_by_a_progress_message_still_takes_a_description(
        self, translator
    ):
        """First non-empty wins, not "only `started` may set it" — a row whose
        start was missed must still be able to name itself."""
        translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="Reading README.md",
                usage={"total_tokens": 1, "tool_uses": 1, "duration_ms": 1},
                uuid="u",
                session_id="s",
                last_tool_name="Read",
            )
        )
        (row,) = translator.rendered_subagents()
        assert row["description"] == "Reading README.md"

    def test_a_finished_subagent_stays_finished(self, translator):
        """A trailing non-terminal message must not revive a task that ended."""
        translator.translate(
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
        translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="",
                usage=None,
                uuid="u",
                session_id="s",
                last_tool_name="Read",
            )
        )
        row = translator.rendered_subagents()[0]
        assert row["terminal"] is True
        assert row["status"] == "completed"
        assert row["summary"] == "Found three call sites"

    def test_each_task_gets_its_own_row(self, translator):
        for task_id in ("task-1", "task-2"):
            translator.translate(
                TaskStartedMessage(
                    subtype="task_started",
                    data={},
                    task_id=task_id,
                    description=f"Work on {task_id}",
                    uuid="u",
                    session_id="s",
                )
            )
        rows = translator.rendered_subagents()
        assert [r["key"] for r in rows] == ["task-1", "task-2"]

    def test_the_snapshot_is_a_copy(self, translator):
        """A caller mutating the payload must not corrupt the turn's own row."""
        translator.translate(
            TaskStartedMessage(
                subtype="task_started",
                data={},
                task_id="task-1",
                description="Explore",
                uuid="u",
                session_id="s",
            )
        )
        translator.rendered_subagents()[0]["description"] = "clobbered"
        assert translator.rendered_subagents()[0]["description"] == "Explore"

    def test_permission_prompts_are_recorded_by_the_gate_not_a_message(
        self, translator
    ):
        translator.note_permission_prompt()
        translator.note_permission_prompt()
        events = translator.translate(result_message())
        assert events[0].payload["permission_prompts"] == 2


# ---------------------------------------------------------------------------
# Tasks that are not subagents
# ---------------------------------------------------------------------------


class TestNonSubagentTasks:
    """A slow ``Bash`` command is a task, and it is not a delegation.

    Found live on 2026-08-17: a turn that delegated four subagents produced
    twenty rows, sixteen of them shell commands the CLI had backgrounded. Every
    surface downstream — the indented row, the tab, the LED, the reconnect
    snapshot — reads one filter, so these assert the filter and not each
    surface.
    """

    def _started(self, task_id, task_type, description="sleep 9"):
        return TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id=task_id,
            description=description,
            uuid="u",
            session_id="s",
            task_type=task_type,
        )

    def test_a_backgrounded_bash_command_is_neither_event_nor_row(
        self, translator
    ):
        events = translator.translate(self._started("b1", "local_bash"))
        assert events == []
        assert translator.rendered_subagents() == []

    def test_the_task_id_is_latched_because_only_the_first_event_types_it(
        self, translator
    ):
        """`TaskProgressMessage` has no `task_type` field at all."""
        translator.translate(self._started("b1", "local_bash"))
        progress = translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="b1",
                description="",
                usage={"total_tokens": 12, "tool_uses": 1, "duration_ms": 9000},
                uuid="u",
                session_id="s",
                last_tool_name="Bash",
            )
        )
        # And the notification the CLI sends when the command returns arrives
        # with `task_type=None` — the shape that reopened this as a tab.
        notification = translator.translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="b1",
                status="completed",
                output_file=None,
                summary=None,
                uuid="u",
                session_id="s",
            )
        )
        assert progress == []
        assert notification == []
        assert translator.rendered_subagents() == []

    def test_a_real_subagent_is_untouched(self, translator):
        events = translator.translate(
            self._started("task-1", "local_agent", description="Explore")
        )
        assert names(events) == ["subagentEvent"]
        assert [r["key"] for r in translator.rendered_subagents()] == ["task-1"]

    def test_a_task_with_no_type_at_all_is_still_a_subagent(self, translator):
        """The filter names the kinds it drops; anything else stays a row.

        The SDK types `task_type` as `str | None` and documents no vocabulary,
        so an unrecognised or absent type must fall through to a visible row
        rather than be silently dropped.
        """
        events = translator.translate(self._started("task-1", None, "Explore"))
        assert names(events) == ["subagentEvent"]
        assert len(translator.rendered_subagents()) == 1

    def test_a_bash_task_interleaved_with_a_subagent_disturbs_nothing(
        self, translator
    ):
        """The live shape: subagents shell out, so the two streams interleave."""
        translator.translate(self._started("task-1", "local_agent", "Explore"))
        translator.translate(self._started("b1", "local_bash"))
        translator.translate(
            TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="task-1",
                description="",
                usage=None,
                uuid="u",
                session_id="s",
                last_tool_name="Grep",
            )
        )
        translator.translate(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="b1",
                status="completed",
                output_file=None,
                summary=None,
                uuid="u",
                session_id="s",
            )
        )
        rows = translator.rendered_subagents()
        assert [r["key"] for r in rows] == ["task-1"]
        assert rows[0]["last_tool_name"] == "Grep"
        assert rows[0]["terminal"] is False

    def test_the_latch_keys_on_the_task_id_not_the_tool_use_id(self, translator):
        """Two bash tasks under one `Bash` call must not merge or leak.

        `_task_key` prefers `task_id`, which is the only id every `Task*`
        message carries; the ids here are distinct so a second command's
        events cannot be filed under the first's.
        """
        translator.translate(self._started("b1", "local_bash"))
        events = translator.translate(self._started("b2", "local_bash"))
        assert events == []
        assert translator.rendered_subagents() == []
