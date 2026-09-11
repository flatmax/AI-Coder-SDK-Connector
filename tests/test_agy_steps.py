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

import os

import pytest

from aic_dc.agy import steps
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
        events = AgyTranslator("r1").translate(self._done(target))
        assert not [e for e in events if e.name == "systemEvent"]

    def test_a_missing_file_alone_is_not_reported(self, tmp_path, monkeypatch):
        """Narrow on purpose: "missing" has innocent explanations.

        The model may name a path it never created, or the tool may have
        failed for an unrelated reason. A false alarm about a write that
        did land would be worse than the silence it replaces, so only the
        pair — missing *here*, present *there* — has no innocent reading.
        """
        monkeypatch.setattr(steps, "SCRATCH_DIR", tmp_path / "scratch")
        events = AgyTranslator("r1").translate(self._done(tmp_path / "gone.txt"))
        assert not [e for e in events if e.name == "systemEvent"]

    def test_missing_here_and_present_in_scratch_is_reported(
        self, tmp_path, monkeypatch
    ):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "diverted.txt").write_text("the real content", encoding="utf-8")
        monkeypatch.setattr(steps, "SCRATCH_DIR", scratch)

        events = AgyTranslator("r1").translate(self._done(tmp_path / "diverted.txt"))
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
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "d.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(steps, "SCRATCH_DIR", scratch)
        events = AgyTranslator("r1").translate(self._done(tmp_path / "d.txt"))
        assert [e.name for e in events] == ["toolUse", "systemEvent", "toolResult"]

    def test_a_read_is_never_checked(self, tmp_path, monkeypatch):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "v.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(steps, "SCRATCH_DIR", scratch)
        events = AgyTranslator("r1").translate(frame({
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

    def _translator(self, tmp_path, monkeypatch, *, repo=None, brain=None):
        monkeypatch.setattr(steps, "BRAIN_DIR", brain or (tmp_path / "brain"))
        repo = repo if repo is not None else tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        return AgyTranslator("r1", repo_root=repo, conversation_id=CONVERSATION)

    def _generated(self, tmp_path, name="ai_test_pattern_1788856211696.jpg"):
        directory = tmp_path / "brain" / CONVERSATION
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / name
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
        nested = tmp_path / "brain" / CONVERSATION / "sub"
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
        monkeypatch.setattr(steps, "BRAIN_DIR", tmp_path / "brain")
        self._generated(tmp_path)

        result = self._result(
            AgyTranslator("r1", agent_id="ag-1").translate(image_step())
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
