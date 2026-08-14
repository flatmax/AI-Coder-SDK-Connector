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

TOOL_CLASSES: dict[str, str] = {
    **{name: "read" for name in _READ_TOOLS},
    **{name: "write" for name in _WRITE_TOOLS},
    **{name: "exec" for name in _EXEC_TOOLS},
    **{name: "delegate" for name in _DELEGATE_TOOLS},
    **{name: "interact" for name in _INTERACT_TOOLS},
}

# The CLI's default posture per class, for the dialog's own copy. We do not
# implement gating — see the module docstring.
GATED_BY_DEFAULT: dict[str, bool] = {
    "read": False,
    "write": True,
    "exec": True,
    "delegate": False,
    "interact": True,
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

_DECISION_ACTIONS = frozenset({"allow", "allow_always", "deny", "deny_interrupt"})


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

    ``selections`` is one list of option indices per question, in the order
    the payload's ``questions`` list carries them. A short list, a missing
    entry or an out-of-range index is dropped rather than guessed at: an
    unanswered question reads to the CLI as one the user declined, which is
    at least true.
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
        chosen = selections[index]
        if not isinstance(chosen, list):
            continue
        options = entry.get("options") or []
        labels = [
            str(options[position].get("label", ""))
            for position in chosen
            if isinstance(position, int)
            and not isinstance(position, bool)
            and 0 <= position < len(options)
        ]
        raw_entry = raw_questions[index] if index < len(raw_questions) else None
        text = raw_entry.get("question") if isinstance(raw_entry, dict) else None
        if not isinstance(text, str) or not text:
            text = entry.get("question")
        if not labels or not isinstance(text, str) or not text:
            continue
        answers[text] = ", ".join(labels)

    if not answers:
        return None
    return {**tool_input, "answers": answers}


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
    destination: str = "projectSettings",
    origin: str = "derived",
) -> dict[str, Any]:
    return {
        "label": _rule_label(tool_name, rule_content, behavior),
        "tool_name": tool_name,
        "rule_content": rule_content,
        "behavior": behavior,
        "destination": destination,
        "origin": origin,
    }


# Commands whose first token is a subcommand dispatcher: `git status` is a
# meaningfully different grant from `git push`, so the derived rule keeps two
# tokens. Only used when the CLI offered no suggestion of its own.
_TWO_TOKEN_COMMANDS = frozenset(
    {"git", "npm", "pnpm", "yarn", "uv", "pip", "cargo", "go", "docker", "make", "gh", "poetry"}
)


def _derived_command_rule(command: str) -> str | None:
    tokens = command.strip().split()
    if not tokens:
        return None
    head = tokens[0]
    if head in _TWO_TOKEN_COMMANDS and len(tokens) > 1 and not tokens[1].startswith("-"):
        return f"{head} {tokens[1]}:*"
    return f"{head}:*"


def _derived_path_rule(repo_root: Path, raw_path: Any) -> str | None:
    absolute = _resolve_path(repo_root, raw_path)
    if absolute is None:
        return None
    try:
        relative = absolute.relative_to(repo_root)
    except ValueError:
        # Outside the repo: scope the rule to the directory itself rather
        # than writing an absolute glob that reads like a whole-disk grant.
        return f"{absolute.parent}/**"
    parent = relative.parent
    return "**" if str(parent) == "." else f"{parent.as_posix()}/**"


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
            # setMode / addDirectories are not rules and have no place on a
            # control labelled "always allow this call".
            logger.debug("Ignoring a %r permission suggestion from the CLI", kind)
            continue
        behavior = getattr(suggestion, "behavior", None) or "allow"
        destination = getattr(suggestion, "destination", None) or "projectSettings"
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
        content = _derived_command_rule(tool_input["command"])
        if content:
            rules.append(_suggested_rule(tool_name, content))
    elif tool_class in ("write", "read"):
        key = _WRITE_PATH_KEYS.get(tool_name, "file_path")
        content = _derived_path_rule(repo_root, tool_input.get(key) or tool_input.get("path"))
        if content:
            rules.append(_suggested_rule(tool_name, content))
    # Deliberately nothing for `mcp`, `delegate`, or `interact`: the only
    # rule we could derive for them is a bare tool grant, and AC-DC never
    # writes one (specs5/3-engine/permissions.md § Decisions).
    return rules


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
        localhost_available: Callable[[], bool] | None = None,
        decision_timeout: float = DECISION_TIMEOUT,
        no_localhost_timeout: float = NO_LOCALHOST_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._broadcast = broadcast
        self._note_prompt = note_prompt
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

        action, reason, rule = self._normalise(pending, decision)
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
                    "updated_input": updated_input,
                }
            )
        logger.info(
            "Permission %s resolved as %s by %s", permission_id, action, resolved_by
        )
        await self._announce(pending, action=action, reason=reason, rule=rule)
        return {"status": "accepted"}

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
            "diff": diff,
            "command": (
                build_command_payload(self._repo_root, tool_name, tool_input)
                if tool_class == "exec"
                else None
            ),
            "question": (
                build_question_payload(tool_input) if tool_class == "interact" else None
            ),
            "expires_at": _iso(expires_at),
            "localhost_available": localhost,
        }

    def _normalise(
        self, pending: PendingPermission, decision: Any
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Coerce a browser decision into ``(action, reason, rule)``.

        A malformed decision becomes a deny with a reason rather than an
        error: the user pressed a button and is entitled to have the turn
        move on, and a deny is the safe reading of an unreadable answer.
        """
        if not isinstance(decision, dict):
            return "deny", DENY_DEFAULT_REASON, None

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
            )

        if action in ("deny", "deny_interrupt"):
            reason = decision.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = DENY_DEFAULT_REASON
            return action, reason, None

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
        return action, None, rule

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
        if rule:
            update = self._build_update(rule)
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
                destination=rule.get("destination") or "projectSettings",
            )
        except Exception:
            logger.exception("Could not build a PermissionUpdate from %r", rule)
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
