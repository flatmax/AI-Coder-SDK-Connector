"""Tests for aic_dc.framing — the one description of the user's screen.

The module exists because the text used to live inside the Claude adapter,
where the other two engines could not reach it: the browser pushed the open
file to every engine and only one of them ever read it (AG-33). So the
assertions here are about **sameness**, not about the sentences.

Three are load-bearing.

**The words did not change when they moved.** ``test_the_rendered_block_is
_byte_for_byte_what_claude_sent`` pins the whole block as a literal rather
than comparing against the function under test, so a refactor that quietly
reworded the framing fails here. Written from the shipped output, measured
2026-09-14.

**One validator, not three.** Every engine normalises the browser's dict
through :meth:`Viewer.from_dict`, so a payload cannot be accepted by one
adapter and dropped by another — which is the shape the bug took the first
time and would take again.

**Silence is a feature.** :func:`build` returns ``""`` when there is
nothing to say, and :func:`compose` then returns the user's message
unchanged. Every adapter composes unconditionally, so an ordinary turn's
prompt is byte-identical to what the user typed **only** if that holds.
"""

from __future__ import annotations

import pytest

from aic_dc import framing


class TestTheBlockIsWhatClaudeAlreadySent:
    """The refactor's own guard. Literals, so it survives the move."""

    def test_the_rendered_block_is_byte_for_byte_what_claude_sent(self):
        """Pinned as a literal, on purpose.

        Comparing this against ``claude_code.session.build_framing`` would
        pass for free now that the two are the same code — which is exactly
        why it is written out. What has to keep working is the *prompt the
        model receives*, and a shared renderer makes a reword three times
        as expensive to notice as it used to be.
        """
        block = framing.build(
            viewer=framing.Viewer("src/a.py", start_line=10, end_line=20),
            review={
                "active": True,
                "branch": "feature",
                "base_branch": "main",
                "merge_base": "abc123",
            },
        )
        assert block == (
            "<aic-dc-ui-context>\n"
            "Open in the user's editor pane:\n"
            "- src/a.py (lines 10-20 selected)\n"
            "Code review is active:\n"
            "- branch: feature\n"
            "- base branch: main\n"
            "- merge base: abc123\n"
            "</aic-dc-ui-context>"
        )

    def test_a_cursor_reads_as_a_cursor(self):
        assert framing.build(
            viewer=framing.Viewer("src/a.py", start_line=10, end_line=10)
        ) == (
            "<aic-dc-ui-context>\n"
            "Open in the user's editor pane:\n"
            "- src/a.py (cursor on line 10)\n"
            "</aic-dc-ui-context>"
        )

    def test_a_path_with_no_range_is_just_the_path(self):
        assert framing.build(viewer=framing.Viewer("src/a.py")) == (
            "<aic-dc-ui-context>\n"
            "Open in the user's editor pane:\n"
            "- src/a.py\n"
            "</aic-dc-ui-context>"
        )

    def test_the_claude_adapter_still_exports_the_names_it_used_to(self):
        """``ViewerFraming`` is in ``aic_dc.claude_code.__all__``.

        So it is a published name rather than an internal one, and the specs
        refer to it. Positional construction is part of the contract too,
        because that is how ``test_claude_code_session.py`` builds it.
        """
        from aic_dc.claude_code.session import ViewerFraming, build_framing

        assert ViewerFraming is framing.Viewer
        assert ViewerFraming("a.py", 1, 2) == framing.Viewer("a.py", 1, 2)
        assert callable(build_framing)

    def test_the_history_reader_strips_what_this_writes(self):
        """The pair that has to agree, asserted as a round trip.

        The tags were spelled in three places before this module. Two of
        them were a renderer and a reader, and a wrapper edited on one side
        only leaves the block in every stored prompt with nothing reporting
        it.
        """
        from aic_dc.claude_code.history import strip_framing

        prompt = framing.compose(
            framing.build(viewer=framing.Viewer("src/a.py", start_line=4)),
            "why is this failing?",
        )
        assert strip_framing(prompt) == "why is this failing?"


class TestSilenceIsAFeature:
    def test_nothing_to_say_is_the_empty_string(self):
        assert framing.build() == ""
        assert framing.build(viewer=None, review=None) == ""

    def test_an_inactive_review_contributes_nothing(self):
        assert framing.build(review={"active": False, "branch": "x"}) == ""

    def test_composing_nothing_returns_the_users_words_untouched(self):
        """The property every adapter's unconditional compose rests on."""
        assert framing.compose("", "hello") == "hello"
        assert framing.compose(framing.build(), "hello") == "hello"

    def test_framing_precedes_the_users_words(self):
        """``strip_framing`` matches the block at the start of the prompt."""
        prompt = framing.compose(
            framing.build(viewer=framing.Viewer("a.py")), "fix this"
        )
        assert prompt.index(framing.FRAMING_OPEN) < prompt.index("fix this")
        assert prompt.endswith("fix this")


class TestOneValidator:
    def test_it_reads_the_rpc_payload(self):
        assert framing.Viewer.from_dict(
            {"path": "src/a.py", "start_line": 3, "end_line": 9}
        ) == framing.Viewer("src/a.py", 3, 9)

    def test_string_line_numbers_are_coerced(self):
        """They arrive from JavaScript, which is loose about this."""
        got = framing.Viewer.from_dict({"path": "a", "start_line": "3"})
        assert got.start_line == 3

    @pytest.mark.parametrize(
        "payload", [None, {}, {"path": ""}, {"path": 42}, "src/a.py", []]
    )
    def test_a_bad_shape_yields_no_framing(self, payload):
        """A malformed viewer must not fail the turn."""
        assert framing.Viewer.from_dict(payload) is None

    @pytest.mark.parametrize("bad", [True, False, None, "x", [], {}, object()])
    def test_a_line_number_that_is_not_one_is_dropped(self, bad):
        """``bool`` included deliberately: it is an ``int`` in Python, and
        "cursor on line True" is not a sentence anyone meant to write."""
        got = framing.Viewer.from_dict({"path": "a", "start_line": bad})
        assert got.start_line is None

    def test_a_dropped_range_still_frames_the_path(self):
        """The path is the fact worth having; the range is a refinement."""
        block = framing.build(framing.Viewer.from_dict({"path": "a.py", "start_line": {}}))
        assert "- a.py\n" in block
        assert "line" not in block.split("editor pane:")[1]


class TestThePushIsNormalisedToo:
    """``viewer_payload`` — the other end of the same rule.

    ``set_viewer_state`` was written twice and had **no test on either
    engine** before AG-33, which is how the two came to differ without
    anyone noticing: the Claude adapter cleared to ``None`` and the
    Antigravity one to ``{}``, and it kept ``start_line: None`` verbatim
    where Claude omitted the key. Both harmless while nothing read them.
    """

    def test_a_path_and_two_lines_are_kept(self):
        assert framing.viewer_payload("src/a.py", 3, 9) == {
            "path": "src/a.py",
            "start_line": 3,
            "end_line": 9,
        }

    @pytest.mark.parametrize("path", [None, "", 42, [], {}])
    def test_a_path_that_is_not_one_clears(self, path):
        """Closing the pane has to be sayable.

        Otherwise the last pushed path stays in front of the model for the
        rest of the session, pointing it at a file nobody is looking at.
        """
        assert framing.viewer_payload(path) is None

    @pytest.mark.parametrize("bad", [None, "3", 3.5, True, False, [], object()])
    def test_a_line_that_is_not_an_int_is_omitted_not_stored(self, bad):
        """A stored ``None`` renders as ``(cursor on line None)``.

        Which is the kind of sentence a model believes. ``"3"`` is rejected
        here and *coerced* by ``Viewer.from_dict``, deliberately: this is a
        typed RPC argument the browser controls, and that is a dict off the
        wire whose shape it does not.
        """
        assert framing.viewer_payload("a.py", bad, bad) == {"path": "a.py"}

    def test_the_stored_shape_round_trips_through_the_validator(self):
        """The two ends have to agree, or a push survives and frames wrong."""
        stored = framing.viewer_payload("src/a.py", 3, 9)
        assert framing.Viewer.from_dict(stored) == framing.Viewer("src/a.py", 3, 9)

    def test_as_payload_is_the_inverse(self):
        """What an adapter stores when the viewer arrived on the turn."""
        viewer = framing.Viewer("src/a.py", 3, 9)
        assert viewer.as_payload() == {
            "path": "src/a.py",
            "start_line": 3,
            "end_line": 9,
        }
        assert framing.Viewer.from_dict(viewer.as_payload()) == viewer

    def test_as_payload_omits_a_range_it_does_not_have(self):
        assert framing.Viewer("a.py").as_payload() == {"path": "a.py"}

    def test_every_adapter_uses_it(self):
        """The assertion that keeps this from drifting apart a third time.

        Named methods rather than behaviour, because behaviour is what the
        two service test files check — what this pins is that there is one
        implementation for all three to agree with.
        """
        import inspect

        from aic_dc.antigravity.service import AntigravityService
        from aic_dc.claude_code.service import ClaudeCodeService

        for cls in (ClaudeCodeService, AntigravityService):
            source = inspect.getsource(cls.set_viewer_state)
            assert "viewer_payload" in source, cls.__name__


class TestWhichArrivalPathAnswers:
    """``resolve`` — the turn argument versus the pushed state.

    In this app it is always the push: ``viewer-framing.js`` is the only
    writer and leaves the turn argument null. Both are honoured anyway, and
    the precedence is the whole content of the distinction.
    """

    def test_the_push_stands_in_when_the_turn_says_nothing(self):
        assert framing.resolve(None, {"path": "pushed.py"}) == framing.Viewer(
            "pushed.py"
        )

    def test_the_turn_wins_when_it_states_something(self):
        assert framing.resolve(
            {"path": "turn.py"}, {"path": "pushed.py"}
        ) == framing.Viewer("turn.py")

    def test_an_empty_mapping_on_the_turn_is_an_answer_not_a_silence(self):
        """"Nothing is open" has to be sayable, or a stale push outlives a
        closed pane for the rest of the session."""
        assert framing.resolve({}, {"path": "pushed.py"}) is None

    def test_neither_source_is_no_framing(self):
        assert framing.resolve(None, None) is None
        assert framing.resolve(None, {}) is None
