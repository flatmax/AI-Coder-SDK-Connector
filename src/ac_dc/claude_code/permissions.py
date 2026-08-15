"""The permission gate: ``can_use_tool`` as a browser dialog.

When Claude Code wants to use a tool the permission flow does not already
approve, the SDK calls :meth:`PermissionBroker.can_use_tool`. This module
turns that call into a dialog in every connected browser, waits for a
**localhost** client to answer, and returns the answer to the SDK.

Three things about this module are load-bearing
(``specs5/3-engine/permissions.md`` § Invariants):

- **It is the only ask path, and it is not a display channel.** The
  callback fires only when the flow falls through to a prompt: calls
  approved by an allow rule, by ``permission_mode``, or by a ``PreToolUse``
  hook never reach it. Tool *display* comes from the message stream. A UI
  that drew tool cards from here would go silent the moment a user switched
  to ``acceptEdits``.
- **Every request resolves exactly once, and the SDK always gets a
  result.** By a localhost decision, by the timeout, or by
  :meth:`PermissionBroker.cancel_all` at teardown. A request that resolved
  twice would answer one control request and leave another hanging; one
  that never resolved would wedge the turn.
- **Only localhost may answer.** ``can_use_tool`` authorises arbitrary
  ``Bash``; a remote participant able to answer it would make
  collaboration mode a remote-code-execution grant. The gate itself lives
  in :class:`~ac_dc.claude_code.service.ClaudeCodeService` (it needs the
  RPC caller's identity); this module only reports whether a localhost
  client is *present*, which shortens the timeout when one is not.

The tool classification map below chooses **dialog content**, not whether
to ask. Whether a call is gated is the CLI's decision, made before we are
called — so a ``Read`` that reaches this callback (because the user wrote
an ``ask`` rule, or because it points outside the allowed directories) is
asked about, notwithstanding that reads are ungated by default.

Governing spec: ``specs5/3-engine/permissions.md``.
Reference: ``specs-reference/3-engine/permissions.md``.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ac_dc.claude_code.messages import Event, mcp_server_name, summarise_tool_input

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numeric constants
# (specs-reference/3-engine/permissions.md § Numeric constants)
# ---------------------------------------------------------------------------

# Long enough to read a diff carefully; short enough that a forgotten tab
# does not wedge a turn for the rest of the day.
DECISION_TIMEOUT = 300.0

# The shorter path when no localhost client is connected when the request is
# raised. A fast deny is more useful than a five-minute stall, and a
# localhost client that connects inside the window can still answer.
NO_LOCALHOST_TIMEOUT = 30.0

# Per side. Above it the dialog gets a summary and an explicit "too large to
# diff" label rather than a Monaco instance that hangs the tab.
DIFF_CEILING_BYTES = 2 * 1024 * 1024

# Commands longer than this are sent truncated, with `truncated: true` so the
# dialog can offer a full-text expander from the verbatim `input`. Never
# truncated silently.
COMMAND_DISPLAY_CHARS = 4_000

# How many resolutions to remember after the fact, so a late second click
# gets `already_resolved` (with attribution) rather than `unknown`.
RESOLUTION_MEMORY = 64


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# The MCP server AC-DC itself exposes (phase 4). Its tools are repo
# introspection — the same class of consequence as Read.
AC_DC_MCP_SERVER = "ac-dc"

_READ_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookRead", "TodoWrite"}
)
_WRITE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
_EXEC_TOOLS = frozenset({"Bash", "BashOutput", "KillShell"})
_DELEGATE_TOOLS = frozenset({"Task"})
_INTERACT_TOOLS = frozenset({"AskUserQuestion"})
_PLAN_TOOLS = frozenset({"ExitPlanMode"})

TOOL_CLASSES: dict[str, str] = {
    **{name: "read" for name in _READ_TOOLS},
    **{name: "write" for name in _WRITE_TOOLS},
    **{name: "exec" for name in _EXEC_TOOLS},
    **{name: "delegate" for name in _DELEGATE_TOOLS},
    **{name: "interact" for name in _INTERACT_TOOLS},
    **{name: "plan" for name in _PLAN_TOOLS},
}

# The CLI's default posture per class, for the dialog's own copy. We do not
# implement gating — see the module docstring.
GATED_BY_DEFAULT: dict[str, bool] = {
    "read": False,
    "write": True,
    "exec": True,
    "delegate": False,
    "interact": True,
    "plan": True,
    "mcp": True,
}

# Which file path each write tool carries. NotebookEdit's is a notebook, not
# a text file, which is why it gets the full-content fallback rather than a
# diff.
_WRITE_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def classify_tool(tool_name: str) -> str:
    """Classify a tool for dialog content.

    Unknown names classify as ``mcp`` when they look like MCP tools and
    ``exec`` otherwise: a built-in tool that appears in a CLI upgrade must
    arrive with the *most* cautious dialog, not the least.
    """
    known = TOOL_CLASSES.get(tool_name)
    if known is not None:
        return known
    server = mcp_server_name(tool_name)
    if server is not None:
        return "read" if server == AC_DC_MCP_SERVER else "mcp"
    if tool_name.startswith("mcp__"):
        # `mcp__server` with no tool part — malformed, but still MCP.
        return "mcp"
    logger.info(
        "Unknown tool %r reached the permission gate; classified as exec so the "
        "dialog shows the full input",
        tool_name,
    )
    return "exec"


# ---------------------------------------------------------------------------
# Deny reasons
# ---------------------------------------------------------------------------

# Every denial carries a reason, because a blank denial produces an agent
# that retries the same call. These three distinguish "nobody was there"
# from "the user said no" so the model can tell it was not refused on the
# merits.

DENY_TIMEOUT_REASON = (
    "Nobody answered this permission request within {seconds:.0f} seconds, so it "
    "was denied automatically. This is not a refusal on the merits — say what you "
    "need and why, or continue with something that does not need permission."
)

DENY_NO_LOCALHOST_REASON = (
    "No local AC-DC client was connected to answer this permission request within "
    "{seconds:.0f} seconds, so it was denied automatically. Remote collaborators "
    "cannot grant permissions. Treat this session as read-only until a local "
    "client is back."
)

DENY_SHUTDOWN_REASON = (
    "The AC-DC session shut down before this permission request was answered."
)

DENY_DEFAULT_REASON = "The user denied this call without giving a reason."

_DECISION_ACTIONS = frozenset(
    {"allow", "allow_always", "allow_mode", "deny", "deny_interrupt"}
)

# The actions that let the call through. Named once, because "is this a
# denial?" is asked in several places and enumerating the allows at each
# one is how a new allow ends up rendered as a denial.
ALLOW_ACTIONS = frozenset({"allow", "allow_always", "allow_mode"})


# ---------------------------------------------------------------------------
# Pending state
# ---------------------------------------------------------------------------


@dataclass
class PendingPermission:
    """One in-flight request and the future the callback is waiting on."""

    permission_id: str
    request_id: str | None
    tool_name: str
    tool_use_id: str
    payload: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]
    expires_at: float
    resolved: bool = False
    resolved_by: str | None = None
    suggested_rules: list[dict[str, Any]] = field(default_factory=list)
    # Held here, not read back off the wire. `resolve_permission` is
    # localhost-only, but a mode is a session-wide grant and the request we
    # built is the only trustworthy statement of which one was offered.
    suggested_mode: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Payload builders (pure, so they can be tested without a session)
# ---------------------------------------------------------------------------


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _resolve_path(repo_root: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else repo_root / path


def _count_changes(original: str, proposed: str) -> tuple[int, int]:
    """Added and removed line counts, as the dialog's header reports them."""
    additions = deletions = 0
    diff = difflib.unified_diff(
        original.splitlines(), proposed.splitlines(), n=0, lineterm=""
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _apply_edits(original: str, edits: list[dict[str, Any]]) -> str | None:
    """Apply ``Edit``/``MultiEdit`` replacements in order.

    Returns ``None`` when any replacement does not match, which is the
    honest answer: we cannot show a proposed result we could not compute,
    and guessing would show the user a diff the agent is not asking for.
    """
    text = original
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if old == "":
            # The CLI's "create/prepend" form. Nothing to match.
            text = new + text
            continue
        if old not in text:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def build_diff_payload(repo_root: Path, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """``DiffPayload`` for a file-mutation call. Synchronous: run in an executor.

    The diff is the feature (``specs5/3-engine/permissions.md`` § The diff
    is the feature), so this never returns "just the tool name": where a
    diff is impossible — a new file, a notebook, a binary, something over
    :data:`DIFF_CEILING_BYTES` — it returns the payload with the labels the
    dialog needs to say why and to show the full proposed content instead.
    """
    key = _WRITE_PATH_KEYS.get(tool_name, "file_path")
    raw_path = tool_input.get(key)
    absolute = _resolve_path(repo_root, raw_path)
    if absolute is None:
        return None

    try:
        display = str(absolute.relative_to(repo_root))
    except ValueError:
        # Outside the repo. Show the absolute path — that it is outside is
        # the most important thing on the dialog.
        display = str(absolute)

    payload: dict[str, Any] = {
        "path": display,
        "is_new_file": False,
        "is_binary": False,
        "too_large": False,
        "original": None,
        "proposed": None,
        "additions": 0,
        "deletions": 0,
    }

    exists = absolute.is_file()
    payload["is_new_file"] = not exists

    original: str | None = None
    if exists:
        try:
            size = absolute.stat().st_size
        except OSError as exc:
            logger.debug("Could not stat %s for the permission diff: %s", absolute, exc)
            size = 0
        if size > DIFF_CEILING_BYTES:
            payload["too_large"] = True
            return payload
        try:
            original = absolute.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            payload["is_binary"] = True
            return payload
        except OSError as exc:
            logger.warning("Could not read %s for the permission diff: %s", absolute, exc)
            return payload

    proposed: str | None
    if tool_name == "Write":
        content = tool_input.get("content")
        proposed = content if isinstance(content, str) else None
    elif tool_name == "Edit":
        proposed = _apply_edits(original or "", [tool_input])
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        proposed = _apply_edits(original or "", edits) if isinstance(edits, list) else None
    elif tool_name == "NotebookEdit":
        # A notebook is JSON on disk and cells in the UI; diffing the raw
        # JSON would show the user noise. Show the new cell source, and let
        # the dialog label it as a notebook cell.
        source = tool_input.get("new_source")
        proposed = source if isinstance(source, str) else None
        original = None
    else:
        proposed = None

    if proposed is not None and len(proposed.encode("utf-8")) > DIFF_CEILING_BYTES:
        payload["too_large"] = True
        return payload

    payload["original"] = original
    payload["proposed"] = proposed
    if proposed is not None:
        payload["additions"], payload["deletions"] = _count_changes(original or "", proposed)
    return payload


# Advisory display hints, derived from the command text. They must never
# gate anything: a heuristic that gated would be either bypassable or
# wrong. Ordered longest-first inside each group so `git push` beats `git`.
_COMMAND_FLAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "deletes",
        ("rm ", "rm -", "rmdir ", "shred ", "unlink ", "git clean", "-delete", "truncate "),
    ),
    (
        "writes",
        (
            ">", ">>", "tee ", "sed -i", "mv ", "cp ", "mkdir ", "touch ", "chmod ",
            "chown ", "ln -s", "git checkout", "git reset", "git commit", "git apply",
            "patch ", "install ",
        ),
    ),
    (
        "network",
        (
            "curl ", "wget ", "ssh ", "scp ", "rsync ", "nc ", "git push", "git pull",
            "git fetch", "git clone", "npm i", "npm install", "npm publish", "pip install",
            "uv add", "uv sync", "cargo publish", "docker push", "docker pull",
        ),
    ),
    ("sudo", ("sudo ", "doas ", "su -")),
)


def command_flags(command: str) -> list[str]:
    """Advisory flags for a shell command. Display only, never a gate."""
    lowered = f" {command.lower()} "
    flags = []
    for label, needles in _COMMAND_FLAGS:
        if any(needle in lowered for needle in needles):
            flags.append(label)
    return flags


def build_command_payload(
    repo_root: Path, tool_name: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    """``CommandPayload`` for an execution call."""
    raw = tool_input.get("command")
    command = raw if isinstance(raw, str) else summarise_tool_input(tool_input)
    truncated = len(command) > COMMAND_DISPLAY_CHARS
    description = tool_input.get("description")
    cwd = tool_input.get("cwd")
    return {
        "command": command[:COMMAND_DISPLAY_CHARS] if truncated else command,
        "truncated": truncated,
        "cwd": str(cwd) if isinstance(cwd, str) and cwd else str(repo_root),
        "description": description if isinstance(description, str) and description else None,
        "flags": command_flags(command),
    }


def plan_headline(plan: Any, limit: int = 120) -> str:
    """The plan's first line of prose, for the header and announcements.

    Leading ``#`` are stripped so a plan that opens with a markdown heading
    reads as a title rather than as syntax.
    """
    if not isinstance(plan, str):
        return ""
    for line in plan.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text if len(text) <= limit else text[: limit - 1] + "…"
    return ""


def build_plan_payload(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """``PlanPayload`` for ``ExitPlanMode``.

    Sent whole and **never truncated**, unlike a command: the plan is the
    artefact the user is being asked to approve, and a plan with its tail
    cut off is a plan approved unread. ``COMMAND_DISPLAY_CHARS`` exists
    because a 40 KB shell command is pathological; a long plan is ordinary.

    ``plan`` is optional in the CLI's own schema — "injected by
    ``normalizeToolInput`` from disk", with ``planFilePath`` naming the file
    it came from (observed in the bundled CLI 2.1.229). So an absent plan is
    a real case, and it returns ``None`` rather than an empty string, which
    the dialog would render as an empty body over an Approve button.
    """
    plan = tool_input.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        return None
    path = tool_input.get("planFilePath") or tool_input.get("plan_file_path")
    return {
        "plan": plan,
        "headline": plan_headline(plan),
        "file_path": path if isinstance(path, str) and path else None,
    }


def build_question_payload(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """``QuestionPayload`` for ``AskUserQuestion``.

    The tool takes a *list* of questions (the SDK's shape, not the
    reference spec's single question), so the first is promoted to the
    top-level fields the dialog renders and the whole list is carried in
    ``questions`` for the rest.
    """
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        text = tool_input.get("question")
        if not isinstance(text, str) or not text:
            return None
        questions = [{"question": text, "options": tool_input.get("options") or []}]

    normalised: list[dict[str, Any]] = []
    for entry in questions:
        if not isinstance(entry, dict):
            continue
        options: list[dict[str, Any]] = []
        for option in entry.get("options") or []:
            if isinstance(option, dict):
                label = option.get("label")
                options.append(
                    {
                        "label": label if isinstance(label, str) else str(label),
                        "description": option.get("description"),
                    }
                )
            elif isinstance(option, str):
                options.append({"label": option, "description": None})
        normalised.append(
            {
                "question": str(entry.get("question") or entry.get("header") or ""),
                "header": entry.get("header"),
                "options": options,
                "multi_select": bool(entry.get("multiSelect") or entry.get("multi_select")),
            }
        )
    if not normalised:
        return None
    first = normalised[0]
    return {
        "question": first["question"],
        "options": first["options"],
        "multi_select": first["multi_select"],
        "questions": normalised,
    }


def _split_selection(chosen: Any) -> tuple[list[int], str]:
    """One question's answer, as ``(option indices, freeform reply)``.

    Two accepted shapes, because the freeform reply arrived after the
    indices did: a bare list is indices alone, and a mapping carries both.
    """
    if isinstance(chosen, dict):
        raw = chosen.get("options")
        reply = chosen.get("text")
    elif isinstance(chosen, list):
        raw, reply = chosen, None
    else:
        return [], ""
    indices = [
        position
        for position in (raw if isinstance(raw, list) else [])
        if isinstance(position, int) and not isinstance(position, bool)
    ]
    return indices, reply.strip() if isinstance(reply, str) else ""


def build_answer_input(
    tool_input: dict[str, Any],
    question: dict[str, Any] | None,
    selections: Any,
) -> dict[str, Any] | None:
    """The ``updated_input`` that carries the user's answers to the CLI.

    ``AskUserQuestion`` reads its answers off its own input: the CLI's
    permission component allows the call with an ``answers`` map merged in,
    keyed by the *question text*, one string per question, multi-select
    joined with ``", "`` — the CLI splits on exactly that separator to
    check the parts back against the option labels.

    Allowing the call without that key is not a neutral act. The tool
    result becomes "The user did not answer the questions", so a dialog
    that collected a selection and then allowed the call plainly would show
    the user an answered question and hand the agent silence.

    ``selections`` is one entry per question, in the order the payload's
    ``questions`` list carries them: either a list of option indices, or a
    mapping ``{"options": [...], "text": "..."}`` when the user typed their
    own reply. A short list, a missing entry or an out-of-range index is
    dropped rather than guessed at: an unanswered question reads to the CLI
    as one the user declined, which is at least true.

    **The freeform reply is an ordinary answer string, not a field of its
    own.** The tool's schema does have a top-level ``response``, but the
    terminal never sets it, and the CLI's result mapping reads it *instead
    of* the answers map ("The user responded: …" pre-empts the per-question
    branch entirely) — so routing "Other" through ``response`` would
    silently discard every option the user had also picked. Verified against
    the bundled CLI 2.1.229; recorded in
    ``specs-reference/3-engine/permissions.md`` § Answering an ``interact``
    request.

    A reply that is not an option label is accepted by the CLI: it fails the
    every-answer-is-a-label check, which switches its own framing to "Read
    the answers carefully — they may request clarification, changes, or that
    you not proceed". That is the correct reading of a freeform answer, so
    the check failing here is the feature rather than a defect.
    """
    questions = (question or {}).get("questions") or []
    if not questions or not isinstance(selections, list):
        return None

    # The key has to be the question text the tool was *called* with, not
    # our normalisation of it: the CLI looks each answer up by that exact
    # string, so a payload whose question text was filled in from `header`
    # would produce an answer nothing reads.
    raw = tool_input.get("questions")
    raw_questions = raw if isinstance(raw, list) else []

    answers: dict[str, str] = {}
    for index, entry in enumerate(questions):
        if index >= len(selections):
            break
        chosen, reply = _split_selection(selections[index])
        options = entry.get("options") or []
        labels = [
            str(options[position].get("label", ""))
            for position in chosen
            if 0 <= position < len(options)
        ]
        # Single-select: the reply *replaces* the labels, because "Other" is
        # one of the choices in a radio group rather than an addition to it.
        # Multi-select: it is one more item in the joined list, which is how
        # the terminal's own Other row behaves alongside checked options.
        if reply:
            parts = [reply] if not entry.get("multi_select") else [*labels, reply]
        else:
            parts = labels
        raw_entry = raw_questions[index] if index < len(raw_questions) else None
        text = raw_entry.get("question") if isinstance(raw_entry, dict) else None
        if not isinstance(text, str) or not text:
            text = entry.get("question")
        if not parts or not isinstance(text, str) or not text:
            continue
        answers[text] = ", ".join(parts)

    if not answers:
        return None
    return {**tool_input, "answers": answers}


# Where an "always allow" grant is written (CC-16).
#
# `localSettings` is `.claude/settings.local.json`, git-ignored, and it is
# where the CLI persists its own approvals (observed against 2.1.229 and
# recorded in specs-reference/3-engine/permissions.md). Defaulting anywhere
# else means the same approval lands in a different file depending on which
# front end the user was in, so "what have I allowed here?" has two answers.
#
# `projectSettings` is `.claude/settings.json`, git-tracked. It is reachable
# only through the separately-tagged menu entry built below, because a grant
# that arrives in someone else's checkout by `git pull` is wider than the
# one call the dialog put on screen — and wider than its label admits.
DEFAULT_RULE_DESTINATION = "localSettings"
SHARED_RULE_DESTINATION = "projectSettings"


def _rule_label(tool_name: str, rule_content: str | None, behavior: str) -> str:
    target = f"({rule_content})" if rule_content else ""
    verb = {"allow": "Always allow", "deny": "Always deny", "ask": "Always ask about"}.get(
        behavior, "Always"
    )
    return f"{verb} {tool_name}{target}"


def _suggested_rule(
    tool_name: str,
    rule_content: str | None,
    *,
    behavior: str = "allow",
    destination: str = DEFAULT_RULE_DESTINATION,
    origin: str = "derived",
    shared: bool = False,
) -> dict[str, Any]:
    return {
        "label": _rule_label(tool_name, rule_content, behavior),
        "tool_name": tool_name,
        "rule_content": rule_content,
        "behavior": behavior,
        "destination": destination,
        "origin": origin,
        # True only for the one entry that writes to the git-tracked file.
        # The dialog needs it to say so; nothing else depends on it.
        "shared": shared,
    }


# Commands whose first token is a subcommand dispatcher: `git status` is a
# meaningfully different grant from `git push`, so the prefix rule keeps two
# tokens. Only used when the CLI offered no suggestion of its own.
_TWO_TOKEN_COMMANDS = frozenset(
    {"git", "npm", "pnpm", "yarn", "uv", "pip", "cargo", "go", "docker", "make", "gh", "poetry"}
)


def _derived_command_rules(command: str) -> list[str]:
    """Rule contents for a shell command, **narrowest first**.

    The first is the literal command, which is what the CLI itself derives
    (observed against 2.1.229: a compound command produced
    ``Bash(echo "---exit:$?---")``, the exact sub-command needing approval).
    It grants precisely what the dialog put on screen, and nothing else.

    The second is a prefix pattern. It is offered as a *choice* rather than
    as the default because it grants more than the user looked at:
    ``git push:*`` covers ``git push --force origin main`` from a click on a
    dialog that said ``git push origin main``. Prefix rules are legitimate
    syntax and are the right thing to write by hand in a settings file —
    they are the wrong thing to put behind the button someone reaches for
    without reading it.

    The command is stripped but otherwise left alone. Collapsing its
    internal whitespace would produce a rule that does not match the
    command it came from, which is the same silent no-op as naming the
    wrong tool in a path rule.
    """
    literal = command.strip()
    tokens = literal.split()
    if not tokens:
        return []
    head = tokens[0]
    if head in _TWO_TOKEN_COMMANDS and len(tokens) > 1 and not tokens[1].startswith("-"):
        prefix = f"{head} {tokens[1]}:*"
    else:
        prefix = f"{head}:*"
    return [literal, prefix]


# Claude Code consults path rules for ``Edit`` and ``Read`` only. A path
# rule written for ``Write``, ``MultiEdit``, ``NotebookEdit`` or ``Glob`` is
# accepted, never consulted, and warned about at startup (CLI v2.1.210+) —
# so "always allow" would write a rule that silently does nothing and the
# user would be asked again on the next call. Mapping to the tool the CLI
# actually checks is the whole point of this table.
#
# Tools absent from it derive no path rule at all: ``Grep`` takes a ``path``
# but is not one of the two names the CLI checks, and a rule we cannot
# predict the effect of is worse than no rule.
_RULE_TOOL_FOR_PATHS = {
    "Write": "Edit",
    "Edit": "Edit",
    "MultiEdit": "Edit",
    "NotebookEdit": "Edit",
    "Read": "Read",
    "Glob": "Read",
    "NotebookRead": "Read",
}

# Characters gitignore treats as pattern syntax. The CLI escapes these in a
# path it turns into a rule, "so the generated rule matches only the literal
# path you approved" — without it, a directory called ``[2024-06] Reports``
# produces a rule that does not match its own path.
_GITIGNORE_META = "\\*?[]"


def _escape_gitignore(path: str) -> str:
    """Escape a literal path for use as a gitignore-style rule pattern."""
    return "".join(f"\\{ch}" if ch in _GITIGNORE_META else ch for ch in path)


def _derived_path_rule(repo_root: Path, raw_path: Any) -> str | None:
    """A rule granting **the one path approved**, and nothing else.

    This mirrors the CLI, which "escapes gitignore pattern characters in
    that path … so the generated rule matches only the literal path you
    approved". It deliberately does not widen to the containing directory.

    An earlier version emitted ``<dir>/**``, which reads like "this
    directory" but is recursive in gitignore syntax — and for a file at the
    repo root it collapsed to ``**``, so approving one root file granted
    writes to every file in the repository. Widening a grant beyond what
    the user looked at is the one error this dialog exists to prevent;
    being too narrow only costs another prompt.

    Returns ``None`` for anything under a ``.claude/`` directory, wherever it
    is (CC-16). A rule granting writes to `.claude/settings.json` is a
    permission to grant permissions: once the agent holds it, it can write
    ``"Bash(*)": "allow"`` into its own gate and this dialog never opens
    again. The call itself stays approvable — once, on purpose, by a human
    reading the diff — but no click here turns it into a standing grant.
    """
    absolute = _resolve_path(repo_root, raw_path)
    if absolute is None:
        return None
    if any(part == ".claude" for part in absolute.parts):
        logger.info(
            "No standing rule derived for %s: paths under .claude/ grant the "
            "power to grant permissions (CC-16)",
            absolute,
        )
        return None
    try:
        relative = absolute.relative_to(repo_root)
    except ValueError:
        # Outside the repo. The leading ``//`` is the CLI's anchor for an
        # absolute filesystem path; a single leading slash means "relative
        # to the settings file", so ``/home/x`` would resolve under the
        # project root and never match.
        return f"//{_escape_gitignore(absolute.as_posix().lstrip('/'))}"
    return _escape_gitignore(relative.as_posix())


def derive_suggested_rules(
    repo_root: Path,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_class: str,
    suggestions: Any,
) -> list[dict[str, Any]]:
    """The "always allow" options, CLI suggestions first.

    ``ToolPermissionContext.suggestions`` carries rules the CLI has already
    normalised with its own semantics — including how it turns a shell
    command into a prefix pattern. Ours are a fallback, marked
    ``origin: "derived"`` so the dialog can say so
    (``specs-reference/3-engine/permissions.md`` § Prefer the CLI's own
    suggestions).
    """
    rules: list[dict[str, Any]] = []
    for suggestion in suggestions or []:
        kind = getattr(suggestion, "type", None)
        if kind != "addRules":
            # setMode and addDirectories are not rules and have no place on a
            # control labelled "always allow this call": switching to
            # acceptEdits stops the dialog appearing for *every* later edit,
            # which is a far larger grant than the one call on screen.
            #
            # A setMode suggestion is not discarded, though — it is offered
            # by its own separately-labelled control, built by
            # ``derive_suggested_mode`` below. Observed against CLI 2.1.229:
            # for an in-repo file edit the CLI's *only* suggestion is
            # `setMode acceptEdits (session)`, so dropping it outright meant
            # never offering what the terminal offers for the same call.
            logger.debug("Not a rule suggestion (%r); not on the rule control", kind)
            continue
        behavior = getattr(suggestion, "behavior", None) or "allow"
        # A destination the CLI named is used as named — including `session`,
        # which it suggests often. The default only covers a suggestion that
        # omitted one.
        destination = getattr(suggestion, "destination", None) or DEFAULT_RULE_DESTINATION
        for rule in getattr(suggestion, "rules", None) or []:
            name = getattr(rule, "tool_name", None)
            if not name:
                continue
            rules.append(
                _suggested_rule(
                    name,
                    getattr(rule, "rule_content", None),
                    behavior=behavior,
                    destination=destination,
                    origin="cli",
                )
            )
    if rules:
        return rules

    if tool_class == "exec" and isinstance(tool_input.get("command"), str):
        for content in _derived_command_rules(tool_input["command"]):
            rules.append(_suggested_rule(tool_name, content))
    elif tool_class in ("write", "read"):
        rule_tool = _RULE_TOOL_FOR_PATHS.get(tool_name)
        key = _WRITE_PATH_KEYS.get(tool_name, "file_path")
        content = _derived_path_rule(repo_root, tool_input.get(key) or tool_input.get("path"))
        if rule_tool and content:
            # Named for the tool the CLI checks, not the tool that asked, so
            # the label states the rule that will actually be written.
            rules.append(_suggested_rule(rule_tool, content))
    # Deliberately nothing for `mcp`, `delegate`, `interact` or `plan`: the
    # only rule we could derive for them is a bare tool grant, and AC-DC never
    # writes one (specs5/3-engine/permissions.md § Decisions). For `plan` a
    # standing grant would be worse than useless — it would approve every
    # future plan sight-unseen, which is the one thing the dialog is for.
    if rules:
        rules.append(_shared_variant(rules[0]))
    return rules


def _shared_variant(rule: dict[str, Any]) -> dict[str, Any]:
    """The same rule, written to the git-tracked file instead (CC-16).

    Offered for the *narrowest* derived rule only, and only as an extra menu
    entry — one more row, not a second row per rule. A team allowlist is a
    real thing to want; making it the same click as a personal grant is what
    CC-16 rules out.

    Never built for a CLI suggestion. The CLI chooses its own destination and
    frequently chooses `session`; turning that into a committed rule would
    invent a persisted grant it declined to ask for.
    """
    return {
        **rule,
        "destination": SHARED_RULE_DESTINATION,
        "shared": True,
    }


# The permission modes a dialog may offer, and the copy that states what
# each one costs. A mode absent from this table is never offered, however
# insistently the CLI suggests it: the control has to say what the user is
# giving up, and copy we have not written is copy we cannot stand behind.
#
# `bypassPermissions` will never be in here. It is the one mode the plan
# forbids reaching by accident, and a dialog button is exactly the accident.
_MODE_OFFERS: dict[str, dict[str, str]] = {
    "acceptEdits": {
        "label": "Accept all edits for the rest of this session",
        "detail": (
            "Every later file edit is applied without asking — you will not "
            "see a diff for it, and this dialog will not open for it. Reads, "
            "shell commands and MCP calls still ask. Lasts until the engine "
            "restarts; nothing is written to a settings file. The permission "
            "mode control in the chat panel shows and undoes it."
        ),
    },
}


def derive_suggested_mode(suggestions: Any) -> dict[str, Any] | None:
    """The mode switch the CLI offered for this call, or ``None``.

    Only ever what the CLI suggested. AC-DC does not invent a mode switch:
    a rule grants one path or one command and can be read back out of a
    settings file, whereas a mode change silences the gate wholesale, and
    that is not a grant to offer on our own initiative.

    Returns at most one — the first recognised suggestion. The CLI sends a
    single ``setMode`` per call, and two mode buttons on one dialog would
    be a worse answer than ignoring the second.
    """
    for suggestion in suggestions or []:
        if getattr(suggestion, "type", None) != "setMode":
            continue
        mode = getattr(suggestion, "mode", None)
        offer = _MODE_OFFERS.get(mode) if isinstance(mode, str) else None
        if offer is None:
            logger.debug("Not offering the CLI's suggested permission mode %r", mode)
            continue
        return {
            "mode": mode,
            # `session` is what the CLI suggests and the only destination
            # that makes sense: a mode persisted to settings would outlive
            # the session that asked for it.
            "destination": getattr(suggestion, "destination", None) or "session",
            "label": offer["label"],
            "detail": offer["detail"],
        }
    return None


def summarise_request(tool_name: str, tool_input: dict[str, Any], tool_class: str) -> str:
    """The dialog's one-line headline."""
    if tool_class == "write":
        key = _WRITE_PATH_KEYS.get(tool_name, "file_path")
        path = tool_input.get(key)
        if isinstance(path, str) and path:
            return f"{tool_name} {path}"
    if tool_class == "exec":
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            single = " ".join(command.split())
            capped = single if len(single) <= 120 else single[:119] + "…"
            return f"{tool_name}: {capped}"
    if tool_class == "plan":
        headline = plan_headline(tool_input.get("plan"))
        if headline:
            return f"{tool_name}: {headline}"
    summary = summarise_tool_input(tool_input)
    return f"{tool_name} {summary}".strip()


# ---------------------------------------------------------------------------
# Deny-read rules (the file picker's third checkbox state)
# ---------------------------------------------------------------------------

# `.claude/settings.local.json` is git-ignored and one of the settings
# sources we enable, so a rule written here applies to the CLI in this repo
# too — which is the honest consequence of sharing settings, and the reason
# the exclusion is per-user rather than a project policy
# (specs-reference/3-engine/permissions.md § The file picker's third
# checkbox state).
LOCAL_SETTINGS_RELATIVE = Path(".claude") / "settings.local.json"

_READ_RULE = re.compile(r"^Read\((?P<target>.*)\)$")


def local_settings_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / LOCAL_SETTINGS_RELATIVE


def _load_local_settings(repo_root: Path | str) -> dict[str, Any]:
    """Parse ``.claude/settings.local.json``, or ``{}`` when there is none.

    Raises ``ValueError`` on malformed JSON rather than starting from
    ``{}``: overwriting a file the user hand-edited badly would delete
    their other rules to fix a typo.
    """
    path = local_settings_path(repo_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not valid JSON ({exc}). Fix it by hand — AC-DC will not "
            f"overwrite a settings file it cannot parse."
        ) from exc
    return data if isinstance(data, dict) else {}


def read_denied_read_files(repo_root: Path | str) -> list[str]:
    """The paths currently carrying a ``Read`` deny rule, in file order."""
    try:
        settings = _load_local_settings(repo_root)
    except ValueError as exc:
        logger.warning("%s", exc)
        return []
    deny = ((settings.get("permissions") or {}).get("deny")) or []
    paths = []
    for entry in deny:
        if not isinstance(entry, str):
            continue
        match = _READ_RULE.match(entry.strip())
        if match:
            target = match.group("target").strip()
            if target:
                paths.append(target)
    return paths


def write_denied_read_files(repo_root: Path | str, paths: list[str]) -> list[str]:
    """Replace the ``Read`` deny rules with exactly ``paths``.

    Every other deny rule and every other settings key is preserved: this
    file is the user's, and the CLI writes to it too.

    Raises
    ------
    ValueError
        The existing file is unparseable, or it could not be written.
    """
    settings = _load_local_settings(repo_root)
    wanted: list[str] = []
    for entry in paths or []:
        if isinstance(entry, str) and entry.strip() and entry.strip() not in wanted:
            wanted.append(entry.strip())

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    existing = permissions.get("deny")
    kept = [
        entry
        for entry in (existing or [])
        if not (isinstance(entry, str) and _READ_RULE.match(entry.strip()))
    ]
    deny = kept + [f"Read({target})" for target in wanted]
    if deny:
        permissions["deny"] = deny
    else:
        permissions.pop("deny", None)
    if permissions:
        settings["permissions"] = permissions
    else:
        settings.pop("permissions", None)

    path = local_settings_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError(f"Could not write {path}: {exc}") from exc
    return wanted


# ---------------------------------------------------------------------------
# The broker
# ---------------------------------------------------------------------------

Broadcast = Callable[[Event], Awaitable[None]]
# Records a prompt against the turn in flight and returns its request ID,
# so the dialog can be attributed to a turn without this module knowing
# anything about turns.
NotePrompt = Callable[[str | None], str | None]
# Tells the caller the session's permission mode has changed underneath it,
# because the CLI applies a mode carried on a permission result without
# announcing it on the message stream. Without this the mode selector keeps
# showing the mode the session started in, which is a lie about what the
# next tool call will do.
NoteMode = Callable[[str], Awaitable[None]]


class PermissionBroker:
    """Turns ``can_use_tool`` calls into browser dialogs and back.

    Parameters
    ----------
    repo_root:
        The session's ``cwd``. Diff paths are reported relative to it and
        it is the default working directory shown for shell calls.
    broadcast:
        ``async (Event) -> None``. Session-wide dispatch to every connected
        browser; the service supplies its own.
    note_prompt:
        ``(tool_use_id) -> request_id | None``. Called once per request so
        the turn's translator can count the prompt and mark the tool card
        as gated. Returns the request ID the dialog belongs to.
    note_mode:
        ``async (mode) -> None``. Called after a decision that switches the
        session's permission mode, so the caller can update the mode it
        reports and tell the browsers. Optional: the mode still reaches the
        CLI without it — what goes missing is everyone else's knowledge of
        it.
    localhost_available:
        ``() -> bool``. Whether a localhost client is connected *now*.
        Only shortens the timeout — the authority check itself is the
        service's, because only it knows who is calling.
    clock:
        Epoch-seconds source, injectable for tests.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        broadcast: Broadcast,
        note_prompt: NotePrompt | None = None,
        note_mode: NoteMode | None = None,
        localhost_available: Callable[[], bool] | None = None,
        decision_timeout: float = DECISION_TIMEOUT,
        no_localhost_timeout: float = NO_LOCALHOST_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._broadcast = broadcast
        self._note_prompt = note_prompt
        self._note_mode = note_mode
        self._localhost_available = localhost_available or (lambda: True)
        self._decision_timeout = decision_timeout
        self._no_localhost_timeout = no_localhost_timeout
        self._clock = clock

        self._pending: dict[str, PendingPermission] = {}
        # permission_id → who resolved it, for the last RESOLUTION_MEMORY
        # requests. A late click then gets attribution instead of "unknown".
        self._resolutions: dict[str, str] = {}
        self._issued: set[str] = set()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        """The dialog queue for a client that connects mid-request.

        Ordered by ``expires_at`` ascending, which is the order the dialog
        presents them in, so "1 of 3" means the same thing on every client.
        """
        return [
            dict(entry.payload)
            for entry in sorted(self._pending.values(), key=lambda p: p.expires_at)
            if not entry.resolved
        ]

    # ------------------------------------------------------------------
    # The callback
    # ------------------------------------------------------------------

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> Any:
        """The SDK's ``can_use_tool``. Never raises; always returns a result.

        Raising here answers the CLI's control request with an error, which
        it reports as a tool failure rather than as a denial — so a bug in
        payload building would look to the user like the tool broke. Every
        failure path below is therefore a deny with a reason instead.
        """
        from claude_agent_sdk import PermissionResultDeny

        tool_input = tool_input if isinstance(tool_input, dict) else {}
        tool_use_id = getattr(context, "tool_use_id", None) or ""
        permission_id = self._new_id()

        request_id: str | None = None
        if self._note_prompt is not None:
            try:
                request_id = self._note_prompt(tool_use_id or None)
            except Exception:
                logger.exception("Could not record the permission prompt on the turn")

        localhost = self._localhost_present()
        timeout = self._decision_timeout if localhost else self._no_localhost_timeout
        expires_at = self._clock() + timeout

        try:
            payload = await self._build_payload(
                permission_id=permission_id,
                request_id=request_id,
                tool_name=tool_name,
                tool_input=tool_input,
                context=context,
                expires_at=expires_at,
                localhost=localhost,
            )
        except Exception:
            logger.exception(
                "Could not build the permission payload for %s; denying", tool_name
            )
            return PermissionResultDeny(
                message=(
                    f"AC-DC could not render a permission dialog for {tool_name}, so "
                    f"the call was denied. This is an AC-DC fault, not a refusal."
                ),
                interrupt=False,
            )

        loop = asyncio.get_running_loop()
        pending = PendingPermission(
            permission_id=permission_id,
            request_id=request_id,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            payload=payload,
            future=loop.create_future(),
            expires_at=expires_at,
            suggested_rules=list(payload.get("suggested_rules") or []),
            suggested_mode=payload.get("suggested_mode"),
        )
        self._pending[permission_id] = pending

        await self._broadcast(Event("permissionRequest", payload, turn_scoped=False))
        logger.info(
            "Permission %s asked for %s (turn %s, localhost=%s, %.0fs)",
            permission_id,
            tool_name,
            request_id,
            localhost,
            timeout,
        )

        try:
            decision = await asyncio.wait_for(pending.future, timeout)
        except asyncio.TimeoutError:
            return await self._deny_on_timeout(pending, localhost, timeout)
        except asyncio.CancelledError:
            # The SDK cancels its in-flight control-request handlers when
            # the client closes. Nothing to broadcast — the transport is
            # going away with us.
            pending.resolved = True
            self._pending.pop(permission_id, None)
            raise
        finally:
            self._pending.pop(permission_id, None)

        return self._to_result(pending, decision)

    async def cancel_all(self, reason: str = DENY_SHUTDOWN_REASON) -> None:
        """Deny everything still pending, so no callback is left waiting.

        Called at teardown. The SDK receives a result for every request it
        raised, which is the invariant that keeps a shutdown from wedging
        the CLI's own exit.
        """
        for pending in list(self._pending.values()):
            if pending.resolved or pending.future.done():
                continue
            pending.resolved = True
            pending.resolved_by = "shutdown"
            pending.future.set_result({"action": "deny", "reason": reason})
            self._remember(pending.permission_id, "shutdown")
            await self._announce(pending, action="shutdown", reason=reason, rule=None)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    async def resolve(
        self,
        permission_id: str,
        decision: Any,
        *,
        resolved_by: str = "localhost",
    ) -> dict[str, Any]:
        """Record a browser's decision. First one wins.

        The localhost check is the caller's — see the module docstring. By
        the time we are here the caller is authorised.
        """
        if not isinstance(permission_id, str) or not permission_id:
            return {"error": "unknown", "reason": "No permission ID was given."}

        pending = self._pending.get(permission_id)
        if pending is None or pending.resolved:
            previous = (
                pending.resolved_by
                if pending is not None and pending.resolved_by
                else self._resolutions.get(permission_id)
            )
            if previous is not None:
                return {"error": "already_resolved", "resolved_by": previous}
            return {
                "error": "unknown",
                "reason": (
                    f"No permission request {permission_id} is pending. It may have "
                    f"timed out, or the turn it belonged to may have ended."
                ),
            }

        action, reason, rule, mode = self._normalise(pending, decision)
        updated_input = decision.get("updated_input") if isinstance(decision, dict) else None
        if not isinstance(updated_input, dict):
            updated_input = None

        if action not in ("deny", "deny_interrupt") and (
            pending.payload.get("tool_class") == "interact"
        ):
            # The dialog sends option indices, not an input patch: the
            # answers belong in the tool's own input and only this side
            # knows the question text they key off. See build_answer_input.
            answered = build_answer_input(
                updated_input
                if updated_input is not None
                else pending.payload.get("input") or {},
                pending.payload.get("question"),
                decision.get("answers") if isinstance(decision, dict) else None,
            )
            if answered is not None:
                updated_input = answered

        pending.resolved = True
        pending.resolved_by = resolved_by
        self._remember(permission_id, resolved_by)
        if not pending.future.done():
            pending.future.set_result(
                {
                    "action": action,
                    "reason": reason,
                    "rule": rule,
                    "mode": mode,
                    "updated_input": updated_input,
                }
            )
        logger.info(
            "Permission %s resolved as %s by %s", permission_id, action, resolved_by
        )
        await self._announce(pending, action=action, reason=reason, rule=rule, mode=mode)
        if mode is not None:
            await self._note_mode_change(mode)
        return {"status": "accepted"}

    async def _note_mode_change(self, mode: dict[str, Any]) -> None:
        """Tell the caller a decision moved the session's permission mode.

        After the announcement, and never in place of it: the dialog has to
        close even if this fails, because the call itself has already been
        answered.
        """
        if self._note_mode is None:
            return
        try:
            await self._note_mode(str(mode.get("mode")))
        except Exception:
            logger.exception(
                "Could not report the permission-mode switch to %r", mode.get("mode")
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _localhost_present(self) -> bool:
        try:
            return bool(self._localhost_available())
        except Exception:
            # Fail closed on the *timeout*, not on the decision: a shorter
            # wait is the conservative answer when we cannot tell.
            logger.warning("Could not determine localhost presence; assuming none")
            return False

    def _new_id(self) -> str:
        """``perm-{epoch_ms}-{6 alphanumerics}``, never reused in a process."""
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        while True:
            suffix = "".join(secrets.choice(alphabet) for _ in range(6))
            candidate = f"perm-{int(self._clock() * 1000)}-{suffix}"
            if candidate not in self._issued:
                self._issued.add(candidate)
                return candidate

    def _remember(self, permission_id: str, resolved_by: str) -> None:
        self._resolutions[permission_id] = resolved_by
        while len(self._resolutions) > RESOLUTION_MEMORY:
            self._resolutions.pop(next(iter(self._resolutions)))

    async def _build_payload(
        self,
        *,
        permission_id: str,
        request_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
        expires_at: float,
        localhost: bool,
    ) -> dict[str, Any]:
        tool_class = classify_tool(tool_name)

        diff = None
        if tool_class == "write":
            # In an executor. The callback itself runs in its own task, so
            # the SDK's read loop is not what is at risk here; a synchronous
            # read of a 2 MiB file blocks the single event loop, and that
            # loop owns the WebSocket that has to deliver this dialog.
            loop = asyncio.get_running_loop()
            diff = await loop.run_in_executor(
                None, build_diff_payload, self._repo_root, tool_name, tool_input
            )

        return {
            "permission_id": permission_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "server": mcp_server_name(tool_name),
            "tool_use_id": getattr(context, "tool_use_id", None) or "",
            "agent_id": getattr(context, "agent_id", None),
            "tool_class": tool_class,
            "gated_by_default": GATED_BY_DEFAULT.get(tool_class, True),
            "input": tool_input,
            "summary": summarise_request(tool_name, tool_input, tool_class),
            "blocked_path": getattr(context, "blocked_path", None),
            "decision_reason": getattr(context, "decision_reason", None),
            # The CLI's own copy for this call, added to
            # ToolPermissionContext in 0.2.137. Preferred over our summary
            # where present, because it is what the terminal would show.
            "title": getattr(context, "title", None),
            "display_name": getattr(context, "display_name", None),
            "description": getattr(context, "description", None),
            "suggested_rules": derive_suggested_rules(
                self._repo_root,
                tool_name,
                tool_input,
                tool_class,
                getattr(context, "suggestions", None),
            ),
            "suggested_mode": derive_suggested_mode(
                getattr(context, "suggestions", None)
            ),
            "diff": diff,
            "command": (
                build_command_payload(self._repo_root, tool_name, tool_input)
                if tool_class == "exec"
                else None
            ),
            "question": (
                build_question_payload(tool_input) if tool_class == "interact" else None
            ),
            "plan": (
                build_plan_payload(tool_input) if tool_class == "plan" else None
            ),
            "expires_at": _iso(expires_at),
            "localhost_available": localhost,
        }

    def _normalise(
        self, pending: PendingPermission, decision: Any
    ) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Coerce a browser decision into ``(action, reason, rule, mode)``.

        A malformed decision becomes a deny with a reason rather than an
        error: the user pressed a button and is entitled to have the turn
        move on, and a deny is the safe reading of an unreadable answer.
        """
        if not isinstance(decision, dict):
            return "deny", DENY_DEFAULT_REASON, None, None

        action = decision.get("action")
        if action not in _DECISION_ACTIONS:
            logger.warning(
                "Permission %s got an unrecognised action %r; treating it as a deny",
                pending.permission_id,
                action,
            )
            return (
                "deny",
                f"AC-DC did not recognise the decision {action!r}, so the call was denied.",
                None,
                None,
            )

        if action in ("deny", "deny_interrupt"):
            reason = decision.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = DENY_DEFAULT_REASON
            return action, reason, None, None

        if action == "allow_mode":
            # The mode comes from the request we built, never from the
            # decision. A client that could name its own mode could name
            # `bypassPermissions` and turn one click on one dialog into a
            # session with no gate at all.
            mode = pending.suggested_mode
            if mode is None:
                logger.warning(
                    "Permission %s asked to switch mode, but no mode was offered "
                    "for it; allowing once",
                    pending.permission_id,
                )
            return action, None, None, mode

        rule: dict[str, Any] | None = None
        if action == "allow_always":
            index = decision.get("rule_index")
            rules = pending.suggested_rules
            if isinstance(index, bool) or not isinstance(index, int):
                index = 0 if rules else None
            if index is not None and 0 <= index < len(rules):
                rule = rules[index]
            else:
                # Allow the call, but say plainly that no rule was written
                # rather than inventing one. `rule_written: null` on the
                # resolved broadcast is what the UI shows.
                logger.warning(
                    "Permission %s asked for always-allow with no usable rule "
                    "(rule_index=%r, %d suggestions); allowing once",
                    pending.permission_id,
                    decision.get("rule_index"),
                    len(rules),
                )
        return action, None, rule, None

    def _to_result(self, pending: PendingPermission, decision: dict[str, Any]) -> Any:
        """Map a recorded decision to the SDK's result type."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        action = decision.get("action")
        if action in ("deny", "deny_interrupt"):
            reason = decision.get("reason") or DENY_DEFAULT_REASON
            return PermissionResultDeny(
                message=reason, interrupt=action == "deny_interrupt"
            )

        updates = None
        rule = decision.get("rule")
        mode = decision.get("mode")
        if rule:
            update = self._build_update(rule)
            if update is not None:
                updates = [update]
        elif mode:
            # The mode rides back on the permission result rather than going
            # out as a separate `set_permission_mode` control request. Two
            # reasons: it is atomic with the allow, so there is no window in
            # which the mode changed but the call did not; and the CLI is
            # waiting on *this* response, so issuing another control request
            # before answering it is a deadlock waiting for a slow user.
            update = self._build_mode_update(mode)
            if update is not None:
                updates = [update]
        return PermissionResultAllow(
            updated_input=decision.get("updated_input"),
            updated_permissions=updates,
        )

    def _build_update(self, rule: dict[str, Any]) -> Any | None:
        """A ``PermissionUpdate`` that writes one scoped rule to settings."""
        from claude_agent_sdk import PermissionUpdate
        from claude_agent_sdk.types import PermissionRuleValue

        tool_name = rule.get("tool_name")
        if not tool_name:
            return None
        try:
            return PermissionUpdate(
                type="addRules",
                rules=[
                    PermissionRuleValue(
                        tool_name=tool_name, rule_content=rule.get("rule_content")
                    )
                ],
                behavior=rule.get("behavior") or "allow",
                destination=rule.get("destination") or DEFAULT_RULE_DESTINATION,
            )
        except Exception:
            logger.exception("Could not build a PermissionUpdate from %r", rule)
            return None

    def _build_mode_update(self, mode: dict[str, Any]) -> Any | None:
        """A ``PermissionUpdate`` that switches the session's mode.

        Re-checks the mode against ``_MODE_OFFERS`` rather than trusting the
        recorded decision. The check is cheap and it is the last point
        before the CLI acts, which is where a guard on a session-wide grant
        belongs.
        """
        from claude_agent_sdk import PermissionUpdate

        name = mode.get("mode")
        if not isinstance(name, str) or name not in _MODE_OFFERS:
            logger.warning("Refusing to switch to the permission mode %r", name)
            return None
        try:
            return PermissionUpdate(
                type="setMode",
                mode=name,
                destination=mode.get("destination") or "session",
            )
        except Exception:
            logger.exception("Could not build a setMode PermissionUpdate from %r", mode)
            return None

    async def _deny_on_timeout(
        self, pending: PendingPermission, localhost: bool, timeout: float
    ) -> Any:
        from claude_agent_sdk import PermissionResultDeny

        template = DENY_TIMEOUT_REASON if localhost else DENY_NO_LOCALHOST_REASON
        reason = template.format(seconds=timeout)
        pending.resolved = True
        pending.resolved_by = "timeout"
        self._remember(pending.permission_id, "timeout")
        logger.warning(
            "Permission %s for %s timed out after %.0fs (localhost=%s); denying",
            pending.permission_id,
            pending.tool_name,
            timeout,
            localhost,
        )
        await self._announce(pending, action="timeout", reason=reason, rule=None)
        return PermissionResultDeny(message=reason, interrupt=False)

    async def _announce(
        self,
        pending: PendingPermission,
        *,
        action: str,
        reason: str | None,
        rule: dict[str, Any] | None,
        mode: dict[str, Any] | None = None,
    ) -> None:
        """Close the dialog on every client, with attribution."""
        try:
            await self._broadcast(
                Event(
                    "permissionResolved",
                    {
                        "permission_id": pending.permission_id,
                        "request_id": pending.request_id,
                        "tool_use_id": pending.tool_use_id,
                        "action": action,
                        "reason": reason,
                        "resolved_by": pending.resolved_by or "unknown",
                        "rule_written": rule,
                        # What the decision changed beyond this one call, so a
                        # second window can say *why* its dialogs stopped
                        # appearing rather than looking broken.
                        "mode_set": (mode or {}).get("mode"),
                    },
                    turn_scoped=False,
                )
            )
        except Exception:
            # A failed broadcast must not stop the decision reaching the
            # SDK; the worst case is a stale dialog on one client, which
            # the next state snapshot clears.
            logger.exception(
                "Could not broadcast the resolution of %s", pending.permission_id
            )
