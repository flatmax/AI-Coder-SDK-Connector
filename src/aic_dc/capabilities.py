"""The capability descriptor: which surfaces each engine can feed.

``specs5/plan-ag/decisions.md`` AG-3 and AG-9. Both engines mount under one
RPC namespace, so the browser's call sites do not fork; what tells the
webapp that a panel has no data on the running engine is this table, and
**a surface with no counterpart is hidden rather than drawn empty**.

Why hiding rather than stubbing, restated because it is the whole point
====================================================================
An empty list does not say *"no servers"*, it says *"no answer"* — the
lesson the deleted ``EngineHealth.mcp`` field left behind. A Context tab
drawing a 0% bar for an engine that cannot report its context window is
worse than one that draws no bar, because the first is a measurement and
the second is an absence. A number on screen is believed.

So this module exists to let the browser ask "should I render this at
all?" and get an answer, and to make an *unanswered* question loud rather
than quiet — :func:`supports` raises on a key it does not know, instead of
returning ``False`` and hiding a panel nobody meant to hide.

Absent is not the same as unbuilt
=================================
Two reasons a surface is hidden, and collapsing them would throw away the
thing that makes phase 6 tractable:

- :data:`ABSENT` — the engine has no source data and never will. USD cost
  on Antigravity is absent: there is no dollar figure anywhere on the SDK
  or on ``agy``'s wire, and the only route to one is a price table AIC⚡DC
  would maintain and that would go stale silently (AG-6).
- :data:`UNBUILT` — the data exists and nothing reads it yet. Antigravity's
  transcript history is unbuilt: ``Step`` carries everything needed, flat
  with ``trajectory_id`` and ``depth``, and no renderer has been written.

The webapp hides both identically. The difference is for us: ``ABSENT`` is
a decision and ``UNBUILT`` is a to-do, and
``specs5/plan-ag/README.md``'s ordering constraint — *"every phase from 3
onward must record which surfaces it could not serve, otherwise phase 6 is
an archaeology exercise"* — is discharged by keeping them apart while the
reason is still fresh.

What is deliberately not described
==================================
**A surface neither engine serves is not in this table.** AG-9: *"A
surface that is hidden on both engines is dead code and should be deleted
rather than described."* That cuts, from ``sdk-surface.md`` §
*Antigravity capabilities with no home in the current UI*, structured
output, audio and video input, daemon commands, ``triggers`` and
multi-model routing — all real SDK capabilities with no AIC⚡DC surface at
either end. Describing them would be describing a UI that does not exist.

**A surface both engines serve is not in this table either.** Chat,
cancel, the diff viewer, the file tree, the permission dialog: an entry
that is always ``SUPPORTED`` is a browser branch that is never taken, and
a table of them would rot without anybody noticing, because nothing reads
it. The descriptor covers the surfaces where the engines *differ*, which
is the only place a decision has to be made.

Governing spec: ``specs5/plan-ag/`` — AG-3, AG-6, AG-9, AG-R-4, AG-R-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: This engine feeds this surface today.
SUPPORTED = "supported"

#: This engine has no source data for it and never will. A decision.
ABSENT = "absent"

#: The data exists; nothing reads it yet. A to-do, with the phase named.
UNBUILT = "unbuilt"

#: The two engine identifiers. Strings rather than an enum because they
#: cross the RPC boundary as JSON, and because the browser must never
#: branch on them — AG-R-4 makes the *descriptor* the thing the webapp
#: keys off, never the engine's name. They appear here so the server can
#: look a row up, and :func:`descriptor` deliberately does not send them.
CLAUDE = "claude"
ANTIGRAVITY = "antigravity"
#: The ``agy`` transport (AG-14). The *same product* as ``ANTIGRAVITY``,
#: reached through the CLI on the owner's Google subscription rather than
#: through the SDK on a metered API key. It is a separate identifier
#: because a session runs on one or the other and they differ in what they
#: can feed — not because it is a third engine.
AGY = "agy"

#: What the engine selector calls each one. **Supplied by the server**,
#: because a label map in the webapp would be a branch on an engine name,
#: which is exactly what AG-R-4 forbids — and because the thing a user is
#: choosing between here is *which account pays*, which only this side
#: knows.
ENGINE_LABELS: dict[str, str] = {
    CLAUDE: "claude",
    ANTIGRAVITY: "antigravity (API key)",
    AGY: "antigravity (subscription)",
}


@dataclass(frozen=True)
class Surface:
    """One UI or RPC surface, and what each engine can do about it.

    ``note`` is per-engine prose explaining a non-supported status. It is
    for a developer reading the descriptor, not for the browser to render:
    a UI that displays "this engine cannot report USD cost" where the
    panel used to be has replaced a measurement with an excuse, which is
    the failure AG-9 is about. The webapp reads ``status`` and nothing
    else.
    """

    key: str
    title: str
    claude: str
    antigravity: str
    note: str = ""
    #: The ``agy`` transport's status, when it differs from the SDK's.
    #:
    #: ``None`` means *the same as* ``antigravity``, and that default is
    #: the honest one: both reach the same product, so a surface the SDK
    #: cannot feed is one Antigravity cannot feed, whichever way it is
    #: driven. Only where the *transport* changes the answer does this
    #: carry a value — which today is the transcript surfaces, because
    #: ``agy`` writes a full conversation log to disk and the SDK does not.
    agy: str | None = None

    def status_for(self, engine: str) -> str:
        if engine == CLAUDE:
            return self.claude
        if engine == AGY and self.agy is not None:
            return self.agy
        return self.antigravity


#: Every surface where the two engines differ.
#:
#: Sourced from ``sdk-surface.md`` § *What does not translate*, both
#: directions, and checked against it by
#: ``test_every_surface_in_the_spec_has_an_entry``. Adding a per-engine
#: feature means adding its key here in the same commit (AG-9).
SURFACES: tuple[Surface, ...] = (
    Surface(
        key="account_rate_limits",
        title="Account rate-limit windows",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="Anthropic-specific REST endpoint (GET /api/oauth/usage). "
        "Google exposes no per-account window; the nearest signal is "
        "StopReason.QUOTA_EXHAUSTED, which says the account ran out "
        "rather than how much is left.",
    ),
    Surface(
        key="usd_cost",
        title="USD cost — turn footer, session cost, max_budget_usd",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="AG-6. There is no dollar figure anywhere on the SDK or on "
        "agy's wire; UsageMetadata is tokens only and BudgetConfig caps "
        "calls and tokens, never dollars. A price table AIC-DC "
        "maintained would go stale silently and be believed.",
    ),
    Surface(
        key="context_window_usage",
        title="Live context-window usage and compaction threshold",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="AG-R-5. compaction_threshold is a number you set, not a "
        "window you can query, so there is no read-back at all. The "
        "cache-hit fraction is offered instead, and is a different "
        "measurement rather than a substitute.",
    ),
    Surface(
        key="slash_commands",
        title="Slash-command palette",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="BuiltinSlashCommandName has exactly one member, PLAN. Near-"
        "total loss rather than total, and a one-item palette is worse "
        "than none.",
    ),
    Surface(
        key="persisted_permission_rules",
        title='"Always allow" — permission rules that outlive the call',
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="AG-5's one genuine loss. updated_permissions has no "
        "counterpart at any layer, so the gate offers no suggested_rules "
        "and an always-allow degrades to allow-once. AIC-DC would have "
        "to own the rule store to change this.",
    ),
    Surface(
        key="amend_tool_input",
        title="Amend a tool's input before approving it",
        claude=SUPPORTED,
        antigravity=SUPPORTED,
        note="Present on both, and listed because it is the capability "
        "AG-5 chose the raw PreToolCallDecideHook over policy.ask_user "
        "to keep — ask_user returns a bare bool and would have given it "
        "away permanently. Recoverable via HookResult.modified_args.",
    ),
    Surface(
        key="mcp_server_inventory",
        title="MCP server list and health",
        claude=SUPPORTED,
        antigravity=UNBUILT,
        note="AG-4 routes AIC-DC's own indexes to Antigravity as plain "
        "callables, so the in-process bridge does not port and is not "
        "needed. User-configured stdio and streamable-HTTP servers are "
        "supported by the SDK and have no settings surface yet.",
    ),
    Surface(
        key="session_mirror",
        title="Repo-local verbatim session mirror",
        claude=SUPPORTED,
        antigravity=UNBUILT,
        note="Phase 5. There is no SessionStore protocol to implement — "
        "Antigravity owns an opaque save_dir — so the mirror is rebuilt "
        "as a step observer rather than as a store.",
    ),
    Surface(
        key="transcript_history",
        title="History browser and transcript rendering",
        claude=SUPPORTED,
        antigravity=UNBUILT,
        note="Phase 5. Step is flat, with trajectory_id and depth, rather "
        "than nested content blocks, so history.py needs a full sibling "
        "rather than a branch.",
    ),
    Surface(
        key="rate_limit_events",
        title="Mid-turn rate-limit and conversation-reset notices",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="RateLimitEvent and ConversationResetMessage are CLI "
        "message types. The SDK's retry_config absorbs a 429 invisibly, "
        "so there is nothing to report mid-turn — measured in phase 1, "
        "where a 503 was retried through without the caller seeing it.",
    ),
    Surface(
        key="subagent_tabs",
        title="Subagent rows and their own tabs",
        claude=SUPPORTED,
        antigravity=UNBUILT,
        note="Every Step carries trajectory_id, parent_trajectory_id and "
        "depth, and usage is per-trajectory, so this is buildable. The "
        "pump already attributes a nested trajectory to an agent_id; "
        "what is missing is a chat that renders more than one.",
    ),
    Surface(
        key="agent_questions",
        title="Agent-initiated structured questions",
        claude=SUPPORTED,
        antigravity=UNBUILT,
        note="Antigravity's ask_question is richer than Claude's — option "
        "IDs and multi-select — and reaches the permission gate as an "
        "'interact' call. The dialog's question payload is not built for "
        "it yet, so the tool stays disabled rather than half-rendered.",
    ),
    Surface(
        key="file_checkpointing",
        title="Undo an agent's file changes back to a message",
        claude=SUPPORTED,
        antigravity=ABSENT,
        note="``rewind_files`` is the Claude SDK's own checkpointing, and "
        "Antigravity has no counterpart at any layer — no checkpoint, no "
        "restore, nothing to build one from. Git already covers most of "
        "what it would undo, which is why this is absent rather than a "
        "gap worth filling.",
    ),
    Surface(
        key="image_generation",
        title="Generated images",
        claude=ABSENT,
        antigravity=SUPPORTED,
        note="AG-1's worked example and the reason for a second engine: a "
        "thing one engine can do and the other cannot. Reachable from "
        "Claude as a consultant tool (AG-7). Note that a free-tier key "
        "reports limit: 0 for every image model, so this is supported by "
        "the engine and gated by the account (AG-12).",
    ),
)

_BY_KEY = {surface.key: surface for surface in SURFACES}

#: Engines this module knows how to answer for.
ENGINES = (CLAUDE, ANTIGRAVITY, AGY)


class UnknownSurfaceError(KeyError):
    """A surface nobody declared was asked about.

    Raised rather than answered, because the alternative is returning
    ``False`` for a typo and silently hiding a panel — an absence that
    looks exactly like a deliberate one. This is the same failure AG-9 is
    written against, one layer up: an unanswered question must not be
    mistaken for a negative answer.
    """


def supports(engine: str, key: str) -> bool:
    """Whether ``engine`` can feed the surface ``key`` today.

    ``UNBUILT`` and ``ABSENT`` both answer ``False``: the browser hides a
    surface it has no data for, and *why* there is no data is not the
    browser's business. The distinction is preserved in
    :func:`descriptor` for the people who have to build the missing half.
    """
    surface = _BY_KEY.get(key)
    if surface is None:
        raise UnknownSurfaceError(
            f"No surface {key!r} is declared. Add it to SURFACES with a "
            f"status for both engines, or fix the caller: guessing False "
            f"here would hide a panel and look deliberate."
        )
    if engine not in ENGINES:
        raise UnknownSurfaceError(f"No engine {engine!r} is declared.")
    return surface.status_for(engine) == SUPPORTED


def descriptor(engine: str) -> dict[str, Any]:
    """The whole capability map for one engine, as the webapp reads it.

    Keyed by surface, each entry carrying ``supported`` for the browser
    and ``status``/``note`` for a developer. The engine's own name is
    **not** in the payload: AG-R-4 requires that no webapp branch keys off
    an engine name string, and the surest way to hold that line is to give
    the browser nothing to branch on.
    """
    if engine not in ENGINES:
        raise UnknownSurfaceError(f"No engine {engine!r} is declared.")
    return {
        surface.key: {
            "title": surface.title,
            "supported": surface.status_for(engine) == SUPPORTED,
            "status": surface.status_for(engine),
            "note": surface.note,
        }
        for surface in SURFACES
    }


def hidden_surfaces(engine: str) -> list[str]:
    """Surface keys this engine cannot feed. Sorted, for a stable UI."""
    return sorted(key for key in _BY_KEY if not supports(engine, key))


def unbuilt_surfaces(engine: str) -> list[str]:
    """Surfaces that are missing rather than impossible — the to-do list.

    Not read by the browser. This is what stops phase 6 being an
    archaeology exercise: the difference between "we decided against
    this" and "nobody has got to it" is recorded while the reason is
    still known.
    """
    return sorted(
        key
        for key, surface in _BY_KEY.items()
        if surface.status_for(engine) == UNBUILT
    )
