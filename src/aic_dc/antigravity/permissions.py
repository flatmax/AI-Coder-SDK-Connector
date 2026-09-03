"""The AG-5 permission gate for the Antigravity engine.

The dialog that renders a proposed edit as a diff before the user approves
it is a **requirement** of the second engine, not a feature of it, and an
engine that cannot support it does not ship as master. Phase 2 measured
that Antigravity can: a ``PreToolCallDecideHook`` receives the full
proposed edit and ``HookResult(allow=False)`` leaves the file
byte-identical. This is that measurement turned into the gate.

One ask path, not a second one
==============================
``specs5/3-engine/permissions.md``'s three load-bearing properties — one
ask path, every request resolves exactly once, localhost-only — are
engine-agnostic and are **not re-derived here**. This module owns no
queue, no deadline, no presence check and no broadcast. It calls
:meth:`PermissionBroker.can_use_tool`, the same method the Claude engine
calls, and converts the answer.

That is the whole design, and it is why the class below is small. A second
broker would be a second place a request could be lost, a second countdown
to keep in step with the dialog's, and a second implementation of the
localhost check that decides whether a remote collaborator can approve a
write. The only thing that legitimately differs between the engines is the
*callback's shape*, and that is the two conversions at the bottom of this
file.

What differs, and why it needs a subclass
=========================================
:class:`_AntigravityBroker` overrides exactly one method,
``_build_payload``, because two of the facts the dialog renders are
per-engine:

- **Classification.** ``classify_tool`` knows Claude's names. Asked about
  ``edit_file`` it returns ``exec`` — its deliberate most-cautious
  fallback, which is right as a default and wrong as an answer, because
  the dialog would show a shell-command card for a file edit and no diff
  at all. :data:`TOOL_CLASSES` is the Antigravity table.
- **Argument spelling.** The hook's arguments arrive as free-form JSON
  from the Go side in CamelCase — ``TargetFile``, ``TargetContent``,
  ``ReplacementContent`` — while ``build_diff_payload`` and
  ``build_command_payload`` read Claude's ``file_path`` / ``old_string`` /
  ``new_string`` / ``command``. :func:`normalise_args` is that
  translation, and it is a *field* rename rather than a *tool* rename: the
  payload keeps saying ``edit_file``, because telling the user their agent
  called ``Edit`` would be a lie about which engine is running and the
  kind of engine-name leak AG-R-4 exists to prevent.

Everything else — the queue, the countdown, the broadcast, the
resolution, the deny reasons — is inherited unchanged.

Two shapes on the wire, and this one is the rich one
====================================================
The step stream and the hook carry *different data for the same call*.
The stream's ``edit_file`` is the typed proto sub-message
``{file_path, diff_block}``; the hook's is untyped JSON carrying
``TargetContent`` + ``ReplacementContent`` + a line range, and
``create_file`` carries ``CodeContent``, the whole new file. So the diff
the dialog renders comes from **here**, not from ``steps.py`` — which is
also why this gate does not need to read the file from disk to render one,
though it still does, because the surrounding context is what makes a
hunk readable.

What is genuinely lost
======================
**Rule persistence.** Claude's ``updated_permissions`` has no counterpart
at any layer of Antigravity. "Always allow" must be AIC⚡DC's own store,
consulted by this hook before it opens a dialog, and it is **not built**:
an allow-always decision currently allows the one call and is not
remembered. :meth:`AntigravityPermissionGate.run` says so in its log
rather than silently discarding the user's intent.

What is *not* lost is the amend path: ``HookResult.modified_args`` is
Claude's ``updated_input``, which is the capability AG-5 chose the raw
hook over ``policy.ask_user`` to keep.

Governing spec: ``specs5/plan-ag/`` — AG-5, AG-R-11; and
``specs5/3-engine/permissions.md``, unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aic_dc.antigravity.agy import tools as agy_tools
from aic_dc.antigravity.options import MUTATING_TOOLS
from aic_dc.claude_code.permissions import (
    GATED_BY_DEFAULT,
    PermissionBroker,
    build_command_payload,
    build_diff_payload,
    summarise_request,
)

logger = logging.getLogger(__name__)

#: How Antigravity's builtin tools map onto the dialog's classes.
#:
#: The classes are the existing ones — ``read``, ``write``, ``exec``,
#: ``delegate``, ``interact`` — because they describe what a call *does* to
#: the user's machine, which is not an engine-specific question. Only the
#: names on the left are new.
#:
#: ``generate_image`` is ``write``: it lands bytes in the working tree, and
#: the dialog should say which file. ``start_subagent`` is ``delegate``,
#: matching Claude's ``Task`` — the child's own calls are gated
#: individually as they happen, which is the only reading that works, since
#: a subagent's tool list is not knowable at the moment it is spawned.
TOOL_CLASSES: dict[str, str] = {
    "list_directory": "read",
    "search_directory": "read",
    "find_file": "read",
    "view_file": "read",
    "read_url_content": "read",
    "search_web": "read",
    "finish": "read",
    "create_file": "write",
    "edit_file": "write",
    "generate_image": "write",
    "run_command": "exec",
    "start_subagent": "delegate",
    "ask_question": "interact",
}

#: Antigravity's hook-argument names, in the spelling the existing payload
#: builders read.
#:
#: Mostly measured, and the two exceptions are labelled where they sit.
#: The Go side sends CamelCase through ``PreToolArgs.arguments_json`` and
#: this is the only place that knows it — see ``sdk-surface.md``
#: § *One call, two vocabularies*, which is the phase-4 finding that the
#: hook's spelling and the step stream's are not the same spelling.
#:
#: An entry that stops matching degrades to "no path named, full input
#: shown". That is legible, and it is also quiet: the read tools sat
#: mis-aliased from the day they were written and the dialog looked
#: healthy throughout, because the *mutating* entries were correct and
#: they are the ones that render the diff.
ARG_ALIASES: dict[str, dict[str, str]] = {
    "edit_file": {
        "TargetFile": "file_path",
        "TargetContent": "old_string",
        "ReplacementContent": "new_string",
        "Instruction": "description",
    },
    "create_file": {
        "TargetFile": "file_path",
        "CodeContent": "content",
        "Description": "description",
    },
    "generate_image": {
        "OutputPath": "file_path",
        "Prompt": "description",
    },
    "run_command": {
        "CommandLine": "command",
        "Command": "command",
        "WorkingDir": "cwd",
        "Explanation": "description",
    },
    # Measured off live hook frames in the phase-4 run (2026-09-03). The
    # read tools had been aliased against names the SDK does not send —
    # `view_file` against `TargetFile`, and `find_file` not at all — so the
    # dialog rendered `PATH (none named)` above an input block containing
    # the path. The mutating entries above were right, which is exactly why
    # nobody noticed: the diff rendered, so the dialog looked healthy.
    #
    # `TargetFile` is kept beside `AbsolutePath` rather than replaced. The
    # aliases are additive and cost nothing when absent, and on an SDK at
    # 0.1.x a name that moved once can move back.
    "view_file": {"AbsolutePath": "file_path", "TargetFile": "file_path"},
    # `find_file`'s path is the directory it searches; `Pattern` is the
    # query and is deliberately not aliased to a path field, because the
    # dialog would then name a glob where it promises a file.
    "find_file": {"SearchDirectory": "file_path"},
    # Unmeasured, and marked as such rather than quietly trusted: no live
    # frame for either has been read, and the phase-4 finding was precisely
    # that the step stream's spelling is not the hook's. They degrade to
    # "no path named, full input shown", which is legible.
    "list_directory": {"DirectoryPath": "file_path"},
    "search_directory": {"SearchDirectory": "file_path"},
}

#: The tools this gate must never allow without asking.
#:
#: The same set ``options.MUTATING_TOOLS`` enables, read from there rather
#: than restated, so the seam cannot drift between the module that turns
#: the tools on and the module that gates them. AG-R-11 is why
#: ``run_command`` is in it: a denied ``edit_file`` came back as ``sed -i``
#: on both probe runs, so a gate that covered only the file tools would
#: produce a manufactured record of consent.
ALWAYS_ASK = MUTATING_TOOLS | agy_tools.MUTATING_TOOLS

# --- The `agy` transport's vocabulary, merged rather than kept beside ---
#
# AG-14 adds a second transport that reaches the *same* Antigravity through
# the CLI, and the two products agree on argument names while disagreeing on
# tool names — `replace_file_content` rather than `edit_file`. The names do
# not collide, so one table can hold both vocabularies, and one table cannot
# disagree with itself; two would be the copy that drifts. Same reasoning as
# `ALWAYS_ASK is MUTATING_TOOLS` above, which is why that seam is widened
# here rather than duplicated.
#
# Merged *after* the SDK entries and with `setdefault` semantics in mind:
# where a name is shared (`run_command`, `view_file`, `generate_image`) the
# two agree, and an SDK entry must win any future disagreement, because the
# SDK path is the one with an enforcing gate.
for _name, _cls in agy_tools.TOOL_CLASSES.items():
    TOOL_CLASSES.setdefault(_name, _cls)
for _name, _aliases in agy_tools.ARG_ALIASES.items():
    ARG_ALIASES.setdefault(_name, _aliases)


def normalise_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Antigravity's hook arguments in the field names the dialog reads.

    Aliases are *added* rather than substituted, and the original keys are
    kept. Two reasons, both learned from the shape of the data:

    - The dialog renders the raw ``input`` dict beside the diff, and a
      user looking at what their agent actually asked for should see the
      engine's own words, not a translation of them.
    - This is an alpha SDK whose argument names are not a documented
      contract. An alias that stops matching degrades to "no diff, full
      input shown", which is legible; a substitution that drops the
      original would degrade to an empty dialog.
    """
    aliases = ARG_ALIASES.get(tool_name)
    if not aliases:
        return dict(args)
    merged = dict(args)
    for source, target in aliases.items():
        if source in args and target not in merged:
            merged[target] = args[source]
    return merged


def denormalise_args(tool_name: str, amended: dict[str, Any]) -> dict[str, Any]:
    """The reverse, for ``modified_args`` on the way back to the harness.

    The amend path is the capability AG-5 chose the raw hook to keep, and
    it only works if what goes back is spelled the way the Go side reads
    it. Only the aliased keys are translated back; anything the dialog
    added under a name this module does not know is passed through, on the
    same reasoning as :func:`normalise_args` — an unknown key is more
    likely to be a newer SDK's than a mistake.
    """
    aliases = ARG_ALIASES.get(tool_name)
    if not aliases:
        return dict(amended)
    # First alias wins, which is why this is a loop and not a
    # comprehension. Two source names can share a target — `CommandLine`
    # and `Command` both mean `command`, `AbsolutePath` and `TargetFile`
    # both mean `file_path` — and a comprehension keeps the *last*, so an
    # amended command went back as `Command` while the engine sends and
    # reads `CommandLine`. `overwrite`/`modified_args` is a merge, so an
    # unrecognised key lands *beside* the real one and leaves it in place:
    # the amend silently does nothing and the original command runs. The
    # aliases are ordered with the engine's own spelling first for exactly
    # this reason, and `setdefault` is what honours that order.
    reverse: dict[str, str] = {}
    for source, target in aliases.items():
        reverse.setdefault(target, source)
    out: dict[str, Any] = {}
    for key, value in amended.items():
        out[reverse.get(key, key)] = value
    return out


class _AntigravityBroker(PermissionBroker):
    """The shared broker, with the two per-engine facts overridden.

    Subclassed rather than parameterised because this is the only engine
    that needs it and a constructor argument for a payload builder would
    be a seam with one caller. If a third engine ever arrives, that is the
    moment to lift the table lookup into a strategy — not before.
    """

    async def _build_payload(
        self,
        *,
        permission_id: str,
        request_id: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
        expires_at: float | None,
        localhost: bool,
    ) -> dict[str, Any]:
        import asyncio

        from aic_dc.claude_code.permissions import _iso_or_none

        tool_class = TOOL_CLASSES.get(tool_name, "exec")
        normalised = normalise_args(tool_name, tool_input)

        diff = None
        if tool_class == "write":
            # In an executor, for the same reason the Claude broker does
            # it: a synchronous read of a large file blocks the single
            # event loop, and that loop owns the WebSocket this dialog has
            # to travel down.
            loop = asyncio.get_running_loop()
            diff = await loop.run_in_executor(
                None,
                build_diff_payload,
                self._repo_root,
                _diff_tool_for(tool_name),
                normalised,
            )

        return {
            "permission_id": permission_id,
            "request_id": request_id,
            # The engine's own tool name, not a Claude equivalent. The
            # dialog reports what was actually called.
            "tool_name": tool_name,
            "server": None,
            "tool_use_id": getattr(context, "tool_use_id", None) or "",
            "agent_id": getattr(context, "agent_id", None),
            "tool_class": tool_class,
            # A mutating tool is gated whatever its class says. The class
            # shapes the dialog's wording; this decides whether the dialog
            # can be presented as routine, and the two must not be able to
            # disagree. Belt and braces today — every class in ALWAYS_ASK
            # already maps to True — and the thing that holds if somebody
            # later reclassifies one to make a demo smoother.
            "gated_by_default": (
                True
                if tool_name in ALWAYS_ASK
                else GATED_BY_DEFAULT.get(tool_class, True)
            ),
            "input": normalised,
            "summary": summarise_request(tool_name, normalised, tool_class),
            "blocked_path": None,
            "decision_reason": None,
            "title": None,
            "display_name": None,
            "description": normalised.get("description") or None,
            # Empty rather than absent. Antigravity has no
            # `updated_permissions` at any layer, so there is no rule for
            # the dialog to offer to persist — and an offer it cannot keep
            # is worse than no offer. See this module's docstring.
            "suggested_rules": [],
            "suggested_mode": None,
            "diff": diff,
            "command": (
                build_command_payload(self._repo_root, tool_name, normalised)
                if tool_class == "exec"
                else None
            ),
            "question": None,
            "plan": None,
            "expires_at": _iso_or_none(expires_at),
            "localhost_available": localhost,
        }


def _diff_tool_for(tool_name: str) -> str:
    """The name ``build_diff_payload`` should reason about this call as.

    The one place a Claude tool name is deliberately borrowed, and it does
    not reach the user: ``build_diff_payload`` switches on the name to
    decide whether it is looking at a whole-file replacement or a
    string-for-string edit, and reproducing that logic here would be a
    second implementation of the diff the product is built around.

    ``edit_file`` carries old and new text, which is Claude's ``Edit``.
    ``create_file`` and ``generate_image`` carry whole content or no
    content, which is ``Write``.

    ``agy``'s names are answered from :data:`agy_tools.DIFF_SHAPE`, because
    that transport calls the same operations something else. Getting this
    wrong is the quiet failure ``agy/tools.py`` documents: the gate still
    holds, and the dialog renders no diff.
    """
    shape = agy_tools.DIFF_SHAPE.get(tool_name)
    if shape is not None:
        return shape
    return "Edit" if tool_name == "edit_file" else "Write"


class AntigravityPermissionGate:
    """A ``PreToolCallDecideHook`` that asks the user.

    Constructed with a repo root and the same callbacks the Claude
    session gives its broker, so the dialog, the queue and the localhost
    rule are literally the same ones.

    Not registered anywhere yet: ``options.build_config_kwargs`` takes a
    ``decide_hook`` and :meth:`as_hook` is what will be passed to it, but
    nothing constructs one outside tests until the engine router exists.

    **Pass :meth:`as_hook`, not this object.** The SDK's ``HookRunner``
    registers by ``isinstance`` against ``PreToolCallDecideHook``
    (``hooks/hook_runner.py:148-153``) and raises ``ValueError`` on
    anything else, so the thing handed to the config has to be a real
    subclass. Keeping the logic here and the subclass in a factory is what
    lets this module — and its tests — import on a machine with no
    ``google-antigravity`` wheel, which is the rule the whole package
    holds to because the SDK ships a bundled 119 MB binary (AG-R-10).
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        broadcast: Any,
        note_prompt: Any = None,
        localhost_available: Any = None,
        denied_reads: Any = None,
        **broker_kwargs: Any,
    ) -> None:
        self._repo_root = Path(repo_root)
        # A callable, not a list. The user toggles these from the file tree
        # mid-session, and a snapshot taken when the gate was built would
        # go stale at the first shift-click.
        self._denied_reads = denied_reads or (lambda: [])
        self.broker = _AntigravityBroker(
            repo_root,
            broadcast=broadcast,
            note_prompt=note_prompt,
            localhost_available=localhost_available,
            **broker_kwargs,
        )

    def _denied_read_target(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """The denied path this read would touch, or ``None``.

        The user's shift-click on the file tree is a **deny**, not an ask,
        so this answers a different question from the dialog's: matching
        means the call is refused with a reason, never presented.

        Prefix matching rather than equality, so denying a directory
        denies the files under it — which is what shift-clicking a folder
        in the tree means. Paths are resolved first because the hook is
        handed absolute paths while the tree records repo-relative ones.
        """
        raw = args.get("file_path")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            target = Path(raw)
            if not target.is_absolute():
                target = self._repo_root / target
            target = target.resolve()
        except (OSError, ValueError):  # noqa: BLE001 - a probe, not a control path
            return None
        for entry in self._denied_reads() or []:
            if not isinstance(entry, str) or not entry.strip():
                continue
            try:
                denied = Path(entry.strip())
                if not denied.is_absolute():
                    denied = self._repo_root / denied
                denied = denied.resolve()
            except (OSError, ValueError):  # noqa: BLE001
                continue
            if target == denied or denied in target.parents:
                return entry.strip()
        return None

    def as_hook(self) -> Any:
        """This gate as a real ``PreToolCallDecideHook``, for the config.

        The subclass is built here rather than at module scope because
        defining it needs the SDK imported, and this module must stay
        importable without it. Delegation rather than inheritance so that
        every test in ``tests/test_antigravity_permissions.py`` can drive
        the gate directly, offline, and only the registration path needs a
        wheel.
        """
        from google.antigravity.hooks.hooks import PreToolCallDecideHook

        gate = self

        class _Gate(PreToolCallDecideHook):
            async def run(self, context: Any, data: Any) -> Any:
                return await gate.run(context, data)

        return _Gate()

    async def run(self, context: Any, data: Any) -> Any:
        """The hook. Never raises; always returns a decision.

        Raising here would reach the harness as a hook failure rather than
        as a denial, which the model reads as "the tool broke" — the same
        reasoning as the Claude gate's, and the same resolution: every
        failure path is a deny with a reason the model can act on.

        **A tool this gate does not recognise is asked about, not
        allowed.** That is the direction that survives an SDK release
        adding a tool: an unknown name classifies as ``exec`` and gets the
        most cautious dialog, where the alternative would ungate whatever
        arrives next.

        **The read class is answered here rather than by the broker**, and
        the phase-4 live run is why. A ``PreToolCallDecideHook`` fires for
        *every* call, so forwarding all of them produced a modal for
        ``find_file`` and two more for ``view_file`` in a turn whose only
        mutation was one edit — four dialogs, three of them for reads. On
        Claude the CLI decides which calls need ``can_use_tool`` at all and
        allows reads itself, so the shared broker never sees them; sharing
        a broker does not by itself give two engines one behaviour, because
        half of Claude's behaviour lives in the CLI rather than in the
        broker. The harness agrees, incidentally — its own log reads
        ``permissions: skipping check for step 2: handler *handlers.FindHandler
        does not declare permissions``.

        It also made the dialog lie. It explains a gated read with *"read
        calls are not normally gated. This one is: a deny or ask rule
        matched"* — true on Claude, and on this engine a sentence about an
        exception that was in fact the rule.

        The narrowing is not a blanket allow. ``ALWAYS_ASK`` wins over the
        class, so a tool that is both mutating and somehow classed ``read``
        is still asked about, and a **denied** read is refused outright —
        that is the user's own shift-click on the file tree, and it must
        not be softened into an auto-allow by the very change that stops
        the asking.
        """
        from google.antigravity.types import HookResult

        tool_name = str(getattr(data, "name", "") or "")
        args = dict(getattr(data, "args", None) or {})

        if tool_name not in ALWAYS_ASK and not GATED_BY_DEFAULT.get(
            TOOL_CLASSES.get(tool_name, "exec"), True
        ):
            normalised = normalise_args(tool_name, args)
            denied = self._denied_read_target(tool_name, normalised)
            if denied is not None:
                logger.info(
                    "Antigravity %s refused: %s is denied to the agent",
                    tool_name,
                    denied,
                )
                return HookResult(
                    allow=False,
                    message=(
                        f"The user has denied the agent read access to "
                        f"{denied}. Do not try to read it by another route; "
                        f"ask them for what you need from it instead."
                    ),
                )
            return HookResult(allow=True)

        try:
            result = await self.broker.can_use_tool(
                tool_name, args, _HookContext(data, context)
            )
        except Exception:
            logger.exception("The Antigravity permission gate failed for %s", tool_name)
            return HookResult(
                allow=False,
                message=(
                    f"AIC-DC could not render a permission dialog for "
                    f"{tool_name}, so the call was denied. This is an AIC-DC "
                    f"fault, not a refusal by the user."
                ),
            )
        return self._to_hook_result(tool_name, result)

    def _to_hook_result(self, tool_name: str, result: Any) -> Any:
        """A Claude ``PermissionResult`` as an Antigravity ``HookResult``.

        Duck-typed on the two attributes that distinguish them rather than
        on ``isinstance``. That is deliberate: ``can_use_tool`` returns a
        Claude result object from three separate places — the ordinary
        decision, the payload-failure deny, and the no-localhost expiry —
        and a check that named the classes would have to be kept in step
        with all three. Reading the attributes works for every one of them
        and for whatever a fourth would return.
        """
        from google.antigravity.types import HookResult

        message = getattr(result, "message", None)
        if message is not None:
            # Only a deny carries a message. `interrupt` has no Antigravity
            # counterpart — there is no "stop the turn as well as this
            # call" — so it is dropped, and the turn ends by the model
            # reading the denial rather than by the harness being halted.
            return HookResult(allow=False, message=str(message))

        updates = getattr(result, "updated_permissions", None)
        if updates:
            # AG-5's one genuine loss, made visible rather than silent.
            # The user asked for a rule; there is nowhere to put it, so
            # they will be asked again next time.
            logger.warning(
                "Antigravity has no updated_permissions; the 'always allow' "
                "for %s applies to this call only. AIC-DC would have to own "
                "the rule store (AG-5).",
                tool_name,
            )

        amended = getattr(result, "updated_input", None)
        if isinstance(amended, dict) and amended:
            return HookResult(
                allow=True, modified_args=denormalise_args(tool_name, amended)
            )
        return HookResult(allow=True)


class _HookContext:
    """The attributes the shared broker reads off a Claude tool context.

    A translation object rather than a dict because the broker reaches for
    these with ``getattr(context, ...)`` and defaults, so anything absent
    degrades to ``None`` on its own. Only the two that exist on the
    Antigravity side carry a value; the rest are Claude's own suggestions
    machinery, which has no counterpart and must read as absent rather
    than as empty.
    """

    def __init__(self, call: Any, hook_context: Any) -> None:
        self.tool_use_id = getattr(call, "id", None) or ""
        self.agent_id = getattr(hook_context, "trajectory_id", None) or None
        self.suggestions = None
        self.blocked_path = None
        self.decision_reason = None
        self.title = None
        self.display_name = None
        self.description = None
