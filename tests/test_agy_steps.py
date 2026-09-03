"""Tests for the ``agy`` stream translator.

**The fixtures are transcribed from a real capture**, not invented — one
bidirectional turn on 2026-09-03, recorded in ``sdk-surface.md`` § *The
stream, measured in bidirectional mode*. That matters more here than
usual, because phase 3's lesson on the SDK side was that a fake describing
a friendlier engine than the real one passes every test while the pump is
wrong.

Three properties carry the file, and each is a way this pump could be
plausible and wrong:

**Frames are nested.** Read flat, every field is ``None`` and the turn
renders empty with nothing raised.

**``text_delta`` is a delta.** The SDK's equivalent is cumulative and the
browser replaces by ``block_id``, so forwarding ``agy``'s fragment would
render only the last few words of each message. This pump accumulates.

**The step vocabulary is not closed.** It was documented as three members
and a plain turn produced a fourth, so an unknown one is rendered rather
than dropped.

Offline. No ``agy``, no network, no subprocess.
"""

from __future__ import annotations

import pytest

from aic_dc.agy.steps import AgyTranslator, unwrap


def frame(step: dict) -> dict:
    return {"event": "step_update", "step_update": step}


# Transcribed verbatim from the capture.
TEXT_A = {
    "conversation_id": "b1d377c5",
    "step_index": 5,
    "state": "ACTIVE",
    "step_type": "agent_response",
    "text_delta": "I am searching for `calc.py` ",
}
TEXT_B = dict(TEXT_A, text_delta="to read its contents.", state="DONE")
TOOL_ACTIVE = {
    "conversation_id": "b1d377c5",
    "step_index": 2,
    "state": "ACTIVE",
    "step_type": "tool",
    "tool_name": "find_by_name",
    "tool_info": {
        "name": "find_by_name",
        "parameters": {"Pattern": "calc.py", "SearchDirectory": "/tmp/x"},
    },
}
TOOL_DONE = dict(TOOL_ACTIVE, state="DONE", duration_seconds=0.065)
RESULT = {
    "event": "result",
    "result": {
        "conversation_id": "b1d377c5",
        "status": "SUCCESS",
        "response": "calc.py defines add().",
        "duration_seconds": 29.75,
        "num_turns": 1,
        "usage": {
            "input_tokens": 72286,
            "output_tokens": 274,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 72560,
        },
    },
}


def names(events):
    return [e.name for e in events]


class TestTheFramesAreNested:
    def test_unwrap_reads_the_inner_payload(self):
        assert unwrap(RESULT, "result")["status"] == "SUCCESS"

    @pytest.mark.parametrize(
        "junk",
        [
            {"status": "SUCCESS"},  # flat — the shape that reads as None
            {"event": "result"},
            {"event": "result", "result": "nope"},
            None,
            7,
        ],
    )
    def test_a_flat_or_broken_frame_is_not_silently_empty(self, junk):
        assert unwrap(junk, "result") is None

    def test_a_flat_result_produces_no_events_rather_than_wrong_ones(self):
        t = AgyTranslator("r1")
        assert t.translate({"status": "SUCCESS", "response": "x"}) == []
        assert t.response_text() == ""


class TestTextIsADelta:
    def test_fragments_accumulate_into_a_running_total(self):
        """The browser replaces by block id, so it must receive the whole."""
        t = AgyTranslator("r1")
        first = t.translate(frame(TEXT_A))
        second = t.translate(frame(TEXT_B))
        assert names(first) == ["streamChunk"]
        assert first[0].payload["content"] == "I am searching for `calc.py` "
        assert second[0].payload["content"] == (
            "I am searching for `calc.py` to read its contents."
        )

    def test_the_sequence_advances_so_a_stale_chunk_can_be_dropped(self):
        t = AgyTranslator("r1")
        a = t.translate(frame(TEXT_A))[0].payload["seq"]
        b = t.translate(frame(TEXT_B))[0].payload["seq"]
        assert b > a

    def test_one_block_per_step_index(self):
        t = AgyTranslator("r1")
        a = t.translate(frame(TEXT_A))[0].payload["block_id"]
        b = t.translate(frame(dict(TEXT_A, step_index=9)))[0].payload["block_id"]
        assert a != b

    def test_done_is_marked_only_on_a_terminal_state(self):
        t = AgyTranslator("r1")
        assert t.translate(frame(TEXT_A))[0].payload["done"] is False
        assert t.translate(frame(TEXT_B))[0].payload["done"] is True

    def test_an_empty_delta_emits_nothing(self):
        t = AgyTranslator("r1")
        assert t.translate(frame(dict(TEXT_A, text_delta=""))) == []


class TestToolCards:
    def test_an_active_call_opens_a_card_with_its_arguments(self):
        t = AgyTranslator("r1")
        events = t.translate(frame(TOOL_ACTIVE))
        assert names(events) == ["toolUse"]
        card = events[0].payload
        assert card["name"] == "find_by_name"
        # From `tool_info.parameters` — the stream's nesting, which is not
        # the hook's `toolCall.args`.
        assert card["input"]["Pattern"] == "calc.py"
        assert card["status"] == "pending"
        assert card["gated"] is True, "every call on this transport passes the gate"

    def test_completion_resolves_the_same_card(self):
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        events = t.translate(frame(TOOL_DONE))
        assert names(events) == ["toolResult"]
        assert events[0].payload["tool_use_id"] == "agy-tool-2"
        assert events[0].payload["status"] == "success"
        assert events[0].payload["duration_ms"] == 65

    def test_a_call_seen_only_once_still_opens_and_closes(self):
        """A DONE with no prior ACTIVE must not leave a card pending."""
        t = AgyTranslator("r1")
        assert names(t.translate(frame(TOOL_DONE))) == ["toolUse", "toolResult"]

    def test_a_missing_output_is_complete_with_none_not_pending(self):
        """`tool_info.output` is per-tool, so nothing may require it."""
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        result = t.translate(frame(TOOL_DONE))[0].payload
        assert result["content"] == ""
        assert result["status"] == "success"

    def test_an_output_is_carried_when_there_is_one(self):
        t = AgyTranslator("r1")
        done = dict(TOOL_DONE)
        done["tool_info"] = dict(done["tool_info"], output="calc.py")
        assert t.translate(frame(done))[1].payload["content"] == "calc.py"

    def test_an_errored_call_says_so(self):
        t = AgyTranslator("r1")
        assert (
            t.translate(frame(dict(TOOL_DONE, state="ERROR")))[1].payload["status"]
            == "error"
        )


class TestTheVocabularyIsNotClosed:
    def test_a_system_message_is_a_notice_not_assistant_prose(self):
        """Rendering it as prose would put words in the assistant's mouth."""
        t = AgyTranslator("r1")
        events = t.translate(
            frame(
                {
                    "step_index": 6,
                    "state": "DONE",
                    "step_type": "system_message",
                    "text": "Switching model.",
                }
            )
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload["subtype"] == "engine_notice"
        assert events[0].payload["data"]["message"] == "Switching model."

    def test_an_unknown_step_type_is_rendered_not_dropped(self):
        """The vocabulary was documented as three members and has four."""
        t = AgyTranslator("r1")
        events = t.translate(
            frame({"step_index": 3, "state": "DONE", "step_type": "from_the_future"})
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload["subtype"] == "unknown_step"
        assert events[0].payload["data"]["step_type"] == "from_the_future"

    def test_our_own_echoed_prompt_is_not_rendered_twice(self):
        t = AgyTranslator("r1")
        assert t.translate(frame({"step_index": 0, "state": "DONE", "step_type": "user_input"})) == []

    def test_an_unreadable_frame_is_reported_rather_than_dropped(self, monkeypatch):
        t = AgyTranslator("r1")

        def boom(_step):
            raise RuntimeError("bad shape")

        monkeypatch.setattr(t, "_step", boom)
        events = t.translate(frame(TEXT_A))
        assert names(events) == ["systemEvent"]
        assert events[0].payload["subtype"] == "step_unreadable"


class TestTheTurnCloses:
    def test_usage_is_the_last_running_total_not_a_sum(self):
        """Later frames repeat the total; summing would multiply the bill."""
        t = AgyTranslator("r1")
        t.translate(frame(dict(TEXT_A, usage={"total_tokens": 100})))
        t.translate(frame(dict(TEXT_B, usage={"total_tokens": 250})))
        assert t.turn_usage()["total_tokens"] == 250

    def test_the_result_supplies_the_prose_and_the_usage(self):
        t = AgyTranslator("r1")
        t.translate(RESULT)
        assert t.response_text() == "calc.py defines add()."
        assert t.turn_usage()["total_tokens"] == 72560

    def test_a_success_reports_no_stop_reason(self):
        """An unrecognised reason draws a red badge, so a clean turn sends none.

        Phase 3's lesson on the SDK side: forwarding `UNSPECIFIED` would
        have put a red badge reading `UNSPECIFIED` on every clean turn.
        """
        t = AgyTranslator("r1")
        t.translate(RESULT)
        complete = t.stream_complete()[-1].payload
        assert complete["stop_reason"] == ""

    def test_anything_other_than_success_is_forwarded_verbatim(self):
        t = AgyTranslator("r1")
        t.translate({"event": "result", "result": {"status": "CANCELED"}})
        assert t.stream_complete()[-1].payload["stop_reason"] == "CANCELED"

    def test_a_turn_with_no_result_still_reports_its_prose(self):
        """A cancel, or a process that died, has deltas and no result."""
        t = AgyTranslator("r1")
        t.translate(frame(TEXT_A))
        assert "searching" in t.response_text()

    def test_the_closing_events_are_what_the_browser_already_reads(self):
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        t.translate(frame(TOOL_DONE))
        t.translate(RESULT)
        events = t.stream_complete()
        assert names(events) == ["turnUsage", "streamComplete"]
        payload = events[-1].payload
        assert payload["request_id"] == "r1"
        assert payload["num_tool_calls"] == 1
        assert set(payload) == {
            "request_id",
            "stop_reason",
            "num_tool_calls",
            "files_modified",
            "usage",
            "response_text",
        }
