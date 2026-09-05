"""Tests for aic_dc.capabilities — the AG-3/AG-9 descriptor.

Two assertions are load-bearing, and both are about the table being an
honest inventory rather than a plausible one.

**Every surface in the spec has an entry.** AG-9 makes the descriptor a
spec artifact: *"Every surface in sdk-surface.md § What does not translate
needs an entry, and adding a per-engine feature means adding its key there
in the same commit."* ``TestItMatchesTheSpec`` reads that document and
checks the tables against this table, so a surface added to the spec and
forgotten here fails the build rather than being noticed in phase 6.

**Nothing that is dead code is described.** The same decision, in the
other direction: a surface neither engine serves is not a hidden surface,
it is a UI that does not exist, and describing it would make the table
read as coverage.

The rest pins the behaviour the browser depends on — that an unanswered
question is loud, and that the engine's name never crosses the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aic_dc import capabilities
from aic_dc.capabilities import (
    ABSENT,
    ANTIGRAVITY,
    CLAUDE,
    SUPPORTED,
    SURFACES,
    UNBUILT,
    UnknownSurfaceError,
    descriptor,
    hidden_surfaces,
    supports,
    unbuilt_surfaces,
)

SPEC = Path(__file__).resolve().parents[1] / "specs5" / "plan-ag" / "sdk-surface.md"


# ----------------------------------------------------------------------
# The first one that matters: the table is complete in both directions
# ----------------------------------------------------------------------


class TestItMatchesTheSpec:
    def test_the_spec_is_where_it_is_expected(self):
        assert SPEC.is_file(), (
            "sdk-surface.md has moved. This file reads it as the source of "
            "truth for the surface list; update the path rather than "
            "deleting the check."
        )

    def test_every_surface_is_described_for_both_engines(self):
        for surface in SURFACES:
            for engine in (CLAUDE, ANTIGRAVITY):
                assert surface.status_for(engine) in (SUPPORTED, ABSENT, UNBUILT), (
                    f"{surface.key} has no status for {engine}."
                )

    def test_nothing_is_hidden_on_both_engines(self):
        """AG-9: a surface neither engine serves is dead code.

        Describing it would make this table read as coverage of a UI that
        does not exist. The five real SDK capabilities that fall in this
        bucket — structured output, audio and video input, daemon
        commands, triggers, multi-model routing — are named in the
        module's docstring rather than given rows.
        """
        dead = [
            s.key
            for s in SURFACES
            if s.claude != SUPPORTED and s.antigravity != SUPPORTED
        ]
        assert not dead, (
            f"{dead} are unsupported on both engines. AG-9 says delete them "
            "rather than describe them — a surface no engine serves is not "
            "a hidden surface, it is a UI that does not exist."
        )

    def test_nothing_is_supported_on_both_engines_without_a_reason(self):
        """A row that is always true is a browser branch never taken.

        Allowed only where the row is carrying an argument rather than a
        decision — ``amend_tool_input`` is here because it is the
        capability AG-5 chose the raw hook to keep, and losing that note
        would lose the reason. Anything else that is universal should be
        deleted.
        """
        universal = {
            s.key
            for s in SURFACES
            if s.claude == SUPPORTED and s.antigravity == SUPPORTED
        }
        # `persisted_permission_rules` joined this set with AG-15, and it
        # stays for the same reason `amend_tool_input` is here: the row
        # carries an argument. It records that the two engines reach the
        # same capability by different means — Claude writes a settings
        # file through `updated_permissions`, Antigravity has no such
        # channel and AIC-DC keeps the rule itself — and that a row reading
        # ABSENT was what named the work that closed it.
        allowed = {"amend_tool_input", "persisted_permission_rules"}
        assert universal <= allowed, (
            f"{sorted(universal - allowed)} are supported "
            "everywhere. The descriptor covers surfaces where the engines "
            "differ; delete the row or say why it earns one."
        )

    def test_every_absent_or_unbuilt_row_says_why(self):
        for surface in SURFACES:
            if SUPPORTED in (surface.claude, surface.antigravity) and not (
                surface.claude == surface.antigravity == SUPPORTED
            ):
                assert surface.note.strip(), (
                    f"{surface.key} is hidden on one engine and says nothing "
                    "about why. The note is what makes phase 6 tractable."
                )

    def test_the_spec_tables_are_covered(self):
        """Each row of § What does not translate maps onto a surface.

        Matched loosely — by a distinctive word from each spec row —
        because the spec's cells are prose and this is a completeness
        check, not a parser. A spec row with no matching key here is the
        failure worth catching; exact wording is not.
        """
        text = SPEC.read_text(encoding="utf-8")
        section = text.split("## What does not translate")[1].split("\n---")[0]
        rows = [
            line for line in section.splitlines() if line.startswith("| ") and "|" in line
        ]
        assert len(rows) > 10, "the spec section did not parse as expected"

        # The distinctive term each spec row is recognisable by, and the
        # surface it belongs to. Deliberately explicit: a spec row that
        # stops matching should be re-read, not auto-matched.
        expected = {
            "rate-limit windows": "account_rate_limits",
            "USD cost": "usd_cost",
            "context-window usage": "context_window_usage",
            "Slash-command palette": "slash_commands",
            "Always allow": "persisted_permission_rules",
            "Amend input": "amend_tool_input",
            "MCP bridge": "mcp_server_inventory",
            "session mirror": "session_mirror",
            "history rendering": "transcript_history",
            "RateLimitEvent": "rate_limit_events",
            "Image generation": "image_generation",
            "ask_question": "agent_questions",
        }
        keys = {s.key for s in SURFACES}
        for term, key in expected.items():
            assert key in keys, f"{key} (spec row {term!r}) has no entry"
            assert any(term in row for row in rows) or term in section, (
                f"the spec row for {term!r} has gone. Either it was renamed "
                f"— update this map — or the surface {key} no longer needs "
                "an entry."
            )


# ----------------------------------------------------------------------
# The second: an unanswered question is loud
# ----------------------------------------------------------------------


class TestAnUnknownKeyIsNotFalse:
    def test_an_unknown_surface_raises(self):
        """Returning False for a typo hides a panel and looks deliberate."""
        with pytest.raises(UnknownSurfaceError):
            supports(CLAUDE, "context_usage")  # near-miss for the real key

    def test_an_unknown_engine_raises(self):
        with pytest.raises(UnknownSurfaceError):
            supports("gpt", "usd_cost")

    def test_descriptor_refuses_an_unknown_engine(self):
        with pytest.raises(UnknownSurfaceError):
            descriptor("gpt")


# ----------------------------------------------------------------------
# What the browser sees
# ----------------------------------------------------------------------


class TestTheDescriptor:
    def test_it_covers_every_surface(self):
        assert set(descriptor(CLAUDE)) == {s.key for s in SURFACES}

    def test_the_payload_carries_no_engine_identity(self):
        """AG-R-4: no webapp branch may key off an engine name string.

        Checked structurally, on the *fields*, not by scanning the prose.
        A note legitimately names the engine it is explaining — that is
        what makes it useful to the developer it is written for — and
        grepping the payload for the word would forbid the documentation
        rather than the branch. What must not exist is a field the
        browser could switch on.
        """
        for engine in (CLAUDE, ANTIGRAVITY):
            payload = descriptor(engine)
            identity = {"engine", "engine_name", "name", "id", "adapter"}
            assert not (set(payload) & identity), (
                f"{sorted(set(payload) & identity)} is a top-level field the "
                "browser could branch on."
            )
            for entry in payload.values():
                assert not (set(entry) & identity), (
                    f"{sorted(set(entry) & identity)} in an entry gives the "
                    "browser an engine to switch on."
                )

    def test_the_two_descriptors_have_the_same_shape(self):
        """So a call site cannot be written against one engine's payload."""
        claude, antigravity = descriptor(CLAUDE), descriptor(ANTIGRAVITY)
        assert set(claude) == set(antigravity)
        for key in claude:
            assert set(claude[key]) == set(antigravity[key])

    def test_unbuilt_and_absent_both_read_as_unsupported(self):
        """Why there is no data is not the browser's business."""
        d = descriptor(ANTIGRAVITY)
        assert d["usd_cost"]["supported"] is False
        assert d["transcript_history"]["supported"] is False

    def test_the_distinction_survives_for_developers(self):
        d = descriptor(ANTIGRAVITY)
        assert d["usd_cost"]["status"] == ABSENT
        assert d["transcript_history"]["status"] == UNBUILT


class TestTheEnginesDisagreeWhereExpected:
    def test_claude_cannot_generate_images(self):
        """AG-1's whole argument: a thing one engine can do."""
        assert supports(ANTIGRAVITY, "image_generation")
        assert not supports(CLAUDE, "image_generation")

    def test_antigravity_reports_no_dollars(self):
        """AG-6, and the reason the Context tab hides rather than zeroes."""
        assert supports(CLAUDE, "usd_cost")
        assert not supports(ANTIGRAVITY, "usd_cost")

    def test_always_allow_is_supported_on_both_by_different_means(self):
        """**Restated for AG-15.** The premise moved; the row still earns its place.

        This asserted ``antigravity == ABSENT``, reasoning that
        ``updated_permissions`` has no counterpart at any layer and so the
        capability "must never drift into the unbuilt list and become
        somebody\'s sprint task".

        The first half is still true and the conclusion did not follow from
        it. No counterpart meant the *engine* could not persist a rule — not
        that the *product* could not, and the row\'s own note said so:
        "AIC-DC would have to own the rule store to change this." AG-15 did
        exactly that, so the honest assertion is the opposite one, and the
        lesson is that a surface can be absent from an SDK and present in
        the app.
        """
        row = next(s for s in SURFACES if s.key == "persisted_permission_rules")
        assert row.antigravity == SUPPORTED
        assert row.claude == SUPPORTED

    def test_the_permission_gate_itself_is_not_a_hidden_surface(self):
        """It works on both, so it earns no row — and must not gain one.

        AG-5 makes the dialog a requirement of the second engine rather
        than a feature of it. A descriptor entry for it would imply an
        engine could ship without one.
        """
        assert "permission_dialog" not in {s.key for s in SURFACES}


class TestTheHelpers:
    def test_hidden_surfaces_is_sorted_and_consistent(self):
        hidden = hidden_surfaces(ANTIGRAVITY)
        assert hidden == sorted(hidden)
        assert all(not supports(ANTIGRAVITY, key) for key in hidden)

    def test_unbuilt_is_a_subset_of_hidden(self):
        for engine in (CLAUDE, ANTIGRAVITY):
            assert set(unbuilt_surfaces(engine)) <= set(hidden_surfaces(engine))

    def test_antigravitys_to_do_list_is_the_later_phases(self):
        """The ordering constraint, discharged as data rather than memory."""
        assert set(unbuilt_surfaces(ANTIGRAVITY)) == {
            "agent_questions",
            "mcp_server_inventory",
            "session_mirror",
            "subagent_tabs",
            "transcript_history",
        }

    def test_claude_has_nothing_unbuilt(self):
        """It is the shipped engine; a to-do here would be a regression."""
        assert unbuilt_surfaces(CLAUDE) == []


def test_statuses_are_distinct():
    assert len({SUPPORTED, ABSENT, UNBUILT}) == 3
    # AG-14 added a third identifier: `agy` is the *same product* as
    # `antigravity`, reached on the owner's subscription rather than a
    # metered key. A session runs on one or the other and they differ in
    # what they can feed, so it is a row in the descriptor — not a third
    # engine, which is why `Surface.agy` defaults to "same as antigravity".
    assert capabilities.ENGINES == (CLAUDE, ANTIGRAVITY, capabilities.AGY)


def test_the_agy_transport_inherits_antigravitys_answers_by_default():
    """Both reach the same product, so a surface it cannot feed is unfed.

    The default is the honest one. Only where the *transport* changes the
    answer should a surface override it, and a blanket copy would hide the
    places it genuinely differs.
    """
    for surface in capabilities.SURFACES:
        if surface.agy is None:
            assert surface.status_for(capabilities.AGY) == surface.antigravity


def test_every_engine_has_a_label_for_the_selector():
    """Supplied by the server, because a map in the webapp is an AG-R-4 branch.

    And because what a user chooses between here is which account pays,
    which is not something the browser can know.
    """
    for engine in capabilities.ENGINES:
        assert capabilities.ENGINE_LABELS.get(engine)
    assert len(set(capabilities.ENGINE_LABELS.values())) == len(capabilities.ENGINES)


def test_the_two_antigravity_labels_say_which_account_pays():
    """The thing that actually differs, at the moment of choosing."""
    assert "subscription" in capabilities.ENGINE_LABELS[capabilities.AGY]
    assert "API key" in capabilities.ENGINE_LABELS[capabilities.ANTIGRAVITY]


def test_the_descriptor_names_no_engine_the_browser_could_branch_on():
    """AG-R-4, checked for the third engine as well as the first two.

    Scoped to *structure* rather than to a substring. A `note` may well
    say "agy's equivalent returns '2 lines, 18 bytes'" — that is developer
    prose about another transport and the browser never renders it. What
    AG-R-4 forbids is the payload giving a component something to key off,
    so what is asserted is that no field carries the engine's identity and
    that the fields present are the same four whatever engine is asked.
    """
    shapes = set()
    for engine in capabilities.ENGINES:
        payload = capabilities.descriptor(engine)
        assert "engine" not in payload
        for entry in payload.values():
            shapes.add(tuple(sorted(entry)))
            assert entry.get("engine") is None
    assert shapes == {("note", "status", "supported", "title")}
