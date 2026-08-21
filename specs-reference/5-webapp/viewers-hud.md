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
| Width | 340 px fixed |
| Max height | 80 vh with overflow scroll |

The z-index dropped from 10000. A HUD floating over a modal permission request would obscure the one
surface in the app that blocks a turn, and the HUD is transient information about a turn that is already
over.

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

`model_usage` rows are rendered as-is with two computed columns:

| Column | Formula | Suppressed when |
|---|---|---|
| Cache hit % | `cacheReadInputTokens / (inputTokens + cacheReadInputTokens) × 100` | Denominator is 0 → render `—` |
| Context window | `contextWindow` from the row, formatted with thousands separators | Field absent |

Cache-hit colour thresholds are retained from the native engine (≥ 50% green, ≥ 20% amber, below that
default text) but the metric is demoted from a header badge to a table column. It was headline-worthy
only while AC⚡DC was the thing doing the caching.

**Rows are never summed.** No total row, no aggregate header figure. A turn that delegated to a cheaper
model reports two rows and keeps two rows.

Deleted with the cache: `provider_cache_rate` and its precedence rule over a locally computed
`cache_hit_rate` (there is no local computation to prefer against), and Cache ROI
(`(cache_read / cache_write − 1) × 100`), which answered "is our cache paying for itself" about a cache
we no longer own.

### Cost rendering

Driven by `total_cost_usd` on the `ResultMessage`, which is `null` under subscription billing.

| Condition | Row | Value | Tooltip |
|---|---|---|---|
| `total_cost_usd` is a positive number | Shown | `$0.0000` (4 decimals) | Absolute turn cost |
| `total_cost_usd` is `0` and tokens were consumed | Shown | Billing-mode label, e.g. `subscription` | `This turn is billed under your plan, not per token.` |
| `total_cost_usd` is `null` | Shown | Billing-mode label | Same |
| `max_budget_usd` configured | Additional bar | `spent / max_budget_usd` with the gauge palette | Remaining budget in dollars |

**`$0.00` is never rendered.** A zero cost for a turn that plainly consumed tokens reads as a broken HUD
and teaches users to ignore the figure (`specs5/plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap`).

4-decimal precision is retained for the priced case: a turn whose subagents ran on a cheap model can cost
fractions of a cent, and 2 decimals would round it to `$0.00` — the exact display this rule exists to
prevent.

The native engine's `priced_request_count` / `unpriced_request_count` split is gone. It existed because
litellm's pricing table could lack an entry for a configured model; the CLI reports cost or reports
nothing, with no third "we could not price this" state.

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

Printed server-side after each turn, cancelled or not:

```
Model: claude-opus-4-6
Turn:  8.4s · 3 turns · completed · 2 permission prompts
Usage: claude-opus-4-6      12,481 in / 1,208 out · cache 41,502 r / 0 w · 78% hit
       claude-haiku-4-5      3,004 in /   612 out · cache      0 r / 0 w ·  0% hit
Cost:  $0.1842
Ctx:   118,204 / 200,000 (59%) · auto-compact at 160,000
Files: src/auth/session.py, src/auth/tokens.py
```

One `Usage:` line per `model_usage` entry, first line labelled, continuations aligned. `Cost:` prints the
billing mode when `total_cost_usd` is null. `Files:` is omitted when the turn modified nothing.

Deleted: the boxed `╭─ Cache Blocks ─╮` table with per-tier `(entry_n+)` thresholds, the mode-aware
category table with its "Symbol Map" / "Doc Map" label swap, the 📈/📉 tier-change log, and the one-shot
`╭─ Initial Tier Distribution ─╮` startup box.

## Schemas

### localStorage keys

| Key | Purpose |
|---|---|
| `ac-dc-context-section` | `"usage"` / `"session"` / `"debug"` — active Context tab section (unknown values fall back to `"usage"`) |
| `ac-dc-context-debug-enabled` | `"true"` / `"false"` — whether the Debug section is offered at all (default `"false"`) |
| `ac-dc-session-expanded` | JSON-serialized array of expanded Session-section group names |
| `ac-dc-hud-collapsed` | JSON-serialized array of collapsed section names in the Usage HUD |

Session-section defaults: Memory files and Tools expanded; System prompt sections and Agents/skills
collapsed.

Deleted keys: `ac-dc-context-subview` (the Budget/Cache pill toggle), `ac-dc-cache-expanded`,
`ac-dc-cache-sort`, `ac-dc-budget-expanded`. On upgrade they are simply ignored — no migration, since
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

The `ac-dc` server is grouped and rendered by the same code path as any third-party server, with no
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
