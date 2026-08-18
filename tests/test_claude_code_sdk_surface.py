"""Tests for ac_dc.claude_code.sdk_surface — the SDK-drift gate.

``test_claude_code_options`` is a tripwire for surface that *moved or
vanished* under us: it fails when a field we set stops existing. This file
is the tripwire for the other direction, which nothing caught before —
surface that **appeared**. An SDK release that adds an option, a hook
event, a message type or a beta gate is not a build break and never was;
it is a feature we quietly do not offer, discovered whenever somebody next
reads the wheel.

The gate is deliberately not "coverage must be complete" — most of this
surface is for hosts AC⚡DC is not, and a test demanding we set
``output_format`` would be wrong. It is "coverage must be **decided**":
every name the installed SDK exposes is handled, declined with a reason,
or listed as pending with what it would buy. Only the undecided fail, and
they fail *by name*, so the failure message is the triage list.

Closing a failure means one of three edits, all of them cheap:

- implement it → ``options.py`` assigns it, ``hooks.py`` registers it
- refuse it → ``options.NEVER_SET`` / :data:`HOOK_EVENTS`, with the reason
- defer it → :data:`PENDING_OPTIONS` / :data:`KNOWN_BETAS`, with the argument

Offline: pure reflection over the installed wheel and this package's own
syntax trees. No client, no CLI, no engine.
"""

from __future__ import annotations

import pytest

from ac_dc.claude_code import sdk_surface
from ac_dc.claude_code.sdk_surface import (
    DECLINED,
    HANDLED,
    HOOK_EVENTS,
    KNOWN_BETAS,
    PENDING,
    PENDING_OPTIONS,
    assigned_option_keys,
    beta_report,
    client_report,
    diff_server_info,
    hook_event_names,
    hook_report,
    message_report,
    option_fields,
    option_report,
    registered_hook_events,
    surface_report,
)

SECTIONS = ("options", "hooks", "messages", "client", "betas")


@pytest.fixture(autouse=True)
def _clear_ast_caches():
    """Drop the AST caches between tests.

    The readers are ``lru_cache``d because a report reads three modules
    and the tab can refresh; that cache would otherwise leak a monkey-
    patched source from one test into the next.
    """
    for reader in (
        sdk_surface.assigned_option_keys,
        sdk_surface.registered_hook_events,
        sdk_surface.dispatched_message_types,
        sdk_surface.called_client_methods,
    ):
        reader.cache_clear()
    yield
    for reader in (
        sdk_surface.assigned_option_keys,
        sdk_surface.registered_hook_events,
        sdk_surface.dispatched_message_types,
        sdk_surface.called_client_methods,
    ):
        reader.cache_clear()


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


class TestNothingUntriaged:
    """The whole point of the file: no surface without a decision."""

    @pytest.mark.parametrize("section", SECTIONS)
    def test_section_has_no_unclassified_surface(self, section):
        report = surface_report()["sections"][section]
        unclassified = report["unclassified"]
        assert not unclassified, (
            f"The installed claude-agent-sdk exposes {section} surface that "
            f"AC-DC has not decided about: {', '.join(unclassified)}.\n"
            "This is an SDK release adding something, not a bug in this "
            "test. Implement it, refuse it in NEVER_SET/HOOK_EVENTS with "
            "the reason, or defer it in PENDING_OPTIONS/KNOWN_BETAS with "
            "what it would buy. See ac_dc/claude_code/sdk_surface.py."
        )

    @pytest.mark.parametrize("section", ("options", "hooks", "betas"))
    def test_no_stale_entries(self, section):
        """Names we still explain that the SDK no longer has.

        Not a failure of coverage but of prose: a pending note arguing
        about a field that no longer exists sends the next reader to look
        for it. ``build_options`` already fails startup for a *set* field
        that vanished; this catches the ones we only talk about.
        """
        stale = surface_report()["sections"][section].get("stale", [])
        assert not stale, (
            f"sdk_surface still classifies {section} that the installed SDK "
            f"no longer exposes: {', '.join(stale)}. Delete the entries."
        )

    def test_table_never_overstates_hook_coverage(self):
        """A ``HANDLED`` hook that nobody registered would be a lie.

        The one inconsistency in this module that could mislead rather
        than merely age: a reader trusting the table would take a real
        gap for covered ground.
        """
        claimed = hook_report()["claimed_unregistered"]
        assert not claimed, (
            "HOOK_EVENTS marks these handled but build_hook_matchers does "
            f"not register them: {', '.join(claimed)}."
        )


# ----------------------------------------------------------------------
# The probe's own honesty — the two ways the naive versions were wrong
# ----------------------------------------------------------------------


class TestCoverageIsDerivedNotGuessed:
    """Regression tests for how the first two drafts got this wrong."""

    def test_conditionally_set_options_count_as_handled(self):
        """A field set only inside an ``if`` is still handled.

        The first draft diffed the *output* of ``build_option_kwargs``
        under a default config and reported 36 gaps, because ``model``,
        ``hooks``, ``resume`` and ``thinking`` are set only when something
        asks for them. Reading assignments from the AST has no such blind
        spot, and these four are the proof.
        """
        assigned = assigned_option_keys()
        for name in ("model", "hooks", "resume", "thinking", "session_store"):
            assert name in assigned, (
                f"{name} is assigned in options.py inside a conditional and "
                "must still read as handled"
            )

    def test_options_named_only_in_comments_are_not_handled(self):
        """A word in a comment is not an implementation.

        The second draft word-searched ``options.py`` and reported
        ``skills`` as handled, because a comment there mentions
        ``.claude/skills/``. ``skills`` is genuinely unset, so it must
        read as pending.
        """
        assigned = assigned_option_keys()
        assert "skills" not in assigned
        entry = next(
            e for e in option_report()["entries"] if e["name"] == "skills"
        )
        assert entry["status"] == PENDING

    def test_always_set_options_are_handled(self):
        """The literal dict at the top of ``build_option_kwargs`` counts."""
        assigned = assigned_option_keys()
        for name in ("cwd", "system_prompt", "permission_mode", "env"):
            assert name in assigned

    def test_registration_is_read_from_hooks_module(self):
        assert registered_hook_events() == {"PostToolUse", "PreCompact"}


# ----------------------------------------------------------------------
# Per-surface shape
# ----------------------------------------------------------------------


class TestOptionReport:
    def test_every_field_gets_exactly_one_status(self):
        entries = option_report()["entries"]
        names = [e["name"] for e in entries]
        assert names == option_fields()
        assert len(names) == len(set(names))
        assert all(e["status"] in (HANDLED, DECLINED, PENDING) for e in entries)

    def test_never_set_options_are_declined_with_a_reason(self):
        by_name = {e["name"]: e for e in option_report()["entries"]}
        for name in ("allowed_tools", "agents"):
            assert by_name[name]["status"] == DECLINED
            assert len(by_name[name]["note"]) > 40

    def test_pending_options_carry_an_argument(self):
        """A bare name is not triage; the next reader needs the why."""
        for name, note in PENDING_OPTIONS.items():
            assert len(note) > 40, f"{name} needs a real note, not {note!r}"

    def test_pending_and_never_set_do_not_overlap(self):
        """One owner per decision: refused *or* deferred, not both."""
        from ac_dc.claude_code.options import NEVER_SET

        assert not set(PENDING_OPTIONS) & set(NEVER_SET)

    def test_nothing_we_set_is_still_argued_against(self):
        """The bucket ``stale`` structurally cannot cover.

        A pending note is an argument for *not* setting an option. Once
        the option is set the note is worse than absent — it reads as a
        reason to undo the work. ``stale`` only catches names the SDK
        removed, and these names still exist.
        """
        assert option_report()["resolved"] == []

    def test_a_resolved_entry_is_reported(self, monkeypatch):
        """The check has to be able to fail, or it is decoration."""
        monkeypatch.setitem(
            PENDING_OPTIONS, "cwd", "a note arguing against something we do"
        )
        assert option_report()["resolved"] == ["cwd"]

    def test_the_two_new_options_read_as_handled(self):
        """``max_buffer_size`` and ``stderr``, both closed the same day.

        Named rather than left to the generic sweep because both were
        pending with an argument, and this is the assertion that the
        argument is gone rather than merely outvoted.
        """
        by_name = {e["name"]: e for e in option_report()["entries"]}
        for name in ("max_buffer_size", "stderr"):
            assert by_name[name]["status"] == HANDLED
            assert name not in PENDING_OPTIONS


class TestHookReport:
    def test_every_event_classified(self):
        entries = hook_report()["entries"]
        assert [e["name"] for e in entries] == hook_event_names()
        assert all(e["status"] in (HANDLED, DECLINED, PENDING) for e in entries)

    def test_post_tool_use_is_handled(self):
        by_name = {e["name"]: e for e in hook_report()["entries"]}
        assert by_name["PostToolUse"]["status"] == HANDLED

    def test_declined_events_say_what_covers_them_instead(self):
        for name, (status, note) in HOOK_EVENTS.items():
            if status == DECLINED:
                assert len(note) > 30, f"{name} needs a reason"

    def test_precompact_is_handled_and_the_table_agrees(self):
        """The gap this used to document, closed at both ends.

        It asserted ``PENDING`` and said "if somebody registers
        ``PreCompact``, this test fails and the reminder to move it out of
        pending is the failure itself". Somebody did, and it did.
        """
        by_name = {e["name"]: e for e in hook_report()["entries"]}
        assert by_name["PreCompact"]["status"] == HANDLED
        assert "PreCompact" in registered_hook_events()
        assert HOOK_EVENTS["PreCompact"][0] == HANDLED

    def test_no_event_is_registered_without_a_reason_recorded(self):
        """The table is the per-event prose; a registration needs an entry.

        Not caught by ``claimed_unregistered``, which is the other
        direction. A matcher added with no entry here reads as handled with
        an empty note, which tells the next reader nothing about why.
        """
        for name in registered_hook_events():
            assert name in HOOK_EVENTS, f"{name} is registered but not described"
            status, note = HOOK_EVENTS[name]
            assert status == HANDLED, f"{name} is registered; the table says {status}"
            assert len(note) > 30, f"{name} needs a real note, not {note!r}"


class TestMessageReport:
    def test_whole_union_is_dispatched(self):
        report = message_report()
        assert report["entries"], "the SDK's Message union should be non-empty"
        assert all(e["status"] == HANDLED for e in report["entries"])

    def test_pump_handles_more_than_the_union(self):
        """Task/hook/mirror messages arrive off-union and are handled.

        Recorded so the handled count is not mistaken for the whole story
        — and so a release that folds these *into* ``Message`` does not
        read as a sudden coverage jump.
        """
        beyond = message_report()["beyond_union"]
        assert "TaskStartedMessage" in beyond
        assert "HookEventMessage" in beyond


class TestClientReport:
    def test_every_method_classified(self):
        entries = client_report()["entries"]
        assert all(e["status"] in (HANDLED, DECLINED, PENDING) for e in entries)

    def test_the_methods_the_ui_depends_on_are_called(self):
        by_name = {e["name"]: e for e in client_report()["entries"]}
        for name in (
            "connect",
            "disconnect",
            "query",
            "interrupt",
            "set_permission_mode",
            "set_model",
            "get_context_usage",
            "get_mcp_status",
            "get_server_info",
            "stop_task",
            "rewind_files",
        ):
            assert by_name[name]["status"] == HANDLED, f"{name} lost its call site"


class TestBetaReport:
    def test_known_betas_are_declined_not_pending(self):
        entries = {e["name"]: e for e in beta_report()["entries"]}
        for value in KNOWN_BETAS:
            if value in entries:
                assert entries[value]["status"] == DECLINED

    def test_known_betas_carry_a_decision(self):
        for value, note in KNOWN_BETAS.items():
            assert len(note) > 40, f"{value} needs a real decision, not {note!r}"


# ----------------------------------------------------------------------
# The live-CLI half
# ----------------------------------------------------------------------


class TestDiffServerInfo:
    """The CLI ships independently, so its advertised surface is probed too."""

    def test_none_is_not_an_error(self):
        """A report of the static surface is worth having with no engine.

        Which is exactly when someone opens this tab.
        """
        result = diff_server_info(None)
        assert result["available"] is False
        assert result["commands"] == []

    def test_reads_dicts_and_bare_strings(self):
        """The payload's item shape is the CLI's, and it has changed before."""
        result = diff_server_info(
            {
                "commands": [{"name": "commit"}, "review"],
                "tools": ["Read", {"name": "Write"}],
                "output_styles": [{"name": "concise"}],
            }
        )
        assert result["commands"] == ["commit", "review"]
        assert result["tools"] == ["Read", "Write"]
        assert result["output_styles"] == ["concise"]

    def test_accepts_camel_case_output_styles(self):
        """The CLI has used both spellings; neither should read as empty."""
        assert diff_server_info({"outputStyles": ["md"]})["output_styles"] == ["md"]

    def test_junk_shapes_do_not_raise(self):
        for payload in ({"commands": "nope"}, {"tools": [None, 3]}, {}, []):
            diff_server_info(payload)


# ----------------------------------------------------------------------
# The report the browser reads
# ----------------------------------------------------------------------


class TestSurfaceReport:
    def test_shape(self):
        report = surface_report()
        assert report["sdk_available"] is True
        assert set(report["sections"]) == set(SECTIONS)
        assert set(report["versions"]) == {
            "sdk_version",
            "sdk_cli_pin",
            "minimum_cli_version",
        }

    def test_counts_add_up_to_the_entries(self):
        report = surface_report()
        for section, counts in report["counts"].items():
            entries = report["sections"][section]["entries"]
            assert sum(counts.values()) == len(entries), section

    def test_unclassified_only_lists_sections_that_have_some(self):
        """Empty is the healthy state, and it must read as empty."""
        report = surface_report()
        assert all(v for v in report["unclassified"].values())

    def test_server_info_flows_through(self):
        report = surface_report({"commands": [{"name": "commit"}]})
        assert report["cli"]["available"] is True
        assert report["cli"]["commands"] == ["commit"]

    def test_json_serialisable(self):
        """It crosses a JSON-RPC boundary, so it must survive the trip."""
        import json

        json.dumps(surface_report())


class TestDegradesWithoutTheSdk:
    """A diagnostic must not be the thing that breaks."""

    def test_report_is_still_shaped_when_the_sdk_is_missing(self, monkeypatch):
        monkeypatch.setattr(sdk_surface, "_sdk_module", lambda: None)
        report = surface_report()
        assert report["sdk_available"] is False
        assert set(report["sections"]) == set(SECTIONS)
        assert report["sections"]["options"]["entries"] == []

    def test_unreadable_source_yields_no_false_coverage(self, monkeypatch):
        """A module we cannot read must report nothing handled, not crash."""
        monkeypatch.setattr(sdk_surface, "_module_source", lambda name: "")
        sdk_surface.assigned_option_keys.cache_clear()
        assert assigned_option_keys() == frozenset()
