"""What the installed SDK offers, and what AC⚡DC actually reaches for.

``specs5/plan/sdk-surface.md`` is a hand-written snapshot of the SDK read
out of the wheel. It was accurate the day it was written and nothing
enforces it since, which is the failure mode this module exists to close:
the SDK ships often, each release may add options, hook events, message
types and beta gates, and none of that arrives as a build break. It
arrives as a feature we silently do not offer.

So this probe reflects over the *installed* SDK and diffs it against what
this package handles, producing three answers per surface:

- **handled** — we set it, register it, or dispatch on it.
- **declined** — we deliberately do not, with the reason kept next to the
  code that declines (``options.NEVER_SET``, :data:`HOOK_EVENTS`).
- **pending** — real surface nobody has triaged yet.

The point of the third bucket is that it must be *empty by declaration*,
not empty by accident. :mod:`tests.test_claude_code_sdk_surface` fails
when the SDK exposes something absent from all three, so an SDK bump that
adds a field is a red test with the field's name in it rather than a
discovery months later.

Coverage is **derived, not declared, wherever it can be.** An earlier
draft of this module hand-listed the options we set and a text-search
fallback decided the rest; both were wrong in opposite directions. A
runtime diff of :func:`~ac_dc.claude_code.options.build_option_kwargs`
under a default config reported 36 unhandled fields, because ``model``,
``hooks``, ``resume`` and ``thinking`` are set only when something asks
for them. A word-search of ``options.py`` reported ``skills`` as handled,
because the word appears in a comment about ``.claude/skills/``. Reading
the *assignments* out of the module's syntax tree has neither failure:
a conditional branch is still an assignment, and a comment is not one.

What this cannot do, and why the CLI is probed too
--------------------------------------------------
Reflection sees **shape**. It sees a new field, a new hook event, a new
``Literal`` value. It cannot see a field whose meaning changed under a
stable name, a new string accepted by an existing ``str`` field, or a
feature that lands entirely in the CLI — which is a separate Node binary
on its own release train, and the larger half of the product. For that,
:func:`diff_server_info` reads what the live CLI advertises at
initialize (commands, tools, output styles) and diffs it against what the
webapp renders. Static reflection structurally cannot find those; the two
probes together cover more than either.

Governing spec: ``specs5/plan/sdk-surface.md``.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import logging
import typing
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Surface statuses, in the order a reader wants them: what works, what we
# chose against, what nobody has looked at.
HANDLED = "handled"
DECLINED = "declined"
PENDING = "pending"

#: Options we have deliberately not implemented yet, each with what it
#: would buy — so triage after an SDK bump starts from an argument rather
#: than a bare name.
#:
#: Distinct from ``options.NEVER_SET``, which is a standing refusal with a
#: reason. These are "not yet", and moving one out of this dict and into
#: ``options.py`` is the expected way to close it.
PENDING_OPTIONS: dict[str, str] = {
    "betas": "gates opt-in SDK features; today's only value is "
    "context-1m-2025-08-07, the 1M-token context window. Worth wiring as "
    "a config field once we decide whether the cost profile suits AC-DC.",
    "sandbox": "SandboxSettings would confine tool execution (filesystem "
    "and network) below the permission layer, so a bypassPermissions "
    "session would still be contained. Overlaps can_use_tool but is "
    "enforced by the CLI rather than by our callback.",
    "plugins": "SdkPluginConfig loads CLI plugins. The repo's own "
    ".claude/ is already honoured via setting_sources; this is for "
    "plugins we would ship, and we ship none.",
    "skills": "list[str] | 'all' selects which skills load. Omitted "
    "today, which takes the CLI's default. Only worth setting if a user "
    "asks to restrict them; the repo's .claude/skills/ already applies.",
    "task_budget": "TaskBudget caps spend per background task. "
    "max_budget_usd already caps the session, so this is a finer grain of "
    "the same control and wants a UI before it wants a config field.",
    "max_turns": "caps agent turns per query. AC-DC's stop control and "
    "budget cap already bound a runaway turn, and a turn limit hit "
    "mid-task looks like a hang unless the UI explains it.",
    "max_thinking_tokens": "a hard thinking budget. We set thinking "
    "adaptive on purpose (options.py: the CLI's own posture); this would "
    "pin a number the model currently chooses.",
    "fallback_model": "retries on a different model when the primary is "
    "overloaded. Attractive for reliability, but a turn that silently "
    "changes model changes cost and behaviour, so it needs surfacing in "
    "the transcript first.",
    "add_dirs": "extra roots the agent may read. AC-DC is deliberately "
    "single-repo — cwd is the root every tool path resolves against, and "
    "the diff viewer and file tree assume it.",
    "disallowed_tools": "a deny list. Denials belong in project settings "
    "where the user can see them, same argument as NEVER_SET's "
    "allowed_tools; listed here rather than there because a deny rule "
    "does not ungate anything, so it is a choice rather than a hazard.",
    "output_format": "structured output schema forcing. A host-level "
    "feature for programmatic callers; AC-DC renders prose and tool calls "
    "to a human.",
    "permission_prompt_tool_name": "delegates permission prompts to a "
    "named MCP tool instead of can_use_tool. We own the dialog.",
    "user": "an end-user identifier for multi-tenant hosts. AC-DC is "
    "single-user and localhost-restricted.",
    "settings": "a path to an alternative settings file. setting_sources "
    "already loads user/project/local, which is the point (CC-11).",
    "continue_conversation": "resumes the most recent session implicitly. "
    "We pass `resume` with an explicit session ID, which is what the "
    "session browser needs; the implicit form also demands "
    "list_sessions() from the store.",
    "session_id": "pins the new session's ID. The SDK generates one and "
    "the mirror keys on what it reports, so choosing it ourselves buys "
    "nothing and risks a collision.",
    "resume_session_at": "resumes at a specific message, truncating what "
    "follows. Real editing power, and the natural partner of the rewind "
    "UI that delivery.md still lists as unbuilt.",
    "resume_drops_turn": "resumes dropping a turn — the retry-a-bad-turn "
    "primitive. Same missing UI as resume_session_at.",
    "strict_mcp_config": "ignores MCP config outside what we pass. Would "
    "cut the repo's own .mcp.json, which CC-11 keeps.",
    "load_timeout_ms": "how long to wait for CLI startup. health.py "
    "already probes and reports a slow binary, and the SDK's default has "
    "not been the failure.",
    "debug_stderr": "legacy debug sink, superseded by `stderr` — which we "
    "now set, so this could only be a second reader of the same lines.",
    "tools": "restricts the tool set wholesale. Same reasoning as "
    "allowed_tools/disallowed_tools: the user should see it in settings.",
}

#: Every hook event the SDK accepts, and what AC⚡DC does about it.
#:
#: ``hooks.build_hook_matchers`` registers two, and its docstring says
#: every other event is "either already covered by the message pump or is
#: a permission decision we must not make here". True, but not checkable
#: and not per-event: it cannot tell a reader which of the two applies to
#: ``Notification``, and it cannot notice an eleventh event. This is that
#: sentence, itemised.
HOOK_EVENTS: dict[str, tuple[str, str]] = {
    "PostToolUse": (HANDLED, "broadcasts the write and queues re-indexing"),
    "PreToolUse": (
        DECLINED,
        "a pre-tool veto is a permission decision, and can_use_tool is "
        "where AC-DC makes those — two gates would disagree",
    ),
    "PermissionRequest": (
        DECLINED,
        "same reason, more directly: can_use_tool owns the dialog",
    ),
    "PostToolUseFailure": (
        DECLINED,
        "the failure arrives in the stream as a ToolResultBlock the "
        "message pump already renders",
    ),
    "UserPromptSubmit": (
        DECLINED,
        "we send the prompt, so we already know; a hook would tell us "
        "what we just did",
    ),
    "Stop": (
        DECLINED,
        "ResultMessage ends the turn in the pump, carrying terminal_reason",
    ),
    "SubagentStart": (
        DECLINED,
        "TaskStartedMessage covers it in the stream; the per-subagent tabs "
        "are built from those",
    ),
    "SubagentStop": (
        DECLINED,
        "TaskUpdatedMessage / TaskNotificationMessage cover it in the stream",
    ),
    "Notification": (
        DECLINED,
        "CLI-facing idle and permission nudges; the browser has its own "
        "toasts driven by events it can already see",
    ),
    "PreCompact": (
        HANDLED,
        "broadcasts the compaction as a systemEvent, before the pause "
        "rather than after it. The stream's own compact_boundary arrives "
        "when compaction has finished, so it can only explain a stall the "
        "user has already read as a hang",
    ),
}

#: Beta gates we have seen and decided about, value → decision.
#:
#: This is the list the gate really cares about. Every other surface here
#: changes when the SDK *refactors*; ``betas`` changes when Anthropic ships
#: something. A value arriving that is absent from this dict is the
#: clearest "there is a new feature" signal the wheel can give us, so it
#: fails the test by name rather than sitting in a report nobody opened.
KNOWN_BETAS: dict[str, str] = {
    "context-1m-2025-08-07": "the 1M-token context window. Not requested: "
    "it changes the cost profile of every turn, and the Context tab's "
    "compaction thresholds are read from the live window, so enabling it "
    "is a config decision with a UI consequence rather than a flag flip.",
}

#: Client methods that exist for hosts we are not. Checked so the client
#: surface has the same three buckets as the rest.
DECLINED_CLIENT_METHODS: dict[str, str] = {
    "receive_messages": "receive_response() is the same stream bounded by "
    "ResultMessage, which is what a turn is",
}


def _sdk_module() -> Any:
    """Import the SDK, or ``None`` when it is not installed.

    Every caller degrades to an empty report rather than raising: this is
    a diagnostic, and a diagnostic that breaks startup is worse than one
    that says "unknown".
    """
    try:
        import claude_agent_sdk

        return claude_agent_sdk
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("claude-agent-sdk is not importable; surface unknown")
        return None


def _flatten_literals(annotation: Any) -> list[str]:
    """Every ``str`` in a possibly-nested ``Literal``/``Union``.

    Hook event names arrive as ``dict[Literal['A'] | Literal['B'], ...]``
    rather than one ``Literal['A', 'B']``, and ``betas`` as
    ``list[Literal[...]]``. Recursing over ``get_args`` and keeping only
    strings handles both without caring which shape a release used.
    """
    found: list[str] = []
    for arg in typing.get_args(annotation) or ():
        if isinstance(arg, str):
            found.append(arg)
        else:
            found.extend(_flatten_literals(arg))
    return found


def _resolve(annotation: Any) -> Any:
    """A field annotation as a type, resolving ``from __future__`` strings.

    ``dataclasses.fields`` hands back whatever the module wrote down, and
    the SDK's types module uses postponed evaluation, so half the
    annotations are strings. Evaluated against the SDK's own namespace,
    which is where the names it references live.
    """
    if not isinstance(annotation, str):
        return annotation
    sdk = _sdk_module()
    if sdk is None:
        return None
    try:
        return eval(annotation, vars(sdk.types))  # noqa: S307 - SDK's own source
    except Exception:  # noqa: BLE001 - unresolvable annotation is not fatal
        return None


def option_fields() -> list[str]:
    """Field names on the installed ``ClaudeAgentOptions``."""
    sdk = _sdk_module()
    if sdk is None:
        return []
    return [f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)]


def hook_event_names() -> list[str]:
    """Hook event names the installed SDK's ``hooks=`` mapping accepts.

    Read from the annotation rather than from the ``*HookInput`` class
    names, because the accepted *keys* are what a registration is checked
    against — and ``BaseHookInput``/``HookInput`` are not events.
    """
    sdk = _sdk_module()
    if sdk is None:
        return []
    field = {f.name: f for f in dataclasses.fields(sdk.ClaudeAgentOptions)}.get("hooks")
    if field is None:
        return []
    resolved = _resolve(field.type)
    args = typing.get_args(resolved)
    if not args:
        return []
    # dict[keys, list[HookMatcher]] — the key union is the first arg, and
    # only it; flattening the whole annotation would sweep in HookMatcher.
    return sorted(set(_flatten_literals(args[0])))


def message_union_members() -> list[str]:
    """Class names in the SDK's ``Message`` union."""
    sdk = _sdk_module()
    if sdk is None:
        return []
    return [
        getattr(m, "__name__", str(m)) for m in typing.get_args(sdk.Message) or ()
    ]


def client_methods() -> list[str]:
    """Public methods on ``ClaudeSDKClient``."""
    sdk = _sdk_module()
    if sdk is None:
        return []
    return sorted(
        name
        for name in dir(sdk.ClaudeSDKClient)
        if not name.startswith("_") and callable(getattr(sdk.ClaudeSDKClient, name, None))
    )


def beta_values() -> list[str]:
    """Values the installed SDK's ``betas`` option accepts."""
    sdk = _sdk_module()
    if sdk is None:
        return []
    return sorted(set(_flatten_literals(sdk.SdkBeta)))


# ----------------------------------------------------------------------
# What this package handles, read out of its own syntax tree
# ----------------------------------------------------------------------


def _module_source(module_name: str) -> str:
    """Source of a sibling module in this package, or ``""``.

    By path rather than by ``inspect.getsource`` so the probe does not
    import the module it is measuring — ``options`` pulls in the SDK and
    the engine config, and a diagnostic should not have side effects.
    """
    path = Path(__file__).with_name(f"{module_name}.py")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("Could not read %s for surface analysis", path)
        return ""


@functools.cache
def assigned_option_keys() -> frozenset[str]:
    """Option names ``options.py`` assigns, by reading its AST.

    Catches both shapes the module uses: the literal dict it builds
    ``kwargs`` from, and the later ``kwargs["name"] = ...`` assignments
    inside conditionals. Nothing here executes ``build_option_kwargs``,
    so a field set only for a repoless run or only when resuming counts
    exactly like one set unconditionally.

    Deliberately syntax, not text. See this module's docstring for the
    two ways the obvious approaches got it wrong.
    """
    source = _module_source("options")
    if not source:
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - our own module
        logger.warning("Could not parse options.py for surface analysis")
        return frozenset()

    names: set[str] = set()
    for node in ast.walk(tree):
        # kwargs["name"] = value
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "kwargs"
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    names.add(target.slice.value)
        # kwargs: dict[str, Any] = {"name": value, ...}
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            is_kwargs = any(
                isinstance(t, ast.Name) and t.id == "kwargs" for t in targets
            )
            if is_kwargs and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        names.add(key.value)
    return frozenset(names)


@functools.cache
def registered_hook_events() -> frozenset[str]:
    """Hook events ``hooks.py`` registers, by reading its AST.

    Same reasoning as :func:`assigned_option_keys`: the returned mapping's
    literal keys are the registration, and reading them from syntax means
    adding a matcher updates this without touching :data:`HOOK_EVENTS`'s
    status by hand.
    """
    source = _module_source("hooks")
    if not source:
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - our own module
        return frozenset()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "build_hook_matchers":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                return frozenset(
                    key.value
                    for key in inner.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
    return frozenset()


@functools.cache
def dispatched_message_types() -> frozenset[str]:
    """Message classes ``messages.py`` dispatches on.

    The pump is an ``isinstance`` chain, so the class names it names are
    the coverage. Read from syntax for the reason the others are: a
    comment mentioning ``ResultMessage`` is not a branch handling one.
    """
    source = _module_source("messages")
    if not source:
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - our own module
        return frozenset()

    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
        ):
            target = node.args[1]
            candidates = (
                target.elts if isinstance(target, ast.Tuple) else [target]
            )
            for candidate in candidates:
                if isinstance(candidate, ast.Name):
                    names.add(candidate.id)
    return frozenset(names)


@functools.cache
def called_client_methods() -> frozenset[str]:
    """SDK client methods this package calls anywhere.

    Every module in the package, not just ``session.py``: the session
    wraps most calls but the service reaches the client for some, and a
    method called from either is handled. Attribute names are matched
    without resolving the receiver — a false "handled" from an unrelated
    ``.query(`` is possible, and preferable to a false gap, because the
    bucket this feeds is advisory while ``pending`` is a test failure.
    """
    package = Path(__file__).parent
    names: set[str] = set()
    for path in sorted(package.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return frozenset(names)


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


def _never_set() -> dict[str, str]:
    """``options.NEVER_SET``, or ``{}`` if options cannot be imported.

    Imported rather than copied so the refusal and its reason have one
    owner — the module that does the refusing.
    """
    try:
        from ac_dc.claude_code.options import NEVER_SET

        return dict(NEVER_SET)
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("Could not import options.NEVER_SET")
        return {}


def option_report() -> dict[str, Any]:
    """Per-option status, plus anything the installed SDK added."""
    fields = option_fields()
    assigned = assigned_option_keys()
    declined = _never_set()
    entries: list[dict[str, Any]] = []
    for name in fields:
        if name in assigned:
            status, note = HANDLED, ""
        elif name in declined:
            status, note = DECLINED, declined[name]
        elif name in PENDING_OPTIONS:
            status, note = PENDING, PENDING_OPTIONS[name]
        else:
            status, note = PENDING, "not yet triaged — new in this SDK release"
        entries.append({"name": name, "status": status, "note": note})
    classified = assigned | set(declined) | set(PENDING_OPTIONS)
    return {
        "entries": entries,
        "unclassified": sorted(set(fields) - classified),
        # Names we still talk about that the SDK no longer has. options.py
        # already fails startup for these when it sets one, but a declined
        # or pending entry for a removed field is just stale prose.
        "stale": sorted((set(declined) | set(PENDING_OPTIONS)) - set(fields)),
        # Options we now set that a table here still argues about. The
        # entry is harmless — the assignment wins, so the status is
        # `handled` either way — but the *note* is a reason not to do the
        # thing we did, which is worse than no note. `stale` cannot catch
        # these: the field still exists, so it is only ever the prose that
        # went out of date.
        "resolved": sorted((set(declined) | set(PENDING_OPTIONS)) & assigned),
    }


def hook_report() -> dict[str, Any]:
    """Per-hook-event status, cross-checked against the registration."""
    events = hook_event_names()
    registered = registered_hook_events()
    entries: list[dict[str, Any]] = []
    for name in events:
        declared_status, note = HOOK_EVENTS.get(
            name, (PENDING, "not yet triaged — new in this SDK release")
        )
        # The registration is the truth; the table only supplies the
        # reason. A matcher added without updating HOOK_EVENTS reads as
        # handled, and a table claiming "handled" for an event nobody
        # registered reads as pending — neither can quietly overstate
        # coverage.
        if name in registered:
            status = HANDLED
        else:
            status = PENDING if declared_status == HANDLED else declared_status
        entries.append({"name": name, "status": status, "note": note})
    return {
        "entries": entries,
        "unclassified": sorted(set(events) - set(HOOK_EVENTS)),
        "stale": sorted(set(HOOK_EVENTS) - set(events)),
        # A table that claims "handled" for something unregistered is a
        # documentation bug, and the only one here that could mislead a
        # reader into thinking a gap is covered.
        "claimed_unregistered": sorted(
            name
            for name, (status, _) in HOOK_EVENTS.items()
            if status == HANDLED and name not in registered and name in set(events)
        ),
    }


def message_report() -> dict[str, Any]:
    """Union membership versus what the pump dispatches on."""
    members = message_union_members()
    dispatched = dispatched_message_types()
    return {
        "entries": [
            {
                "name": name,
                "status": HANDLED if name in dispatched else PENDING,
                "note": "" if name in dispatched else "in the SDK's Message union "
                "but no isinstance branch in messages.py",
            }
            for name in members
        ],
        "unclassified": sorted(set(members) - dispatched),
        # Classes the pump handles that are not in the union — not a gap,
        # and worth showing so the count is not read as over-coverage.
        # The Task*/HookEvent/MirrorError messages arrive on the same
        # stream without being union members.
        "beyond_union": sorted(
            name
            for name in dispatched
            if name.endswith(("Message", "Event", "Block")) and name not in members
        ),
    }


def client_report() -> dict[str, Any]:
    """Client methods versus the ones this package calls."""
    methods = client_methods()
    called = called_client_methods()
    entries: list[dict[str, Any]] = []
    for name in methods:
        if name in called:
            status, note = HANDLED, ""
        elif name in DECLINED_CLIENT_METHODS:
            status, note = DECLINED, DECLINED_CLIENT_METHODS[name]
        else:
            status, note = PENDING, "no call site in ac_dc.claude_code"
        entries.append({"name": name, "status": status, "note": note})
    return {
        "entries": entries,
        "unclassified": sorted(
            set(methods) - called - set(DECLINED_CLIENT_METHODS)
        ),
    }


def beta_report() -> dict[str, Any]:
    """Beta gates the SDK offers. ``betas`` is unset, so none are on.

    Separated from :func:`option_report` because a beta list that grows is
    the single most likely place a *feature* appears — the option's own
    status never changes, but its accepted values do.
    """
    values = beta_values()
    enabled = "betas" in assigned_option_keys()
    entries = []
    for value in values:
        if enabled:
            status, note = HANDLED, ""
        elif value in KNOWN_BETAS:
            status, note = DECLINED, KNOWN_BETAS[value]
        else:
            status, note = PENDING, "new beta gate — triage it into KNOWN_BETAS"
        entries.append({"name": value, "status": status, "note": note})
    return {
        "entries": entries,
        # A known-and-declined beta is not untriaged surface. Only a value
        # we have never seen belongs in the bucket that fails the gate.
        "unclassified": sorted(set(values) - set(KNOWN_BETAS)),
        "stale": sorted(set(KNOWN_BETAS) - set(values)),
    }


def diff_server_info(server_info: Any) -> dict[str, Any]:
    """What the live CLI advertises that this build does not account for.

    The complement to every other function here, and the reason the probe
    is not purely static: commands, tools and output styles are the CLI's,
    and the CLI ships independently of the Python wheel. A slash command
    added there appears in no dataclass — only in this payload.

    Takes the payload rather than fetching it: ``get_server_info`` needs a
    connected session, and every other function in this module works
    without one. Passing it in keeps the whole probe callable from a test
    with no engine, which is where it has to run to be a useful gate.

    ``None`` or an unexpected shape yields empty lists rather than an
    error — a report of the *static* surface is still worth having when
    the engine is down, which is exactly when someone is reading this.
    """
    if not isinstance(server_info, dict):
        return {"available": False, "commands": [], "tools": [], "output_styles": []}

    def _names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        names = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str):
                    names.append(name)
        return sorted(set(names))

    return {
        "available": True,
        "commands": _names(server_info.get("commands")),
        "tools": _names(server_info.get("tools")),
        "output_styles": _names(
            server_info.get("output_styles") or server_info.get("outputStyles")
        ),
    }


def _versions() -> dict[str, str]:
    """Version facts, reusing ``health``'s readers rather than re-deriving.

    ``health`` already owns the private-module reads and their fallbacks
    (``_cli_version``, ``MINIMUM_CLAUDE_CODE_VERSION``); duplicating them
    here would be a second thing to fix when a private name moves.
    """
    try:
        from ac_dc.claude_code import health

        return {
            "sdk_version": health.sdk_version(),
            "sdk_cli_pin": health.sdk_cli_pin(),
            "minimum_cli_version": health.minimum_cli_version(),
        }
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("Could not read version facts from health")
        return {
            "sdk_version": "unknown",
            "sdk_cli_pin": "unknown",
            "minimum_cli_version": "unknown",
        }


def surface_report(server_info: Any = None) -> dict[str, Any]:
    """The whole probe, as the dict the browser and the test both read.

    One shape for both callers on purpose. A test that asserts on a
    different structure than the tab renders would let the tab go wrong
    while staying green.
    """
    sections = {
        "options": option_report(),
        "hooks": hook_report(),
        "messages": message_report(),
        "client": client_report(),
        "betas": beta_report(),
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
        if section.get("unclassified")
    }
    return {
        "sdk_available": _sdk_module() is not None,
        "versions": _versions(),
        "sections": sections,
        "counts": counts,
        #: Everything the gate fails on, gathered so the test and the tab
        #: agree on what "needs triage" means.
        "unclassified": unclassified,
        "cli": diff_server_info(server_info),
    }
