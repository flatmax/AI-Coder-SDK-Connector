"""The ``agy`` tool tables, and the invariants that keep them honest.

`scripts/probe_agy_tool_inventory.py` is the live half: it reads the tool
list off a real `init` frame and fails when the binary advertises a name
neither table knows. It needs `agy` installed, so it cannot run in CI, and
the checks here are the ones that hold without it — the internal
consistency the probe's verdict assumes.

The tables were widened on 2026-09-10 against `agy` 1.2.0, where **43 of 57
advertised tools were in neither table**. Two of the additions carry the
whole argument for the pass:

- ``sed_file``. [AG-R-11](../specs5/plan-ag/risks.md#ag-r-11) exists because
  an agent refused an ``edit_file`` reached for ``sed -i`` through
  ``run_command``, unprompted, on both probe runs. On 1.2.0 that is a
  first-class tool, and the write seam did not name it.
- ``define_subagent``, ``manage_subagents``, ``browser_subagent``. AG-5 puts
  a spawner in the seam because a child inherits the tool set; this release
  has three more spellings of spawning than the table knew.
"""

from __future__ import annotations

from aic_dc.agy import tools as agy_tools
from aic_dc.antigravity.permissions import ALWAYS_ASK, GATED_BY_DEFAULT, TOOL_CLASSES


class TestTheTablesAgree:
    def test_every_mutating_tool_has_a_class(self):
        """A name in the seam with no class would gate without a dialog shape.

        ``summarise_request``, the diff builder and ``derive_rules`` all
        switch on the class. A tool in ``MUTATING_TOOLS`` that is not in
        ``TOOL_CLASSES`` still asks — the default is to ask — but it asks
        with nothing in the body, which is the least useful moment for the
        one control this transport's containment rests on.
        """
        missing = agy_tools.MUTATING_TOOLS - set(agy_tools.TOOL_CLASSES)
        assert not missing, f"in MUTATING_TOOLS with no class: {sorted(missing)}"

    def test_a_name_is_classified_or_declared_but_never_both(self):
        """``SEEN_UNCLASSIFIED`` is the *complement* of the table, not a note.

        Overlap would make the probe's arithmetic wrong in the direction
        that hides a name: it subtracts both sets from what the binary
        advertises, so a name in both would be counted as covered twice and
        a rename could pass unnoticed.
        """
        both = set(agy_tools.TOOL_CLASSES) & agy_tools.SEEN_UNCLASSIFIED
        assert not both, f"classified and declared unclassified: {sorted(both)}"

    def test_nothing_declared_unclassified_is_in_the_write_seam(self):
        """The seam is the one table that must not have a deliberate gap."""
        both = agy_tools.MUTATING_TOOLS & agy_tools.SEEN_UNCLASSIFIED
        assert not both, sorted(both)


class TestTheDeclaredSetChangesNothingAboutGating:
    """Being on the seen-and-unclassified list is a record, not a policy.

    This is the claim that made it safe to write the list rather than guess
    at 34 tools' semantics: every name on it already raises the dialog, and
    goes on raising it, because the default for an unknown class is to ask.
    """

    def test_a_declared_name_still_gates_by_default(self):
        for name in sorted(agy_tools.SEEN_UNCLASSIFIED):
            tool_class = TOOL_CLASSES.get(name)
            assert tool_class is None, f"{name} is classified after all"
            assert GATED_BY_DEFAULT.get(tool_class, True) is True

    def test_no_declared_name_leaked_into_always_ask(self):
        """Not because it would be wrong — because it would be misleading.

        ``ALWAYS_ASK`` is the write seam under another name, and a reader
        checking whether a tool is treated as mutating should not find a
        name there that this module says it has not classified.
        """
        assert not (agy_tools.SEEN_UNCLASSIFIED & ALWAYS_ASK)


class TestTheSeamKnowsTheRoutesRoundARefusedWrite:
    """AG-R-11's list, in 1.2.0's vocabulary.

    Each of these is a way to change a file after the dialog said no. They
    are named individually rather than asserted as a set, so a removal is a
    failing test with a name on it rather than a smaller number.
    """

    def test_sed_file_is_a_write(self):
        assert agy_tools.TOOL_CLASSES["sed_file"] == "write"
        assert "sed_file" in agy_tools.MUTATING_TOOLS

    def test_the_shell_is_still_a_write(self):
        assert "run_command" in agy_tools.MUTATING_TOOLS

    def test_every_spelling_of_spawning_is_in_the_seam(self):
        for name in (
            "invoke_subagent",
            "define_subagent",
            "manage_subagents",
            "browser_subagent",
        ):
            assert name in agy_tools.MUTATING_TOOLS, name
            assert agy_tools.TOOL_CLASSES[name] == "delegate"

    def test_running_code_by_another_name_is_exec(self):
        for name in (
            "execute_browser_javascript",
            "notebook_execution",
            "send_command_input",
            "call_mcp_tool",
        ):
            assert agy_tools.TOOL_CLASSES[name] == "exec", name
            assert name in agy_tools.MUTATING_TOOLS, name
