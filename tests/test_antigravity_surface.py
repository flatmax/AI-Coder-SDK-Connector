"""Tests for aic_dc.antigravity.surface — the Antigravity drift gate.

The same gate ``test_claude_code_sdk_surface`` is, against a much less
stable target. That SDK was 0.2.137 and shipping occasionally; this one is
**0.1.16, alpha, and releasing roughly daily with no compatibility
commitment**. An Antigravity release that adds a step type, a builtin tool
or a hook class is not a build break and never will be — it is a feature
we quietly do not offer, or worse, a step the chat silently drops.

It has caught one already: 0.1.16 added ``StopHook`` and this file went
red with that name in the message, which is the whole mechanism working.

The gate is deliberately not "coverage must be complete". At phase 1 there
is no engine at all, so *nothing* is handled and that is correct: AG-8
says the probe fails on **untriaged, never on unimplemented**, because a
gate that failed on unbuilt surface would earn an ignore-list within a
week. It is "coverage must be **decided**": every name the installed SDK
exposes is handled, declined with a reason, or pending with an argument.
Only the undecided fail, and they fail *by name*, so the failure message
is the triage list.

Closing a failure means one of three edits, all cheap:

- implement it → this package passes the keyword, subclasses the hook, or
  names the enum member, and the coverage is derived from that
- refuse it → :data:`NEVER_SET_CONFIG` / the per-section tables, with why
- defer it → :data:`PENDING_CONFIG` / the per-section tables, with what it
  would buy

Offline: pure reflection over the installed wheel and this package's own
syntax trees. No agent, no harness binary, no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from aic_dc.antigravity import surface
from aic_dc.antigravity.surface import (
    BUILTIN_TOOLS,
    CAPABILITY_FIELDS,
    DECLINED,
    HANDLED,
    HOOK_CLASSES,
    NEVER_SET_CONFIG,
    PENDING,
    PENDING_CONFIG,
    POLICY_BUILDERS,
    STEP_MEMBERS,
    builtin_tool_names,
    capability_fields,
    capability_report,
    config_fields,
    config_report,
    diff_agy_init,
    hook_class_names,
    hook_report,
    policy_builder_names,
    policy_report,
    referenced_enum_members,
    step_enum_members,
    step_report,
    surface_report,
    tool_report,
)

SECTIONS = ("config", "tools", "hooks", "steps", "policy", "capabilities")

#: The tables, by the section whose names they classify. Several checks
#: apply to all six, and listing them once means a seventh section added
#: later cannot quietly skip them.
TABLES = {
    "tools": BUILTIN_TOOLS,
    "hooks": HOOK_CLASSES,
    "steps": STEP_MEMBERS,
    "policy": POLICY_BUILDERS,
    "capabilities": CAPABILITY_FIELDS,
}

pytestmark = pytest.mark.skipif(
    surface._sdk() is None,
    reason="google-antigravity is an optional extra (AG-R-10); nothing to probe",
)


@pytest.fixture(autouse=True)
def _clear_ast_caches():
    """Drop the AST caches between tests.

    The readers are cached because a report parses the whole package and
    the tab can refresh; that cache would otherwise leak one test's
    monkey-patched sources into the next.
    """
    readers = (
        surface.config_keywords,
        surface.capability_keywords,
        surface.referenced_names,
        surface.referenced_enum_members,
    )
    for reader in readers:
        reader.cache_clear()
    yield
    for reader in readers:
        reader.cache_clear()


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


class TestNothingUntriaged:
    """The whole point of the file: no surface without a decision."""

    @pytest.mark.parametrize("section", SECTIONS)
    def test_section_has_no_unclassified_surface(self, section):
        unclassified = surface_report()["sections"][section]["unclassified"]
        assert not unclassified, (
            f"The installed google-antigravity exposes {section} surface that "
            f"AIC-DC has not decided about: {', '.join(unclassified)}.\n"
            "This is an alpha SDK release adding something, not a bug in "
            "this test. Implement it, refuse it in the section's table with "
            "the reason, or defer it there with what it would buy. See "
            "src/aic_dc/antigravity/surface.py and specs5/plan-ag/."
        )

    @pytest.mark.parametrize("section", SECTIONS)
    def test_no_stale_entries(self, section):
        """Names we still explain that the SDK no longer has.

        Not a failure of coverage but of prose: a note arguing about a
        field that no longer exists sends the next reader looking for it.
        On a package at 0.1.x this is the *likelier* direction — names
        move and vanish between releases with no deprecation.
        """
        stale = surface_report()["sections"][section]["stale"]
        assert not stale, (
            f"surface.py still classifies {section} that the installed SDK "
            f"no longer exposes: {', '.join(stale)}. Delete the entries."
        )

    @pytest.mark.parametrize("section", SECTIONS)
    def test_nothing_we_use_is_still_argued_against(self, section):
        """The bucket ``stale`` structurally cannot cover.

        A pending note is an argument for *not* doing something. Once the
        code does it, the note is worse than absent — it reads as a reason
        to undo the work. ``stale`` only catches names the SDK removed;
        these names still exist and the argument against them is what went
        out of date.
        """
        resolved = surface_report()["sections"][section]["resolved"]
        assert not resolved, (
            f"These {section} names are now used by aic_dc.antigravity but a "
            f"table still argues against them: {', '.join(resolved)}. Delete "
            "the entries — the code already outvoted them."
        )


class TestEveryDecisionCarriesItsReason:
    """A bare name is not triage; the next reader needs the why."""

    @pytest.mark.parametrize("section,table", TABLES.items())
    def test_table_notes_are_arguments(self, section, table):
        for name, (status, note) in table.items():
            assert status in (HANDLED, DECLINED, PENDING), f"{section}: {name}"
            assert len(note) > 30, f"{section}: {name} needs a real note, not {note!r}"

    def test_config_notes_are_arguments(self):
        for table in (NEVER_SET_CONFIG, PENDING_CONFIG):
            for name, note in table.items():
                assert len(note) > 40, f"{name} needs a real note, not {note!r}"

    def test_refused_and_deferred_do_not_overlap(self):
        """One owner per decision: refused *or* deferred, not both."""
        assert not set(NEVER_SET_CONFIG) & set(PENDING_CONFIG)


# ----------------------------------------------------------------------
# The decisions this directory is not free to reverse silently
# ----------------------------------------------------------------------


class TestBindingDecisions:
    """Rows that are a spec commitment rather than a status.

    Each of these is a decision in ``specs5/plan-ag/decisions.md`` that a
    later phase could undo by accident — by reaching for the convenient
    SDK path, or by gating the obvious tools and stopping there. A generic
    "everything is classified" sweep would stay green through all of it.
    """

    def test_the_permission_gate_builders_stay_declined(self):
        """AG-5. The surface most likely to be adopted by accident.

        ``policy.ask_user`` is the documented, convenient path, and taking
        it gives away both the message the model reads and
        ``modified_args`` — the ability to amend a tool call before it
        runs. AG-5's argument turns on there being a dialog to lose, so
        the *gate* builders are what it forbids; the consultant's
        ``deny_all`` + single ``allow`` is a capability restriction with
        no user in the loop and is out of scope of it.

        ``allow_all`` is the one that must never move: it is blanket
        bypass under a friendly name.
        """
        by_name = {e["name"]: e["status"] for e in policy_report()["entries"]}
        assert by_name, "the policy module should expose builders"
        for name in (
            "ask_user",
            "allow_all",
            "safe_defaults",
            "confirm_run_command",
            "enforce",
        ):
            assert by_name[name] == DECLINED, (
                f"{name} is a permission-gate builder. It reading as handled "
                "means the dialog's amend path has been given away, which is "
                "the whole of AG-5."
            )

    def test_run_command_is_gated_like_the_file_tools(self):
        """AG-R-11, the finding nobody predicted.

        On both phase-2 probe runs the model answered a denied
        ``edit_file`` by rewriting the file through ``run_command`` —
        ``sed -i``, then inline ``python3``, neither suggested by the
        prompt. A dialog that gates only the file tools shows the user a
        diff, records their refusal, and lets the edit through anyway.
        So the three mutating tools move together: any one of them
        reaching ``handled`` while another lags is the exact hole.
        """
        by_name = {e["name"]: e["status"] for e in tool_report()["entries"]}
        mutating = ("CREATE_FILE", "EDIT_FILE", "RUN_COMMAND")
        statuses = {name: by_name[name] for name in mutating}
        assert len(set(statuses.values())) == 1, (
            "AG-5 defines the permission seam as all mutating tools. These "
            f"have diverged: {statuses}. RUN_COMMAND is not a lesser case — "
            "it is how a refused edit gets made anyway."
        )

    def test_the_permission_hook_is_never_declined(self):
        """AG-5: the dialog is a requirement, not a feature.

        An engine that cannot render a proposed edit as a diff does not
        ship as master. Declining this hook would be that decision, taken
        in a table.
        """
        by_name = {e["name"]: e["status"] for e in hook_report()["entries"]}
        assert by_name["PreToolCallDecideHook"] != DECLINED

    def test_unknown_enum_members_are_never_declined(self):
        """The forward-compatibility escape hatches, on an alpha SDK.

        ``StepType.UNKNOWN`` and friends are how a step this wheel does
        not know arrives. Declining one means deciding to drop it, and on
        a package releasing daily that is the drift this file exists to
        catch arriving as silence in the chat instead.
        """
        by_name = {e["name"]: e["status"] for e in step_report()["entries"]}
        unknowns = [
            name
            for name in by_name
            if name.endswith((".UNKNOWN", ".UNSPECIFIED"))
        ]
        assert unknowns, "the step enums should carry escape-hatch members"
        for name in unknowns:
            assert by_name[name] != DECLINED, f"{name} must be rendered, not dropped"


# ----------------------------------------------------------------------
# Per-surface shape
# ----------------------------------------------------------------------


class TestSectionShape:
    @pytest.mark.parametrize(
        "report,names",
        [
            (config_report, config_fields),
            (tool_report, builtin_tool_names),
            (hook_report, hook_class_names),
            (step_report, step_enum_members),
            (policy_report, policy_builder_names),
            (capability_report, capability_fields),
        ],
    )
    def test_every_name_gets_exactly_one_status(self, report, names):
        entries = report()["entries"]
        assert [e["name"] for e in entries] == names()
        assert entries, "reflection should find something"
        assert all(e["status"] in (HANDLED, DECLINED, PENDING) for e in entries)

    def test_config_reads_pydantic_fields_not_dataclass_fields(self):
        """The first way the Claude probe's reflection does not transfer.

        ``dataclasses.fields`` raises on these models; ``model_fields`` is
        the reader. A silent empty list here would read as "the SDK has no
        config" and pass every other check in the file.
        """
        fields = config_fields()
        assert "workspaces" in fields
        assert "policies" in fields
        assert "api_key" in fields, "LocalAgentConfig's fields must be included"

    def test_hooks_are_found_by_subclass_not_by_name(self):
        """So a hook added in a release appears without being guessed."""
        names = hook_class_names()
        assert "PreToolCallDecideHook" in names
        assert "PostToolCallHook" in names
        for base in surface.HOOK_BASES:
            assert base not in names, f"{base} is an ABC, not an event"
        assert not any(n.startswith("_") for n in names), "internal plumbing"

    def test_step_members_are_qualified_by_enum(self):
        """``USER`` is a member of two step enums and ``UNKNOWN`` of four.

        Bare names would collapse them, and one enum's member being read
        would report the whole taxonomy covered.
        """
        members = step_enum_members()
        assert "StepSource.USER" in members
        assert "StepTarget.USER" in members
        assert len(members) == len(set(members))


# ----------------------------------------------------------------------
# Coverage is derived, not declared
# ----------------------------------------------------------------------


class TestCoverageIsDerived:
    """The tables say *why*; this package's own syntax says *whether*.

    The point is that phases 3 onward move these rows by writing code, not
    by editing a table — which is what keeps the report honest once
    somebody is busy building an engine.
    """

    def test_coverage_is_exactly_what_has_been_built(self):
        """AG-8: the gate fails on untriaged, never on unimplemented.

        This assertion is *meant* to move, and has three times now.
        Writing ``credentials.py`` turned four config rows handled;
        ``consultant.py`` turned four more plus two policy builders and
        ``enabled_tools``; phase 3's ``options.py`` added ``hooks`` and
        ``tools`` and pinned ``agent_behavior``, and its ``steps.py``
        turned the whole step taxonomy over. Each time the gate named the
        newly-stale arguments rather than leaving the report claiming
        nobody had got to them — a better record of each transition than a
        comment claiming it happened.

        The step rows are the one section that is *declared* rather than
        derived, because the pump compares enum members on ``.name``
        against string literals and the syntax tree cannot see that. Its
        cross-check is
        ``test_antigravity_steps.py::TestEveryStepMemberIsNamedInThePump``.
        """
        report = surface_report()
        handled = {
            section: sorted(
                e["name"] for e in body["entries"] if e["status"] == HANDLED
            )
            for section, body in report["sections"].items()
            if any(e["status"] == HANDLED for e in body["entries"])
        }
        assert handled == {
            # credentials.py resolves four; consultant.py passes four more
            # for a one-shot config; options.py adds `hooks` (where AG-5's
            # gate attaches) and `tools` (AG-4's index callables).
            "config": [
                "api_key",
                "capabilities",
                "hooks",
                "location",
                "model",
                "policies",
                "project",
                "tools",
                "vertex",
                "workspaces",
            ],
            # The minimal static allowlist, and nothing else. AG-5's gate
            # is the raw decide hook, which is the row below.
            "policy": ["allow", "deny_all"],
            # permissions.py subclasses it in `as_hook`. The one hook that
            # is a requirement rather than a feature.
            "hooks": ["PreToolCallDecideHook"],
            "capabilities": ["agent_behavior", "enabled_tools"],
            # The whole step taxonomy, less StopReason — the pump forwards
            # a stop reason verbatim but nothing renders the difference
            # between a budget cap and an ordinary stop yet.
            "steps": sorted(
                name
                for name, (status, _note) in surface.STEP_MEMBERS.items()
                if status == HANDLED
            ),
        }, (
            f"Coverage has moved: {handled}. If that is a later phase "
            "arriving, update this and check the per-section assertions "
            "still say what they should."
        )
        assert "steps" in handled and not any(
            name.startswith("StopReason.") for name in handled["steps"]
        ), "a StopReason row reads as handled but nothing renders one yet"
        assert "tools" not in handled, (
            "the tools section reads as handled, but a tool is handled when "
            "the chat card and the gate both know about it, and there is no "
            "per-tool card table yet."
        )

    def test_the_index_route_is_wired_and_the_mcp_one_is_not(self):
        """AG-4: ``tools`` is the route in, and ``mcp_servers`` is not.

        ``tools`` read as pending through phase 1 and turned handled when
        ``options.py`` wired it. That the *reader* can tell the difference
        is the thing worth pinning: it is deliberately scoped to the
        config constructors, because an earlier draft collected keyword
        names from every call in the package and reported AG-4's route as
        built on the strength of ``bridge.py`` passing ``tools=`` to
        *Claude's* ``create_sdk_mcp_server``. Two SDKs in one package
        means a bare keyword is not evidence about either.

        ``mcp_servers`` stays pending on its merits: Antigravity's MCP
        support is stdio and streamable-HTTP only, and AG-4 routes the
        indexes through callables instead precisely so nothing needs it.
        """
        by_name = {e["name"]: e["status"] for e in config_report()["entries"]}
        assert by_name["tools"] == HANDLED
        assert by_name["mcp_servers"] == PENDING

    def test_the_consultants_builtin_tools_do_not_read_as_the_config_field(self):
        """The near-miss the scoping exists to prevent, still checked.

        ``consultant.py``'s own parameter is named ``builtin_tools`` rather
        than ``tools`` for this reason. If it is ever renamed, ``tools``
        would read as handled from the consultant alone — true today by
        accident rather than because AG-4's route was built.
        """
        from pathlib import Path

        from aic_dc.antigravity import consultant

        source = Path(consultant.__file__).read_text(encoding="utf-8")
        assert "builtin_tools" in source
        assert "\n        tools=" not in source

    def test_a_passed_keyword_reads_as_handled(self, monkeypatch):
        """A check that cannot fail is decoration.

        Simulates phase 3 setting ``workspaces=`` on the config, which is
        the assignment AG-10 pins to the repo root.
        """
        monkeypatch.setattr(
            surface, "config_keywords", lambda: frozenset({"workspaces"})
        )
        by_name = {e["name"]: e for e in config_report()["entries"]}
        assert by_name["workspaces"]["status"] == HANDLED
        assert by_name["workspaces"]["note"] == "", "a handled row needs no argument"

    def test_a_subclassed_hook_reads_as_handled(self, monkeypatch):
        """Simulates phase 4 attaching the permission dialog."""
        monkeypatch.setattr(
            surface, "referenced_names", lambda: frozenset({"PreToolCallDecideHook"})
        )
        by_name = {e["name"]: e["status"] for e in hook_report()["entries"]}
        assert by_name["PreToolCallDecideHook"] == HANDLED
        assert by_name["PostToolCallHook"] == PENDING, "one hook, not the section"

    def test_enum_coverage_does_not_leak_between_enums(self, monkeypatch):
        """Reading one enum's member must not cover another's namesake.

        Phase 3 declared the ``Step*`` rows handled in the table, which
        would make this pass for the wrong reason — the table, not the
        reader. So it is asserted on ``StopReason``, the one step-section
        enum still pending, against a ``StepStatus`` member spelled the
        same way. Both are ``UNSPECIFIED``/``UNKNOWN``-adjacent names of
        the kind a qualified reader is easiest to get wrong on.
        """
        monkeypatch.setattr(
            surface,
            "referenced_enum_members",
            lambda: frozenset({"StopReason.UNSPECIFIED"}),
        )
        monkeypatch.setitem(
            surface.STEP_MEMBERS, "StepStatus.UNSPECIFIED", (PENDING, "a fake row")
        )
        by_name = {e["name"]: e["status"] for e in step_report()["entries"]}
        assert by_name["StopReason.UNSPECIFIED"] == HANDLED
        assert by_name.get("StepStatus.UNSPECIFIED", PENDING) == PENDING

    def test_qualified_reads_match_either_import_style(self, monkeypatch, tmp_path):
        """``StepType.THINKING`` and ``types.StepType.THINKING`` both count.

        Which one a module writes is an import-style choice, and a probe
        that only understood one would report a real dispatch as a gap.
        """
        module = tmp_path / "pump.py"
        module.write_text(
            "def render(step):\n"
            "    if step.type is types.StepType.THINKING:\n"
            "        return 'thinking'\n"
            "    if step.status is StepStatus.CANCELED:\n"
            "        return 'cancelled'\n"
            "    return ''\n",
            encoding="utf-8",
        )
        import ast

        monkeypatch.setattr(
            surface,
            "_package_trees",
            lambda: [ast.parse(module.read_text(encoding="utf-8"))],
        )
        members = referenced_enum_members()
        assert "StepType.THINKING" in members
        assert "StepStatus.CANCELED" in members

    def test_a_name_only_in_a_comment_is_not_coverage(self, monkeypatch, tmp_path):
        """The way the Claude probe's second draft got this wrong.

        A word-search reported ``skills`` handled because a comment
        mentioned ``.claude/skills/``. Reading syntax has no such blind
        spot, and a docstring naming a hook is not registering one.
        """
        module = tmp_path / "notes.py"
        module.write_text(
            '"""We will want PreToolCallDecideHook and workspaces later."""\n'
            "# PostToolCallHook is also interesting\n",
            encoding="utf-8",
        )
        import ast

        monkeypatch.setattr(
            surface,
            "_package_trees",
            lambda: [ast.parse(module.read_text(encoding="utf-8"))],
        )
        assert "PreToolCallDecideHook" not in surface.referenced_names()
        assert "workspaces" not in surface.config_keywords()

    def test_a_conditionally_set_field_still_counts(self, monkeypatch, tmp_path):
        """The way the Claude probe's *first* draft got it wrong.

        Diffing the output of a config builder under a default config
        reported 36 false gaps, because half the fields are set only when
        something asks for them. Syntax has no such blind spot: a branch
        is still an assignment.
        """
        module = tmp_path / "options.py"
        module.write_text(
            "def build(cfg):\n"
            "    kwargs = {'workspaces': [cfg.root]}\n"
            "    if cfg.resume:\n"
            "        kwargs['conversation_id'] = cfg.resume\n"
            "    return LocalAgentConfig(model=cfg.model, **kwargs)\n",
            encoding="utf-8",
        )
        import ast

        monkeypatch.setattr(
            surface,
            "_package_trees",
            lambda: [ast.parse(module.read_text(encoding="utf-8"))],
        )
        passed = surface.config_keywords()
        for name in ("workspaces", "conversation_id", "model"):
            assert name in passed


# ----------------------------------------------------------------------
# The live-CLI half
# ----------------------------------------------------------------------


class TestDiffAgyInit:
    """``agy`` is not the engine and is still the only inventory there is.

    AG-8 wires its ``init`` frame in as the analogue of the Claude probe's
    ``diff_server_info``, because static reflection structurally cannot
    reach a separate 208 MB binary's 57-tool list.
    """

    FRAME = {
        "event": "init",
        "conversation_id": "57f59897-0000-0000-0000-000000000000",
        "init": {
            "model": "gemini-3.7-flash-low",
            "cwd": "/tmp/agy-probe",
            "permission_mode": "request-review",
            "tools": ["run_command", "write_to_file", "generate_image", "run_command"],
        },
    }

    def test_none_is_not_an_error(self):
        """A report of the static surface is worth having with no CLI.

        Which is exactly when someone opens this tab.
        """
        result = diff_agy_init(None)
        assert result["available"] is False
        assert result["tools"] == []

    def test_reads_the_nested_frame(self):
        """The shape is ``{"event":…, "init":{…}}``, not flat.

        Phase 0 transcribed it flat and the correction landed on
        re-measurement at 1.1.22. A parser written against the flat shape
        read ``None`` for every field *without erroring*, which is the
        failure this test pins: wrong and green.
        """
        result = diff_agy_init(self.FRAME)
        assert result["available"] is True
        assert result["model"] == "gemini-3.7-flash-low"
        assert result["cwd"] == "/tmp/agy-probe"
        assert result["permission_mode"] == "request-review"
        assert result["tools"] == ["generate_image", "run_command", "write_to_file"]

    def test_reads_an_already_unwrapped_payload(self):
        """A caller that unwrapped the frame itself gets the same answer."""
        assert diff_agy_init(self.FRAME["init"]) == diff_agy_init(self.FRAME)

    def test_junk_shapes_do_not_raise(self):
        for payload in ({"init": "nope"}, {"init": {"tools": [None, 3]}}, {}, [], 7):
            diff_agy_init(payload)


# ----------------------------------------------------------------------
# The report the browser reads
# ----------------------------------------------------------------------


class TestSurfaceReport:
    def test_shape(self):
        report = surface_report()
        assert report["sdk_available"] is True
        assert set(report["sections"]) == set(SECTIONS)
        assert set(report["versions"]) == {"sdk_version", "harness_binary"}

    def test_the_bundled_harness_binary_is_located(self):
        """An installed wheel is not a present harness.

        The SDK spawns a bundled Go binary; a version number alone would
        assert the second from the first.
        """
        assert surface_report()["versions"]["harness_binary"].endswith("localharness")

    def test_counts_add_up_to_the_entries(self):
        report = surface_report()
        for section, counts in report["counts"].items():
            entries = report["sections"][section]["entries"]
            assert sum(counts.values()) == len(entries), section

    def test_unclassified_only_lists_sections_that_have_some(self):
        """Empty is the healthy state, and it must read as empty."""
        assert all(v for v in surface_report()["unclassified"].values())

    def test_agy_init_flows_through(self):
        report = surface_report(TestDiffAgyInit.FRAME)
        assert report["cli"]["available"] is True
        assert report["cli"]["model"] == "gemini-3.7-flash-low"

    def test_json_serialisable(self):
        """It crosses a JSON-RPC boundary, so it must survive the trip."""
        json.dumps(surface_report())


class TestDegradesWithoutTheSdk:
    """A diagnostic must not be the thing that breaks.

    ``google-antigravity`` is an optional extra (AG-R-10: it bundles a
    second ~119 MB binary on top of the ~295 MB Claude CLI), so absent is
    a supported state rather than a broken install.
    """

    def test_report_is_still_shaped_when_the_sdk_is_missing(self, monkeypatch):
        monkeypatch.setattr(surface, "_sdk", lambda: None)
        report = surface_report()
        assert report["sdk_available"] is False
        assert set(report["sections"]) == set(SECTIONS)
        assert report["sections"]["config"]["entries"] == []
        assert not report["unclassified"]
        json.dumps(report)

    def test_unreadable_sources_yield_no_false_coverage(self, monkeypatch):
        """A module we cannot parse reports nothing handled, not a crash."""
        monkeypatch.setattr(surface, "_package_trees", lambda: [])
        assert surface.config_keywords() == frozenset()
        assert surface.capability_keywords() == frozenset()
        assert surface.referenced_names() == frozenset()
        assert surface.referenced_enum_members() == frozenset()
