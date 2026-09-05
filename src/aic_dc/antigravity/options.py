"""Config assembly for the Antigravity engine.

The analogue of ``claude_code/options.py``: one place that turns AIC⚡DC's
own settings into the SDK's ``LocalAgentConfig``, so that every field the
engine sets — and every field it refuses to set — has a reason recorded
beside the code that sets it.

Split in two on purpose, the same way the Claude side is split.
:func:`build_config_kwargs` is a pure dict builder that never imports
``google.antigravity``; :func:`build_config` is the one line that
constructs. That is what lets the whole of this module's policy be tested
on a machine with no SDK and no 119 MB binary (AG-R-10), which is most of
what there is to get wrong here.

The write seam
--------------
:data:`MUTATING_TOOLS` is the AG-5 boundary and the reason this module
exists before the session does. Phase 2 measured an agent that, refused an
``edit_file``, went after the same change with ``sed -i`` through
``run_command`` — unprompted, on both runs (AG-R-11). So ``run_command``
is gated with the same standing as the file tools, from the adapter's
first line rather than retrofitted once somebody notices.

**The SDK's own ``nondestructive()`` is not that boundary and must not be
mistaken for it.** ``BuiltinTools.nondestructive()`` returns everything
except ``run_command`` — it counts ``create_file``, ``edit_file`` and
``generate_image`` as nondestructive, which is defensible for a
"will this hurt the machine" reading and is exactly backwards for
"will this change the working tree". Borrowing it would enable the two
tools the permission dialog exists for. :data:`MUTATING_TOOLS` is
therefore ours, and ``test_nondestructive_is_not_our_write_boundary``
pins the difference so an SDK release that redefines it is a red test
rather than a silent ungating.

A session enables a mutating tool only when it was handed a decide hook to
gate it with. There is no flag that opts out of that, because a flag is
the thing somebody sets while debugging and forgets.

Governing spec: ``specs5/plan-ag/`` — AG-5, AG-6, AG-10, AG-R-11.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aic_dc.antigravity.credentials import Credentials

logger = logging.getLogger(__name__)

#: The model a session runs on, pinned rather than inherited.
#:
#: The SDK's own default moves between 0.1.x releases, and an engine whose
#: model changed under it reports a different conversation than the one the
#: user started. Same reasoning as the consultant's pin, and deliberately a
#: separate constant: a consultation and a session are billed and chosen
#: independently, and collapsing them would make changing one change both.
DEFAULT_MODEL = "gemini-3.7-flash"

#: Steps the ``Conversation`` keeps before it starts discarding the oldest.
#:
#: The SDK's default is a bounded ring buffer, which is right for a library
#: and wrong for a transcript: AIC⚡DC's chat renders the whole turn and its
#: history browser renders the whole session. The pump is what mirrors
#: steps out (phase 5), so the number here only has to outlive one turn's
#: rendering — but a turn can be hundreds of steps, and truncating history
#: mid-turn would lose tool cards whose results had not arrived yet.
MAX_HISTORY_SIZE = 0

#: Tools that can change the working tree, the machine, or the bill.
#:
#: The permission seam of AG-5, as tool-name strings so this table is
#: readable — and testable — without the SDK's enum. ``build_config``
#: resolves them against ``BuiltinTools`` at construction, which is also
#: what makes a name that stops existing fail loudly.
#:
#: ``start_subagent`` is here because a subagent inherits the tool set: a
#: gate that stopped at the top-level trajectory would be bypassed by
#: asking a child to do the write. ``generate_image`` is here because it
#: writes a file. ``ask_question`` is not — it blocks on the user, which is
#: a UI event rather than a mutation, and phase 4 gives it a surface.
MUTATING_TOOLS = frozenset(
    {
        "create_file",
        "edit_file",
        "run_command",
        "generate_image",
        "start_subagent",
    }
)

#: What a session enables when nothing has been gated yet.
#:
#: Deliberately the SDK's own ``read_only()`` set rather than a copy: this
#: is the one place where inheriting the SDK's judgement is right, because
#: a *new* read-only tool in a later release should become available
#: without an edit here, while a new *write* tool must not. The asymmetry
#: is the point — :data:`MUTATING_TOOLS` is a denylist we own and this is
#: an allowlist we borrow, and each is the safe direction for its side.
READ_ONLY_SENTINEL = "read_only"

# Config fields AIC-DC must never set, with the reason, so a future reader
# who is tempted gets the argument rather than a bare prohibition.
#
# Moved here from ``surface.NEVER_SET_CONFIG`` in phase 3, which is where
# it was always headed: ``surface._declined_config`` has preferred this
# module since phase 1 and starts reading it with no other edit. The
# refusal now sits beside the code doing the refusing, which is the
# ownership ``claude_code.options.NEVER_SET`` has.
NEVER_SET = {
    "response_schema": "forces the model's output into a JSON schema. A "
    "host-level feature for programmatic callers; AIC-DC renders prose and "
    "tool calls to a human, and a turn that can only answer in a schema "
    "cannot hold a conversation.",
    "env": "environment for the spawned localharness process. AG-11 is "
    "explicit that the credential is passed as a config field and never "
    "written into an environment; a general-purpose env passthrough is the "
    "hole that puts it back, since GEMINI_API_KEY is exactly what the "
    "binary reads from there. A proxy or a non-default endpoint is a "
    "reason to add a narrow field, not this one.",
}


def build_config_kwargs(
    *,
    repo_root: Path | str,
    credentials: Credentials,
    model: str = DEFAULT_MODEL,
    decide_hook: Any = None,
    tools: tuple[Any, ...] = (),
    write_tools: frozenset[str] | None = None,
    resume: str | None = None,
) -> dict[str, Any]:
    """The keyword arguments for a ``LocalAgentConfig``, as plain data.

    No SDK import and no construction, so a test can assert on what the
    engine *would* ask for without a binary on the machine. The two
    non-obvious returns are ``enabled_tools`` — a list of tool-name
    strings, resolved to enum members by :func:`build_config` — and the
    absence of ``policies``, which is explained below.

    Parameters
    ----------
    repo_root:
        The single workspace root (AG-10). Not a list, not configurable:
        cwd is what the diff viewer, the file tree and every tool path
        resolve against, and a configuration with more roots than the
        product understands is one where "the agent says it edited the
        file and the diff is empty" is diagnosable only by reading
        somebody else's settings file.
    decide_hook:
        The ``PreToolCallDecideHook`` that gates writes. **Without one, no
        mutating tool is enabled at all** — not gated-then-denied, but
        absent from ``enabled_tools``, so there is no posture in which a
        write reaches the model with nothing between it and the disk.
        Phase 4 supplies the real hook; phase 3 runs read-only.
    write_tools:
        Which mutating tools to enable. Defaults to all of
        :data:`MUTATING_TOOLS` when a hook was supplied and to none when
        it was not. Present so the session can *narrow* the set — a
        review session with no shell, say — and never to widen it:
        anything outside :data:`MUTATING_TOOLS` has no gate and is
        refused.
    resume:
        A conversation id to continue (phase 5). Emitted as
        ``conversation_id`` plus ``session_continuation_mode: "resume"``,
        which is the SDK's own pair — ``AgentConfig`` refuses ``RESUME``
        with no id, so they are set together or not at all. **``RESUME``
        rather than ``CREATE_OR_RESUME``**: the third mode would answer a
        request to continue a conversation by silently starting a new one,
        which is the failure ``connect_engine`` refused to commit before
        this was built. ``save_dir`` is deliberately left unset, so the
        harness reads back the trajectory store it wrote the session into;
        pointing it somewhere of ours would make every existing
        conversation unresumable.

    Raises
    ------
    ValueError
        If a write tool was asked for with no hook to gate it, or if the
        requested set is not a subset of :data:`MUTATING_TOOLS`. Both are
        programming errors and both fail here rather than at connect,
        because the SDK's own refusal (``agent.py:93-103``) fires on
        *policy absent*, which a stray ``policy.allow_all()`` would
        satisfy while giving away everything this checks.
    """
    root = Path(repo_root).resolve()
    # The default is "everything the hook can gate", which with no hook is
    # nothing at all. Written this way round rather than defaulting to the
    # full set and rejecting it, because a read-only session is the
    # ordinary phase-3 case and it should not have to pass an empty
    # frozenset to say so. An *explicit* write_tools with no hook is still
    # an error: that one is somebody asking for a write and not noticing
    # it had no gate.
    if write_tools is None:
        requested = MUTATING_TOOLS if decide_hook is not None else frozenset()
    else:
        requested = frozenset(write_tools)

    unknown = requested - MUTATING_TOOLS
    if unknown:
        raise ValueError(
            f"{', '.join(sorted(unknown))} is not in MUTATING_TOOLS. A tool "
            "the write seam does not name has no gate; add it to "
            "MUTATING_TOOLS with a reason rather than passing it here."
        )
    if requested and decide_hook is None:
        raise ValueError(
            f"Write tools were requested ({', '.join(sorted(requested))}) "
            "with no decide hook to gate them. AG-5 makes the permission "
            "dialog a requirement of this engine, not an option, and "
            "AG-R-11 is the measurement that says gating the file tools "
            "alone is not a boundary."
        )

    enabled = [READ_ONLY_SENTINEL, *sorted(requested)]

    kwargs: dict[str, Any] = {
        "model": model,
        # AG-10: one root, and only one. The SDK would default this to
        # os.getcwd(), which is the same value today and is not a promise.
        "workspaces": [str(root)],
        "capabilities": {
            "enabled_tools": enabled,
            # AG-9 in the small: INTERACTIVE is what turns on planning
            # mode and slash commands. AUTONOMOUS is the SDK's default and
            # is the wrong posture for a UI with a human in it — it is the
            # setting that decides whether the agent expects to be
            # interrupted.
            "agent_behavior": "INTERACTIVE",
        },
    }
    if decide_hook is not None:
        # Where AG-5's gate attaches. The raw hook rather than
        # policy.ask_user, which returns a bare bool and would give away
        # both the message the model reads and the ability to amend a call
        # before it runs.
        kwargs["hooks"] = [decide_hook]
    if tools:
        # AG-4: the symbol and document indexes as plain callables. No MCP
        # server, no transport, no lifecycle — the SDK derives schemas from
        # signatures.
        kwargs["tools"] = list(tools)
    if resume:
        # Set as a pair, because the SDK validates them as one: RESUME
        # with no id raises at construction. The string rather than the
        # enum member keeps this function free of the SDK import, which is
        # the whole reason it is split from `build_config`; the enum is a
        # `str` subclass, so the value is what it compares equal to.
        kwargs["conversation_id"] = resume
        kwargs["session_continuation_mode"] = "resume"

    kwargs.update(credentials.config_kwargs())
    return kwargs


def build_config(**kwargs: Any) -> Any:
    """Construct the SDK's ``LocalAgentConfig`` from :func:`build_config_kwargs`.

    The only function here that imports the SDK, and it does so inside the
    body: these modules must stay importable where ``google-antigravity``
    is not installed, because it ships a bundled 119 MB Go binary and a
    base install is a one-engine install (AG-R-10).

    Two things are resolved here rather than in the kwargs builder,
    because both need the SDK's own enums and both should fail loudly if a
    release moves them:

    - ``enabled_tools``, from names to ``BuiltinTools`` members, expanding
      :data:`READ_ONLY_SENTINEL` to the SDK's ``read_only()`` set.
    - ``policies``, which is **not** optional and is why this function
      exists at all rather than a bare splat.

    **Leaving ``policies`` unset is not "no policy".** ``LocalAgentConfig``
    defaults it to ``policy.confirm_run_command()`` — deny ``run_command``,
    *approve everything else* — which is the blanket-bypass posture AG-5
    says must never reach a shipped path, arriving as a default nobody
    chose. Restricting ``enabled_tools`` makes that inert today, and
    relying on it is precisely the layered assumption that stops being
    true the first time somebody adds a tool. So the allowlist is set on
    every config, exactly as the consultant sets it, and it is a
    capability restriction rather than a permission decision: the
    permission decision is the decide hook's, per call, with the user in
    the loop.
    """
    from google.antigravity import LocalAgentConfig, types
    from google.antigravity.hooks import policy

    kwargs = dict(kwargs)
    capabilities = dict(kwargs.pop("capabilities", {}))
    enabled = _resolve_tools(capabilities.pop("enabled_tools", []), types)
    behavior = getattr(
        types.AgentBehavior, capabilities.pop("agent_behavior", "INTERACTIVE")
    )

    return LocalAgentConfig(
        # Both capability fields named rather than splatted, so the AG-8
        # probe reads them off this call's syntax tree and reports them as
        # handled. A `**capabilities` splat works identically at runtime
        # and is invisible to the probe, which would leave the drift gate
        # arguing that a field we set is unwired.
        capabilities=types.CapabilitiesConfig(
            enabled_tools=enabled,
            agent_behavior=behavior,
            **capabilities,
        ),
        policies=[policy.deny_all(), *(policy.allow(t.value) for t in enabled)],
        **kwargs,
    )


def _resolve_tools(names: list[str], types: Any) -> list[Any]:
    """Tool-name strings to ``BuiltinTools`` members, deduplicated.

    ``read_only`` expands to the SDK's own set rather than to a copy of it,
    which is the asymmetry :data:`READ_ONLY_SENTINEL` documents: a new
    read-only tool arrives with an SDK bump, a new write tool does not.

    ``FINISH`` is added unconditionally. It is already inside
    ``read_only()``, so this is belt-and-braces against a release that
    reclassifies it — an agent with no way to end its turn does not fail,
    it runs until a budget cap stops it.
    """
    resolved: list[Any] = [types.BuiltinTools.FINISH]
    seen = {types.BuiltinTools.FINISH}
    for name in names:
        members = (
            sorted(types.BuiltinTools.read_only(), key=lambda t: t.value)
            if name == READ_ONLY_SENTINEL
            else [types.BuiltinTools(name)]
        )
        for member in members:
            if member not in seen:
                seen.add(member)
                resolved.append(member)
    return resolved
