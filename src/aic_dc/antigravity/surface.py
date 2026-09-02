"""What the installed Antigravity SDK offers, and what AIC⚡DC reaches for.

``specs5/plan-ag/sdk-surface.md`` is a hand-written snapshot of both
Antigravity products read out of the wheel and measured against live
turns. It was accurate the day it was written and nothing enforces it
since — which is the failure mode this module exists to close, and it
bites harder here than it does for Claude Code. That probe was written
against a ``claude-agent-sdk`` that was 0.2.137 and stable. This one is
written against **0.1.15 and alpha**, on a release cadence of roughly one
per day, with no stability commitment for anything in it.

So this probe reflects over the *installed* SDK and diffs it against what
this package handles, producing three answers per surface:

- **handled** — we set it, register it, or dispatch on it.
- **declined** — we deliberately do not, with the reason next to the name.
- **pending** — real surface nobody has triaged yet.

The point of the third bucket is that it must be *empty by declaration*,
not empty by accident. :mod:`tests.test_antigravity_surface` fails when
the SDK exposes something absent from all three, so an SDK bump that adds
a step type or a builtin tool is a red test with the name in it rather
than a discovery months later.

Why almost nothing is ``handled`` yet
-------------------------------------
``specs5/plan-ag/decisions.md`` AG-8 lands this module in **phase 1**,
alongside the consultant and *before* any engine work, precisely so the
engine is not written against a snapshot that has already moved. At the
time of writing there is no session, no options assembly and no step
pump — only :mod:`~aic_dc.antigravity.credentials`, which is why the four
credential fields are the only handled rows. Everything else is declined
or pending, and the gate is green in that state: **it fails on untriaged,
never on unimplemented.** A gate that failed on unbuilt surface would
earn an ignore-list within a week.

Coverage becomes derived as the phases land. The ``handled`` bucket is
read out of this package's own syntax trees — the config keys it passes,
the hook classes it subclasses, the enum members it names — so phase 3
setting ``workspaces`` moves that row without anyone editing a table
here.

What this cannot do, and why ``agy`` is probed too
--------------------------------------------------
Reflection sees **shape**. Every correction in ``sdk-surface.md`` that
actually mattered was type-satisfied and behaviour-wrong: ``agy`` frames
that carry a target path and no content, ``policy.ask_user`` returning a
bare ``bool``. No reflection would have caught either. And nothing runs
this on a schedule, so a ``pip install --upgrade`` with no commits after
it leaves a window where the report is stale and does not say so.

For the half reflection structurally cannot reach, :func:`diff_agy_init`
reads what the ``agy`` CLI advertises in its ``init`` frame — model, cwd,
permission mode, and its full 57-tool list. ``agy`` is not the engine
(AG-2) and is still the only machine-readable capability inventory either
Antigravity surface offers, which is why AG-8 wires it in anyway. It is
the analogue of the Claude probe's ``diff_server_info``.

Governing spec: ``specs5/plan-ag/sdk-surface.md`` § *The probe*.
"""

from __future__ import annotations

import ast
import functools
import importlib.metadata
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Surface statuses, in the order a reader wants them: what works, what we
# chose against, what nobody has looked at.
HANDLED = "handled"
DECLINED = "declined"
PENDING = "pending"

#: The step enums the pump has to dispatch on, by attribute name on
#: ``google.antigravity.types``. Members are reported qualified
#: (``StepType.THINKING``) because ``USER``, ``UNKNOWN`` and
#: ``UNSPECIFIED`` each appear in more than one of them.
STEP_ENUMS = ("StepType", "StepSource", "StepTarget", "StepStatus", "StopReason")

#: The abstract hook bases. Enumerated so their concrete subclasses can
#: be found, and excluded from the report — an ABC is not a hook event.
HOOK_BASES = ("InspectHook", "DecideHook", "TransformHook")

#: Config fields we will not set, each with a standing reason.
#:
#: **Moved to** :data:`aic_dc.antigravity.options.NEVER_SET` **in phase 3**,
#: which is where it was always headed: the refusal now sits beside the
#: code doing the refusing, the same ownership
#: ``claude_code.options.NEVER_SET`` has.
#:
#: This name is kept as the fallback :func:`_declined_config` reads when
#: ``options`` cannot be imported, and only for that. It is deliberately
#: *empty* rather than a stale copy — a duplicated table is the version
#: that silently disagrees with the real one, and an empty fallback fails
#: the ``unclassified`` gate loudly, which is the correct outcome if the
#: options module ever stops importing.
NEVER_SET_CONFIG: dict[str, str] = {}

#: Config fields nobody has wired yet, each with what it would buy — so
#: triage after an SDK bump starts from an argument rather than a name.
PENDING_CONFIG: dict[str, str] = {
    "app_data_dir":"where localharness keeps its state. The default is "
    "right until two engines' state directories need separating.",
    "budget_config": "max_model_calls / max_tool_calls / max_total_tokens, "
    "with StopReason.MAX_*_EXCEEDED naming which cap fired. AG-6 offers "
    "this in place of max_budget_usd, which has no Antigravity equivalent "
    "— a better control than a dollar cap for a session whose price is not "
    "observable.",
    "conversation_id":"resumes a prior conversation. Phase 5, with the "
    "repo-local mirror built as a step observer.",
    "debug_config": "the SDK's debug sink. logging_setup owns AIC-DC's "
    "logging; worth wiring once there is somewhere to route it, which is "
    "the analogue of the Claude engine's engine-errors.jsonl.",
    "mcp_servers":"stdio and streamable-HTTP MCP servers. AG-4 routes "
    "AIC-DC's own indexes through `tools` as plain callables instead, so "
    "nothing needs this today; user-configured servers have no Antigravity "
    "path yet and want a settings surface before a config field.",
    "models":"per-ModelType overrides — TEXT and IMAGE are separate "
    "targets, which is how generate_image picks a model distinct from the "
    "one holding the conversation. Phase 1's consultant needs it.",
    "retry_config":"ModelAPIRetryConfig / ModelOutputRetryConfig. The "
    "field that turns a rate limit into a slow turn instead of a dead one: "
    "the free tier throttles at 5 RPM and both later probe runs hit 429s "
    "*mid-turn*, because an agent turn is many model calls.",
    "save_dir": "where a conversation is persisted. Phase 5.",
    "session_continuation_mode": "RESUME / CREATE_OR_RESUME / CREATE_ONLY. "
    "Phase 5 — the choice between resuming and starting fresh is the "
    "history browser's, not the adapter's.",
    "skills_paths": "skill directories. The repo's .claude/skills/ is "
    "Claude's format and there is no shared one, so this is a second set "
    "of files to author rather than a switch to flip.",
    "subagents": "SubagentConfig list. The steps carry parent_trajectory_id "
    "and per-trajectory usage, so subagent tabs are buildable — but the "
    "chat has to render one trajectory before it can render several.",
    "system_instructions": "SystemInstructions / TemplatedSystemInstructions "
    "— the analogue of the Claude system prompt, and where AIC-DC would "
    "tell the agent about its own tools.",
    "triggers": "out-of-band wakeups — `every`, `on_file_change`. Listed "
    "in sdk-surface.md § Antigravity capabilities with no home in the "
    "current UI; it needs a UI before it needs a config field.",
}

#: Every builtin tool, and what AIC⚡DC intends to do about it.
#:
#: The three mutating entries are the load-bearing ones. AG-5 defines the
#: permission seam as *all* mutating tools rather than the file tools,
#: because a denied edit_file was rewritten through run_command — `sed -i`,
#: then inline python3 — unprompted, on both probe runs (AG-R-11).
BUILTIN_TOOLS: dict[str, tuple[str, str]] = {
    "CREATE_FILE": (
        PENDING,
        "mutating. Carries CodeContent — the whole new file — into the "
        "permission hook, so the dialog can render it without reading disk",
    ),
    "EDIT_FILE": (
        PENDING,
        "mutating. Carries TargetContent + ReplacementContent + a line "
        "range, which is a complete diff hunk. This is the measurement "
        "phase 2 existed to take",
    ),
    "RUN_COMMAND": (
        PENDING,
        "mutating, and gated with the same standing as the file tools from "
        "the adapter's first line rather than retrofitted. AG-R-11: a "
        "dialog that gates only create_file/edit_file shows the user a "
        "diff, records their refusal, and lets the edit through anyway",
    ),
    "LIST_DIR": (PENDING, "read-only; a tool card for the chat to render"),
    "SEARCH_DIR": (PENDING, "read-only; a tool card for the chat to render"),
    "FIND_FILE": (PENDING, "read-only; a tool card for the chat to render"),
    "VIEW_FILE": (
        PENDING,
        "read-only. Worth naming separately because agy's equivalent "
        "returns '2 lines, 18 bytes' rather than the file — the SDK's is "
        "the one that can feed a viewer",
    ),
    "GENERATE_IMAGE": (
        PENDING,
        "the disjoint capability AG-1 exists for — Google offers it and "
        "Anthropic does not — and phase 1's worked example",
    ),
    "ASK_QUESTION": (
        PENDING,
        "agent-initiated structured questions, via AskQuestionInteractionSpec "
        "and OnInteractionHook. Listed in sdk-surface.md § Antigravity "
        "capabilities with no home in the current UI: it needs a dialog "
        "Claude never asked for",
    ),
    "START_SUBAGENT": (
        PENDING,
        "spawns a subagent on its own trajectory. Same ordering as the "
        "subagents config field: render one trajectory first",
    ),
    "SEARCH_WEB": (PENDING, "read-only; a tool card, and a source to cite"),
    "READ_URL_CONTENT": (PENDING, "read-only; a tool card, and a source to cite"),
    "FINISH": (
        DECLINED,
        "the loop's own terminator. It arrives as StepType.FINISH and "
        "there is nothing for a host to decide about it — gating it would "
        "mean asking the user for permission to stop",
    ),
}

#: Concrete hook classes, and what AIC⚡DC does about each.
#:
#: The abstract bases in :data:`HOOK_BASES` are excluded: they are how the
#: subclasses are found, not events in their own right.
HOOK_CLASSES: dict[str, tuple[str, str]] = {
    "PostToolCallHook": (
        PENDING,
        "receives a ToolResult. The analogue of the Claude engine's "
        "PostToolUse matcher, which broadcasts the write and queues "
        "re-indexing — both engine-agnostic jobs with an engine-specific "
        "trigger",
    ),
    "OnCompactionHook": (
        PENDING,
        "compaction as a systemEvent *before* the pause rather than after "
        "it. Same argument the Claude engine's PreCompact carries: the "
        "stream's own boundary arrives when compaction has finished, so it "
        "can only explain a stall the user already read as a hang",
    ),
    "OnSessionStartHook": (
        PENDING,
        "session start. The adapter already knows it started, so this is "
        "worth wiring only if it carries something the caller does not have",
    ),
    "OnSessionEndHook": (
        PENDING,
        "session end, including ends the adapter did not ask for. That is "
        "the case worth having — a session that died is not the same event "
        "as one we closed",
    ),
    "OnInteractionHook": (
        PENDING,
        "answers an agent-initiated ask_question. Blocked on the same "
        "missing dialog as the ASK_QUESTION tool, and pending for that "
        "reason rather than on its own merits",
    ),
    "PreTurnHook": (
        DECLINED,
        "a veto on the user's own prompt. We send the prompt, so a hook "
        "would tell us what we just did",
    ),
    "PostTurnHook": (
        DECLINED,
        "the turn's end is already on the step stream — StepStatus.DONE "
        "and Step.is_complete_response — which is what the pump reads to "
        "know a turn is over",
    ),
    "OnToolErrorHook": (
        DECLINED,
        "a tool failure arrives on the step as `error` and on the result "
        "as `exception`; the tool card renders it from there. A transform "
        "hook here would be rewriting failures, not reporting them",
    ),
}

#: Step-enum members, qualified by enum. What the pump must dispatch on.
#:
#: Every member is listed rather than every member the pump *uses*,
#: because a closed vocabulary is only useful if the closure is checked:
#: a 0.1.x release adding a step type is exactly the drift this file
#: exists to catch, and it would otherwise arrive as a step the chat
#: silently drops.
#:
#: **This section declares rather than derives, and the reason is worth
#: knowing before trusting it.** :func:`referenced_enum_members` finds
#: ``StepType.TOOL_CALL`` written as an attribute. The pump does not write
#: it that way: ``steps.py`` compares on ``.name`` against string literals,
#: because ``Step``'s enums are ``str``-valued and an alpha SDK that turned
#: one into a plain string would otherwise mis-dispatch silently. That
#: defensiveness is right and it costs the derived signal, so phase 3 set
#: these rows by hand — with
#: ``test_every_step_member_is_named_in_the_pump`` reading ``steps.py`` for
#: each member's name as the cross-check the syntax tree cannot give.
#:
#: ``StopReason`` members stay pending: the pump forwards the reason
#: verbatim onto ``streamComplete``, but nothing renders the difference
#: between a budget cap and an ordinary stop yet, and that rendering is
#: what these notes are arguing for.
STEP_MEMBERS: dict[str, tuple[str, str]] = {
    "StepType.TEXT_RESPONSE": (HANDLED, "assistant prose; the chat's main body"),
    "StepType.THINKING": (HANDLED, "reasoning, with its own delta channel"),
    "StepType.TOOL_CALL": (HANDLED, "a tool card, and the permission gate's subject"),
    "StepType.SYSTEM_MESSAGE": (HANDLED, "a systemEvent in the transcript"),
    "StepType.COMPACTION": (HANDLED, "the compaction boundary, after the fact"),
    "StepType.FINISH": (HANDLED, "the loop's terminator; ends the turn"),
    "StepType.UNKNOWN": (
        HANDLED,
        "the forward-compatibility escape hatch, and the most important "
        "member here: on an alpha SDK it is how a step type this wheel "
        "does not know arrives. It must render as an unknown card, never "
        "be dropped",
    ),
    "StepSource.MODEL": (
        HANDLED,
        "the agent speaking; the only source whose text is the assistant's "
        "own prose rather than an echo or a harness notice",
    ),
    "StepSource.USER": (HANDLED, "our own prompt, echoed back into the stream"),
    "StepSource.SYSTEM": (HANDLED, "the harness speaking, not the model"),
    "StepSource.UNKNOWN": (HANDLED, "unrecognised source; render, do not drop"),
    "StepTarget.USER": (HANDLED, "addressed to the human — the chat renders it"),
    "StepTarget.ENVIRONMENT": (
        HANDLED,
        "addressed to the tools rather than the reader. The distinction "
        "the transcript needs to avoid showing the user machine chatter",
    ),
    "StepTarget.UNSPECIFIED": (HANDLED, "no stated target; treat as user-facing"),
    "StepTarget.UNKNOWN": (HANDLED, "unrecognised target; render, do not drop"),
    "StepStatus.ACTIVE": (HANDLED, "in flight; the step is still streaming deltas"),
    "StepStatus.DONE": (HANDLED, "complete. Bounds a turn together with is_complete_response"),
    "StepStatus.WAITING_FOR_USER": (
        HANDLED,
        "the agent is blocked on us — a permission decision or an "
        "ask_question. The state the UI must not render as a hang",
    ),
    "StepStatus.ERROR": (
        HANDLED,
        "failed, with Step.error carrying why. The tool card renders that "
        "text; a pump that only checked DONE would show a silent stall",
    ),
    "StepStatus.CANCELED": (
        HANDLED,
        "cancelled, including by our own mid-turn cancel. Worth naming "
        "because agy reports a *permission denial* this way with no error "
        "key at all, and the SDK must not be assumed to differ",
    ),
    "StepStatus.UNKNOWN": (HANDLED, "unrecognised status; render, do not drop"),
    "StopReason.UNSPECIFIED": (
        PENDING,
        "the turn ended on its own terms, without a BudgetConfig cap firing "
        "— the ordinary case, and the one the footer says nothing about",
    ),
    "StopReason.MAX_MODEL_CALLS_EXCEEDED": (
        PENDING,
        "the cap on model calls fired. AG-6 offers these in place of "
        "max_budget_usd, so the UI's whole account of why a turn stopped "
        "early is this enum naming which cap it was",
    ),
    "StopReason.MAX_TOOL_CALLS_EXCEEDED": (
        PENDING,
        "the cap on tool calls fired — the one that bounds a runaway "
        "edit-then-verify loop rather than a runaway conversation",
    ),
    "StopReason.MAX_INPUT_TOKENS_EXCEEDED": (
        PENDING,
        "the input cap fired. Nearer than it looks: the measured floor is "
        "13,873 input tokens to answer 'reply with exactly the word: ok'",
    ),
    "StopReason.MAX_OUTPUT_TOKENS_EXCEEDED": (
        PENDING,
        "the output cap fired, so the turn was truncated rather than "
        "finished. The distinction the transcript has to make visible",
    ),
    "StopReason.MAX_TOTAL_TOKENS_EXCEEDED": (
        PENDING,
        "the session-wide token cap fired. AG-6's substitute for a dollar "
        "cap, and the one a settings UI would expose first",
    ),
    "StopReason.QUOTA_EXHAUSTED": (
        PENDING,
        "the account ran out, not the session. AG-6 hides the USD panels "
        "for this engine, so this is the only quota signal there is",
    ),
}

#: The policy DSL, with the reason per name so a reader reaching for one
#: finds the argument rather than a silence.
#:
#: AG-5 declines this layer **as the permission gate**: ``ask_user``
#: returns a bare ``bool``, which gives away both the message the model
#: reads and the ability to amend a tool call before it runs. That
#: argument turns on there being a dialog to lose, so it does not reach
#: the two builders the consultant uses — ``deny_all`` plus a single
#: ``allow`` for the one tool a one-shot call exists to invoke, where
#: there is no user in the loop and nothing to amend. Those two read as
#: handled from :mod:`~aic_dc.antigravity.consultant`, which is where the
#: narrowing is argued.
#:
#: Everything still listed here is declined, and the list is kept as a
#: section rather than a footnote because this is the surface an
#: implementer is most likely to adopt by accident: it is the documented,
#: convenient path, and taking it for the *gate* gives away the dialog.
#: ``allow_all`` in particular remains probe-only and must never ship.
POLICY_BUILDERS: dict[str, tuple[str, str]] = {
    "ask_user": (
        DECLINED,
        "the trap. AskUserHandler returns a bare bool, so the reason the "
        "model reads and the ability to amend the tool input before it "
        "runs are both unreachable. AG-5 takes the raw hook for exactly "
        "this",
    ),
    "allow_all": (
        DECLINED,
        "a probe-only posture. AG-5 states it must never reach a shipped "
        "path — it is blanket bypass under a friendly name",
    ),
    "deny": (
        DECLINED,
        "a static denylist entry. Where a decision is the user's, the "
        "dialog makes it per call; where it is not, an allowlist says what "
        "is permitted rather than enumerating what is not",
    ),
    "safe_defaults": (
        DECLINED,
        "a curated policy set built on ask_user, so it inherits the bare-bool "
        "loss. The curation is worth reading; the mechanism is not adoptable",
    ),
    "confirm_run_command": (
        DECLINED,
        "gates run_command through ask_user. AG-5 gates it through the same "
        "hook as the file tools, which is the point of AG-R-11 — one gate, "
        "not two that can disagree",
    ),
    "workspace_only": (
        DECLINED,
        "path containment as policies. AG-10 makes containment a startup "
        "health check that fails visibly, because the failure mode is a "
        "silently diverted write rather than a refused one",
    ),
    "enforce": (DECLINED, "compiles policies into a decide hook. We write the hook"),
    "flatten_policies": (DECLINED, "a helper for the DSL we do not use"),
    "Policy": (
        DECLINED,
        "the DSL's record type — a tool, a Decision, and an optional "
        "predicate. Nothing to adopt once the verdict is ours to make",
    ),
    "Decision": (DECLINED, "APPROVE / DENY / ASK_USER — the DSL's verdict enum"),
}

#: ``CapabilitiesConfig`` fields. The tool-gating layer *below* the
#: permission hook, and the reason the two are separate sections: a tool
#: disabled here never reaches the dialog at all.
CAPABILITY_FIELDS: dict[str, tuple[str, str]] = {
    "disabled_tools":(
        PENDING,
        "a denylist. Denials belong in project settings where the user can "
        "see them, so this wants a settings surface before a config field",
    ),
    "compaction_threshold": (
        PENDING,
        "when to compact. The nearest thing to a context-window read-back "
        "the SDK offers — it is a threshold we set, not a window it "
        "reports, which is why AG-9 hides the Context tab's bar rather "
        "than deriving one from this",
    ),
    "enable_subagents": (PENDING, "on by default; same ordering as the subagents field"),
    "allowed_subagents": (PENDING, "restricts which subagents may be spawned"),
    "max_subagent_depth": (
        PENDING,
        "bounds subagent recursion. Cheap insurance the day subagents are "
        "enabled, and meaningless before then — so it moves with them",
    ),
    "run_command_config": (
        PENDING,
        "enable_daemons and timeout_seconds. Daemons are listed in "
        "sdk-surface.md § Antigravity capabilities with no home in the "
        "current UI; the timeout is worth setting the day run_command runs",
    ),
    "finish_tool_schema_json": (
        DECLINED,
        "shapes the finish tool's arguments into a schema. The structured-"
        "output path under another name, declined for the same reason as "
        "response_schema",
    ),
}


# ----------------------------------------------------------------------
# The installed SDK
# ----------------------------------------------------------------------


def _sdk() -> Any:
    """Import the SDK, or ``None`` when it is not installed.

    Every caller degrades to an empty report rather than raising: this is
    a diagnostic, and a diagnostic that breaks startup is worse than one
    that says "unknown". ``google-antigravity`` is an optional extra
    (AG-R-10 — it bundles a second ~119 MB binary), so absent is a
    supported state rather than a broken install.
    """
    try:
        import google.antigravity

        return google.antigravity
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("google-antigravity is not importable; surface unknown")
        return None


def _model_fields(model: Any) -> list[str]:
    """Field names on a pydantic model, or ``[]``.

    ``model_fields`` rather than ``dataclasses.fields``: this SDK's
    configs are pydantic, which is the first way the Claude probe's
    reflection does not transfer.
    """
    fields = getattr(model, "model_fields", None)
    if not isinstance(fields, dict):
        return []
    return sorted(fields)


def config_fields() -> list[str]:
    """Field names across the installed agent configs.

    The union of ``LocalAgentConfig`` and ``AgentConfig``. Today the first
    is a superset of the second, and taking the union rather than the
    subclass means a field added to the base — where the transport-neutral
    surface lives — cannot hide behind that happening to be true.
    """
    sdk = _sdk()
    if sdk is None:
        return []
    names: set[str] = set()
    for name in ("LocalAgentConfig", "AgentConfig"):
        names.update(_model_fields(getattr(sdk, name, None)))
    return sorted(names)


def builtin_tool_names() -> list[str]:
    """Member names of ``types.BuiltinTools``.

    Names rather than values (``EDIT_FILE``, not ``edit_file``) because
    the member is the stable identifier: a release renaming the wire value
    under a stable member is a behaviour change this probe cannot see
    either way, and a release adding a member is what it can.
    """
    sdk = _sdk()
    if sdk is None:
        return []
    tools = getattr(sdk.types, "BuiltinTools", None)
    return [m.name for m in tools] if tools is not None else []


def hook_class_names() -> list[str]:
    """Concrete hook classes in the SDK's ``hooks`` module.

    Found by subclass rather than by name pattern: the bases in
    :data:`HOOK_BASES` are the SDK's own taxonomy, so a hook added in a
    release appears here without this function knowing what it is called.
    Private classes (``_PreStepHook``) are skipped — they are internal
    plumbing, not a host-facing event.
    """
    sdk = _sdk()
    if sdk is None:
        return []
    module = getattr(sdk, "hooks", None)
    bases = tuple(
        base
        for base in (getattr(module, name, None) for name in HOOK_BASES)
        if isinstance(base, type)
    )
    if not bases:
        return []
    names = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name, None)
        if isinstance(obj, type) and issubclass(obj, bases) and obj not in bases:
            names.append(name)
    return sorted(names)


def step_enum_members() -> list[str]:
    """Every member of every step enum, qualified as ``Enum.MEMBER``."""
    sdk = _sdk()
    if sdk is None:
        return []
    names = []
    for enum_name in STEP_ENUMS:
        enum_cls = getattr(sdk.types, enum_name, None)
        if enum_cls is None:
            continue
        names.extend(f"{enum_name}.{m.name}" for m in enum_cls)
    return names


def policy_builder_names() -> list[str]:
    """Public callables in the SDK's ``hooks.policy`` module.

    Classes included: ``Policy`` and ``Decision`` are as much of the DSL's
    surface as its functions are, and a reader who reaches for one is
    making the same mistake AG-5 declines.
    """
    sdk = _sdk()
    if sdk is None:
        return []
    try:
        from google.antigravity.hooks import policy
    except Exception:  # noqa: BLE001 - a probe, never a control path
        return []
    return sorted(
        name
        for name in dir(policy)
        if not name.startswith("_")
        and callable(getattr(policy, name, None))
        and getattr(getattr(policy, name), "__module__", "").startswith(
            "google.antigravity"
        )
    )


def capability_fields() -> list[str]:
    """Field names on ``types.CapabilitiesConfig``."""
    sdk = _sdk()
    if sdk is None:
        return []
    return _model_fields(getattr(sdk.types, "CapabilitiesConfig", None))


# ----------------------------------------------------------------------
# What this package handles, read out of its own syntax tree
# ----------------------------------------------------------------------


def _package_trees() -> list[ast.Module]:
    """Parsed sources of every module in this package but this one.

    By path rather than by import, so the probe does not load the modules
    it is measuring — the engine adapter will pull in a 119 MB binary's
    Python wrapper, and a diagnostic should not have side effects.

    This is where the ``handled`` bucket comes from, and why it needs no
    maintenance: phase 3 passing ``workspaces=`` or subclassing
    ``PreToolCallDecideHook`` moves those rows on its own.
    """
    trees = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            trees.append(ast.parse(path.read_text(encoding="utf-8")))
        except (OSError, SyntaxError):  # pragma: no cover - our own modules
            logger.debug("Could not parse %s for surface analysis", path)
    return trees


#: Constructors whose keyword arguments are agent-config fields.
CONFIG_CONSTRUCTORS = (
    "LocalAgentConfig",
    "AgentConfig",
    "LiteRTAgentConfig",
    "LocalOpenAIAgentConfig",
)

#: And the one whose keywords are capability fields.
CAPABILITY_CONSTRUCTORS = ("CapabilitiesConfig",)


def _callee(node: ast.Call) -> str:
    """A call's callee name, however it was imported."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _call_keywords(constructors: tuple[str, ...]) -> set[str]:
    """Keyword names passed to the named constructors, anywhere here.

    Scoped to the constructors rather than collected from every call, and
    that scoping is load-bearing rather than tidiness. An earlier draft
    swept up every keyword in the package and immediately reported the
    ``tools`` config field — AG-4's route for the symbol index — as built,
    because ``bridge.py`` passes ``tools=`` to *Claude's*
    ``create_sdk_mcp_server``. Two SDKs in one package means a bare
    keyword name is not evidence about either.
    """
    names: set[str] = set()
    for tree in _package_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) in constructors:
                names.update(kw.arg for kw in node.keywords if kw.arg)
    return names


def _kwargs_dict_keys() -> set[str]:
    """Config names assembled into a ``kwargs`` dict before being splatted.

    The conditional-assembly shape: ``kwargs["conversation_id"] = …``
    inside an ``if``, and the literal dict it starts from. Read from
    syntax so a field set only when resuming counts exactly like one set
    unconditionally — the blind spot that made the Claude probe's first
    draft report 36 false gaps.

    Only names containing ``kwargs`` are followed, which is what keeps
    ``bridge.py``'s JSON-schema dicts out of the config surface.
    """
    names: set[str] = set()

    def _is_kwargs(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and "kwargs" in node.id

    for tree in _package_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                # kwargs["name"] = value
                if (
                    isinstance(target, ast.Subscript)
                    and _is_kwargs(target.value)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    names.add(target.slice.value)
                # kwargs = {"name": value, ...}
                elif _is_kwargs(target) and isinstance(node.value, ast.Dict):
                    names.update(
                        key.value
                        for key in node.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
    return names


@functools.cache
def config_keywords() -> frozenset[str]:
    """Config field names this package passes to an agent config."""
    return frozenset(_call_keywords(CONFIG_CONSTRUCTORS) | _kwargs_dict_keys())


@functools.cache
def capability_keywords() -> frozenset[str]:
    """Capability field names this package passes to ``CapabilitiesConfig``."""
    return frozenset(_call_keywords(CAPABILITY_CONSTRUCTORS))


@functools.cache
def referenced_names() -> frozenset[str]:
    """Every identifier this package names — bare, attribute, or base class.

    Covers the two ways a hook or a policy builder gets used: subclassing
    it, and calling it. Unlike the config readers this is not scoped to a
    constructor, because a hook or a policy builder has no single call
    shape — so a name shared with something unrelated would read as
    handled. That direction is the safe one: ``handled`` is advisory
    while ``pending`` is a test failure.
    """
    names: set[str] = set()
    for tree in _package_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        names.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        names.add(base.attr)
                    # PreToolCallDecideHook[types.ToolCall] — subscripted
                    elif isinstance(base, ast.Subscript):
                        inner = base.value
                        if isinstance(inner, ast.Name):
                            names.add(inner.id)
                        elif isinstance(inner, ast.Attribute):
                            names.add(inner.attr)
    return frozenset(names)


@functools.cache
def referenced_enum_members() -> frozenset[str]:
    """Qualified ``Enum.MEMBER`` names this package reads.

    Qualified rather than bare because ``USER`` is a member of both
    ``StepSource`` and ``StepTarget``, and ``UNKNOWN`` of four enums —
    a bare-name match would report the whole taxonomy handled the moment
    the pump touched one of them.

    Matches both ``StepType.THINKING`` and ``types.StepType.THINKING``,
    since the import style is the caller's business.
    """
    members: set[str] = set()
    for tree in _package_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            if isinstance(owner, ast.Name):
                members.add(f"{owner.id}.{node.attr}")
            elif isinstance(owner, ast.Attribute):
                members.add(f"{owner.attr}.{node.attr}")
    return frozenset(members)


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


def _declined_config() -> dict[str, str]:
    """The standing config refusals, preferring ``options.py`` when it exists.

    :data:`NEVER_SET_CONFIG` lives here only because phase 1 has no
    options module yet. When phase 3 writes one, moving the table there
    puts the refusal beside the code that does the refusing — the same
    ownership the Claude engine's ``options.NEVER_SET`` has — and this
    function starts reading it without any other edit.
    """
    try:
        from aic_dc.antigravity.options import NEVER_SET  # type: ignore[attr-defined]

        return dict(NEVER_SET)
    except Exception:  # noqa: BLE001 - expected until phase 3
        return dict(NEVER_SET_CONFIG)


def _section(
    names: list[str],
    handled: frozenset[str],
    table: dict[str, tuple[str, str]],
    *,
    untriaged: str,
) -> dict[str, Any]:
    """One section's entries and its three cross-checks.

    The derived set wins over the table: a name this package demonstrably
    uses reads as ``handled`` even if a table still argues against it, so
    the report cannot understate coverage. ``resolved`` is how the stale
    argument gets noticed instead of quietly contradicting the code.
    """
    entries = []
    for name in names:
        if name in handled:
            status, note = HANDLED, ""
        else:
            status, note = table.get(name, (PENDING, untriaged))
        entries.append({"name": name, "status": status, "note": note})
    return {
        "entries": entries,
        # The only bucket that fails the gate: surface in none of the three.
        "unclassified": sorted(set(names) - handled - set(table)),
        # Names we still explain that the SDK no longer has. Not a coverage
        # failure but a prose one — a note arguing about a field that does
        # not exist sends the next reader looking for it.
        "stale": sorted(set(table) - set(names)),
        # Names the code now uses that a table still argues about. The
        # status is right either way; the *note* is a reason not to do the
        # thing we did, which is worse than no note.
        "resolved": sorted(set(table) & handled),
    }


def config_report() -> dict[str, Any]:
    """Per-config-field status, plus anything the installed SDK added."""
    declined = _declined_config()
    table: dict[str, tuple[str, str]] = {
        **{name: (DECLINED, note) for name, note in declined.items()},
        **{name: (PENDING, note) for name, note in PENDING_CONFIG.items()},
    }
    return _section(
        config_fields(),
        config_keywords(),
        table,
        untriaged="not yet triaged — new in this SDK release",
    )


def tool_report() -> dict[str, Any]:
    """Per-builtin-tool status.

    Nothing is derived here: a tool is "handled" when the permission gate
    and the chat card know about it, and neither is a syntactic fact the
    way a keyword argument is. Phase 4 is what fills this in, by which
    point the gate has a per-tool table of its own to read.
    """
    return _section(
        builtin_tool_names(),
        frozenset(),
        BUILTIN_TOOLS,
        untriaged="not yet triaged — new builtin tool in this SDK release",
    )


def hook_report() -> dict[str, Any]:
    """Per-hook-class status, cross-checked against what we subclass."""
    return _section(
        hook_class_names(),
        referenced_names(),
        HOOK_CLASSES,
        untriaged="not yet triaged — new hook class in this SDK release",
    )


def step_report() -> dict[str, Any]:
    """Per-step-enum-member status, cross-checked against the pump."""
    return _section(
        step_enum_members(),
        referenced_enum_members(),
        STEP_MEMBERS,
        untriaged="not yet triaged — new enum member in this SDK release",
    )


def policy_report() -> dict[str, Any]:
    """The policy DSL, which AG-5 declines wholesale.

    Derived coverage is checked anyway rather than hard-coding every row
    to ``declined``: if a policy builder ever does appear in this package,
    that is a decision being reversed silently, and the report should say
    so under ``resolved`` rather than keep insisting we declined it.
    """
    return _section(
        policy_builder_names(),
        referenced_names(),
        POLICY_BUILDERS,
        untriaged="not yet triaged — new policy builder in this SDK release",
    )


def capability_report() -> dict[str, Any]:
    """Per-capability-field status."""
    return _section(
        capability_fields(),
        capability_keywords(),
        CAPABILITY_FIELDS,
        untriaged="not yet triaged — new capability field in this SDK release",
    )


def diff_agy_init(init_frame: Any = None) -> dict[str, Any]:
    """What the ``agy`` CLI advertises, from its ``init`` frame.

    The complement to every other function here, and the reason the probe
    is not purely static: ``agy`` is a separate 208 MB product on its own
    release train that shares no symbols with the SDK's bundled harness,
    and its 57-tool inventory appears in no Python class. AG-8 wires it in
    even though AG-2 rules ``agy`` out as the engine, because it is the
    only machine-readable capability inventory either Antigravity surface
    offers and it is free to query.

    Takes the frame rather than fetching it, for the reason the Claude
    probe's ``diff_server_info`` does: every other function here works
    with no process running, and the gate has to be runnable in a test
    with no engine and no credentials.

    The frame is **nested** — ``{"event":"init", "init":{…}}`` — not flat.
    That was a phase-0 transcription error corrected on re-measurement at
    ``agy`` 1.1.22, and a parser written against the flat shape read
    ``None`` for every field without erroring, which is exactly why this
    reports ``available: False`` rather than an empty success.
    """
    empty = {
        "available": False,
        "model": "",
        "cwd": "",
        "permission_mode": "",
        "tools": [],
    }
    if not isinstance(init_frame, dict):
        return empty
    # Accept either the whole frame or the inner payload: a caller reading
    # NDJSON has the first, a caller that already unwrapped has the second.
    inner = init_frame.get("init")
    payload = inner if isinstance(inner, dict) else init_frame
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []

    def _text(key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    return {
        "available": True,
        "model": _text("model"),
        "cwd": _text("cwd"),
        "permission_mode": _text("permission_mode"),
        "tools": sorted({name for name in tools if isinstance(name, str)}),
    }


def _versions() -> dict[str, str]:
    """Version facts about the installed SDK and its bundled harness.

    The harness path is reported because the SDK is not one artifact: it
    spawns a bundled Go binary, and "the wheel is installed" is not the
    same claim as "the thing that runs the turn is present". A version
    number alone would assert the second from the first.
    """
    facts = {"sdk_version": "unknown", "harness_binary": ""}
    try:
        facts["sdk_version"] = importlib.metadata.version("google-antigravity")
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("Could not read the google-antigravity version")
    sdk = _sdk()
    if sdk is not None:
        try:
            bin_dir = Path(sdk.__file__).parent / "bin"
            binaries = [p for p in bin_dir.glob("*") if p.is_file() and not p.suffix]
            if binaries:
                facts["harness_binary"] = str(binaries[0])
        except Exception:  # noqa: BLE001 - a probe, never a control path
            logger.debug("Could not locate the bundled localharness binary")
    return facts


def surface_report(agy_init: Any = None) -> dict[str, Any]:
    """The whole probe, as the dict the browser and the test both read.

    One shape for both callers on purpose, and the same shape the Claude
    probe's ``surface_report`` returns — a tab that renders one engine's
    report should not need a second renderer for the other's (AG-3).
    """
    sections = {
        "config": config_report(),
        "tools": tool_report(),
        "hooks": hook_report(),
        "steps": step_report(),
        "policy": policy_report(),
        "capabilities": capability_report(),
    }
    counts = {
        name: {
            status: sum(1 for e in section["entries"] if e["status"] == status)
            for status in (HANDLED, DECLINED, PENDING)
        }
        for name, section in sections.items()
    }
    unclassified = {
        name: section["unclassified"]
        for name, section in sections.items()
        if section["unclassified"]
    }
    return {
        "sdk_available": _sdk() is not None,
        "versions": _versions(),
        "sections": sections,
        "counts": counts,
        #: Everything the gate fails on, gathered so the test and the tab
        #: agree on what "needs triage" means.
        "unclassified": unclassified,
        "cli": diff_agy_init(agy_init),
    }
