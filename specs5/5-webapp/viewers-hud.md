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

- **Memory files are clickable — the ones that can be opened.** `CLAUDE.md` is the most-edited file in a Claude Code repo, its cost was previously unknowable, and the natural next action after seeing that it costs 4 000 tokens is to go edit it. Clicking one also minimizes the dialog, because the viewer is behind it and a click that opens a file under an opaque panel is indistinguishable from a click that did nothing. The engine reports these paths as *absolute* and every repo read rejects an absolute path, so `get_context_usage` adds `relPath` to each entry that lives inside the repo root and the browser makes exactly those rows clickable. A user-level `~/.claude/CLAUDE.md` is outside the repo, has no repo-relative name, and stays text.
- **`ac-dc` appears in the tools table like any other MCP server**, with its token cost. Our own bridge is not exempt from the accounting it exists to provide (see [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)).

Two of the interactions above are not implementable from this payload, and saying so here is cheaper than
each reader rediscovering it:

- **An agent or skill row cannot open its defining file.** Both carry `source`, and `source` is a
  settings *scope* — `projectSettings`, `userSettings`, `plugin`, `built-in` — not a path. The rows show
  the scope in the CLI's own words instead. Opening the file would need a second source that maps a scope
  and a name to a location on disk.
- **A system prompt section cannot expand to its text.** The element shape is `{name, tokens}`; the text
  is not in the response. The table's own hedge — "where the payload carries it" — resolves to nowhere.

Two more shapes the implementation had to answer for:

- **A server that failed to start still gets a row.** It has no tools *because* it failed, so a listing
  built from `mcpTools` alone answers "which servers do I have" by omitting the broken one. Groups are
  the union of `mcpTools` and `get_mcp_status()`, and an unwell server sorts above a heavier healthy one.
- **Health comes from `get_mcp_status()`, fetched beside the breakdown and allowed to fail.** The
  breakdown is the point of the tab and health is a decoration on it, so a status call that times out
  leaves the numbers on screen and the groups unpilled. `EngineHealth.mcp` is not the source: it is a
  field with no writer. A pill is never carried over from an earlier fetch — "connected" is a claim about
  now, and this is the one figure where being out of date is worse than being absent.

### Debug Section

Off by default — Usage is where the tab opens and Debug is never it, though a reader who chooses it gets
it back on the next visit like the other two. It diagnoses the engine, not the code:

- **Engine** — which `claude` binary was resolved, its version and where it came from, the SDK version
  and the CLI it pins, the credential source, and the mirror-gap count as a verdict rather than a
  number. This is `EngineHealth`, *not* `get_server_info()` as an earlier draft of this section had it:
  the binary resolution is AC⚡DC's, recorded in `claude_code/health.py`, and the engine's own reply
  knows nothing about it. Health arrives pushed on `engineHealth` and a pushed record wins over the
  fetched one, because `mirror_gaps` moves during a turn.
- **The initialize reply** from `get_server_info()`, summarised by key and then printed verbatim. By
  key, because this repo has not read a schema for its shape and naming unverified fields is the exact
  mistake § *Verified field shapes* records. One control request, on the way into the section and not
  before — a reader who never opens Debug never spends it — and again on Refresh or a session change.
- **Hook traffic** from `hookEvent` — time, hook, tool, outcome, each row expanding to the payload the
  engine sent. Collected from the moment the panel mounts rather than the moment the section opens,
  since the traffic worth reading is the turn that just ran. Bounded to the newest few dozen and
  labelled as bounded: the `PostToolUse` re-index fires on every file the agent writes.
- **MCP server status** from `get_mcp_status()`, verbatim. The Session section renders the same payload
  through `mcpHealth`; the two together are how a reader checks our reading against what arrived.
- The raw `gridRows` payload, for cross-checking our layout against the CLI's.
- **A link out to the SDK Surface tab**, sitting under the Engine block's version line. That block already
  names the SDK and the CLI it pins; *which of their features this build wired up* is the next question a
  reader has there, and the answer is a whole panel rather than a row
  ([`../plan/sdk-surface.md`](../plan/sdk-surface.md#the-probe)). It is a `<button>` dispatching
  `request-dialog-tab` with `{tab: 'sdk-surface'}` — the same mechanism the picker's ⚙️ and 📄 use — and it
  names Alt+5 in its own sentence, because the destination has no entry in any rendered tab bar and a
  reader who arrives by link is the only reader who will ever learn the keystroke.

`gridRows` is displayed here and **never used for layout**. It is a terminal's pre-laid-out grid;
rendering it would couple this tab to a CLI presentation choice that can change under us.

The segmented control appears once there is a breakdown **or an error**. Gating it on a successful fetch
hid Debug in the one situation it is worth the most — the breakdown itself failing — so the Debug
section renders ahead of the error branch, from sources that do not depend on the breakdown at all.

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
| This turn | This turn's cost or why there is none, duration, engine-internal turn count, terminal reason, permission-prompt count |
| Per-model usage | One row per entry in `turn_model_usage` — input, output, cache read, cache creation, cost, context window |
| Context | `totalTokens` / `maxTokens` with the auto-compact mark |
| Rate limits | Limit type, utilisation, and reset time when a rate-limit event is in play |
| Files modified | The turn's `files_modified`, each clickable to the diff viewer |

`Files modified` is new to the HUD and earns its place: the single most useful thing to know
immediately after an agentic turn is which files changed.

### Cost Is Cumulative, and the HUD Reports One Turn

**Corrected in phase 6, against the CLI's own wire schema.** This section previously said
`total_cost_usd` is null under a subscription, and the HUD was built on that: it printed the field
under the heading "This turn", and labelled a null as "included".

Both halves were wrong. The schema types the field as a plain number with **no null branch**, and
describes it as *"cumulative estimated cost in USD for this query() call … cumulative across turns in
streaming-input sessions — each result carries the running total so far, so read the latest result
rather than summing across results."* `modelUsage` carries the same warning. AC⚡DC runs one
streaming-input client, so:

- The HUD's "This turn" cost was the **whole session's** spend, growing every turn.
- Its model list named every model the session had ever used, not the ones that answered.
- Every null in this codebase is one AC⚡DC wrote itself — a synthetic failure footer, or a replayed
  turn. A live result always carries a figure, subscription or not; it is an estimate the CLI computes,
  not a billing statement.
- `credential_source` on the engine-health record is the only real billing-mode signal.

The turn's own cost is therefore a **difference** against the previous result. The baseline is session
state, so the engine takes the difference (`ac_dc/claude_code/cost.py`) and every client reads the same
answer; a per-turn `TurnTranslator` could not hold the baseline, and the browser holding it would lose
it on reconnect. Three per-turn fields ship beside the engine's cumulative ones, under names that cannot
be confused with them: `turn_cost_usd`, `turn_cost_basis`, `turn_model_usage`.

`turn_cost_basis` is what lets the HUD tell apart the two things it used to render identically:

| Basis | Meaning | Rendering |
|---|---|---|
| `measured` | A difference in hand. **Zero is an answer**: the turn cost nothing extra. | The figure, or "nothing extra" |
| `reset` | The running total went backwards — a `/clear`, or a resumed session. | "cost unknown", reason in the tooltip |
| `unpriced` | No usable number: a footer AC⚡DC wrote, or one the CLI zeroed. The schema warns that *"crash/startup-error results may carry zeroed values"*, so a zero on an error is no evidence rather than free. | "cost unknown", reason in the tooltip |
| *(absent)* | A browsed turn. Cost is not in the CLI's transcript, so it was never recorded — a different fact from "we lost track of it". | No cost shown at all |

A `$0.00` is still never printed for a turn whose cost is unknown
(see [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap)) — but the fix is
naming the reason, not labelling a billing mode. An unpriced turn's spend is not lost: the baseline is
deliberately not advanced, so it lands on the next turn the engine can price.

Cost is formatted in the CLI's own format — four decimals up to fifty cents, two above — so a figure
here reads like the one the terminal shows. Two decimals throughout would render most per-turn costs as
`$0.00`.

The rate-limit section remains the analogue of a cost signal for a user who does not care about dollar
estimates, and when `max_budget_usd` is configured a spend-against-budget bar appears in its place.

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
- Never appears for an **empty** turn — but an errored turn is not automatically an empty one. A turn
  that failed *late*, after the model answered and the tools ran, is the most expensive kind of failure
  there is, and the original rule (drop every errored turn) hid exactly that. The HUD appears for a
  failed turn that has usage to report — a measured cost, per-model tokens, or, for a crash footer that
  carries no usage at all, evidence that the turn had already done work — and the row says "failed" so
  the receipt is not mistaken for a finished turn's. A turn that died before doing anything still gets
  nothing: there is no number, and the chat panel and a toast already carry the error.
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
- The HUD never appears for an empty turn. An errored turn that carries real usage is not an empty turn.
- No surface reads `total_cost_usd` or `model_usage` as this turn's; those are the session's running
  totals and only ever appear labelled as such.
- A turn whose cost cannot be established is never rendered as `$0.00`, and never as a claim about the
  billing plan; the reason is named instead.
- A turn that genuinely cost nothing extra renders differently from one whose cost is unknown.
- Per-model usage rows are never summed into one line.
- HUD section collapse state persists across sessions via `localStorage`.
- The terminal HUD prints after every completed turn, cancelled or not.
