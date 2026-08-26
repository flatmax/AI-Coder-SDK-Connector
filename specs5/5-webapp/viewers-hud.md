# Context Tab and Usage HUD

Two surfaces onto the same question — *what does the agent currently know, and what is it costing?* The
**Context tab** answers it persistently, in three sections. The **Usage HUD** answers it transiently,
once per turn, in a floating overlay. A reduced **terminal HUD** prints the same turn summary
server-side.

The data behind both is the engine's own accounting: `get_context_usage()` for composition,
`ResultMessage` for per-turn usage. AIC⚡DC does not model context. The contract, the field list, and the
rationale are in [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md); this file
specifies the components.

## The Shift This File Records

The old version of this spec was mostly about our own cache. Two sub-views, a Budget / Cache pill
toggle, tier bars in an L0–L3 palette, per-item N/threshold stability bars, promotion and demotion logs,
synthetic `meta:` rows, an uncached synthetic tier, a fuzzy filter and sort over tier contents, a
click-to-view map-block modal, and a manual rebuild button. All of it described AIC⚡DC's belief about a
prompt AIC⚡DC assembled.

None of that has a referent now, and the replacement is not a lesser version of it — it is the thing the
old tab was an approximation *of*. Where the old tab could drift from what the provider actually
received with no way to detect the drift from inside the app, this one renders what the engine says
about itself. It also answers questions the old tab structurally could not: what `CLAUDE.md` costs, what
each MCP server costs, what the system prompt is made of, which model did which part of a turn.

The one real loss is per-file token attribution for selected files, which followed the file-selection
contract out the door ([CC-14](../plan/decisions.md#cc-14)) and then the selection itself
([CC-21](../plan/decisions.md#cc-21)). Files enter context because the agent read them, and
`categories` reports the aggregate rather than a per-path split.

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

`files-changed` is not a trigger either — and since [CC-21](../plan/decisions.md#cc-21) there is no
such event to trigger on. It announced a selection change, and a selection never altered context.

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
- **`aic-dc` appears in the tools table like any other MCP server**, with its token cost. Our own bridge is not exempt from the accounting it exists to provide (see [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)).

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
- **A host can act on a server row: Reconnect and Disable / Enable.** `reconnect_mcp_server(name)` and
  `toggle_mcp_server(name, enabled)` had existed with no browser caller at all, which is what made this
  the section that could *show* you a failed server and do nothing about it. Both are localhost-only, so
  a guest reads the facts with no controls rather than controls the engine would refuse.

  Reconnect is offered only for `failed` and `needs-auth` — the loop the SDK documents for it. A
  `pending` row is mid-dial and re-dialling races the attempt already running; a `disabled` one was
  switched off deliberately, and the answer there is Enable. The toast repeats the reply's own word,
  `reconnecting`, rather than claiming a connection: the outcome is the pill's to report, and this is the
  one row that exists because a server was wrong about being fine.

  **Enabling asks first; disabling does not.** The two directions are not symmetric — disabling only
  takes tools away, and the reader reaching for it is usually looking at the token cost in this very
  section. Enabling hands the agent capability it did not have a moment ago, which is the RPC docstring's
  own argument ("the host is the one who decides which tools exist"), so the friction goes there alone.
  The confirmation names the tool count when the engine gave one and says plainly that it did not when it
  has not — a server that is switched off advertises nothing, so an absent count is ordinary here, and a
  guessed number is one somebody would weigh the decision against.

  The actions sit in the expanded group body, not on the head, because the head *is* the disclosure
  `<button>` and a button cannot contain one. The cost is a click to reach a reconnect, which the
  unwell-first sort already softens: the row that needs acting on is the first in the list, with its
  state on the closed head.

  **An in-process SDK server gets neither control, and says so.** Our own `aic-dc` bridge appears in this
  list like any other server — the token-honesty invariant below depends on that — but the CLI reports it
  with `scope: "dynamic"`, and on such a row both RPCs are broken in a way that matters. Measured
  2026-08-26 against CLI 2.1.229: `toggle_mcp_server('aic-dc', false)` replies `{"status": "ok",
  "enabled": false}` and takes the tool count from 6 to 0 **while the pill goes on reading `connected`**,
  and then both ways back refuse with `SDK servers should be handled in print.ts` — the re-enable and
  `reconnect_mcp_server` alike. Only a new session restores the tools.

  So the row states the fact in place of the buttons: *"Served in-process by this app, so the engine
  manages it with the session — it cannot be switched off and back on from here."* A control the engine
  will not honour in both directions is not a toggle, and this is the one server whose tools the *agent*
  runs on: `symbol_map`, `file_symbols`, `find_references`, `doc_outline`, `review_state`, `ui_state`.
  Note what this does to the paragraph above — the enable-only confirmation is justified by disabling
  being cheap and reversible, and on a `dynamic` row it is neither, so the exemption removes the case
  that broke the premise rather than weakening the rule everywhere else.

  The test is `scope === 'dynamic'`, the CLI's own word, not the name `aic-dc` — any SDK server we
  register later inherits the same reasoning, and a *configured* server that happened to be called
  `aic-dc` would still be togglable. `get_mcp_status` is a verbatim passthrough (`session.py:1110`), so
  the field is the CLI's to define. Background:
  [`../plan/sdk-surface.md`](../plan/sdk-surface.md) § Correction, 2026-08-26 — which is also where the
  earlier, false claim that an SDK server does *not* appear in this list is retired.

- **A control call re-reads the whole breakdown, not just the status** — and unlike `set_model`, both
  take effect *now*. Measured 2026-08-26 against `chrome-devtools`, the one configured stdio server on
  the dev machine: disabling a settled `connected` server returned `disabled` with 0 tools on the very
  next `get_mcp_status`, stable across 11s, and `get_context_usage` then reported `mcpTools=0/0 tokens`
  where it had reported 29 tools / 9,071. Re-enabling restored both within 3s. So no "applies next turn"
  sentence is owed here, and refreshing the status while leaving the numbers is how a green pill would
  end up over a stale total.

  Two things that measurement also settled. The dial takes ~1-3s, and the breakdown call costs 3-14s, so
  the refresh fired after the reply *cannot* return before the server has finished connecting — the slow
  call covers the latency for free. And **a disable issued while the initial dial is still in flight is
  silently reverted** by the connection completing: a probe that disabled ~1s after connect found the
  server `connected` with all 29 tools eight seconds later. Not guarded against, because the actions are
  behind a collapsed group and the tab's first breakdown fetch takes longer than the window in which it
  is possible; recorded so that a future report of "Disable didn't work" has somewhere to start.

  The toggle is session-scoped, not a settings edit: `~/.claude.json`'s `mcpServers` entry was unchanged
  after both directions, with no `disabled` key added, matching the docstring's "temporarily disable …
  re-enable it later".

### Debug Section

Off by default — Usage is where the tab opens and Debug is never it, though a reader who chooses it gets
it back on the next visit like the other two. It diagnoses the engine, not the code:

- **Engine** — which `claude` binary was resolved, its version and where it came from, the SDK version
  and the CLI it pins, the credential source, and the mirror-gap count as a verdict rather than a
  number. This is `EngineHealth`, *not* `get_server_info()` as an earlier draft of this section had it:
  the binary resolution is AIC⚡DC's, recorded in `claude_code/health.py`, and the engine's own reply
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

### When the breakdown fails

**The advice under the error is chosen by a `reason` the service sends, not by reading the error
string.** There are two situations and they call for opposite actions:

| `reason` | Situation | What the note says |
|---|---|---|
| `no-engine` | No session to ask — not connected, or lost. | It is unavailable until a session is connected. Waiting is the answer; retrying is not. |
| `failed` | A request went out to a live engine and no answer came back. | A request that failed, not a session that is missing — Refresh asks again, and a turn in flight makes this slow rather than unavailable. |
| absent | A backend older than the field, or a reason this build does not know. | The advice that holds either way. Never one of the two specific ones on a guess. |

One note used to serve both, and it was the waiting one — so a reader whose session was perfectly
healthy was told to connect a session, which reads as the tab being broken rather than the call having
been slow. A client-side failure (the transport dropped it, the deadline fired) is `failed` too: a
request did leave and did not come back, which is all `failed` claims. Whether an engine is up is not
something the browser can conclude from its own timeout.

**Deadlines are laid out so the innermost one fires first.** The engine bounds a control request at
60s; the transport waits 75s; this tab's own deadline is 90s. Each layer knows more about the call than
the layer outside it, so the useful error is the innermost one, and a layer that fires first steals the
message. At 60s for the transport — the value it shipped with — the two 60s clocks raced and the
transport won by the width of the socket, so every timeout read as the contentless "Timed out waiting
for response" and the 90s deadline could never fire at all. The tab's deadline is deliberately the
outermost: this call is slow (3-14s measured, and past 60s often enough to log eight engine timeouts in
one half-hour run), and a deadline that pre-empts a reply on its way stacks a retry onto a subprocess
already struggling with the first.

## Usage HUD

Floating overlay on the viewer background, appearing after each turn.

### Placement

- Top-level element in the app-shell shadow DOM, sibling of the dialog and viewer containers
- Fixed position near a top corner, high z-index — but **below the permission dialog**, which is modal over everything (see [permission-dialog.md](permission-dialog.md))
- Triggered by the stream-complete window event, filtered to exclude errored and empty turns

### Data Flow

One step, not two. The HUD renders entirely from the `streamComplete` payload: the per-turn
`turn_cost_usd` / `turn_cost_basis` / `turn_model_usage` (never the cumulative `model_usage` or
`total_cost_usd` beside them — see § Cost Is Cumulative below), `duration_ms`, `num_turns`,
`terminal_reason`, `permission_prompts`. Context
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
| Per-model usage | One row per entry in `turn_model_usage`: the model, then `↑ prompt · ↓ output`. `↑` is the **whole** prompt — the uncached part and the cached part added together — because three input-side counters do not fit in 300px beside a model name, and their sum is the one figure that is not misleading on its own (see [chat.md § The Token Split](chat.md#the-token-split)). The tooltip carries all four counters unrounded. Per-row cost and context window belong to the Context tab; this row exists to answer whether a turn's tokens were prompt or completion, which the cost line alone cannot |
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
rather than summing across results."* `modelUsage` carries the same warning. AIC⚡DC runs one
streaming-input client, so:

- The HUD's "This turn" cost was the **whole session's** spend, growing every turn.
- Its model list named every model the session had ever used, not the ones that answered.
- Every null in this codebase is one AIC⚡DC wrote itself — a synthetic failure footer, or a replayed
  turn. A live result always carries a figure, subscription or not; it is an estimate the CLI computes,
  not a billing statement.
- `credential_source` on the engine-health record is the only real billing-mode signal.

The turn's own cost is therefore a **difference** — against where the session's total stood when the turn
was admitted, not against the previous result. The distinction only bites when a turn ends more than
once, which a turn with a background subagent does: the HUD renders from the last result of the turn, and
the subagent's tokens are spent after the first. Both figures agree for a turn that ends once. The
baseline is session state, so the engine takes the difference (`aic_dc/claude_code/cost.py`) and every
client reads the same answer; a per-turn `TurnTranslator` could not hold the baseline, and the browser
holding it would lose it on reconnect. Three per-turn fields ship beside the engine's cumulative ones,
under names that cannot be confused with them: `turn_cost_usd`, `turn_cost_basis`, `turn_model_usage`.

A turn with a background subagent therefore reaches the HUD twice. The second `streamComplete` arrives
flagged `continuation` ([`../3-engine/session.md`](../3-engine/session.md#every-result-the-drain-reads-is-emitted-flagged-continuation)),
and the HUD shows again with the turn's figures now that the subagent's spend is in them — it replaces
the reading, it does not add a second turn's. That it may pop up seconds after the user read the first
one is not a race with the next turn: the drain is stopped before the next turn's pump starts, so a
continuation can only ever be about the turn still on screen.

`turn_cost_basis` is what lets the HUD tell apart the two things it used to render identically:

| Basis | Meaning | Rendering |
|---|---|---|
| `measured` | A difference in hand. **Zero is an answer**: the turn cost nothing extra. | The figure, or "nothing extra" |
| `reset` | The running total went backwards — a `/clear`, or a resumed session. | "cost unknown", reason in the tooltip |
| `unpriced` | No usable number: a footer AIC⚡DC wrote, or one the CLI zeroed. The schema warns that *"crash/startup-error results may carry zeroed values"*, so a zero on an error is no evidence rather than free. | "cost unknown", reason in the tooltip |
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

The two cache counters, `cacheReadInputTokens` and `cacheCreationInputTokens`, are reported in the
row's tooltip and nowhere more prominently. No derived hit rate: a ratio was headline-worthy only
while AIC⚡DC was the thing doing the caching, and computing one now would be the app inventing a
figure beside four the engine measured. The counters are there for a reader who wants to work it out.

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
- Memory files, system prompt sections, and every MCP server including `aic-dc` appear in the Session section with their token cost.
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
