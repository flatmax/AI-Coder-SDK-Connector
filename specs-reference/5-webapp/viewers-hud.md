# Reference: Context Tab and Usage HUD

**Supplements:** `specs5/5-webapp/viewers-hud.md`

The payload shapes both surfaces render are canonical elsewhere and are **not** duplicated here:

- `ContextUsageResponse` (the `get_context_usage()` return): `specs5/plan/sdk-surface.md` § `get_context_usage()` return shape
- `get_context_usage` RPC envelope (`{usage, fetched_at}` / `{error}`): `specs-reference/3-engine/session.md` § Service: ClaudeCodeService
- `streamComplete` / `postResponseComplete` payloads: `specs-reference/3-engine/session.md` § Service: AcApp

What follows is what the components add on top: geometry, thresholds, formatting rules, and the local
state they keep.

## Numeric constants

### Usage HUD auto-hide

| Constant | Value |
|---|---|
| `_AUTO_HIDE_MS` | 8000 (ms before fade starts) |
| `_FADE_MS` | 800 (fade duration) |
| Total visible time | 8000 + 800 = 8800 ms |
| Hover behavior | Pauses timer; mouse leave restarts auto-hide |
| Dismiss button | Hides immediately (no fade) |

There is one HUD variant and every HUD is a turn, so the constants above apply to all of them without a
per-variant column.

### HUD geometry

| Constant | Value |
|---|---|
| Position | `position: fixed; top: 16px; right: 16px` |
| Z-index | 500 — **below** the permission dialog's 2000 (see `specs-reference/5-webapp/shell.md` § Viewport-scoped overlay z-index ladder) |
| Width | 300 px fixed |
| Max height | 80 vh with `overflow-y: auto` |

The z-index dropped from 10000 **on 2026-08-28, not when this table was written**. For three phases the
table said 500 and the stylesheet said 10000, so a HUD did float over a modal permission request —
obscuring the one surface in the app that blocks a turn, and taking the clicks in its corner, with the
HUD carrying transient information about a turn that was already over. It is now pinned by a test in
`usage-hud.test.js` that reads the dialog's z-index out of `permission-dialog/styles.js` rather than
copying it.

The width figure was 340 here and 300 in the stylesheet over the same period; 300 is what shipped and
what the component's own comments size their columns against, so this table now says 300. The max
height was likewise unwritten until the Files modified section made it load-bearing — a section whose
height grows with the turn is the first thing here that could run off the screen.

### Context gauge colour thresholds

Applies to the Context tab's budget gauge, the HUD header badge, and the shell's context-capacity bar, so
all three agree at a glance.

| Fill % of `maxTokens` | Colour |
|---|---|
| ≤ 75% | Green |
| 75–90% | Amber |
| > 90% | Red |

**The threshold marker overrides the palette when auto-compact is enabled.** When
`autoCompactThreshold` is present, the amber boundary moves to `autoCompactThreshold - 10%` of
`maxTokens` and the red boundary to the threshold itself, because "red" should mean "about to be
compacted", not "past an arbitrary fraction". With auto-compact disabled the fixed 75/90 boundaries
apply and the gauge carries a "no auto-compact" label.

| Marker property | Value |
|---|---|
| Position | `autoCompactThreshold / maxTokens` as a percentage of bar width |
| Rendering | 2 px vertical rule, full bar height, with a `title` giving the absolute token figure |
| Absent when | `isAutoCompactEnabled` is false, or `autoCompactThreshold` is absent from the payload |

### Category bar

| Aspect | Value |
|---|---|
| Colour source | `categories[].color`, verbatim from the payload. **No local palette.** |
| Ordering | Payload order, unmodified — the engine's order matches what `/context` shows |
| Zero-token categories | Omitted entirely from bar and legend |
| Deferred categories (`isDeferred`) | Rendered in the legend with a "deferred" label and **excluded from the stacked bar**, since a deferred category is not occupying context now |
| Minimum segment width | 2 px, so a small non-zero category is visible rather than rounded away |
| Expandable | No. The payload carries category totals, not per-item membership |

The native engine's five-colour category palette (`System` green, `Symbol Map` blue, `Files` amber,
`URLs` purple, `History` orange) and the entire L0–L3 tier palette are deleted. Both encoded our own
composition model; the engine now supplies both the categories and their colours.

### Per-model row derivations

Rows come from **`turn_model_usage`** — this turn's counters — read by `modelUsageLines`
(`webapp/src/turn-cost.js`). `model_usage` is the session's running total and is never a source for
these rows; see `specs5/5-webapp/viewers-hud.md` § *Cost Is Cumulative, and the HUD Reports One Turn*.

| Aspect | Value |
|---|---|
| Row label | `canonicalModel` when present, otherwise the map key. The key is the raw provider string, which on Bedrock or Vertex is an id like `us.anthropic.claude-opus-5-v1:0`. There is no `modelName` field |
| Ordering | Total tokens descending |
| Suppressed when | The entry's four counters total ≤ 0 |
| Counter spelling | camelCase first, snake_case second — the wire schema uses the first, a replayed transcript the second. Missing reads as 0, negatives are ignored |
| Rendered per row | `↑ prompt · ↓ output`, where `prompt` is `input + cacheCreation + cacheRead` |
| In the tooltip only | The four counters apart, `input` being the uncached remainder rather than the prompt |

**There is no cache-hit column and no context-window column.** Both were the native engine's, and the
ratio is not computed anywhere: the two cache counters are reported and a reader who wants the rate can
take it (`specs5/5-webapp/viewers-hud.md` § *Per-Model Rows Are Not Summed*). `↑` is the whole prompt
because 300px does not hold three numbers beside a model name, and the sum is the honest one to show.

**Rows are never summed** — no total row, and the section head's headline is `{n} models`, a count. A
turn that delegated to a cheaper model reports two rows and keeps two rows. A one-model turn gets no
headline, because the model name is already on the HUD's header.

Deleted with the cache: `provider_cache_rate` and its precedence rule over a locally computed
`cache_hit_rate` (there is no local computation to prefer against), Cache ROI
(`(cache_read / cache_write − 1) × 100`), which answered "is our cache paying for itself" about a cache
we no longer own, and the cache-hit colour bands that went with them.

### Cost rendering

**Corrected in phase 6** against the CLI's wire schema; this section described the pre-phase-6 HUD until
2026-08-29. Driven by **`turn_cost_basis` and `turn_cost_usd`** — the per-turn fields
`aic_dc/claude_code/cost.py` computes as a difference against session state. `total_cost_usd` is the
session's running total and is read for one tooltip and nothing else. Derivations in
`webapp/src/turn-cost.js`; the reasoning is
`specs5/5-webapp/viewers-hud.md` § *Cost Is Cumulative, and the HUD Reports One Turn*.

| `turn_cost_basis` | Row | Value | What the tooltip names |
|---|---|---|---|
| `measured`, figure > 0 | Shown | The formatted cost | What the turn added, and the session total it moved to |
| `measured`, figure 0 | Shown | `nothing extra` | That the estimate did not move — **explicitly not a claim about the billing plan** |
| `reset` | Shown | `cost unknown` | The running total restarted mid-turn (a `/clear`, a resumed session); the next turn is priced normally |
| `unpriced` | Shown | `cost unknown` | No usable figure; the spend is not lost — it lands on the next turn the engine prices |
| Absent or unrecognised | **No row at all** | — | — |
| `max_budget_usd` configured | Additional bar | `total_cost_usd / max_budget_usd` with the gauge palette | Remaining budget in dollars. **Not built** |

`measured` with no usable number behind it is downgraded to `unpriced`: a basis and a figure that
contradict each other are resolved in favour of the figure. A negative cost is treated as absent — a bug
upstream, not a refund.

| Rule | Value |
|---|---|
| Format | `n > 0.5 ? $n.toFixed(2) : $n.toFixed(4)` — the bundled `claude` binary's own format, so a figure here reads like the terminal's |
| Absent row means | A browsed turn. Cost is not in the CLI's transcript, so it was never recorded — a different fact from "we lost track of it" |
| Billing-mode label | **None.** No surface labels a cost with a billing mode |

**`$0.00` is never rendered**, and neither is a billing-mode label. A zero cost for a turn that plainly
consumed tokens reads as a broken HUD and teaches users to ignore the figure
(`specs5/plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap`) — but the fix is naming the reason,
not claiming a plan. Four decimals matter for the priced case: a turn whose subagents ran on a cheap
model can cost fractions of a cent, and two decimals would round it to `$0.00`, the exact display this
rule exists to prevent.

**There is no null branch.** The schema types `total_cost_usd` as a plain number; every null in this
codebase is one AIC⚡DC wrote itself, on a synthetic failure footer or a replayed turn.
`credential_source` on the engine-health record is the only real billing-mode signal.

The native engine's `priced_request_count` / `unpriced_request_count` split is gone. It existed because
litellm's pricing table could lack an entry for a configured model; the CLI reports cost or reports
nothing, with no third "we could not price this" state.

### Rate-limit record

Field shapes are the SDK's `RateLimitInfo`, passed through unchanged by
`aic_dc/claude_code/messages.py::_rate_limit` and held on the session as `rate_limit`.

| Field | Type | Note |
|---|---|---|
| `status` | `allowed` \| `allowed_warning` \| `rejected` | The HUD renders at all three; the chat panel's toast fires on the last two only |
| `rate_limit_type` | `five_hour` \| `seven_day` \| `seven_day_opus` \| `seven_day_sonnet` \| `overage` | Unknown values render with underscores opened out rather than dropped — the enum is the CLI's to extend |
| `utilization` | float **0.0–1.0** | A *fraction*. Read as a percentage it renders every real figure as under 1% |
| `resets_at` | int, **Unix seconds** | Not milliseconds, not ISO. `× 1000` before `new Date` |
| `overage_status` | same three words, or null | One line, no second gauge |
| `overage_resets_at` | int, Unix seconds | |
| `overage_disabled_reason` | string or null | Printed in the CLI's own words |

Both unit facts are pinned by tests, because each has exactly one plausible wrong reading and neither
fails visibly.

| Rule | Value |
|---|---|
| Utilisation colour | The context gauge's bands above, applied to the percentage — **except** `status: "rejected"`, which is `#f85149` at any figure |
| Section absent when | `windowIsOpen` is false (`resets_at × 1000 ≤ Date.now()`), or the record carries no figure, no type and no rejection |
| Expiry decided in | The browser only. The server serves the record raw |
| Cleared by | Nothing short of a newer record — not a session change, not a disconnect |

Labels: `five_hour` → "5-hour", `seven_day` → "7-day", `seven_day_opus` → "7-day Opus",
`seven_day_sonnet` → "7-day Sonnet", `overage` → "Overage". The section's *head* reads
`{label} limit`; its *stored collapse key* is the constant `Rate limits`.

The derivations live in `webapp/src/rate-limit.js`, shared with `chat-panel/streaming.js` —
`formatResetTime` moved there from the latter when the HUD needed the same sentence.

### HUD section collapse

| Section | Collapse key | Head's headline |
|---|---|---|
| Context | `Context` | `{pct}% · {total}/{max}`, in the gauge colour |
| Per-model usage | `Per-model usage` | `{n} models` when n > 1, otherwise nothing — **never a token total** |
| Rate limits | `Rate limits` | `{pct}%`, or `reached`, or `—` |
| Files modified | `Files modified` | The de-duplicated file count |
| This turn | *(not collapsible)* | — |

Stored as a JSON array under `aic-dc-hud-collapsed`. Unparseable values, non-arrays and non-string
members all degrade to "nothing collapsed". Names the build does not render round-trip untouched.

### Thinking token rendering

`usage` carries no separate reasoning-token field. Thinking arrives as content blocks, and its token cost
is inside `outputTokens` — so there is no Reasoning row in either surface. The Thinking Regions in the
transcript are where thinking is visible (`specs5/5-webapp/chat.md` § Thinking Regions); the HUD does not
try to price it separately.

### Refresh queue

| Aspect | Value |
|---|---|
| In-flight guard | One `get_context_usage()` call at a time |
| Queue depth | Exactly 1 — a trigger arriving during a fetch sets `_refreshPending`; the completion path re-fires once and clears it |
| Rationale | A dropped trigger leaves the tab showing pre-turn state with no staleness indicator |
| Adopt-without-fetch | A `postResponseComplete` carrying `context_usage` is adopted directly; no RPC is issued |
| Staleness threshold | `fetched_at` older than the most recent `postResponseComplete` marks the view stale and triggers a refresh on next visibility |

Polling is absent from every surface. The native engine's 1 Hz status poll is deleted and nothing
replaced it: `get_context_usage()` is fetched on state change only.

### Terminal HUD format

**Built 2026-08-29** — `aic_dc/claude_code/turn_hud.py`, emitted as one `logger.info` record from
`ClaudeCodeService._post_response`. Printed server-side after each turn, cancelled or not, and after a
turn with nothing in it. The sample below is real output, not a sketch:

```
Model: claude-opus-4-6
Turn:  8.4s · 3 turns · completed · 2 permission prompts
Usage: claude-opus-4-6   12,481 in / 1,208 out · cache 41,502 r / 0 w ·  77% hit
       claude-haiku-4-5   3,004 in /   612 out · cache      0 r / 0 w ·   0% hit
Cost:  $0.1842 this turn · $3.21 session total
Ctx:   118,204 / 200,000 (59%) · auto-compact at 160,000
Files: src/auth/session.py, src/auth/tokens.py
```

| Row | Source | Absent when |
|---|---|---|
| `Model` | the busiest `turn_model_usage` row's `canonicalModel`, else its key | No per-model counters |
| `Turn` | `duration_ms`, `num_turns`, `cancelled` / `terminal_reason` / `is_error`, `permission_prompts` | Never — this row is the block |
| `Usage` | one line per `turn_model_usage` entry, busiest first, first labelled and continuations aligned | No per-model counters |
| `Cost` | `turn_cost_basis` + `turn_cost_usd`; `total_cost_usd` appears only labelled `session total` | Basis absent or unrecognised (a browsed turn) |
| `Ctx` | the `get_context_usage()` response the same post-turn pass fetched | The engine could not answer |
| `Files` | `files_modified`, repo-relative | The turn modified nothing |

| Rule | Value |
|---|---|
| Model column | Padded to the longest name, capped at 28 chars; an over-long name keeps its **tail** behind a `…`, since the distinguishing part of `us.anthropic.claude-opus-5-v1:0` is the end |
| Counter columns | Right-aligned across the whole block, so two models' figures compare down the column |
| Cache hit % | `cacheRead / (input + cacheRead)`, rounded; `—` when the denominator is 0. **Computed here and not in the browser** — a width difference, not a disagreement |
| Path naming | Repo-relative; a path outside the root prints absolute, `build_diff_payload`'s rule |
| `Files` ceiling | 8 paths, then `, and N more` |
| Extra `Turn` clauses | `not mirrored` when `mirror_gap`; `revised after background work` when `continuation` |
| Failure | Swallowed at `debug`. A summary of successful work never fails the turn it summarises |

`Cost:` follows the browser's rule exactly — the turn's own figure, "nothing extra", or "cost unknown"
with the reason, and never a billing-mode label (§ *Cost rendering*). Building this is what forced that
section's correction: the spec had said this line prints "cost or billing mode", which is the
pre-phase-6 reading of a cumulative field.

Deleted: the boxed `╭─ Cache Blocks ─╮` table with per-tier `(entry_n+)` thresholds, the mode-aware
category table with its "Symbol Map" / "Doc Map" label swap, the 📈/📉 tier-change log, and the one-shot
`╭─ Initial Tier Distribution ─╮` startup box.

## Schemas

### localStorage keys

| Key | Purpose |
|---|---|
| `aic-dc-context-section` | `"usage"` / `"session"` / `"debug"` — active Context tab section (unknown values fall back to `"usage"`) |
| `aic-dc-context-debug-enabled` | `"true"` / `"false"` — whether the Debug section is offered at all (default `"false"`) |
| `aic-dc-session-expanded` | JSON-serialized array of expanded Session-section group names |
| `aic-dc-hud-collapsed` | JSON-serialized array of collapsed section names in the Usage HUD |

Session-section defaults: Memory files and Tools expanded; System prompt sections and Agents/skills
collapsed.

Deleted keys: `aic-dc-context-subview` (the Budget/Cache pill toggle), `aic-dc-cache-expanded`,
`aic-dc-cache-sort`, `aic-dc-budget-expanded`. On upgrade they are simply ignored — no migration, since
none of them names a section that still exists.

### Session-section row shapes

Rows are projections of the payload, not new state. What the component adds is the click target:

| Group | Source field | Click action |
|---|---|---|
| Memory files | `memoryFiles[]` (`path`, `tokens`) | `navigate-file` window event with the path — opens it in the diff viewer |
| System prompt sections | `systemPromptSections[]` | Expands inline where the entry carries text; inert where it carries only a size |
| Tools — built-in | `systemTools[]` | None |
| Tools — deferred built-in | `deferredBuiltinTools[]` | None; labelled "deferred" |
| Tools — per MCP server | `mcpTools[]` grouped by server prefix | Opens the server's `get_mcp_status()` detail |
| Agents | `agents[]` | Opens the defining file under `.claude/agents/` |
| Skills | `skills` | Opens the defining file |
| Slash commands | `slashCommands` | None |

The `aic-dc` server is grouped and rendered by the same code path as any third-party server, with no
special-casing and no exemption from the token column.

### Debug section content

| Panel | Source | Cap |
|---|---|---|
| Hook traffic | `hookEvent` broadcasts, in arrival order | 100 most recent, ring buffer |
| MCP status | `get_mcp_status()` | — |
| Server info | `get_server_info()` | — |
| Raw `gridRows` | `gridRows` from the last snapshot | Rendered as preformatted JSON |

`gridRows` is rendered **only here** and never parsed for layout. It is the CLI's pre-laid-out terminal
grid; consuming it would couple our layout to a presentation choice that can change under us.

### Mirror-gap marker

When a turn's `MirrorErrorMessage` was seen, the HUD renders a marker row linking to the shell's health
banner rather than reporting a clean turn. Field: a boolean the chat panel sets on the turn record, not
something in the engine payload — the engine's turn succeeded; ours failed to write it down.

## Cross-references

- Behavioral specification (sections, refresh triggers, HUD lifecycle): `specs5/5-webapp/viewers-hud.md`
- Engine-side contract and field-by-field rationale: `specs5/3-engine/context-visibility.md`
- `ContextUsageResponse` shape: `specs5/plan/sdk-surface.md`
- RPC envelope and turn-completion payloads: `specs-reference/3-engine/session.md`
- Overlay stacking and the context-capacity bar: `specs-reference/5-webapp/shell.md`
- Why `get_context_breakdown` has no successor: `specs-reference/1-foundation/rpc-inventory.md`
