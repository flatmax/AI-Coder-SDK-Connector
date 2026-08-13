# Context Tab and Usage HUD

Two surfaces onto the same question — *what does the agent currently know, and what is it costing?* The
**Context tab** answers it persistently, in three sections. The **Usage HUD** answers it transiently,
once per turn, in a floating overlay. A reduced **terminal HUD** prints the same turn summary
server-side.

The data behind both is the engine's own accounting: `get_context_usage()` for composition,
`ResultMessage` for per-turn usage. AC⚡DC does not model context. The contract, the field list, and the
rationale are in [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md); this file
specifies the components.

## The Shift This File Records

The old version of this spec was mostly about our own cache. Two sub-views, a Budget / Cache pill
toggle, tier bars in an L0–L3 palette, per-item N/threshold stability bars, promotion and demotion logs,
synthetic `meta:` rows, an uncached synthetic tier, a fuzzy filter and sort over tier contents, a
click-to-view map-block modal, and a manual rebuild button. All of it described AC⚡DC's belief about a
prompt AC⚡DC assembled.

None of that has a referent now, and the replacement is not a lesser version of it — it is the thing the
old tab was an approximation *of*. Where the old tab could drift from what the provider actually
received with no way to detect the drift from inside the app, this one renders what the engine says
about itself. It also answers questions the old tab structurally could not: what `CLAUDE.md` costs, what
each MCP server costs, what the system prompt is made of, which model did which part of a turn.

The one real loss is per-file token attribution for selected files, which followed the file-selection
contract out the door (see
[decisions § CC-14](../plan/decisions.md#cc-14--file-selection-becomes-a-hint-not-a-context-contract)).
Files enter context because the agent read them, and `categories` reports the aggregate rather than a
per-path split.

## Shared Backend Access

Both surfaces reach the engine through the same two RPCs, and neither computes anything of its own
beyond percentages and proportional widths:

- `get_context_usage()` — the composition snapshot. One round trip; no polling.
- Per-turn usage arrives *pushed*, on `streamComplete`, so the HUD renders without a fetch.

There is no shared "breakdown" RPC, no file-context synchronisation step before a read, and no
per-context-manager targeting parameter. Those existed because the breakdown was computed from our
in-memory state and had to be made current first; the engine's snapshot is current by construction.

Subagents do not get their own snapshot. `get_context_usage()` reports the session, and a subagent's
consumption appears in its own row and in the Session section's inventory. The old spec's
"breakdown dispatches by context manager identity" contract is gone with the context managers.

## Context Tab

Three sections, selected by a segmented control at the top: **Usage**, **Session**, **Debug**. Active
section persisted to `localStorage`. All three share the stale-detection and refresh-on-visible
behaviour below.

### Refresh Triggers

The tab subscribes to the window events that mean "the snapshot may have changed" and calls its refresh
on each, regardless of visibility — the tab should be current the moment the user switches to it, not
after a deferred fetch resolves.

| Event | Why it triggers a refresh |
|---|---|
| `post-response-complete` | A turn just changed the context. This is the authoritative post-turn trigger |
| `compaction-event` (stage `compact_boundary`) | The context just shrank — the most interesting moment to show |
| `session-changed` | Resume or fork; the whole snapshot is different |
| `mcp-status-changed` | Tool inventory and its token cost changed |
| `permission-mode-changed` | Cheap, and the Session section shows the posture |
| Tab becoming visible while stale | The user is looking now |
| Manual refresh button | Explicit request |

`stream-complete` is deliberately **not** a trigger. It fires before post-turn housekeeping and before
the engine's own accounting settles, so a refresh on it reads a snapshot that is about to change. The
HUD covers the immediate post-turn moment from pushed data; the tab waits for
`postResponseComplete`, which carries a fresh `context_usage` payload it can adopt without a fetch at
all.

`files-changed` is no longer a trigger. Selection does not alter context.

### Refresh Queue

A naive "skip when already loading" guard silently drops overlapping triggers, leaving the tab showing
pre-turn state with no indication it is stale. The tab instead tracks a pending flag: when a refresh is
in flight, an incoming trigger sets the flag and returns; the in-flight fetch's completion path checks
the flag and re-fires once.

Any number of overlapping triggers collapse into "one current fetch plus at most one queued fetch",
which is sufficient because the queued fetch always reads the latest state. This behaviour is retained
verbatim from the old tab — the mechanism it protected changed, the bug it fixed did not.

### Usage Section

- **Budget gauge** — `totalTokens` against `maxTokens`, coloured green / amber / red by percentage, with the **auto-compact threshold marked on the bar**. The mark is the point of the gauge: "68% full" does not answer "am I about to be compacted?", and a marked threshold does.
- **Category bar** — one stacked proportional bar from `categories`, in the engine's own colours, with a legend below carrying label and token count per category. Only non-zero categories appear. Deferred categories are labelled as deferred rather than silently counted, because a deferred category is not occupying context right now.
- **Message breakdown** — conversation composition from `messageBreakdown`.
- **Compaction forecast** — when auto-compact is enabled, the distance to the threshold in tokens and as a percentage; when it is disabled, a plain note saying so, because a disabled auto-compact changes what the gauge means.

Categories are not expandable. The engine reports category totals, not per-item membership, and a
disclosure triangle that opens onto nothing is worse than no triangle.

### Session Section

What this session is made of and what each part costs. This is the section that answers the question
the old tab could not, and it is mostly a set of tables:

| Group | Rows | Interaction |
|---|---|---|
| Memory files | Each `CLAUDE.md` / memory file with its token cost | Click opens it in the viewer |
| System prompt sections | What the engine prepended, per section, with size | Click expands the section's text where the payload carries it |
| Tools | Built-in, deferred built-in, and per-MCP-server groups with token cost and health | Click a server for its status detail |
| Agents / skills / slash commands | The inventory available to this session | Click an agent or skill to open its defining file |

Two deliberate details:

- **Memory files are clickable.** `CLAUDE.md` is the most-edited file in a Claude Code repo, its cost was previously unknowable, and the natural next action after seeing that it costs 4 000 tokens is to go edit it.
- **`ac-dc` appears in the tools table like any other MCP server**, with its token cost. Our own bridge is not exempt from the accounting it exists to provide (see [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)).

### Debug Section

Off by default. Diagnoses the engine, not the code:

- Recent hook traffic from `hookEvent` — which hooks fired, with payloads.
- MCP server status detail from `get_mcp_status()`.
- Server and CLI info from `get_server_info()`, including which `claude` binary was resolved.
- The raw `gridRows` payload, for cross-checking our layout against the CLI's.

`gridRows` is displayed here and **never used for layout**. It is a terminal's pre-laid-out grid;
rendering it would couple this tab to a CLI presentation choice that can change under us.

## Usage HUD

Floating overlay on the viewer background, appearing after each turn.

### Placement

- Top-level element in the app-shell shadow DOM, sibling of the dialog and viewer containers
- Fixed position near a top corner, high z-index — but **below the permission dialog**, which is modal over everything (see [permission-dialog.md](permission-dialog.md))
- Triggered by the stream-complete window event, filtered to exclude errored and empty turns

### Data Flow

One step, not two. The HUD renders entirely from the `streamComplete` payload: `usage`, `model_usage`,
`total_cost_usd`, `duration_ms`, `num_turns`, `terminal_reason`, `permission_prompts`. Context
percentage comes from the `context_usage` carried on `postResponseComplete`, which arrives moments
later and updates that one line in place.

The old two-phase flow — immediate partial display, then a breakdown RPC to fill in the rest — is gone.
There is nothing left that needs a second round trip.

### Sections

All collapsible; collapse state persisted to `localStorage` as a serialised set of section names.

| Section | Content |
|---|---|
| Header | Model, context percentage badge (colour-coded), dismiss button |
| This turn | Cost or billing mode, duration, engine-internal turn count, terminal reason, permission-prompt count |
| Per-model usage | One row per entry in `model_usage` — input, output, cache read, cache creation, cost, context window |
| Context | `totalTokens` / `maxTokens` with the auto-compact mark |
| Rate limits | Limit type, utilisation, and reset time when a rate-limit event is in play |
| Files modified | The turn's `files_modified`, each clickable to the diff viewer |

`Files modified` is new to the HUD and earns its place: the single most useful thing to know
immediately after an agentic turn is which files changed.

### Cost Under Subscription Billing

`total_cost_usd` is null under a subscription. The HUD **never** renders that as `$0.00` — a zero cost
for a turn that plainly consumed tokens reads as a broken HUD and teaches users to ignore the number
(see [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap)).

Instead it labels the billing mode and shows the figures that are always meaningful: tokens, context
percentage, and rate-limit headroom. The rate-limit section is the subscription-mode analogue of a cost
signal, which is why it gets first-class display rather than a footnote. When `max_budget_usd` is
configured, a spend-against-budget bar appears in its place.

### Per-Model Rows Are Not Summed

A turn that delegated to a subagent on a cheaper model reports two rows, and they stay two rows. "The
expensive model did a little and the cheap model did a lot" is the shape of a well-delegated turn, and
summing it away hides the thing worth seeing. The native engine could not express this at all — it had
one model per request.

Cache hit rate is derived per row from `cacheReadInputTokens` and `cacheCreationInputTokens`. It is a
column, not a headline: it was headline-worthy only while AC⚡DC was the thing doing the caching.

### Behaviour

- Auto-hide after a few seconds, then fade out
- Hover pauses the timer; mouse leave restarts it
- Dismiss button hides immediately
- Fixed width, max height with internal scroll
- Never appears for an errored or empty turn
- A turn that ended with a mirror gap shows a marker linking to the health banner, because a HUD reporting a clean turn over a failed transcript append would be misleading

## Terminal HUD

Printed server-side after each turn, in reduced form: model, per-model usage, cost or billing mode,
context percentage, duration, and terminal reason. Printed for cancelled turns as well as completed
ones — a cancelled turn still consumed tokens.

Deleted with the tiering system: the boxed cache-block table, per-tier token counts with entry-N
thresholds, the tier-changes log, the mode-aware category table, and the one-shot startup
tier-distribution HUD.

## Invariants

- Every figure on both surfaces originates from `get_context_usage()` or from a pushed turn payload. Neither surface estimates or models context composition.
- Category colours come from the engine's payload, not a local palette.
- The auto-compact threshold is marked on every budget gauge while auto-compact is enabled, and its absence is labelled when it is disabled.
- Overlapping refresh triggers collapse into one in-flight plus at most one queued fetch; no trigger is silently dropped.
- The Context tab refreshes on `post-response-complete`, never on `stream-complete`.
- `gridRows` is rendered only in the Debug section and never used for layout.
- The Debug section is off by default and never required to understand normal usage.
- Memory files, system prompt sections, and every MCP server including `ac-dc` appear in the Session section with their token cost.
- The HUD renders without a follow-up RPC.
- The HUD never appears for an errored or empty turn.
- A null or zero cost is never rendered as `$0.00`; the billing mode is shown instead.
- Per-model usage rows are never summed into one line.
- HUD section collapse state persists across sessions via `localStorage`.
- The terminal HUD prints after every completed turn, cancelled or not.
