"""Standing "always allow" rules, which on this engine are ours to keep.

Governing spec: ``specs5/plan-ag/decisions.md`` AG-15.

Why this module exists
======================
The permission dialog on Antigravity offered ``Allow once`` and ``Deny``
and nothing else, because ``permissions._build_payload`` sent
``suggested_rules: []``. The reasoning recorded there was right about the
engine and stopped one step short of the conclusion: Antigravity has no
``updated_permissions`` at any layer, so there is nothing to write a rule
*into* — and therefore AIC⚡DC keeps the rule itself.

One store, both transports
==========================
Keyed by repository, shared by the SDK transport and by ``agy``. A rule the
user set while on the subscription that stopped applying when they switched
to the API key would be an engine-name distinction leaking into behaviour,
which is [AG-R-4](../../../specs5/plan-ag/risks.md#ag-r-4). They are one
engine reached two ways ([AG-3](../../../specs5/plan-ag/decisions.md#ag-3)).

Stored under AIC⚡DC's own config directory rather than in the repository or
in ``~/.gemini/``. Not the repository, because a standing permission grant
is a fact about *this user on this machine* and committing one would grant
it to everyone who pulls. Not ``~/.gemini/``, because that belongs to
Google's products and :mod:`aic_dc.agy.install` is careful about writing
there for the hook; rules deserve the same care.

Matching is exact, and that is the whole safety property
========================================================
**The only bug this feature can have is an ungated write**, so every
decision here is the narrow one:

- A **command** rule matches the identical command, or — for the prefix
  form the dialog offers as a second, clearly-labelled choice — a command
  whose first tokens are followed by a space or nothing. ``git push:*``
  matches ``git push`` and ``git push origin main``; it does not match
  ``git pushover``.
- A **path** rule matches one resolved absolute path. It never widens to a
  directory. A rule for ``src/a.py`` does not match ``src/b.py`` and
  certainly not ``src/``.

The matching data is carried on the rule dict itself, under
:data:`MATCH_KEY`, at the moment the rule is derived — when the resolved
path and the parsed command are already in hand. The alternative is to
re-derive them from ``rule_content`` at match time, which means unescaping
gitignore metacharacters and re-parsing a prefix pattern in a code path
whose failure mode is granting more than was clicked. The dialog echoes the
rule dict back verbatim, so the extra key survives the round trip for free.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the matching data rides on a rule dict. Namespaced because the rest
#: of the dict is the Claude dialog's shape and this part is not.
MATCH_KEY = "aic_dc_match"

#: The rule destination this engine writes to. Not one of the CLI's
#: settings files — those are Claude's and nothing here writes them.
DESTINATION = "aicDcRules"

#: Filename under the config directory. One file, all repositories, keyed by
#: repo root inside: a file per repo would mean deciding how to name it from
#: a path, and a hash is unreadable while the path itself is not a filename.
STORE_NAME = "antigravity-rules.json"


def store_path(config_dir: Path | str | None = None) -> Path:
    """Where the rules live.

    ``config_dir`` is taken rather than resolved, for the same reason
    :func:`aic_dc.agy.registry.registry_dir` takes it: a test that fell
    through to the default would write standing permission grants into the
    developer's own configuration. That is not hypothetical — the first
    version of AG-15's tests did exactly that, leaving three entries keyed
    by ``pytest`` temp directories in the real store, and it was noticed
    only because the file was checked by hand.
    """
    base = Path(config_dir) if config_dir else Path.home() / ".config" / "aic-dc"
    return base / STORE_NAME


def command_match(command: str, *, prefix: bool) -> dict[str, Any] | None:
    """Matching data for a shell-command rule."""
    literal = (command or "").strip()
    if not literal:
        return None
    return {"kind": "command_prefix" if prefix else "command", "value": literal}


def path_match(absolute: Path | str, tool_name: str) -> dict[str, Any]:
    """Matching data for a path rule: one resolved path, one tool name.

    **Keyed on the tool name rather than the class, and that is the
    conservative choice rather than the convenient one.** Both transports
    have two tools that write a file — ``edit_file``/``create_file`` on the
    SDK, ``replace_file_content``/``write_to_file`` on ``agy`` — so matching
    by class would let a rule the user granted by reading a *diff* also
    permit a whole-file overwrite they never saw. The dialog's label names
    one tool; the grant is that tool.

    The cost is a second prompt the first time the agent reaches for the
    other tool on the same file. That is the right direction to be wrong
    in: being too narrow costs a click, being too wide is an unreviewed
    write.
    """
    return {"kind": "path", "value": str(absolute), "tool_name": tool_name}


def _matches(entry: dict[str, Any], *, command: str | None, path: str | None,
             tool_name: str) -> bool:
    """Whether one stored rule permits this call. Narrow on every path."""
    match = entry.get(MATCH_KEY)
    if not isinstance(match, dict):
        return False
    kind = match.get("kind")
    value = match.get("value")
    if not isinstance(value, str) or not value:
        return False

    if kind == "command":
        return command is not None and command.strip() == value
    if kind == "command_prefix":
        if command is None:
            return False
        candidate = command.strip()
        # `value` is the prefix without the trailing `:*` — see
        # `derive_rules`. Equal, or followed by a separator: `git push` must
        # not match `git pushover`, which is the widening this whole module
        # is written to avoid.
        return candidate == value or candidate.startswith(value + " ")
    if kind == "path":
        if path is None:
            return False
        if match.get("tool_name") != tool_name:
            return False
        return path == value
    return False


class RuleStore:
    """The standing rules for one repository, persisted.

    Loaded on construction and re-read on every :meth:`allows` call would be
    wasteful; re-read on nothing at all would mean a rule added by another
    window never takes effect. The compromise is to re-read when the file's
    mtime has moved, which costs one ``stat`` per permission decision — a
    decision that is already about to block on a human.
    """

    def __init__(
        self, repo_root: Path | str, *, config_dir: Path | str | None = None
    ) -> None:
        self._repo = str(Path(repo_root).resolve())
        self._path = store_path(config_dir)
        self._rules: list[dict[str, Any]] = []
        self._mtime: float | None = None
        self._reload()

    @property
    def path(self) -> Path:
        return self._path

    def _reload(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._rules = []
            self._mtime = None
            return
        if self._mtime is not None and stat.st_mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Unreadable reads as "no rules", which costs a prompt. The
            # opposite — treating a corrupt file as a grant — is the failure
            # this module must never have.
            logger.warning("Could not read the Antigravity rule store at %s", self._path)
            self._rules = []
            self._mtime = stat.st_mtime
            return
        self._mtime = stat.st_mtime
        entries = data.get(self._repo) if isinstance(data, dict) else None
        self._rules = [e for e in entries or [] if isinstance(e, dict)]

    def rules(self) -> list[dict[str, Any]]:
        self._reload()
        return list(self._rules)

    def allows(
        self, *, command: str | None, path: str | None, tool_name: str
    ) -> dict[str, Any] | None:
        """The rule permitting this call, or ``None`` to ask the user."""
        self._reload()
        for entry in self._rules:
            if entry.get("behavior", "allow") != "allow":
                continue
            if _matches(entry, command=command, path=path, tool_name=tool_name):
                return entry
        return None

    def add(self, rule: dict[str, Any]) -> bool:
        """Persist one rule. Returns whether it was written.

        A rule with no matching data is **refused rather than stored**: it
        could never permit anything, so keeping it would fill the store with
        entries that look like grants and are not, and the user would be
        asked again while believing they had said always.
        """
        if not isinstance(rule, dict) or not isinstance(rule.get(MATCH_KEY), dict):
            logger.warning("Not storing an Antigravity rule with no matching data")
            return False
        self._reload()
        if any(
            e.get(MATCH_KEY) == rule.get(MATCH_KEY)
            and e.get("behavior") == rule.get("behavior")
            for e in self._rules
        ):
            return True
        try:
            data: Any = {}
            if self._path.exists():
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault(self._repo, [])
            if not isinstance(data[self._repo], list):
                data[self._repo] = []
            data[self._repo].append(rule)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Whole-file write then rename: another window may be reading
            # this at any instant, and a half-written file parses as no
            # rules, which would silently drop every grant the user has.
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception("Could not write the Antigravity rule store")
            return False
        self._mtime = None
        self._reload()
        return True


def _rule(
    tool_name: str,
    rule_content: str | None,
    match: dict[str, Any],
    *,
    behavior: str = "allow",
) -> dict[str, Any]:
    """One suggestion, in the dialog's shape plus our matching data.

    The shape is the Claude dialog's because the dialog is shared — label,
    tool_name, rule_content, behavior, destination, origin, shared. Only
    `destination` differs in meaning: nothing writes a settings file here,
    so it names our own store.
    """
    target = f"({rule_content})" if rule_content else ""
    verb = "Always allow" if behavior == "allow" else "Always deny"
    return {
        "label": f"{verb} {tool_name}{target}",
        "tool_name": tool_name,
        "rule_content": rule_content,
        "behavior": behavior,
        "destination": DESTINATION,
        "origin": "derived",
        # Never. There is no git-tracked file to write, so no rule here can
        # be shared with anyone who pulls the repository.
        "shared": False,
        MATCH_KEY: match,
    }


#: Directories no standing rule may ever be derived for. The CC-16 argument,
#: carried across: a rule granting writes under one of these is a permission
#: to grant permissions, because the agent could then edit the gate that is
#: asking. `.claude` and `.gemini` are the two engines' own configuration;
#: `.aic-dc` is ours if a repo ever grows one. The call itself stays
#: approvable — once, by a human reading the diff — but no click turns it
#: into a standing grant.
_NEVER_STANDING = frozenset({".claude", ".gemini", ".aic-dc"})


def derive_rules(
    repo_root: Path | str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_class: str,
) -> list[dict[str, Any]]:
    """The "always allow" options for one call, narrowest first.

    Deliberately **not** ``claude_code.permissions.derive_suggested_rules``,
    though it reuses that module's two safety-critical derivations. That
    function's path branch is keyed on ``_RULE_TOOL_FOR_PATHS``, a table of
    *Claude* tool names mapping to the tool the *Claude CLI* consults —
    both halves meaningless here, where the store is ours and the tools are
    called ``replace_file_content`` and ``edit_file``. Feeding it an
    Antigravity name yields no rule at all, which is the quiet failure this
    engine has already produced twice: the control would simply never appear
    for file edits and nobody would see an error.

    What *is* reused is `_derived_command_rules` and `_derived_path_rule` —
    the prefix-splitting and the gitignore escaping. Those encode decisions
    that took a CLI-behaviour investigation to get right, and a second copy
    would drift in the direction of granting more than was clicked.
    """
    from aic_dc.claude_code.permissions import (
        _derived_command_rules,
        _derived_path_rule,
        _resolve_path,
    )

    root = Path(repo_root)
    rules: list[dict[str, Any]] = []

    if tool_class == "exec":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            contents = _derived_command_rules(command)
            for index, content in enumerate(contents):
                # `_derived_command_rules` returns [literal, "<prefix>:*"].
                # The prefix is stored without its `:*` suffix because that
                # is pattern syntax for the *label*, not something to match
                # against — see `_matches`.
                if index == 0:
                    match = command_match(content, prefix=False)
                else:
                    match = command_match(content[:-2], prefix=True)
                if match is not None:
                    rules.append(_rule(tool_name, content, match))
        return rules

    if tool_class in ("read", "write"):
        raw = tool_input.get("file_path") or tool_input.get("path")
        absolute = _resolve_path(root, raw)
        if absolute is None:
            return rules
        if any(part in _NEVER_STANDING for part in absolute.parts):
            logger.info(
                "No standing rule derived for %s: paths under %s grant the "
                "power to grant permissions",
                absolute,
                "/".join(sorted(_NEVER_STANDING)),
            )
            return rules
        content = _derived_path_rule(root, raw)
        if content:
            rules.append(
                _rule(tool_name, content, path_match(absolute, tool_name))
            )
    # Nothing for `delegate` or `interact`: a standing grant to spawn
    # subagents or answer the agent's questions is not a grant the dialog
    # can describe in one line, and `start_subagent` is in the write seam
    # precisely because its child's calls are not knowable here.
    return rules
