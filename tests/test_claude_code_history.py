"""Tests for reading a mirrored transcript back as renderable messages.

The transcript is the CLI's format, not ours, so the fixtures here are
built to match what real ``sdk-py``-written transcripts on disk actually
contain — in particular: one entry per content block with ``usage``
repeated across the split, tool results arriving as ``user`` entries, and
**no result entry anywhere**. A test that fed this module a tidier shape
than the CLI produces would pass while the browser showed nothing.

What these assert, in order of what breaks the UI worst:

- the message taxonomy — which entries start a turn and which attach to one
- the footer, rebuilt from usage rather than from a result entry that does
  not exist
- the fields the panel reads by exact name, snake_case and camelCase alike
- what is deliberately absent: no cost, no terminal reason, no invented
  defaults
"""

from __future__ import annotations

from typing import Any

import pytest

from ac_dc.claude_code import history
from ac_dc.claude_code.history import render_messages, strip_framing, summarise_session

SESSION = "sess-1"


# ---------------------------------------------------------------------------
# Fixture building
# ---------------------------------------------------------------------------


class FakeSessionMessage:
    """What the SDK's parser hands back: a type, a uuid and a message body.

    Deliberately not the real ``SessionMessage``: it carries no timestamp
    (which is exactly why this module also reads the raw entries), and
    constructing the real one would tie these tests to an SDK dataclass
    signature that has nothing to do with what is being tested.
    """

    def __init__(self, type: str, uuid: str, message: dict[str, Any]) -> None:
        self.type = type
        self.uuid = uuid
        self.message = message
        self.session_id = SESSION
        self.parent_tool_use_id = None


def human(uuid: str, text: str, *, at: str = "2026-08-16T12:00:00.000Z") -> tuple:
    """A prompt somebody typed, as a (message, entry) pair."""
    message = FakeSessionMessage("user", uuid, {"role": "user", "content": text})
    entry = {"uuid": uuid, "type": "user", "timestamp": at, "parentUuid": None}
    return message, entry


def assistant(
    uuid: str,
    block: dict[str, Any],
    *,
    message_id: str = "msg_1",
    model: str = "claude-opus-5",
    usage: dict[str, Any] | None = None,
    at: str = "2026-08-16T12:00:01.000Z",
) -> tuple:
    """One assistant entry: exactly one content block, as the CLI writes it."""
    body = {
        "id": message_id,
        "role": "assistant",
        "model": model,
        "content": [block],
        "usage": usage
        if usage is not None
        else {"input_tokens": 10, "output_tokens": 5},
    }
    message = FakeSessionMessage("assistant", uuid, body)
    entry = {"uuid": uuid, "type": "assistant", "timestamp": at, "message": body}
    return message, entry


def tool_reply(
    uuid: str,
    tool_use_id: str,
    content: Any,
    *,
    is_error: bool = False,
    at: str = "2026-08-16T12:00:02.000Z",
    **extra: Any,
) -> tuple:
    """A tool reporting back — a ``user`` entry, not an assistant one."""
    body = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }
        ],
    }
    message = FakeSessionMessage("user", uuid, body)
    entry = {"uuid": uuid, "type": "user", "timestamp": at, **extra}
    return message, entry


def render(*pairs, events=None) -> list[dict[str, Any]]:
    messages = [pair[0] for pair in pairs]
    entries = [pair[1] for pair in pairs]
    return render_messages(messages, entries, events or [], session_id=SESSION)


# ---------------------------------------------------------------------------
# The taxonomy
# ---------------------------------------------------------------------------


class TestWhatBecomesAMessage:
    """Which entries start a turn, and which fold into the one in progress."""

    def test_a_prompt_and_a_reply_are_two_messages(self):
        rendered = render(
            human("u1", "hello"),
            assistant("a1", {"type": "text", "text": "hi"}),
        )
        assert [m["role"] for m in rendered] == ["user", "assistant"]
        assert rendered[0]["content"] == "hello"
        assert rendered[1]["content"] == "hi"

    def test_consecutive_assistant_entries_are_one_turn(self):
        """The panel draws one card per turn carrying ordered blocks, not one
        card per API message; splitting them would multiply the footer."""
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "thinking", "thinking": "hmm"}),
            assistant("a2", {"type": "text", "text": "done"}),
        )
        assert [m["role"] for m in rendered] == ["user", "assistant"]
        assert [b["kind"] for b in rendered[1]["blocks"]] == ["thinking", "text"]

    def test_a_tool_reply_does_not_start_a_new_message(self):
        """It is a `user` entry, but a human did not write it, and rendering
        it as a prompt would put words in the user's mouth mid-turn."""
        rendered = render(
            human("u1", "read it"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            tool_reply("u2", "t1", "file body"),
            assistant("a2", {"type": "text", "text": "there"}, message_id="msg_2"),
        )
        assert [m["role"] for m in rendered] == ["user", "assistant"]

    def test_a_second_prompt_closes_the_first_turn(self):
        rendered = render(
            human("u1", "one"),
            assistant("a1", {"type": "text", "text": "first"}),
            human("u2", "two", at="2026-08-16T12:05:00.000Z"),
            assistant("a2", {"type": "text", "text": "second"}, message_id="msg_2"),
        )
        assert [m["role"] for m in rendered] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert rendered[1]["content"] == "first"
        assert rendered[3]["content"] == "second"

    def test_a_prompt_carrying_text_and_an_image_is_still_a_prompt(self):
        """A list content block is not by itself a tool reply."""
        message = FakeSessionMessage(
            "user",
            "u1",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
                    },
                ],
            },
        )
        entry = {"uuid": "u1", "type": "user", "timestamp": "2026-08-16T12:00:00.000Z"}
        rendered = render_messages([message], [entry], [], session_id=SESSION)
        assert rendered[0]["role"] == "user"
        assert rendered[0]["content"] == "what is this"

    def test_an_empty_conversation_renders_nothing(self):
        assert render_messages([], [], [], session_id=SESSION) == []

    def test_a_turn_with_no_prompt_before_it_still_renders(self):
        """A transcript truncated at the front, or a resumed session whose
        first visible entry is a reply. Showing the reply beats showing
        nothing."""
        rendered = render(assistant("a1", {"type": "text", "text": "orphan"}))
        assert [m["role"] for m in rendered] == ["assistant"]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class TestImagesArePointedAtNotInlined:
    def test_an_image_becomes_a_reference(self):
        """Base64 in a history load would send every screenshot in the
        session to every client on every open."""
        message = FakeSessionMessage(
            "user",
            "u1",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "A" * 10_000,
                        },
                    },
                ],
            },
        )
        rendered = render_messages(
            [message], [{"uuid": "u1", "type": "user"}], [], session_id=SESSION
        )
        assert rendered[0]["image_refs"] == [
            {
                "session_id": SESSION,
                "entry_uuid": "u1",
                "block": 1,
                "media_type": "image/png",
            }
        ]
        assert "A" * 100 not in repr(rendered)

    def test_a_prompt_with_no_images_has_no_image_key(self):
        rendered = render(human("u1", "plain"))
        assert "image_refs" not in rendered[0]


# ---------------------------------------------------------------------------
# Tool blocks
# ---------------------------------------------------------------------------


class TestToolBlocks:
    def test_a_call_and_its_result_are_one_block(self):
        rendered = render(
            human("u1", "read"),
            assistant(
                "a1",
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/a.py"},
                },
            ),
            tool_reply("u2", "t1", "contents"),
        )
        (block,) = rendered[1]["blocks"]
        assert block["block_id"] == "t1"
        assert block["kind"] == "tool"
        assert block["done"] is True
        assert block["tool"]["name"] == "Read"
        assert block["tool"]["status"] == "ok"
        assert block["result"]["preview"] == "contents"

    def test_a_call_with_no_result_stays_pending(self):
        """A turn the server was killed in the middle of. The card must show
        as unfinished rather than as having quietly succeeded."""
        rendered = render(
            human("u1", "read"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
        )
        (block,) = rendered[1]["blocks"]
        assert block["done"] is False
        assert block["result"] is None
        assert block["tool"]["status"] == "pending"

    def test_an_error_result_is_marked_error(self):
        rendered = render(
            human("u1", "read"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            tool_reply("u2", "t1", "ENOENT", is_error=True),
        )
        (block,) = rendered[1]["blocks"]
        assert block["result"]["status"] == "error"
        assert block["tool"]["status"] == "error"

    def test_a_long_result_is_truncated_with_its_true_size(self):
        """Truncation is a read-time decision; the stored entry keeps the
        whole thing so "show all" has something to show."""
        rendered = render(
            human("u1", "read"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            tool_reply("u2", "t1", "x" * 9000),
        )
        result = rendered[1]["blocks"][0]["result"]
        assert result["truncated"] is True
        assert len(result["preview"]) == 4000
        assert result["full_bytes"] == 9000

    def test_a_structured_result_is_flattened_for_preview(self):
        rendered = render(
            human("u1", "read"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            tool_reply("u2", "t1", [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]),
        )
        assert rendered[1]["blocks"][0]["result"]["preview"] == "one\ntwo"

    def test_a_call_duration_is_the_real_gap_between_call_and_result(self):
        rendered = render(
            human("u1", "read"),
            assistant(
                "a1",
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                at="2026-08-16T12:00:01.000Z",
            ),
            tool_reply("u2", "t1", "ok", at="2026-08-16T12:00:03.500Z"),
        )
        assert rendered[1]["blocks"][0]["result"]["duration_ms"] == 2500

    def test_a_write_attributes_its_file(self):
        rendered = render(
            human("u1", "write"),
            assistant(
                "a1",
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "/x.py", "content": "hi"},
                },
            ),
            tool_reply("u2", "t1", "written"),
        )
        assert rendered[1]["blocks"][0]["result"]["files_modified"] == ["/x.py"]
        assert rendered[1]["files"] == ["/x.py"]
        assert rendered[1]["turn"]["files_modified"] == ["/x.py"]

    def test_a_failed_write_attributes_nothing(self):
        rendered = render(
            human("u1", "write"),
            assistant(
                "a1",
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "/x.py"},
                },
            ),
            tool_reply("u2", "t1", "permission denied", is_error=True),
        )
        assert rendered[1]["files"] == []

    def test_a_file_written_twice_is_listed_once(self):
        rendered = render(
            human("u1", "write"),
            assistant(
                "a1",
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/x.py"}},
            ),
            tool_reply("u2", "t1", "ok"),
            assistant(
                "a2",
                {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/x.py"}},
                message_id="msg_2",
            ),
            tool_reply("u3", "t2", "ok"),
        )
        assert rendered[1]["files"] == ["/x.py"]

    def test_an_mcp_tool_carries_its_server(self):
        rendered = render(
            human("u1", "go"),
            assistant(
                "a1",
                {"type": "tool_use", "id": "t1", "name": "mcp__ac-dc__ui_state", "input": {}},
            ),
        )
        assert rendered[1]["blocks"][0]["tool"]["server"] == "ac-dc"

    def test_a_server_tool_is_flagged_as_one(self):
        rendered = render(
            human("u1", "search"),
            assistant(
                "a1",
                {"type": "server_tool_use", "id": "t1", "name": "web_search", "input": {"q": "x"}},
            ),
        )
        assert rendered[1]["blocks"][0]["tool"]["server_tool"] is True

    def test_a_result_for_an_unknown_call_is_dropped(self):
        """A card with no header reads as a rendering bug, not as history."""
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "text", "text": "hi"}),
            tool_reply("u2", "nope", "orphan result"),
        )
        assert rendered[1]["blocks"] == [
            {
                "block_id": "a1:0",
                "kind": "text",
                "seq": 0,
                "content": "hi",
                "done": True,
                "agent_id": None,
            }
        ]

    def test_a_tool_use_with_no_id_is_skipped(self):
        """Nothing can ever resolve it, and a block keyed on "" would collide
        with the next one like it."""
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "tool_use", "name": "Read", "input": {}}),
        )
        assert rendered[1]["blocks"] == []

    def test_repeated_todo_writes_supersede_the_earlier_cards(self):
        """One live plan per turn. The superseded cards stay in the list so
        block order does not renumber; the renderer skips them."""
        rendered = render(
            human("u1", "plan"),
            assistant(
                "a1",
                {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": []}},
            ),
            tool_reply("u2", "t1", "ok"),
            assistant(
                "a2",
                {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {"todos": []}},
                message_id="msg_2",
            ),
            tool_reply("u3", "t2", "ok"),
        )
        assert [b["superseded"] for b in rendered[1]["blocks"]] == [True, False]

    def test_other_repeated_tools_do_not_supersede(self):
        rendered = render(
            human("u1", "read"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            assistant(
                "a2",
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {}},
                message_id="msg_2",
            ),
        )
        assert [b["superseded"] for b in rendered[1]["blocks"]] == [False, False]


# ---------------------------------------------------------------------------
# Denials
# ---------------------------------------------------------------------------


class TestDenials:
    def test_a_denied_call_is_gated_not_merely_errored(self):
        """"Error" would hide that a person, or a rule they wrote, stopped
        this — which is the only part of it they can act on."""
        rendered = render(
            human("u1", "rm it"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            tool_reply(
                "u2",
                "t1",
                "The user doesn't want to proceed",
                is_error=True,
                toolDenialKind="permission-rule",
            ),
        )
        (block,) = rendered[1]["blocks"]
        assert block["gated"] is True
        assert block["tool"]["gated"] is True
        assert block["denial"]["action"] == "deny"
        assert block["denial"]["reason"] == "The user doesn't want to proceed"

    def test_who_resolved_it_is_left_empty_rather_than_guessed(self):
        """Live, this names the client that answered the dialog. The
        transcript records the kind of rule instead — a different fact."""
        rendered = render(
            human("u1", "rm it"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            tool_reply("u2", "t1", "no", is_error=True, toolDenialKind="permission-rule"),
        )
        assert rendered[1]["blocks"][0]["denial"]["resolvedBy"] == ""

    def test_an_ordinary_error_is_not_a_denial(self):
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            tool_reply("u2", "t1", "exit 1", is_error=True),
        )
        (block,) = rendered[1]["blocks"]
        assert block["gated"] is False
        assert block["denial"] is None


# ---------------------------------------------------------------------------
# The footer, rebuilt
# ---------------------------------------------------------------------------


class TestTheFooterWithoutAResultEntry:
    """The CLI transcript has no result entry — verified against real ones.

    Everything the footer shows is therefore reconstructed from per-message
    ``usage`` and timestamps, or omitted. These tests pin which is which.
    """

    def test_usage_is_summed_by_model_in_the_keys_the_panel_reads(self):
        rendered = render(
            human("u1", "go"),
            assistant(
                "a1",
                {"type": "text", "text": "a"},
                message_id="msg_1",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 7,
                },
            ),
            assistant(
                "a2",
                {"type": "text", "text": "b"},
                message_id="msg_2",
                usage={"input_tokens": 5, "output_tokens": 3},
            ),
        )
        assert rendered[1]["turn"]["model_usage"] == {
            "claude-opus-5": {
                "input_tokens": 105,
                "output_tokens": 23,
                "cache_read_input_tokens": 7,
            }
        }

    def test_one_message_split_across_entries_is_counted_once(self):
        """The CLI writes one entry per content block and repeats `usage` on
        every one of them, so summing entries would multiply a turn's
        tokens by its block count."""
        usage = {"input_tokens": 100, "output_tokens": 20}
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "thinking", "thinking": "hmm"}, usage=usage),
            assistant(
                "a2",
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                usage=usage,
            ),
        )
        assert rendered[1]["turn"]["model_usage"]["claude-opus-5"] == {
            "input_tokens": 100,
            "output_tokens": 20,
        }
        assert rendered[1]["turn"]["num_turns"] == 1

    def test_two_models_in_one_turn_are_kept_apart(self):
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "text", "text": "a"}, model="claude-opus-5"),
            assistant(
                "a2",
                {"type": "text", "text": "b"},
                message_id="msg_2",
                model="claude-haiku-4-5-20251001",
            ),
        )
        assert set(rendered[1]["turn"]["model_usage"]) == {
            "claude-opus-5",
            "claude-haiku-4-5-20251001",
        }

    def test_tool_calls_are_counted(self):
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            tool_reply("u2", "t1", "ok"),
            assistant(
                "a2",
                {"type": "tool_use", "id": "t2", "name": "Grep", "input": {}},
                message_id="msg_2",
            ),
            tool_reply("u3", "t2", "ok"),
        )
        assert rendered[1]["turn"]["tool_calls"] == 2

    def test_the_duration_is_measured_from_the_prompt(self):
        """What the user waited for, which is what the live footer's
        `duration_ms` reports — not the gap between the first and last
        assistant entry, which would read as instant for a one-block reply."""
        rendered = render(
            human("u1", "go", at="2026-08-16T12:00:00.000Z"),
            assistant("a1", {"type": "text", "text": "a"}, at="2026-08-16T12:00:01.000Z"),
            assistant(
                "a2",
                {"type": "text", "text": "b"},
                message_id="msg_2",
                at="2026-08-16T12:00:09.250Z",
            ),
        )
        assert rendered[1]["turn"]["duration_ms"] == 9250

    def test_a_turn_with_no_prompt_measures_its_own_span(self):
        """A transcript truncated at the front still has two entries to
        measure between."""
        rendered = render(
            assistant("a1", {"type": "text", "text": "a"}, at="2026-08-16T12:00:01.000Z"),
            assistant(
                "a2",
                {"type": "text", "text": "b"},
                message_id="msg_2",
                at="2026-08-16T12:00:04.000Z",
            ),
        )
        assert rendered[0]["turn"]["duration_ms"] == 3000

    def test_cost_is_absent_rather_than_zero(self):
        """It is not in the transcript, the CLI derives it from a pricing
        table we do not have, and under subscription billing it is null
        anyway. `$0.00` would be a claim; nothing is the truth."""
        rendered = render(human("u1", "go"), assistant("a1", {"type": "text", "text": "a"}))
        assert "total_cost_usd" not in rendered[1]["turn"]

    def test_the_terminal_reason_is_null_so_no_badge_is_drawn(self):
        """The transcript never records why a turn ended. A badge claiming a
        clean finish would be worse than no badge."""
        rendered = render(human("u1", "go"), assistant("a1", {"type": "text", "text": "a"}))
        assert rendered[1]["terminalReason"] is None

    def test_a_turn_with_no_timestamps_omits_its_duration(self):
        """Rather than reporting 0 ms, which reads as instantaneous."""
        message = FakeSessionMessage(
            "assistant",
            "a1",
            {"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "x"}]},
        )
        rendered = render_messages([message], [{"uuid": "a1"}], [], session_id=SESSION)
        assert "duration_ms" not in rendered[0]["turn"]


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    """The boundary is a `system` entry, so the SDK's parser drops it.

    It is still in the chain as the parent of the compact-summary user
    entry, which is how the divider gets placed without a chain walk here.
    """

    @staticmethod
    def _boundary(uuid: str = "b1") -> dict[str, Any]:
        return {
            "uuid": uuid,
            "type": "system",
            "subtype": "compact_boundary",
            "parentUuid": None,
            "logicalParentUuid": "a1",
            "content": "Conversation compacted",
            "isMeta": False,
            "timestamp": "2026-08-16T12:10:00.000Z",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 150_000,
                "postTokens": 20_000,
                "durationMs": 4200,
            },
        }

    def _rendered(self):
        summary = FakeSessionMessage(
            "user",
            "u2",
            {"role": "user", "content": "Here is a summary of the conversation so far."},
        )
        summary_entry = {
            "uuid": "u2",
            "type": "user",
            "parentUuid": "b1",
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "timestamp": "2026-08-16T12:10:01.000Z",
        }
        first = human("u1", "start")
        reply = assistant("a1", {"type": "text", "text": "ok"})
        return render_messages(
            [first[0], reply[0], summary],
            [first[1], reply[1], self._boundary(), summary_entry],
            [],
            session_id=SESSION,
        )

    def test_a_divider_lands_before_the_summary(self):
        rendered = self._rendered()
        assert [m.get("system_event", False) for m in rendered] == [
            False,
            False,
            True,
            False,
        ]
        assert rendered[2]["content"] == "Conversation compacted"

    def test_the_metadata_is_translated_to_the_keys_the_panel_reads(self):
        """`compactMetadata`/`preTokens` on disk; the renderer takes
        snake_case, so a pass-through would print blanks."""
        assert self._rendered()[2]["compaction"] == {
            "pre_tokens": 150_000,
            "post_tokens": 20_000,
            "trigger": "auto",
        }

    def test_the_summary_is_marked_as_not_something_the_user_typed(self):
        rendered = self._rendered()
        assert rendered[3]["compact_summary"] is True
        assert rendered[3]["role"] == "user"

    def test_an_ordinary_prompt_gets_no_divider(self):
        rendered = render(
            human("u1", "one"),
            assistant("a1", {"type": "text", "text": "ok"}),
            human("u2", "two"),
        )
        assert not any(m.get("system_event") for m in rendered)
        assert not any("compact_summary" in m for m in rendered)

    def test_a_parent_that_is_not_a_boundary_gets_no_divider(self):
        message, entry = human("u2", "two")
        entry["parentUuid"] = "a1"
        first = human("u1", "one")
        reply = assistant("a1", {"type": "text", "text": "ok"})
        rendered = render_messages(
            [first[0], reply[0], message],
            [first[1], reply[1], entry],
            [],
            session_id=SESSION,
        )
        assert not any(m.get("system_event") for m in rendered)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


class TestFramingIsStripped:
    def test_the_ui_context_block_goes(self):
        """AC-DC wrote it, not the user; leaving it in would bury every
        historical prompt under a context blob."""
        framed = "<ac-dc-ui-context>\nSelected: a.py\n</ac-dc-ui-context>\n\nfix the bug"
        assert strip_framing(framed) == "fix the bug"

    def test_an_unframed_prompt_is_untouched(self):
        assert strip_framing("just a prompt") == "just a prompt"

    def test_a_prompt_that_merely_mentions_the_tag_is_untouched(self):
        text = "why does <ac-dc-ui-context> appear in my prompt?"
        assert strip_framing(text) == text

    def test_unclosed_framing_is_left_alone(self):
        """Truncating at a guess could cut into the user's own words."""
        text = "<ac-dc-ui-context>\nSelected: a.py\nfix it"
        assert strip_framing(text) == text

    def test_stripping_happens_on_the_rendered_prompt(self):
        rendered = render(
            human("u1", "<ac-dc-ui-context>\nctx\n</ac-dc-ui-context>\n\nreal question")
        )
        assert rendered[0]["content"] == "real question"


# ---------------------------------------------------------------------------
# Unknown blocks
# ---------------------------------------------------------------------------


class TestAnUnknownBlockIsVisible:
    def test_it_renders_as_json_rather_than_vanishing(self):
        """A CLI that grows a block kind this build has never seen must
        degrade to something the user can see and report, not to a hole in
        the middle of an answer."""
        rendered = render(
            human("u1", "go"),
            assistant("a1", {"type": "hologram", "payload": {"depth": 3}}),
        )
        (block,) = rendered[1]["blocks"]
        assert block["kind"] == "text"
        assert "hologram" in block["content"]
        assert "unrecognised" in block["content"]

    def test_it_is_logged(self, caplog):
        with caplog.at_level("WARNING"):
            render(human("u1", "go"), assistant("a1", {"type": "hologram"}))
        assert "hologram" in caplog.text


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEventsAreInterleaved:
    @staticmethod
    def _event(at: str, content: str, **extra: Any) -> dict[str, Any]:
        return {
            "id": f"1-{content}",
            "session_id": SESSION,
            "timestamp": at,
            "event": "commit",
            "content": content,
            **extra,
        }

    def test_an_event_lands_in_timestamp_order(self):
        rendered = render(
            human("u1", "one", at="2026-08-16T12:00:00+00:00"),
            assistant("a1", {"type": "text", "text": "ok"}, at="2026-08-16T12:00:01+00:00"),
            human("u2", "two", at="2026-08-16T12:00:30+00:00"),
            events=[self._event("2026-08-16T12:00:10+00:00", "committed")],
        )
        assert [m["content"] for m in rendered] == ["one", "ok", "committed", "two"]

    def test_an_event_card_is_a_system_event(self):
        rendered = render(
            human("u1", "one"),
            events=[self._event("2026-08-16T12:00:10+00:00", "committed")],
        )
        card = rendered[-1]
        assert card["system_event"] is True
        assert card["event"] == "commit"
        assert card["role"] == "user"

    def test_a_request_id_is_carried_through(self):
        """Correlation is by session plus request; the browser groups an
        event with the turn it belongs to."""
        rendered = render(
            human("u1", "one"),
            events=[self._event("2026-08-16T12:00:10+00:00", "c", request_id="req-9")],
        )
        assert rendered[-1]["request_id"] == "req-9"

    def test_the_transcript_wins_a_tie(self):
        """A commit recorded in the same millisecond as the message that
        triggered it belongs after it."""
        rendered = render(
            human("u1", "one", at="2026-08-16T12:00:00+00:00"),
            events=[self._event("2026-08-16T12:00:00+00:00", "committed")],
        )
        assert [m["content"] for m in rendered] == ["one", "committed"]

    def test_an_undated_event_goes_last_not_first(self):
        """A malformed record must not claim to predate the conversation."""
        rendered = render(
            human("u1", "one", at="2026-08-16T12:00:00+00:00"),
            events=[{"session_id": SESSION, "event": "reset", "content": "no clock"}],
        )
        assert [m["content"] for m in rendered] == ["one", "no clock"]

    def test_a_record_with_no_content_is_dropped(self):
        rendered = render(human("u1", "one"), events=[{"event": "reset"}])
        assert len(rendered) == 1

    def test_no_events_leaves_the_transcript_untouched(self):
        pairs = [human("u1", "one"), assistant("a1", {"type": "text", "text": "ok"})]
        assert render(*pairs) == render(*pairs, events=[])

    def test_events_are_ordered_among_themselves(self):
        rendered = render(
            human("u1", "one", at="2026-08-16T12:00:00+00:00"),
            events=[
                self._event("2026-08-16T12:00:20+00:00", "second"),
                self._event("2026-08-16T12:00:10+00:00", "first"),
            ],
        )
        assert [m["content"] for m in rendered] == ["one", "first", "second"]

    def test_a_z_suffixed_and_an_offset_timestamp_sort_together(self):
        """The CLI writes `...Z`; our events log writes `+00:00`. Comparing
        them as strings would put every event after every message."""
        rendered = render(
            human("u1", "one", at="2026-08-16T12:00:00.000Z"),
            human("u2", "two", at="2026-08-16T12:00:20.000Z"),
            events=[self._event("2026-08-16T12:00:10+00:00", "between")],
        )
        assert [m["content"] for m in rendered] == ["one", "between", "two"]


# ---------------------------------------------------------------------------
# The session list
# ---------------------------------------------------------------------------


class FakeInfo:
    """The fields of ``SDKSessionInfo`` that a summary reads."""

    def __init__(self, **kwargs: Any) -> None:
        self.session_id = kwargs.get("session_id", SESSION)
        self.summary = kwargs.get("summary")
        self.first_prompt = kwargs.get("first_prompt")
        self.created_at = kwargs.get("created_at", 1_786_881_600_123)


class TestTheSessionSummary:
    def test_it_has_exactly_the_seven_documented_fields(self):
        summary = summarise_session(FakeInfo(first_prompt="hi"), [object()])
        assert set(summary) == {
            "session_id",
            "timestamp",
            "message_count",
            "preview",
            "first_role",
            "resumable",
            "total_cost_usd",
        }

    def test_the_message_count_is_exact_not_estimated(self):
        summary = summarise_session(FakeInfo(), [object(), object(), object()])
        assert summary["message_count"] == 3

    def test_the_preview_is_capped(self):
        summary = summarise_session(FakeInfo(first_prompt="x" * 500), [object()])
        assert len(summary["preview"]) == history.PREVIEW_CHARS

    def test_the_title_stands_in_for_a_missing_first_prompt(self):
        summary = summarise_session(FakeInfo(summary="Fix the parser"), [object()])
        assert summary["preview"] == "Fix the parser"

    def test_the_timestamp_is_iso_not_epoch_millis(self):
        summary = summarise_session(FakeInfo(), [object()])
        assert summary["timestamp"].startswith("2026-08-16T12:00:00.123")

    def test_a_session_with_no_creation_time_gets_an_empty_timestamp(self):
        summary = summarise_session(FakeInfo(created_at=0), [object()])
        assert summary["timestamp"] == ""

    def test_cost_is_null_rather_than_zero(self):
        assert summarise_session(FakeInfo(), [object()])["total_cost_usd"] is None

    def test_an_unparseable_session_is_listed_as_not_resumable(self):
        """Better a row the user can see and delete than a listing that
        fails because one transcript is broken."""
        summary = summarise_session(FakeInfo(), [])
        assert summary["resumable"] is False
        assert summary["message_count"] == 0


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class TestTimeParsing:
    @pytest.mark.parametrize(
        "value",
        ["", None, "not a date", 12345, "2026-13-45T99:99:99Z"],
    )
    def test_an_unusable_timestamp_yields_no_duration(self, value):
        assert history._elapsed_ms(value, "2026-08-16T12:00:01Z") == 0

    def test_a_clock_step_backwards_does_not_make_a_negative_duration(self):
        """Entries are written in order, but a duration reading as though
        the result preceded the call is a rendering bug either way."""
        assert history._elapsed_ms("2026-08-16T12:00:05Z", "2026-08-16T12:00:01Z") == 0

    def test_a_naive_timestamp_is_read_as_utc(self):
        assert history._elapsed_ms("2026-08-16T12:00:00", "2026-08-16T12:00:02") == 2000
