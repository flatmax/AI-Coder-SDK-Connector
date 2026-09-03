"""``agy``'s tool vocabulary, in the terms the shared dialog already speaks.

The two Antigravity products **agree on argument names and disagree on tool
names**, which is the wrong way round for anyone reusing
:mod:`aic_dc.antigravity.permissions`. Measured 2026-09-03:

===================  =========================  ==========================
Job                  SDK (``BuiltinTools``)     ``agy``
===================  =========================  ==========================
edit a file          ``edit_file``              ``replace_file_content``
create a file        ``create_file``            ``write_to_file``
find files           ``find_file``              ``find_by_name``
list a directory     ``list_directory``         ``list_dir``
read a file          ``view_file``              ``view_file``
run a command        ``run_command``            ``run_command``
===================  =========================  ==========================

**The failure this prevents is quiet, and in the safe direction, which is
what makes it dangerous.** An unrecognised tool name classifies as
``exec``, so the call is still gated and nothing is ungated by the
omission. But the dialog would call a file edit a *command*, and
``_diff_tool_for`` would not recognise it, so **no diff would render** —
the gate holding while the product's central feature silently degrades.
A gate that works and a dialog that shows nothing is not a safe failure;
it is the same manufactured consent [AG-R-11](../../../specs5/plan-ag/risks.md#ag-r-11)
is about, arrived at by a different road.

Why these merge into the shared tables rather than sitting beside them
----------------------------------------------------------------------
The names do not collide — no SDK tool is called ``replace_file_content``
— so one table can hold both vocabularies, and one table cannot disagree
with itself. That is the same reasoning as ``ALWAYS_ASK is
MUTATING_TOOLS``: a second copy is the version that drifts. The merge
happens in :mod:`~aic_dc.antigravity.permissions`, which imports from here
rather than the other way about, so this module stays importable with no
SDK and no engine.

**The dialog still reports the name that was actually called.** These maps
say what a tool *is*, never what to call it: a user gated on
``replace_file_content`` sees ``replace_file_content``, because the
transcript has to match what the agent actually did.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5;
``sdk-surface.md`` § *The tool names differ, and only the tool names*.
"""

from __future__ import annotations

#: ``agy`` tool → the dialog's class, in the same vocabulary
#: :data:`aic_dc.antigravity.permissions.TOOL_CLASSES` uses.
#:
#: Only names that differ from the SDK's need to be here for correctness,
#: but the shared ones are listed too: this is the readable answer to "what
#: does ``agy`` call, and what is each of them", and leaving out the
#: overlapping half would make it look like the overlap does not exist.
TOOL_CLASSES: dict[str, str] = {
    # Mutating — every one of these is also in MUTATING_TOOLS below.
    "replace_file_content": "write",
    "multi_replace_file_content": "write",
    "write_to_file": "write",
    "notebook_edit": "write",
    "generate_image": "write",
    "run_command": "exec",
    "invoke_subagent": "delegate",
    # Read-only.
    "view_file": "read",
    "find_by_name": "read",
    "list_dir": "read",
    "grep_search": "read",
    "codebase_search": "read",
    "search_web": "read",
    "read_url_content": "read",
    # Agent-initiated question. Gated by class, and blocked on the same
    # missing dialog the SDK's `ask_question` is.
    "ask_question": "interact",
}

#: The write seam for this transport — [AG-5](../../../specs5/plan-ag/decisions.md#ag-5)'s
#: boundary in ``agy``'s words.
#:
#: ``run_command`` is here for the reason AG-R-11 exists, and it is no
#: longer a precaution: the phase-8 gate probe watched the model, refused
#: an edit, **reach for ``run_command`` and then ``list_dir``** to make the
#: same change. Three routes to one write, on this transport, measured.
#:
#: ``invoke_subagent`` is here because a subagent inherits the tool set, so
#: a gate stopping at the top-level trajectory is bypassed by asking a
#: child to do the write — the same hole one level down.
MUTATING_TOOLS = frozenset(
    {
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
        "notebook_edit",
        "generate_image",
        "run_command",
        "invoke_subagent",
    }
)

#: ``agy`` argument names → the field names the dialog's payload builders
#: read. Identical in shape to the SDK's aliases, and mostly identical in
#: content, because the *arguments* are the half the two products agree on.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "replace_file_content": {
        "TargetFile": "file_path",
        "TargetContent": "old_string",
        "ReplacementContent": "new_string",
        "Instruction": "description",
    },
    "multi_replace_file_content": {
        "TargetFile": "file_path",
        "TargetContent": "old_string",
        "ReplacementContent": "new_string",
        "Instruction": "description",
    },
    "write_to_file": {
        "TargetFile": "file_path",
        "CodeContent": "content",
        "Description": "description",
    },
    "generate_image": {"OutputPath": "file_path", "Prompt": "description"},
    "run_command": {
        "CommandLine": "command",
        "Cwd": "cwd",
        "Explanation": "description",
    },
    "view_file": {"AbsolutePath": "file_path", "TargetFile": "file_path"},
    "list_dir": {"DirectoryPath": "file_path"},
    # The path is the directory searched. `Pattern` is deliberately not
    # aliased to a path field: the dialog promises a file where it says
    # PATH, and a glob is not one.
    "find_by_name": {"SearchDirectory": "file_path"},
}

#: How the diff builder should reason about an ``agy`` write.
#:
#: ``build_diff_payload`` switches on a *Claude* tool name to decide
#: whether it is looking at a whole-file replacement or a string-for-string
#: edit. This is the one place an ``agy`` name is deliberately translated,
#: and it does not reach the user: it decides the shape of the diff, never
#: the label on it.
DIFF_SHAPE: dict[str, str] = {
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "write_to_file": "Write",
    "notebook_edit": "Write",
    "generate_image": "Write",
}
