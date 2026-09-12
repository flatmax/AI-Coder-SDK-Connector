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

import logging
import os
from typing import Any

import pytest

from aic_dc.agy import steps
from aic_dc.agy.consultant import SECOND_OPINION_POLICY
from aic_dc.agy.gate_server import StaticPolicy
from aic_dc.agy.steps import UNNAMED_TOOL, AgyTranslator, unwrap

#: The consultation's policy as a *stamped* one, which is the only shape
#: that ever reaches a gate: `AgyConsultant._run` mints a nonce per
#: consultation and the pump recognises a refusal by that rather than by
#: the fixed mark, which this repository's own files contain in plain
#: text. A fixture built on the unstamped policy would be testing a state
#: that cannot occur, and every refusal in it would read as a call whose
#: outcome the app could not establish.
POLICY = SECOND_OPINION_POLICY.stamped()


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
# Transcribed from the 2026-09-09 capture at agy 1.1.27, prompt shortened.
# The conversation id is the subagent's own and is what the tab is keyed
# on; `log_uri` names the transcript it writes, which is the only place
# its steps appear — they do not stream into the parent conversation.
SUBAGENT_ENTRY = {
    "type_name": "research",
    "role": "Notes Reader",
    "initial_prompt": "Please read the file `notes.txt` …",
    "conversation_id": "c21acd4d",
    "log_uri": (
        "file:///home/u/.gemini/antigravity-cli/brain/c21acd4d"
        "/.system_generated/logs/transcript.jsonl"
    ),
    "workspace_uris": ["file:///tmp/temp/work"],
}
SUBAGENT_ACTIVE = {
    "conversation_id": "b1d377c5",
    "step_index": 2,
    "state": "ACTIVE",
    "step_type": "subagent",
    "tool_name": "invoke_subagent",
    "subagent_info": {"subagents": [SUBAGENT_ENTRY]},
}
SUBAGENT_DONE = dict(SUBAGENT_ACTIVE, state="DONE", duration_seconds=6.1)
# Transcribed verbatim from a live consultation on 2026-09-12 (P20): the
# question needed the web, the consultation gate refused it, and this is
# the frame `agy` sent back. The reason is *this app's own*, round-tripped
# through the vendor and prefixed by it — which is the whole of what the
# pump used to discard (AG-R-22).
DENIED = {
    "conversation_id": "3e34efc6",
    "step_index": 2,
    "state": "ERROR",
    "step_type": "tool",
    "tool_name": "read_url_content",
    "duration_seconds": 0.260418395,
    "tool_info": {
        "name": "read_url_content",
        "parameters": {"Url": "https://www.kernel.org/"},
        "error": {
            "type": "TOOL_ERROR",
            # `agy` prefixes its own sentence and forwards the rest of
            # ours, which is why the fixture composes the two rather than
            # quoting a string: what the pump matches on is
            # `StaticPolicy.MARK`, at the head of the policy's reason, and
            # a fixture that hard-coded a paraphrase of either would keep
            # passing after the real wording moved out from under the code.
            "message": (
                "tool call denied by pre-tool hook: "
                f"{POLICY.reason}"
            ),
        },
    },
}
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
        assert result["preview"] == ""
        assert result["truncated"] is False
        assert result["full_bytes"] == 0
        assert result["status"] == "success"

    def test_an_output_is_carried_when_there_is_one(self):
        t = AgyTranslator("r1")
        done = dict(TOOL_DONE)
        done["tool_info"] = dict(done["tool_info"], output="calc.py")
        assert t.translate(frame(done))[1].payload["preview"] == "calc.py"

    def test_an_errored_call_says_so(self):
        t = AgyTranslator("r1")
        assert (
            t.translate(frame(dict(TOOL_DONE, state="ERROR")))[1].payload["status"]
            == "error"
        )


class TestADeniedCallSaysWhy:
    """AG-R-22, and it is a data-loss bug rather than a missing feature.

    The reason arrives in the frame and was dropped one line from the
    browser, so every denial and every tool error drew the literal "No
    output." on its card. Measured on both surfaces before it was fixed:
    a consultation denied ``read_url_content``, and the master engine
    denied ``view_file``.
    """

    def test_the_reason_reaches_the_card(self):
        t = AgyTranslator("r1")
        result = t.translate(frame(DENIED))[-1].payload
        assert result["status"] == "error"
        assert "denied by pre-tool hook" in result["preview"]
        assert "no tools here" in result["preview"], (
            "the card shows why, not merely that"
        )
        assert result["full_bytes"] > 0

    def test_an_empty_output_on_a_failure_is_still_replaced(self):
        """``not output``, not ``output is None``.

        A failed call that names an empty output is a failure with nothing
        to read, and the version of this that tested for ``None`` left
        exactly those cards blank.
        """
        step = dict(DENIED)
        step["tool_info"] = dict(step["tool_info"], output="")
        assert "denied" in AgyTranslator("r1").translate(frame(step))[-1].payload[
            "preview"
        ]

    def test_a_real_output_on_a_failure_wins(self):
        """The error is a fallback. A tool that failed *and* said something
        has the thing it said read first."""
        step = dict(DENIED)
        step["tool_info"] = dict(step["tool_info"], output="partial output")
        assert (
            AgyTranslator("r1").translate(frame(step))[-1].payload["preview"]
            == "partial output"
        )

    def test_an_error_that_is_a_bare_string_does_not_raise(self):
        """Measured as a mapping; the SDK transport spells its own as a
        string. Neither shape is assumed, because this runs while drawing
        a card and a raise here would fail the turn over decoration."""
        step = dict(DENIED)
        step["tool_info"] = dict(step["tool_info"], error="it went wrong")
        assert (
            AgyTranslator("r1").translate(frame(step))[-1].payload["preview"]
            == "it went wrong"
        )

    def test_a_success_is_not_given_an_error_body(self):
        """A call that reported no error has none to show."""
        step = dict(DENIED, state="DONE")
        step["tool_info"] = dict(step["tool_info"])
        del step["tool_info"]["error"]
        assert AgyTranslator("r1").translate(frame(step))[-1].payload["preview"] == ""

    def test_an_error_under_a_done_state_is_still_an_error(self):
        """Hole C, from the sixth review round, on the master engine.

        ``failed`` used to fold the error test into a barred-only branch,
        so for every tool on this transport that the allowlist does not
        cover — which is all of them here, and the consultation's own
        ``finish`` — it reduced to ``state == "ERROR"`` alone. A release
        that reported a failed call under ``DONE`` with the reason in
        ``tool_info.error`` therefore drew a green card and discarded the
        reason one line from the browser, which is AG-R-22's original
        shape in the place it was supposed to have been fixed.
        """
        step = dict(DENIED, state="DONE")
        result = AgyTranslator("r1").translate(frame(step))[-1].payload
        assert result["status"] == "error"
        assert "denied by pre-tool hook" in result["preview"]


class TestAnUngroundedAnswerIsMarked:
    """The second half of AG-R-22: the card says a call came back empty,
    and this says what that makes of the *answer*.

    One question decides both, and it is a fact about the policy rather
    than a reading of anybody's prose: *could this tool have run here?* A
    barred tool that reported an error is over, so its card is settled,
    and it took no data into the answer, so the notice is raised. Two
    review rounds were spent on a second condition — that the error carry
    this app's own refusal mark — and it failed open both times it was
    relied on. What that condition was protecting is a claim, and the
    claim lives in the error text the card already shows.
    """

    def setup_method(self) -> None:
        """A stamped policy per test, as `_run` mints one per consultation.

        Not a module constant. The gate mints a **single-use** token per
        denial and the pump spends it on the way back, so a refusal frame
        built once and replayed across tests would authenticate in the
        first test and read as unverified in every one after it. Sharing
        the issuer between the frame and the pump is also what makes
        these tests honest: the token in the message below is one this
        translator's own policy issued, rather than a string the test
        handed to both sides.
        """
        self.policy = SECOND_OPINION_POLICY.stamped()

    def denied(self, **over: Any) -> dict[str, Any]:
        """A denial frame for one barred call, from the live policy.

        The token is minted for whichever tool the frame names, because
        the gate binds each one to the tool it refused — a token issued
        denying `view_file` cannot authenticate a `run_command` that
        escaped.
        """
        step = dict(DENIED, **over)
        tool = str(step.get("tool_name") or "")
        step["tool_info"] = dict(
            step["tool_info"],
            name=tool,
            error={
                "type": "TOOL_ERROR",
                "message": (
                    f"tool call denied by pre-tool hook: "
                    f"{self.policy.refusal(tool)}"
                ),
            },
        )
        return step

    def consultation(self) -> AgyTranslator:
        """A pump told the real policy, not a paraphrase of it."""
        t = AgyTranslator("r1")
        t.note_consultation(self.policy.allowed, self.policy.refusals)
        return t

    def test_the_notice_follows_the_card_that_provoked_it(self):
        """Not at the end of the turn, which is where this started.

        A notice under a thousand words the reader has already believed
        arrives after the damage, and a consultation that is stopped or
        times out never emits the ``result`` frame that used to carry it.
        Here it precedes everything the model writes after being refused.
        """
        t = self.consultation()
        events = t.translate(frame(self.denied()))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        payload = events[-1].payload
        assert payload["subtype"] == "consultation_ungrounded"
        assert payload["data"]["tool"] == "read_url_content"
        assert t.ungrounded_tools == ("read_url_content",)

    def test_the_result_frame_no_longer_carries_it(self):
        """It was said once already, and twice is not twice as true."""
        t = self.consultation()
        t.translate(frame(self.denied()))
        assert t.translate(RESULT) == []

    def test_three_refusals_raise_one_notice_and_the_list_keeps_all_three(self):
        """One sentence, because a model refused three tools has not
        learned three things — and the later cards each carry their own
        reason, which is where per-call attribution belongs. The full list
        still reaches the model that asked, through ``ungrounded_tools``."""
        t = self.consultation()
        notices = []
        for index, name in enumerate(("search_web", "read_url_content", "view_file")):
            notices += [
                e
                for e in t.translate(frame(self.denied(step_index=index, tool_name=name)))
                if e.name == "systemEvent"
            ]
        assert len(notices) == 1
        assert notices[0].payload["data"]["tool"] == "search_web"
        assert t.ungrounded_tools == ("search_web", "read_url_content", "view_file")

    def test_a_consultation_that_asked_for_nothing_says_nothing(self):
        """The ordinary case, and the reason this can be said out loud at
        all: a second opinion on a diff reaches for no tools."""
        t = self.consultation()
        assert t.translate(RESULT) == []
        assert t.ungrounded_tools == ()

    def test_an_allowed_tool_that_failed_is_a_failure_not_a_refusal(self):
        """``generate_image`` is on the image policy's list, so a failed one
        broke rather than being denied, and no grounding claim follows."""
        t = AgyTranslator("r1")
        t.note_consultation({"finish", "generate_image"})
        events = t.translate(frame(self.denied(tool_name="generate_image")))
        assert names(events) == ["toolUse", "toolResult"]
        assert t.ungrounded_tools == ()

    def test_a_tool_that_failed_before_the_gate_still_warns(self):
        """The hole a review round found, and the reason the two questions
        were split apart.

        Arguments that fail validation, or a name the model invented, fail
        without this app being consulted: `agy`'s own error comes back
        with no mark in it. The answer that follows is missing the fetch
        just the same, and keying the warning on the mark meant saying
        nothing about it — AG-R-22 again by another road. What is withheld
        is only the claim that *we* refused it: the card carries the
        vendor's error verbatim and no sentence of ours is put on it.

        **Which is now withheld from the sentence about the answer too.**
        A sixth round called the old rendering of this by its name: the
        app cannot see what happened to a call it was never asked about,
        and "got nothing back" is a certainty it is not entitled to. It
        is warned about, under :attr:`unverified_tools`, in weaker words.
        """
        t = self.consultation()
        step = self.denied(tool_name="call_fabricated_search_tool")
        step["tool_info"] = dict(
            step["tool_info"],
            name="call_fabricated_search_tool",
            error={"type": "TOOL_ERROR", "message": "unknown tool"},
        )
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert "unknown tool" in events[1].payload["preview"]
        assert SECOND_OPINION_POLICY.MARK not in events[1].payload["preview"], (
            "the card says what came back; it does not claim our gate is why"
        )
        assert events[2].payload["subtype"] == "consultation_unverified"
        assert t.unverified_tools == ("call_fabricated_search_tool",)
        assert t.ungrounded_tools == (), (
            "nobody watched this one end; the app does not claim it did"
        )

    def test_a_reason_cut_to_nothing_changes_nothing(self):
        """What the pump reads is the policy, not the string coming back.

        The reason travels out through this app's hook, through `agy`, and
        back in an error field, and an earlier version searched it for a
        fixed mark — which made every length limit on that path a way to
        turn a refusal into an unrecognised failure. A reviewer showed the
        mark does not even arrive first: `agy` prefixes 35 characters of
        its own on a tool step, so a tail-truncating buffer keeps the
        vendor's words and cuts ours. Nothing here depends on that any
        more. One character of error is enough, because the allowlist is
        what says the call could not have run.
        """
        t = self.consultation()
        step = dict(DENIED)
        step["tool_info"] = dict(
            step["tool_info"],
            error={"type": "TOOL_ERROR", "message": "t"},
        )
        assert names(t.translate(frame(step))) == [
            "toolUse",
            "toolResult",
            "systemEvent",
        ]

    def test_a_barred_call_that_errored_is_over_whoever_said_so(self):
        """The card must not be left spinning for want of our own words.

        `agy`'s own auto-deny prose, a future hook of the user's in the
        same merged hooks file, or its schema validator — all of them
        stop a call this app's gate never saw, and an earlier version
        required our mark before settling the card. On a state word this
        pump does not read as terminal that left a pending spinner on a
        finished consultation, which a reader takes for a retrieval still
        in flight: the worst of the readings available. A tool that
        cannot run here and has reported an error has nothing further to
        say, whoever wrote the error.
        """
        t = self.consultation()
        step = self.denied(state="BLOCKED")
        step["tool_info"] = dict(
            step["tool_info"],
            error={
                "type": "TOOL_ERROR",
                "message": "tool call denied by pre-tool hook: not our words",
            },
        )
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[1].payload["status"] == "error"
        assert "not our words" in events[1].payload["preview"]

    def test_the_notice_is_scoped_to_the_consultation(self):
        """It belongs in the tab the refused cards are in.

        Unscoped it landed in the main transcript, where the answer's own
        first paragraph already says the same thing — twice in one place
        and nothing in the other. The frontend routes a system event that
        names an agent into that agent's tab.
        """
        t = AgyTranslator("r1", agent_id="consultation-9")
        t.note_consultation(self.policy.allowed, self.policy.refusals)
        notice = t.translate(frame(self.denied()))[-1].payload
        assert notice["data"]["agent_id"] == "consultation-9"

    def test_the_notice_names_no_tool_so_it_cannot_go_stale(self):
        """It fires once, at the first refusal, and further refused cards
        render beneath it.

        Naming the tool that provoked it made the sentence an inventory
        frozen at one entry — "reached for search_web" sitting above a
        refused `read_url_content`. What is left says what the *policy*
        is rather than counting what has happened, which is a fact no
        later frame can age.
        """
        t = self.consultation()
        message = t.translate(frame(self.denied()))[-1].payload["data"]["message"]
        assert "read_url_content" not in message
        assert "anything else it reaches for will come back the same way" in message

    def test_one_call_is_drawn_finished_once(self):
        """A refusal ends the call on *this app's* authority, so `agy`'s
        own ERROR frame for the same step can still arrive afterwards.

        Tool cards are keyed by call id in `blocks.js`, so a second result
        against the same id would redraw a card the reader has already
        seen settle — and would double-count the call.
        """
        t = self.consultation()
        first = t.translate(frame(self.denied(state="BLOCKED")))
        second = t.translate(frame(self.denied()))
        assert names(first) == ["toolUse", "toolResult", "systemEvent"]
        assert second == [], "the late terminal frame redraws nothing"
        assert t.ungrounded_tools == ("read_url_content",)
        assert t.stats.tool_calls == 1

    def test_a_refusal_is_not_read_off_the_state_word(self):
        """``state == "ERROR"`` is `agy`'s vocabulary, and ``TERMINAL_STATES``
        is this pump's reading of it.

        A release that spelled a denial ``BLOCKED`` would leave the card
        pending forever and the notice unraised — the refusal would be
        rendered as a call still running. That a barred tool reported an
        error is known independently of that vocabulary, so it is what
        ends the call here.
        """
        t = self.consultation()
        events = t.translate(frame(self.denied(state="BLOCKED")))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[1].payload["status"] == "error"
        assert t.ungrounded_tools == ("read_url_content",)

    def test_the_engine_raises_nothing(self):
        """No `note_consultation`, no claim. The master's denials are the
        user's own clicks, and telling them what they just decided is not
        information."""
        t = AgyTranslator("r1")
        t.translate(frame(self.denied()))
        assert t.translate(RESULT) == []
        assert t.ungrounded_tools == ()


class TestTheAppIsNeverTheOneSayingSomethingFalse:
    """Four rounds of review found the app *silent* in cases nobody had
    imagined. These are the cases where it would have been **wrong**, which
    is a different category and was argued as such by a reviewer.
    """

    def setup_method(self) -> None:
        """A stamped policy per test, as `_run` mints one per consultation.

        Not a module constant. The gate mints a **single-use** token per
        denial and the pump spends it on the way back, so a refusal frame
        built once and replayed across tests would authenticate in the
        first test and read as unverified in every one after it. Sharing
        the issuer between the frame and the pump is also what makes
        these tests honest: the token in the message below is one this
        translator's own policy issued, rather than a string the test
        handed to both sides.
        """
        self.policy = SECOND_OPINION_POLICY.stamped()

    def denied(self, **over: Any) -> dict[str, Any]:
        """A denial frame for one barred call, from the live policy.

        The token is minted for whichever tool the frame names, because
        the gate binds each one to the tool it refused — a token issued
        denying `view_file` cannot authenticate a `run_command` that
        escaped.
        """
        step = dict(DENIED, **over)
        tool = str(step.get("tool_name") or "")
        step["tool_info"] = dict(
            step["tool_info"],
            name=tool,
            error={
                "type": "TOOL_ERROR",
                "message": (
                    f"tool call denied by pre-tool hook: "
                    f"{self.policy.refusal(tool)}"
                ),
            },
        )
        return step

    def consultation(self) -> AgyTranslator:
        t = AgyTranslator("r1")
        t.note_consultation(self.policy.allowed, self.policy.refusals)
        return t

    def test_a_refusal_agy_reports_as_done_in_output_is_still_a_refusal(self):
        """The frame shape this whole invariant was added for, and the
        one the first version of it got backwards.

        A release that reports a hook-denied step as ``DONE`` with the
        refusal in ``output`` rather than ``error``: under the rule
        before that round, a green success card for a call that retrieved
        nothing, with the notice and the header both silent. Under the
        first fix for it — which read *barred and no error and output
        means it ran* — the same green card **plus** a containment alarm
        and a withdrawn assurance, for a gate that had held perfectly. A
        sixth round found that; the token minted for the denial is what
        tells the two apart, and it is this app's own, travelling in
        whichever field the vendor puts it in.
        """
        t = self.consultation()
        step = self.denied(state="DONE")
        step["tool_info"] = {
            "name": "read_url_content",
            "parameters": {"Url": "https://www.kernel.org/"},
            "output": (
                "tool call denied by pre-tool hook: "
                f"{self.policy.refusal('read_url_content')}"
            ),
        }
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[1].payload["status"] == "error", (
            "a refused call is not a success, whichever field said so"
        )
        assert events[2].payload["subtype"] == "consultation_ungrounded"
        assert t.ungrounded_tools == ("read_url_content",)
        assert t.breached_tools == (), (
            "our own sentence coming home is not a page somebody fetched"
        )
        assert t.unverified_tools == ()

    def test_a_refusal_this_app_did_not_write_is_not_read_as_its_own(self):
        """The spoof, found by a seventh round, and the reason the mark
        alone was never enough.

        ``StaticPolicy.MARK`` is a fixed sentence written in this
        repository's own source and specs. A test that asks "does the
        output contain the mark" is therefore a **content** test: a gate
        that failed open, plus a consultation that read this file, plus
        a model that echoed what it read, passes it. That path ends with
        a genuine containment breach filed as a refusal — the alarm
        suppressed, the escaped bytes on a card labelled *denied*, and
        the asking model told in this app's own voice that nothing was
        read.

        The nonce is what closes it. It is minted per consultation from
        `secrets`, never written down, and reaches the model only if the
        gate really refused a call and the vendor really carried our
        sentence back. Here the output carries the mark and a *wrong*
        nonce, which is the strongest form the forgery can take.
        """
        t = self.consultation()
        step = self.denied(state="DONE", tool_name="view_file")
        step["tool_info"] = {
            "name": "view_file",
            "parameters": {"AbsolutePath": "/repo/src/aic_dc/agy/gate_server.py"},
            "output": (
                f'    MARK = "{StaticPolicy.MARK}"\n'
                "    # [ref deadbeefdeadbeef] and the rest of the file\n"
            ),
        }
        events = t.translate(frame(step))
        assert [e.payload.get("subtype") for e in events if e.name == "systemEvent"] == [
            "consultation_breach"
        ], "a barred read that came back with this file in it is an escape"
        assert t.breached_tools == ("view_file",)
        assert t.ungrounded_tools == (), (
            "quoting this app's refusal is not being refused by it"
        )
        assert t.unverified_tools == ()

    def test_a_token_the_consultant_has_already_seen_is_worth_nothing(self):
        """The reflection attack, and the reason a token is spent.

        The consultant **reads every refusal this app sends it** — that
        is what the reason is for, prose written to steer a refused model
        into answering instead of looking for another route. So one
        denial hands the model the token. If the gate then fails open, a
        tool that quotes the conversation, echoes its own arguments, or
        errors naming them carries that token home in ``output``, and
        `_outcome` tests for a refusal *before* it tests for output: the
        escape is filed as a denial, the containment alarm never fires,
        and the asking model is told nothing was read. An eighth review
        round put that attack, and it is why the first cut of this —
        one token minted per consultation — was unsound.

        Single use closes it by causality rather than by secrecy. The
        frame that showed the token to the model went through this pump
        first, which spent it; by the time the model can repeat it, it
        authenticates nothing.
        """
        t = self.consultation()
        first = t.translate(frame(self.denied()))
        assert t.ungrounded_tools == ("read_url_content",), (
            "the genuine refusal has to authenticate, or this proves nothing"
        )
        leaked = first[1].payload["preview"]
        assert "[ref " in leaked, "the model was shown no token to reflect"

        escaped = self.denied(state="DONE", step_index=4)
        escaped["tool_info"] = {
            "name": "read_url_content",
            "parameters": {"Url": "https://www.kernel.org/"},
            # The gate failed open, the call ran, and what came back
            # quotes the transcript — which now contains the refusal the
            # model was shown, token and all.
            "output": f"<html>Linux kernel 6.19</html>\n<!-- {leaked} -->",
        }
        events = t.translate(frame(escaped))
        assert [e.payload.get("subtype") for e in events if e.name == "systemEvent"] == [
            "consultation_breach"
        ], "a spent token authenticated a second call"
        assert t.breached_tools == ("read_url_content",)
        assert t.ungrounded_tools == ("read_url_content",), (
            "and the first call is still a refusal, because it was one"
        )

    def test_a_token_minted_for_one_tool_cannot_vouch_for_another(self):
        """The residue of the reflection attack, narrowed.

        Single use leaves one live case: a denial whose frame never
        arrived leaves its token unspent, and a later escape could
        reflect it. The gate binds each token to the tool it refused, so
        that reflection has to come from the same tool as well — which
        does not close the case, but costs an attacker a dropped frame,
        an open gate, a reflection *and* a coincidence of names.
        """
        t = self.consultation()
        # Denied, and the frame is never translated — the token stays
        # outstanding, exactly as a dropped frame would leave it.
        leaked = self.policy.refusal("view_file")
        assert self.policy.refusals.outstanding == 1
        step = self.denied(state="DONE", tool_name="run_command")
        step["tool_info"] = {
            "name": "run_command",
            "parameters": {"Command": "cat /etc/passwd"},
            "output": f"root:x:0:0:root:/root:/bin/bash\n{leaked}",
        }
        events = t.translate(frame(step))
        assert [e.payload.get("subtype") for e in events if e.name == "systemEvent"] == [
            "consultation_breach"
        ]
        assert t.breached_tools == ("run_command",)
        assert self.policy.refusals.outstanding == 2, (
            "and neither token was spent by a call it was not issued for"
        )

    def test_a_consultation_nobody_stamped_claims_nothing(self):
        """The fail-safe, for the one wiring mistake this design can make.

        `AgyConsultant._run` stamps the policy before it starts the pump.
        If some later path forgets to — a new caller, a refactor that
        threads the policy but not the nonce — then no refusal can ever
        be authenticated. The question is what the app says then, and
        the answer has to be *nothing*: an unauthenticated refusal is
        exactly the state `UNVERIFIED` names, and defaulting it to
        `REFUSED` would restore the forgeable test by accident, in the
        one configuration where nobody is looking.
        """
        t = AgyTranslator("r1")
        t.note_consultation(SECOND_OPINION_POLICY.allowed)
        events = t.translate(frame(self.denied()))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[2].payload["subtype"] == "consultation_unverified"
        assert t.unverified_tools == ("read_url_content",)
        assert t.ungrounded_tools == (), (
            "the mark is in that message, and it is still not evidence"
        )
        assert t.breached_tools == (), (
            "and an unstamped consultation is not an escape either: the "
            "call reported no output, so there is nothing to retract"
        )

    def test_a_barred_call_that_completed_clean_ran_even_with_nothing_to_show(self):
        """`DONE`, no error, no output — and that is an escape, not a doubt.

        This test asserted `UNVERIFIED` until P28 measured the wire. The
        premise was that a call carrying neither a refusal nor any output
        is a call the app cannot account for. It is not: *every* shape
        P28 captured that did not run carries an `error` — this app's own
        denial, or the vendor's schema rejection — and `tool_info.output`
        is per-tool, absent even from a **completed** `find_by_name`. So
        a barred call that reaches `DONE` clean is one that ran and had
        nothing to print, which is exactly the silent breach a review
        round asked about: a `run_command` that writes a file and says
        nothing.

        The round's own proposed repair, `output is not None`, would not
        have caught this frame at all — there is no `output` key to test.
        """
        t = self.consultation()
        step = self.denied(state="DONE")
        step["tool_info"] = {
            "name": "read_url_content",
            "parameters": {"Url": "https://www.kernel.org/"},
        }
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "systemEvent", "toolResult"]
        assert events[1].payload["subtype"] == "consultation_breach"
        assert events[2].payload["status"] == "error", (
            "a breach is a failure of the policy, so the card is not green"
        )
        assert t.breached_tools == ("read_url_content",)
        assert t.unverified_tools == ()
        assert t.ungrounded_tools == ()

    def test_a_rejection_the_vendor_wrote_is_still_unaccounted_for(self):
        """P28's measured schema rejection: `DONE`, and an error not ours.

        The frame is verbatim from the wire — `agy` reported a malformed
        `read_url_content` as **`DONE`** in 8ms, with the parser's
        complaint in `tool_info.error` and, tellingly, **no `parameters`**,
        because the arguments never validated and so were never carried
        back.

        It stays `UNVERIFIED`, and the reason is the whole of this
        round's argument. On the wire it is indistinguishable from a
        barred call that escaped the gate and then failed in the host —
        both are a barred tool, a terminal state, an error this app did
        not write, and no token. Separating them means parsing the
        vendor's prose, counting milliseconds, or testing whether
        `parameters` came back, and those rot on a weekly release. The
        error key is what keeps this out of the breach list, and its
        presence is the only thing being read.
        """
        t = self.consultation()
        step = self.denied(state="DONE")
        step["tool_info"] = {
            "name": "read_url_content",
            "error": {
                "type": "TOOL_ERROR",
                "message": (
                    "invalid arguments:\n- missing properties 'Url', "
                    "'toolSummary', 'toolAction'\n- additional properties "
                    "'url' not allowed"
                ),
            },
        }
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[1].payload["status"] == "error", (
            "the state word is the vendor's; the allowlist is ours"
        )
        assert events[2].payload["subtype"] == "consultation_unverified"
        assert t.unverified_tools == ("read_url_content",)
        assert t.ungrounded_tools == ()
        assert t.breached_tools == ()

    def test_an_error_whose_message_a_serialiser_dropped_is_still_an_error(self):
        """`agy` is Go, and Go omits empty fields.

        The first draft of the breach test read `state == "DONE" and not
        message`, and a reviewer took it apart on this: an error whose
        message is the empty string arrives with the *message* gone and
        the `type` still there, so `bool(message)` would call it a clean
        completion and the app would manufacture a containment alarm out
        of a vendor serialising a default. The test is the presence of
        the `error` key, which survives an empty message, an error
        spelled as a bare string, and an explicit `null`.
        """
        for error in ({"type": "TOOL_ERROR"}, "invalid arguments", None):
            t = self.consultation()
            step = self.denied(state="DONE")
            step["tool_info"] = {"name": "read_url_content", "error": error}
            events = t.translate(frame(step))
            assert [
                e.payload.get("subtype") for e in events if e.name == "systemEvent"
            ] == ["consultation_unverified"], (
                f"an error spelled {error!r} is a call that reported failing"
            )
            assert t.breached_tools == ()

    def test_a_tool_this_pump_cannot_name_is_barred(self):
        """An allowlist that cannot identify the call must answer no.

        The names come from the vendor's keys. A release that renamed
        them would empty every one, and the version of this that required
        a name would have called the whole consultation permitted.
        """
        t = self.consultation()
        step = {
            "conversation_id": "3e34efc6",
            "step_index": 2,
            "state": "ERROR",
            "step_type": "tool",
            "tool_info": {"error": "tool call denied by pre-tool hook"},
        }
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult", "systemEvent"]
        assert events[1].payload["status"] == "error"
        assert t.unverified_tools == (UNNAMED_TOOL,), (
            "counted, and not named, because the pump has no name to give"
        )
        assert t.ungrounded_tools == (), (
            "the vendor's error carries no mark of ours, so this app does "
            "not put its own certainty on it"
        )

    def test_a_barred_call_that_came_back_with_data_is_an_escape(self):
        """The one observation that makes the bridge's header a lie.

        Output with no mark in it from a tool the gate bars: this app
        never wrote those bytes, so something else did, which means the
        call ran.

        Not normalised into a refusal, which is what "never let a barred
        tool succeed" means taken literally: that would file a
        containment failure as a denial and leave the header saying
        nothing was read. But it is still a *failure* — the card's word
        is binary and a green one for an escaped call is indefensible,
        which the sixth round pointed out about the first cut of this.
        What makes it different is said in the row above it, where it
        cannot be mistaken for the vendor's opinion of the call.
        """
        t = self.consultation()
        step = self.denied(state="DONE")
        step["tool_info"] = {
            "name": "read_url_content",
            "parameters": {"Url": "https://www.kernel.org/"},
            "output": "<html>Linux kernel 6.19</html>",
        }
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "systemEvent", "toolResult"]
        assert events[1].payload["subtype"] == "consultation_breach", (
            "not `engine_error`, which renders as the engine reporting a "
            "fault and collapses under the next one of its kind"
        )
        assert "it ran" in events[1].payload["data"]["message"]
        assert "agent_id" in events[1].payload["data"], (
            "routed into the consultation's tab, where `onSystemEvent` "
            "reads it from — beside the card it contradicts"
        )
        assert events[2].payload["status"] == "error", (
            "a breach is not a success; the alarm above says which kind "
            "of not-success it is"
        )
        assert events[2].payload["preview"] == "<html>Linux kernel 6.19</html>", (
            "and the card still shows what came back, which is the "
            "evidence somebody now has to go and look at"
        )
        assert t.breached_tools == ("read_url_content",)
        assert t.ungrounded_tools == (), (
            "that call was grounded — in the worst possible way"
        )

    def test_data_that_came_back_with_an_error_beside_it_is_still_an_escape(self):
        """Hole A, from the sixth round, and the sharpest of the three.

        A command that writes to stdout and then exits non-zero; a read
        that returns a page and warns that it truncated it. The first cut
        of the breach test required *no* error — ``barred and not message
        and bool(output)`` — so a single byte in ``tool_info.error``
        turned an escape back into a refusal, and the app then told the
        asking model that nothing had been read about material sitting in
        that model's context. That is the failure the breach test exists
        to catch, reachable by adding an error message to it.
        """
        t = self.consultation()
        step = self.denied(state="ERROR")
        step["tool_info"] = {
            "name": "read_url_content",
            "parameters": {"Url": "https://www.kernel.org/"},
            "output": "<html>Linux kernel 6.19</html>",
            "error": {"type": "TOOL_ERROR", "message": "response truncated"},
        }
        events = t.translate(frame(step))
        assert [e.payload.get("subtype") for e in events if e.name == "systemEvent"] == [
            "consultation_breach"
        ]
        assert t.breached_tools == ("read_url_content",)
        assert t.ungrounded_tools == ()
        assert t.unverified_tools == ()

    def test_an_allowed_tool_that_errors_under_done_is_not_drawn_green(self):
        """Hole C in the consultation, where the allowlist holds one name.

        ``finish`` is permitted, so the barred branch says nothing about
        it, and before the sixth round that left its verdict resting on
        the vendor's state word alone.
        """
        t = self.consultation()
        step = self.denied(state="DONE", tool_name="finish")
        step["tool_info"] = dict(
            step["tool_info"], name="finish", error="could not finish"
        )
        events = t.translate(frame(step))
        assert names(events) == ["toolUse", "toolResult"]
        assert events[1].payload["status"] == "error"
        assert events[1].payload["preview"] == "could not finish"
        assert t.ungrounded_tools == ()
        assert t.unverified_tools == (), (
            "nothing is claimed about a call the policy allowed"
        )

    def test_a_call_that_never_reported_is_closed_when_the_turn_ends(self):
        """A crash, a timeout or a token ceiling between the two frames
        used to leave a spinner under a finished answer, which reads as a
        retrieval still in flight."""
        t = self.consultation()
        opened = t.translate(frame(dict(TOOL_ACTIVE, tool_name="view_file")))
        assert names(opened) == ["toolUse"]
        closing = t.stream_complete()
        assert names(closing)[:2] == ["toolResult", "systemEvent"]
        assert closing[0].payload["status"] == "error"
        assert "ended before this call reported" in closing[0].payload["preview"]
        assert "not known" in closing[0].payload["preview"], (
            "a killed call is not a refused one: whether anything came "
            "back before the process died is the thing nobody saw"
        )
        assert closing[1].payload["subtype"] == "consultation_unverified"
        assert t.unverified_tools == ("view_file",)
        assert t.ungrounded_tools == ()

    def test_a_call_that_reported_is_not_closed_twice(self):
        t = self.consultation()
        t.translate(frame(self.denied()))
        assert names(t.stream_complete()) == ["turnUsage", "streamComplete"]

    def test_the_engine_closes_its_own_stragglers_and_claims_nothing(self):
        """The spinner outlives a master-engine turn too, and that half is
        not about grounding: the card is closed, and no consultation
        sentence is attached to a session that never had a policy."""
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        closing = t.stream_complete()
        assert names(closing) == ["toolResult", "turnUsage", "streamComplete"]
        assert t.ungrounded_tools == ()
        assert t.unverified_tools == ()


class TestASubagentIsAnnounced:
    """The delegation surface, transcribed from the 2026-09-09 capture.

    ``scripts/probe_agy_subagent_frames.py``, one turn at ``agy`` 1.1.27.
    The fixture is the real ``subagent_info`` payload with the prompt
    shortened; every field the pump reads is verbatim, because the
    recorded reason this surface was unbuilt here — "``agy``'s stream
    carries no trajectory or depth field at all" — was true about
    ``depth`` and wrong about subagents, and a fixture written from that
    belief would have kept it true.
    """

    def test_the_announcement_carries_one_identity_in_all_three_fields(self):
        """`streaming.js` falls back through them; setting one is a bet."""
        t = AgyTranslator("r1")
        events = t.translate(frame(SUBAGENT_ACTIVE))
        assert names(events) == ["subagentEvent"]
        payload = events[0].payload
        assert (
            payload["task_id"]
            == payload["agent_id"]
            == payload["tool_use_id"]
            == "c21acd4d"
        )

    def test_the_id_is_the_subagents_own_conversation(self):
        """Borrowed, not minted — it is also the key to the transcript."""
        t = AgyTranslator("r1")
        assert t.translate(frame(SUBAGENT_ACTIVE))[0].payload["agent_id"] == "c21acd4d"

    def test_the_labels_are_the_role_and_the_type(self):
        t = AgyTranslator("r1")
        payload = t.translate(frame(SUBAGENT_ACTIVE))[0].payload
        assert payload["description"] == "Notes Reader"
        assert payload["subagent_type"] == "research"

    def test_it_is_not_terminal_while_active(self):
        t = AgyTranslator("r1")
        payload = t.translate(frame(SUBAGENT_ACTIVE))[0].payload
        assert payload["terminal"] is False
        assert payload["status"] == "running"

    def test_done_settles_the_tab(self):
        """`state.streaming = !row.terminal`: without this it spins forever."""
        t = AgyTranslator("r1")
        t.translate(frame(SUBAGENT_ACTIVE))
        payload = t.translate(frame(SUBAGENT_DONE))[0].payload
        assert payload["terminal"] is True
        assert payload["status"] == "completed"

    def test_the_id_is_stable_across_the_two_frames(self):
        """A second id would be a second row for one subagent."""
        t = AgyTranslator("r1")
        first = t.translate(frame(SUBAGENT_ACTIVE))[0].payload["agent_id"]
        second = t.translate(frame(SUBAGENT_DONE))[0].payload["agent_id"]
        assert first == second

    @pytest.mark.parametrize(
        "state,status",
        [("ERROR", "failed"), ("CANCELED", "stopped"), ("DONE", "completed")],
    )
    def test_the_status_words_are_the_ones_the_led_table_knows(self, state, status):
        """`_TERMINAL_LED` maps these three; anything else lands on amber
        by not being understood, which is the wrong way to reach a colour."""
        t = AgyTranslator("r1")
        payload = t.translate(frame(dict(SUBAGENT_DONE, state=state)))[0].payload
        assert payload["status"] == status

    def test_each_subagent_in_one_step_gets_its_own_row(self):
        """`subagents` is a list, and one step can announce several."""
        t = AgyTranslator("r1")
        both = dict(
            SUBAGENT_ACTIVE,
            subagent_info={
                "subagents": [
                    SUBAGENT_ENTRY,
                    dict(SUBAGENT_ENTRY, conversation_id="second", role="Other"),
                ]
            },
        )
        events = t.translate(frame(both))
        assert [e.payload["agent_id"] for e in events] == ["c21acd4d", "second"]

    def test_a_delegation_with_no_conversation_id_still_gets_a_row(self):
        """This pump renders what it cannot read rather than dropping it."""
        t = AgyTranslator("r1")
        entry = {k: v for k, v in SUBAGENT_ENTRY.items() if k != "conversation_id"}
        events = t.translate(
            frame(dict(SUBAGENT_ACTIVE, subagent_info={"subagents": [entry]}))
        )
        assert names(events) == ["subagentEvent"]
        assert events[0].payload["agent_id"] == "agy-subagent-2-0"

    def test_a_subagent_step_is_no_longer_an_unknown_step(self):
        """It rendered as two `unknown_step` notices before 2026-09-09,
        with the role, the prompt and the transcript all discarded."""
        t = AgyTranslator("r1")
        assert "subagent" in steps.KNOWN_STEP_TYPES
        for event in t.translate(frame(SUBAGENT_ACTIVE)):
            assert event.payload.get("subtype") != "unknown_step"

    def test_the_transcript_location_is_kept_for_the_tabs_content(self):
        """`log_uri` is reported once, in the announcement, and is the
        only route to what the subagent actually did."""
        t = AgyTranslator("r1")
        t.translate(frame(SUBAGENT_ACTIVE))
        assert t._subagents["c21acd4d"]["log_uri"].endswith("transcript.jsonl")

    def test_an_empty_announcement_emits_nothing(self):
        t = AgyTranslator("r1")
        assert t.translate(frame(dict(SUBAGENT_ACTIVE, subagent_info={}))) == []


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

    def test_a_success_reports_no_terminal_reason(self):
        """An unrecognised reason draws a red badge, so a clean turn sends none.

        Phase 3's lesson on the SDK side: forwarding `UNSPECIFIED` would
        have put a red badge reading `UNSPECIFIED` on every clean turn.
        `SUCCESS` is not promoted to `completed` for the mirror of that
        reason — a green tick is a claim, and `agy` says `SUCCESS` about
        a turn held open until `--print-timeout` expired.
        """
        t = AgyTranslator("r1")
        t.translate(RESULT)
        complete = t.stream_complete()[-1].payload
        assert complete["terminal_reason"] == ""

    def test_a_cancel_lands_in_the_browsers_own_word(self):
        """And brings `cancelled` with it, the way the Claude pump does.

        `agy` reports `CANCELED` both for a turn the user stopped and for
        a headless permission denial. Neither is a fault, so both keep the
        LED green — which is what `cancelled` is read for.
        """
        t = AgyTranslator("r1")
        t.translate({"event": "result", "result": {"status": "CANCELED"}})
        payload = t.stream_complete()[-1].payload
        assert payload["terminal_reason"] == "aborted_streaming"
        assert payload["cancelled"] is True

    def test_an_unmapped_status_passes_through_lower_cased(self):
        """A word this build has never seen still has to badge legibly.

        The browser labels an unmapped reason by turning underscores into
        spaces, so the case is all that stands between it and shouting.
        """
        t = AgyTranslator("r1")
        t.translate({"event": "result", "result": {"status": "PAYWALL"}})
        assert t.stream_complete()[-1].payload["terminal_reason"] == "paywall"

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
        assert payload["tool_calls"] == 1
        assert set(payload) == {
            "request_id",
            "terminal_reason",
            "cancelled",
            "tool_calls",
            "permission_prompts",
            "files_modified",
            "usage",
            "response",
        }


class TestTheSharedAccountingObject:
    """`stats` is a contract with code this translator never mentions.

    `AntigravityService._note_permission_prompt` — inherited by
    `AgyService` — reaches straight into `translator.stats` to attribute a
    dialog to the turn that caused it. This translator did not have the
    attribute, so every permission dialog on the `agy` transport raised
    `AttributeError` there. It was caught and logged rather than surfaced,
    so the gate kept working and only the turn's prompt count was lost,
    which is why the whole suite stayed green through it.

    Found by reading the server log during the phase-8 live write run, not
    by a test — so these pin the shape a caller in another module relies
    on.
    """

    def test_a_fresh_translator_exposes_the_stats_a_caller_reaches_into(self):
        t = AgyTranslator("r1")
        assert t.stats.permission_prompts == 0
        assert t.stats.tool_calls == 0

    def test_the_service_can_count_a_prompt_against_the_turn(self):
        # Written as the caller writes it, deliberately: the defect was an
        # attribute error at exactly this expression.
        t = AgyTranslator("r1")
        t.stats.permission_prompts += 1
        assert t.stats.permission_prompts == 1

    def test_tool_calls_are_counted_on_the_same_object_the_stream_reports(self):
        # One counter, not two. The footer's count used to read a private
        # field that `stats` duplicated, which is the drift that lets a
        # HUD and a stream payload disagree about one turn.
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        assert t.stats.tool_calls == 1
        assert t.stream_complete()[-1].payload["tool_calls"] == 1


def config_root(tmp_path, name="root"):
    """A private ``agy`` config root, laid out the way ``roots`` expects.

    AG-21 made both the scratch directory and the brain tree properties of
    the root a translator was built against, replacing the two module
    constants these tests used to monkeypatch. That is the point of
    AG-R-18: a constant pinned to the server's own ``HOME`` at import time
    is silently wrong as soon as there is more than one root, and a test
    that patches the constant cannot notice.
    """
    root = tmp_path / name
    (root / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    return root


def scratch_of(root):
    """Where a diverted write lands, for the given root."""
    path = root / ".gemini" / "antigravity-cli" / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def brain_of(root, conversation):
    """Where ``agy`` writes a conversation's artefacts, for the given root."""
    path = root / ".gemini" / "antigravity-cli" / "brain" / conversation
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestADivertedWriteIsReported:
    """AG-R-3, turned from silence into a sentence.

    `agy` writes a file into its own scratch directory, tells the model it
    succeeded, and the file tree and diff viewer — both rooted at the repo
    — show nothing. The user's reading is "the agent lied about editing my
    file", and there is no path from that symptom to the cause.

    `risks.md` specifies a *startup* check asserting the repo root is a
    workspace the engine will write to. That cannot be built honestly: a
    check phrased against `trustedWorkspaces` passes on a machine where
    writes divert anyway — measured three times on 2026-09-05, from inside
    a trusted root — and the only truthful check is a real write, which
    costs a turn on the user's subscription at every startup. So the check
    runs where it is free, on a completed write that already names its
    target.
    """

    def _done(self, target):
        return frame({
            "conversation_id": "c",
            "step_index": 2,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "write_to_file",
            "tool_info": {
                "name": "write_to_file",
                "parameters": {"TargetFile": str(target), "CodeContent": "x"},
            },
        })

    def test_a_write_that_landed_says_nothing(self, tmp_path):
        target = tmp_path / "landed.txt"
        target.write_text("x", encoding="utf-8")
        events = AgyTranslator(
            "r1", config_root=config_root(tmp_path)
        ).translate(self._done(target))
        assert not [e for e in events if e.name == "systemEvent"]

    def test_a_missing_file_alone_is_not_reported(self, tmp_path, monkeypatch):
        """Narrow on purpose: "missing" has innocent explanations.

        The model may name a path it never created, or the tool may have
        failed for an unrelated reason. A false alarm about a write that
        did land would be worse than the silence it replaces, so only the
        pair — missing *here*, present *there* — has no innocent reading.
        """
        root = config_root(tmp_path)
        scratch_of(root)
        events = AgyTranslator("r1", config_root=root).translate(
            self._done(tmp_path / "gone.txt")
        )
        assert not [e for e in events if e.name == "systemEvent"]

    def test_missing_here_and_present_in_scratch_is_reported(
        self, tmp_path, monkeypatch
    ):
        root = config_root(tmp_path)
        scratch = scratch_of(root)
        (scratch / "diverted.txt").write_text("the real content", encoding="utf-8")

        events = AgyTranslator("r1", config_root=root).translate(
            self._done(tmp_path / "diverted.txt")
        )
        notices = [e for e in events if e.name == "systemEvent"]
        assert len(notices) == 1
        message = notices[0].payload["data"]["message"]
        assert "diverted.txt" in message
        assert str(scratch) in message
        # It must say the edit is not lost. A user told only "the file is
        # not there" would redo work that has already been done.
        assert "content is in that file" in message

    def test_the_tool_card_still_reports_what_agy_said(self, tmp_path, monkeypatch):
        """The correction is beside the card, not folded into it.

        `agy` reported success and the card says so; a card rewritten to
        say "failed" would be this pump asserting something the engine did
        not, and the two disagreeing is exactly the information the user
        needs.
        """
        root = config_root(tmp_path)
        (scratch_of(root) / "d.txt").write_text("x", encoding="utf-8")
        events = AgyTranslator("r1", config_root=root).translate(
            self._done(tmp_path / "d.txt")
        )
        assert [e.name for e in events] == ["toolUse", "systemEvent", "toolResult"]

    def test_a_read_is_never_checked(self, tmp_path, monkeypatch):
        root = config_root(tmp_path)
        (scratch_of(root) / "v.txt").write_text("x", encoding="utf-8")
        events = AgyTranslator("r1", config_root=root).translate(frame({
            "conversation_id": "c", "step_index": 3, "state": "DONE",
            "step_type": "tool", "tool_name": "view_file",
            "tool_info": {
                "name": "view_file",
                "parameters": {"TargetFile": str(tmp_path / "v.txt")},
            },
        }))
        assert not [e for e in events if e.name == "systemEvent"]


CONVERSATION = "7ebd27bf-f356-4c88-9294-79f566bc0983"


def image_step(image_name="ai_test_pattern", state="DONE", **params):
    """A ``generate_image`` step shaped like the real one.

    The parameters are transcribed from the call recorded in `agy`'s own
    conversation store on 2026-09-08 — `AspectRatio`, `ImageName`,
    `Prompt`, and no path of any spelling, because the tool declares none.
    """
    return frame({
        "conversation_id": CONVERSATION,
        "step_index": 4,
        "state": state,
        "step_type": "tool",
        "tool_name": "generate_image",
        "tool_info": {
            "name": "generate_image",
            "parameters": {
                "AspectRatio": "4:3",
                "ImageName": image_name,
                "Prompt": "a test pattern",
                **params,
            },
        },
    })


class TestAGeneratedImageIsCollected:
    """The engine's half of what phase 10 built for the consultant.

    `generate_image` takes `ImageName` and no path — the schema sets
    `additionalProperties: false`, so one cannot be added — and `agy`
    writes into `brain/<conversation_id>/`, outside the repository. The
    consultant has copied that file in since 2026-09-08; the engine did
    not, so an image generated in an ordinary turn was produced correctly
    and then invisible to the file tree, the viewer, and the user.

    Measured against the first real occurrence: a 1200×896 JPEG that the
    user watched succeed and could not find.
    """

    def _translator(self, tmp_path, monkeypatch, *, repo=None, root=None):
        repo = repo if repo is not None else tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        return AgyTranslator(
            "r1",
            repo_root=repo,
            conversation_id=CONVERSATION,
            config_root=root or config_root(tmp_path),
        )

    def _generated(self, tmp_path, name="ai_test_pattern_1788856211696.jpg"):
        source = brain_of(config_root(tmp_path), CONVERSATION) / name
        source.write_bytes(b"\xff\xd8\xff\xe0 a jpeg")
        return source

    def _result(self, events):
        return next(e for e in events if e.name == "toolResult")

    def test_the_image_lands_in_the_repository_under_the_models_name(
        self, tmp_path, monkeypatch
    ):
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path)

        translator.translate(image_step())

        landed = tmp_path / "repo" / "ai_test_pattern.jpg"
        assert landed.is_file()
        assert landed.read_bytes() == b"\xff\xd8\xff\xe0 a jpeg"

    def test_the_extension_is_the_one_agy_produced(self, tmp_path, monkeypatch):
        """A request for `.png` produced JPEG, measured 2026-09-08.

        The tool honours no requested extension, so naming the file after
        what was asked for would produce a `.png` that every viewer then
        refuses to open.
        """
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path, "icon_1788856211696.jpeg")

        translator.translate(image_step("icon"))

        assert (tmp_path / "repo" / "icon.jpeg").is_file()
        assert not (tmp_path / "repo" / "icon.png").exists()

    def test_the_file_tree_is_told_which_file_appeared(self, tmp_path, monkeypatch):
        """`files_modified` was empty, which is the visible half of the bug.

        `files_written_by` is a table of tools to their *path arguments*
        and this tool has none, so it answered `[]` and the tree never
        reloaded — the image could have been in the repository and still
        not shown up.
        """
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path)

        result = self._result(translator.translate(image_step()))

        landed = str(tmp_path / "repo" / "ai_test_pattern.jpg")
        assert result.payload["files_modified"] == [landed]
        assert translator.stats.files_modified == [landed]
        assert landed in result.payload["preview"]

    def test_a_call_still_running_collects_nothing(self, tmp_path, monkeypatch):
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path)

        events = translator.translate(image_step(state="ACTIVE"))

        assert names(events) == ["toolUse"]
        assert not list((tmp_path / "repo").iterdir())

    def test_a_failed_call_collects_nothing(self, tmp_path, monkeypatch):
        """An image `agy` says it did not make is not one to go looking for.

        The directory is the conversation's, not the call's, so a failed
        call plus an earlier success is exactly the shape that would
        collect the wrong picture and report it as this one's.
        """
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path)

        result = self._result(translator.translate(image_step(state="ERROR")))

        assert result.payload["files_modified"] == []
        assert not list((tmp_path / "repo").iterdir())

    def test_an_earlier_turns_image_is_not_collected(self, tmp_path, monkeypatch):
        """The guard the consultant does not need and the engine does.

        A consultation is one process holding one conversation for one
        call. An engine conversation is long-lived and resumable, so its
        directory accumulates every image of every turn — and a model that
        reuses an `ImageName` would otherwise have last turn's picture
        collected and reported as this one's.
        """
        translator = self._translator(tmp_path, monkeypatch)
        stale = self._generated(tmp_path)
        os.utime(stale, (0, translator._started_at - 3600))

        result = self._result(translator.translate(image_step()))

        assert result.payload["files_modified"] == []
        assert not list((tmp_path / "repo").iterdir())

    def test_a_second_image_does_not_overwrite_the_first(self, tmp_path, monkeypatch):
        translator = self._translator(tmp_path, monkeypatch)
        (tmp_path / "repo" / "ai_test_pattern.jpg").write_bytes(b"the first one")
        self._generated(tmp_path)

        result = self._result(translator.translate(image_step()))

        assert (tmp_path / "repo" / "ai_test_pattern.jpg").read_bytes() == b"the first one"
        landed = tmp_path / "repo" / "ai_test_pattern_1788856211696.jpg"
        assert landed.is_file()
        assert result.payload["files_modified"] == [str(landed)]

    def test_a_name_that_is_a_path_cannot_leave_the_repository(
        self, tmp_path, monkeypatch
    ):
        """The schema asks for a name; nothing enforces that it is one.

        A separator in it reaches the destination as a directory, so the
        basename is taken rather than trusted. The image is still
        collected — refusing it would lose a picture over its name.
        """
        translator = self._translator(tmp_path, monkeypatch)
        nested = brain_of(config_root(tmp_path), CONVERSATION) / "sub"
        nested.mkdir(parents=True)
        (nested / "icon_1788856211696.jpg").write_bytes(b"x")

        translator.translate(image_step("sub/icon"))

        assert (tmp_path / "repo" / "icon.jpg").is_file()
        assert not (tmp_path / "repo" / "sub").exists()

    def test_a_name_that_climbs_out_writes_nothing_at_all(
        self, tmp_path, monkeypatch
    ):
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path, "escape_1788856211696.jpg")

        translator.translate(image_step("../../escape"))

        assert not list((tmp_path / "repo").iterdir())
        assert not (tmp_path.parent / "escape.jpg").exists()

    def test_an_image_that_cannot_be_found_does_not_lose_the_turn(
        self, tmp_path, monkeypatch
    ):
        """A pump that raised would cost the rest of the turn a picture."""
        translator = self._translator(tmp_path, monkeypatch)

        result = self._result(translator.translate(image_step()))

        assert result.payload["status"] == "success"
        assert result.payload["files_modified"] == []

    def test_the_consultants_translator_collects_nothing_here(
        self, tmp_path, monkeypatch
    ):
        """It builds its own and collects after the turn, from the frames.

        Two collections of the same image would put it in the repository
        twice under different names, since the consultant's caller supplies
        the destination and the engine derives one.
        """
        self._generated(tmp_path)

        result = self._result(
            AgyTranslator(
                "r1", agent_id="ag-1", config_root=config_root(tmp_path)
            ).translate(image_step())
        )

        assert result.payload["files_modified"] == []

    def test_a_call_with_no_name_at_all_is_survivable(self, tmp_path, monkeypatch):
        translator = self._translator(tmp_path, monkeypatch)
        self._generated(tmp_path)

        result = self._result(translator.translate(image_step(image_name="")))

        assert result.payload["files_modified"] == []


class TestTheLocatorIsSharedAndDiffers:
    """One locator, one option, and the option is the whole difference."""

    def _brain(self, tmp_path, *names):
        directory = tmp_path / CONVERSATION
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_bytes(b"x")
        return tmp_path

    def test_the_name_the_model_chose_is_matched_with_agys_suffix(self, tmp_path):
        brain = self._brain(tmp_path, "duck_1788856211696.jpg", "other_17.jpg")
        found = steps.locate_generated_image(brain, CONVERSATION, "duck")
        assert found.name == "duck_1788856211696.jpg"

    def test_the_engine_does_not_fall_back_to_the_newest_file(self, tmp_path):
        brain = self._brain(tmp_path, "something_else_17.jpg")
        assert steps.locate_generated_image(brain, CONVERSATION, "duck") is None

    def test_the_consultant_does(self, tmp_path):
        brain = self._brain(tmp_path, "something_else_17.jpg")
        found = steps.locate_generated_image(
            brain, CONVERSATION, "duck", allow_newest=True
        )
        assert found.name == "something_else_17.jpg"

    def test_a_conversation_with_no_directory_is_not_an_error(self, tmp_path):
        assert steps.locate_generated_image(tmp_path, CONVERSATION, "duck") is None
        assert steps.locate_generated_image(tmp_path, "", "duck") is None

    def test_sub_directories_are_skipped_because_an_image_is_a_file(self, tmp_path):
        brain = self._brain(tmp_path)
        (brain / CONVERSATION / "duck_1").mkdir()
        assert steps.locate_generated_image(brain, CONVERSATION, "duck") is None
class TestAStoppedSubagentSaysStopped:
    """The word on a stopped row is this host's, and it has to be.

    ``scripts/probe_agy_subagent_stop.py`` starved a real subagent on
    2026-09-10 and ``agy`` reported the step **DONE**, not ``CANCELED`` —
    defensible from the harness's side, since the agent did read the
    refusal and wind down, and wrong on a row, because ``DONE`` maps to
    ``completed`` and puts a green LED over work a user stopped.

    So the override is deliberate and narrow: it changes the *word*, never
    the terminality, and only for an id ``stop_task`` recorded.
    """

    AGENT = SUBAGENT_ENTRY["conversation_id"]

    def test_a_stopped_subagent_is_stopped_rather_than_completed(self):
        t = AgyTranslator("r1")
        t.translate(frame(SUBAGENT_ACTIVE))
        t.mark_stopped(self.AGENT)
        payload = t.translate(frame(SUBAGENT_DONE))[0].payload
        assert payload["status"] == "stopped"
        assert payload["terminal"] is True

    def test_an_untouched_subagent_still_completes(self):
        """The negative control: without ⏹ the stream's word stands.

        Amber everywhere would satisfy the assertion above while meaning
        the LED had stopped distinguishing anything.
        """
        t = AgyTranslator("r1")
        t.translate(frame(SUBAGENT_ACTIVE))
        payload = t.translate(frame(SUBAGENT_DONE))[0].payload
        assert payload["status"] == "completed"

    def test_a_stop_does_not_settle_a_running_subagent(self):
        """Pressing ⏹ is a request; the row settles when the stream says so.

        ``chat-panel/index.js::_stopSubagent`` is written against this:
        ``stop_task`` answers ``stopping``, and a row that went terminal on
        the request would claim a subagent had ended while its tools were
        still running.
        """
        t = AgyTranslator("r1")
        t.mark_stopped(self.AGENT)
        payload = t.translate(frame(SUBAGENT_ACTIVE))[0].payload
        assert payload["status"] == "running"
        assert payload["terminal"] is False

    def test_the_announced_ids_are_readable_for_the_containment_check(self):
        """``stop_task`` refuses to aim at a conversation this turn never had.

        An ``agent_id`` here is ``agy``'s own conversation id, so an
        unchecked one would be "stop any Antigravity conversation on this
        machine by id" — the containment ``agy/subagents.py`` states for
        the reading half of the same identifier.
        """
        t = AgyTranslator("r1")
        assert t.subagents == frozenset()
        t.translate(frame(SUBAGENT_ACTIVE))
        assert t.subagents == frozenset({self.AGENT})


class TestTheViewStopsUpdatingWhenTheStopLands:
    """AG-19's residual gap, handled where it can be: the screen.

    ``PostInvocation`` fires *between* invocations, so a turn already
    inside one runs to its own end — and a single-invocation prose answer
    asks permission for nothing, so the gate cannot starve it either.
    Killing the process would stop it and costs 3.7s of dead session, so
    the specified handling is presentational: *stop updating the view, let
    the stream drain into the warm process*.

    The freeze has to be all three of these at once. A view that stops but
    keeps billing silently is [AG-R-6](../specs5/plan-ag/risks.md#ag-r-6)'s
    family; a view that stops and then pastes the finished answer in at the
    footer reads worse than one that never stopped; and a view that stops
    with nothing said is indistinguishable from a hang.
    """

    def test_the_deltas_stop_becoming_view(self):
        t = AgyTranslator("r1")
        assert t.translate(frame(TEXT_A)) != []
        t.note_cancelled()
        t.translate(frame(TEXT_B))
        assert t.translate(frame(TEXT_B)) == []

    def test_a_tool_card_does_not_appear_after_the_stop(self):
        """Prose is the case this exists for; every step type is suppressed.

        A card opening after the user stopped would say work had started
        that the user has no way to watch finish.
        """
        t = AgyTranslator("r1")
        t.note_cancelled()
        t.translate(frame(TOOL_ACTIVE))
        assert t.translate(frame(TOOL_DONE)) == []

    def test_the_meter_is_not_frozen_with_the_view(self):
        """The turn is still spending, and the footer still has to say so."""
        t = AgyTranslator("r1")
        t.translate(frame(dict(TEXT_A, usage={"total_tokens": 100})))
        t.note_cancelled()
        t.translate(frame(dict(TEXT_B, usage={"total_tokens": 250})))
        assert t.turn_usage()["total_tokens"] == 250

    def test_the_screen_is_said_to_be_final_exactly_once(self):
        """Otherwise the freeze is a hang, and a hang repeated is a stutter.

        The card has to arrive when the stop lands, because the footer's
        badge cannot appear until the turn ends — which on the case this
        exists for is the thing taking the time.
        """
        t = AgyTranslator("r1")
        t.note_cancelled()
        first = t.translate(frame(TEXT_A))
        assert names(first) == ["systemEvent"]
        assert first[0].payload["subtype"] == "stop_acknowledged"
        assert t.translate(frame(TEXT_B)) == []

    def test_the_prose_is_what_was_on_screen_not_what_agy_finished(self):
        """The half that makes the freeze real rather than cosmetic.

        ``agy`` assembles the whole answer into ``result.response`` and the
        browser takes the settled message's content from it, so accepting
        it would freeze the screen for the length of the turn and then
        paste the complete reply in at the footer.
        """
        t = AgyTranslator("r1")
        t.translate(frame(TEXT_A))
        t.note_cancelled()
        t.translate(RESULT)
        assert t.response_text() == TEXT_A["text_delta"]

    def test_an_ordinary_turn_still_prefers_agys_own_assembly(self):
        """The negative control: without ⏹ the result's prose stands."""
        t = AgyTranslator("r1")
        t.translate(frame(TEXT_A))
        t.translate(RESULT)
        assert t.response_text() == "calc.py defines add()."

    def test_the_footer_still_closes_the_turn(self):
        """Suppression is of the view, not of the turn's end.

        A stopped turn that never reached ``streamComplete`` would leave
        the browser streaming forever, which is the failure the freeze is
        supposed to make legible.
        """
        t = AgyTranslator("r1")
        t.translate(frame(TEXT_A))
        t.note_cancelled()
        t.translate({"event": "result", "result": {"status": "CANCELED"}})
        payload = t.stream_complete()[-1].payload
        assert payload["cancelled"] is True
        assert payload["terminal_reason"] == "aborted_streaming"


class TestARefusedTurnIsNotBilledForTheLastOne:
    """`agy` echoes the previous turn's usage on a result it refused.

    Measured 2026-09-10 while probing `/fork`: one real turn cost 5,423
    input tokens over 1.84s, and the turn `agy` then refused came back
    `status: ERROR` carrying *the same* `input_tokens` and
    `duration_seconds` with one output token. Nothing ran, and the footer
    charged it for the work of the turn before it.

    `_absorb_usage` takes last-wins rather than summing, so this never
    doubled a bill — which is exactly why it survived. It is a misreport,
    and a cost figure that is wrong in a way nothing in the UI can tell
    from right is `risks.md` AG-R-6's family.
    """

    ERROR_RESULT = {
        "event": "result",
        "result": {
            "conversation_id": "b1d377c5",
            "status": "ERROR",
            "response": "",
            # The previous turn's numbers, verbatim. That is the defect.
            "duration_seconds": 1.840465383,
            "usage": {"input_tokens": 5423, "output_tokens": 1, "total_tokens": 5424},
        },
    }

    def test_a_successful_turn_still_reports_its_usage(self):
        t = AgyTranslator("r1")
        t.translate(RESULT)
        assert t.turn_usage()["input_tokens"] == 72286

    def test_a_refused_turn_reports_nothing_rather_than_the_last_turns(self):
        t = AgyTranslator("r1")
        t.translate(self.ERROR_RESULT)
        assert t.turn_usage() == {}

    def test_a_turn_that_worked_before_failing_keeps_what_it_spent(self):
        """The step frames are the turn's own measurement and stand.

        Only the result frame's usage is dropped, because only the result
        frame is the one carrying somebody else's numbers.
        """
        t = AgyTranslator("r1")
        t.translate(
            frame(
                dict(
                    TOOL_ACTIVE,
                    usage={"input_tokens": 900, "total_tokens": 950},
                )
            )
        )
        t.translate(self.ERROR_RESULT)
        usage = t.turn_usage()
        assert usage["input_tokens"] == 900
        assert usage["total_tokens"] == 950

    def test_a_result_naming_no_status_is_still_absorbed(self):
        """A frame that named no status is not a frame that named a
        failure, and dropping its usage would lose a real measurement."""
        t = AgyTranslator("r1")
        t.translate(
            {
                "event": "result",
                "result": {"response": "hi", "usage": {"total_tokens": 42}},
            }
        )
        assert t.turn_usage() == {"total_tokens": 42}

    def test_the_footer_carries_the_corrected_figure(self):
        t = AgyTranslator("r1")
        t.translate(self.ERROR_RESULT)
        payload = [
            e for e in t.stream_complete() if e.name == "streamComplete"
        ][-1].payload
        assert payload["usage"] == {}
        # The status still reaches the browser: the turn failed, and
        # dropping the tokens must not also drop the reason. `engine_error`
        # is the Claude pump's word for it, which is what `terminalBadge`
        # has a label for and what `computeTurnOutcome` reddens the LED on.
        assert payload["terminal_reason"] == "engine_error"


class TestAToolResultIsPreviewedTheWayTheCardReadsIt:
    """AG-R-17's fourth instance, and the most visible one.

    This pump sent a tool's output as `content`. `block-render.js`
    renders a result body from `result.preview`, with `''` as its default
    and the literal string *"No output."* as its empty state — and
    `applyToolResult` spreads the payload onto the block without mapping
    anything. So every tool card on this transport drew "No output." over
    a payload that was carrying the output the whole time.

    Nothing failed, which is the point: a missing key renders as an empty
    pane, never as an error.
    """

    def _done(self, output):
        done = dict(TOOL_DONE)
        done["tool_info"] = dict(done["tool_info"], output=output)
        return frame(done)

    def _result(self, translator, output):
        events = translator.translate(self._done(output))
        return [e for e in events if e.name == "toolResult"][-1].payload

    def test_the_card_reads_the_body_off_preview(self):
        payload = self._result(AgyTranslator("r1"), "alpha\nbeta")
        assert payload["preview"] == "alpha\nbeta"
        assert "content" not in payload

    def test_a_long_result_truncates_like_the_other_engines(self):
        """The *same* helper, not a second one.

        Two implementations of "how much of a tool result does a card
        show" would show a user different amounts on different engines for
        the same output, which is the drift the shared function exists to
        prevent.
        """
        from aic_dc.claude_code.messages import TOOL_RESULT_PREVIEW_LINES

        output = "\n".join(f"line {n}" for n in range(TOOL_RESULT_PREVIEW_LINES + 50))
        payload = self._result(AgyTranslator("r1"), output)
        assert payload["truncated"] is True
        assert payload["preview"].count("\n") == TOOL_RESULT_PREVIEW_LINES - 1
        # The full size is the *untruncated* measurement, because that is
        # what the card's marker names.
        assert payload["full_bytes"] == len(output.encode("utf-8"))

    def test_a_short_result_is_not_marked_truncated(self):
        payload = self._result(AgyTranslator("r1"), "ok")
        assert payload["truncated"] is False
        assert payload["full_bytes"] == 2


class TestTheRootIsNeverAssumed:
    """AG-R-18, as a tripwire rather than as a comment.

    ``BRAIN_DIR`` and ``SCRATCH_DIR`` were module constants derived from
    ``Path.home()`` **at import time**. That was correct while there was
    exactly one ``agy`` configuration on the machine and it was the
    user's. AG-21 created a second and then a third — a stable master root
    and an ephemeral one per consultation — and the constants went on
    naming the server's own home, which is the one root no session ever
    runs against.

    The failure that would produce is this file's recurring register: not
    an exception, but an image that was generated and then reported
    missing, and a diverted write that was never noticed, with nothing
    anywhere saying why. So the parameter is required rather than
    defaulted, and these are the two assertions that keep it that way.
    """

    def test_a_translator_without_a_root_collects_nothing_and_says_so(
        self, tmp_path, caplog
    ):
        """Loud, because the silent version is the bug being prevented.

        A translator built without a root is a wiring mistake, and the
        image it cannot collect looks exactly like an image ``agy`` never
        produced. The log line is what separates those two for whoever
        reads it.
        """
        self_root = config_root(tmp_path)
        (brain_of(self_root, CONVERSATION) / "ai_test_pattern_1.jpg").write_bytes(
            b"\xff\xd8\xff\xe0 a jpeg"
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        translator = AgyTranslator(
            "r1", repo_root=repo, conversation_id=CONVERSATION
        )

        with caplog.at_level(logging.ERROR, logger="aic_dc.agy.steps"):
            result = next(
                e
                for e in translator.translate(image_step())
                if e.name == "toolResult"
            )

        assert result.payload["files_modified"] == []
        assert "AG-R-18" in caplog.text

    def test_an_image_under_another_root_is_not_collected(self, tmp_path):
        """The assertion the old constants could not make.

        Two roots exist at once here, and only one of them was given to the
        translator. A collector reading a process-wide constant would find
        this file — which is precisely the master-reads-the-consultant's-
        history failure AG-R-18 names — so the test asserts the *absence*
        of a collection rather than the presence of one.
        """
        other = config_root(tmp_path, "other-root")
        (brain_of(other, CONVERSATION) / "ai_test_pattern_1.jpg").write_bytes(
            b"\xff\xd8\xff\xe0 a jpeg"
        )
        mine = config_root(tmp_path, "mine")
        brain_of(mine, CONVERSATION)  # exists and is empty
        repo = tmp_path / "repo"
        repo.mkdir()

        result = next(
            e
            for e in AgyTranslator(
                "r1",
                repo_root=repo,
                conversation_id=CONVERSATION,
                config_root=mine,
            ).translate(image_step())
            if e.name == "toolResult"
        )

        assert result.payload["files_modified"] == []
        assert not list(repo.iterdir())

    def test_a_diverted_write_is_looked_for_under_the_given_root(self, tmp_path):
        """Same shape, for the scratch directory.

        The diverted-write notice is only correct if it names the scratch
        of the root this turn ran against. Pointed at another root's, it
        would report a file the user never wrote as the rescued copy of one
        they did.
        """
        mine = config_root(tmp_path, "mine")
        other = config_root(tmp_path, "other-root")
        (scratch_of(other) / "d.txt").write_text("somebody else's", encoding="utf-8")
        scratch_of(mine)

        frame_ = frame({
            "conversation_id": "c",
            "step_index": 2,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "write_to_file",
            "tool_info": {
                "name": "write_to_file",
                "parameters": {
                    "TargetFile": str(tmp_path / "d.txt"),
                    "CodeContent": "x",
                },
            },
        })
        events = AgyTranslator("r1", config_root=mine).translate(frame_)
        assert not [e for e in events if e.name == "systemEvent"]

        (scratch_of(mine) / "d.txt").write_text("mine", encoding="utf-8")
        events = AgyTranslator("r1", config_root=mine).translate(frame_)
        notice = next(e for e in events if e.name == "systemEvent")
        assert str(scratch_of(mine)) in notice.payload["data"]["message"]
