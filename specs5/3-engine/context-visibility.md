# Context Visibility

What does the agent currently know, what is it costing, and how close is it to compacting? Under the
native engine those questions were answered by AC⚡DC's own model of the prompt it had assembled.
Under Claude Code they are answered by the engine's own accounting, via
`ClaudeSDKClient.get_context_usage()` — the same data the CLI's `/context` command renders.

This is a net upgrade rather than a replacement in kind. The old Context tab showed *our belief about*
the prompt, which could drift from what the provider actually received; a mismatch between the tier
display and reality was undetectable from inside the app. `get_context_usage()` is the engine
reporting on itself.

## Data Source

One call returns the whole picture:

| Field | What it gives the UI |
|---|---|
| `categories` | Per-category token counts **with the engine's own colours** — the primary display |
| `totalTokens`, `maxTokens`, `rawMaxTokens`, `percentage` | The budget gauge |
| `model` | Which model these numbers describe |
| `isAutoCompactEnabled`, `autoCompactThreshold` | The compaction forecast |
| `memoryFiles` | `CLAUDE.md` and memory files, with per-file token cost |
| `mcpTools`, `systemTools`, `deferredBuiltinTools` | Tool inventory and what each costs |
| `agents`, `skills`, `slashCommands` | What is available to the session |
| `systemPromptSections` | What the system prompt is made of |
| `messageBreakdown` | Conversation composition |
| `apiUsage` | Cumulative usage |
| `gridRows` | The CLI's pre-laid-out grid — kept for the debug view only |

Two deliberate choices about how this is consumed:

- **Use the engine's colours.** `categories` carries a `color`; adopting it means the tab and the CLI
  agree visually, so a user who has seen `/context` recognises the tab immediately.
- **Do not treat `gridRows` as layout.** It is a terminal's layout decision. Rendering it would couple
  our UI to a CLI presentation choice that can change under us. Lay out from `categories`; keep
  `gridRows` behind the debug view for cross-checking.

## Refresh Policy

`get_context_usage()` is a round trip to the engine, so it is fetched on state changes rather than on
a timer:

| Trigger | Why |
|---|---|
| `postResponseComplete` | A turn just changed the context |
| Compaction boundary | The context just shrank — the most interesting moment to show |
| Tab becoming visible while stale | The user is looking now |
| Manual refresh | Explicit user request |
| MCP server toggled or reconnected | Tool inventory changed |

The refresh-queue behaviour from the old tab is retained: overlapping triggers collapse into "one
in-flight fetch plus at most one queued fetch", rather than dropping the later trigger. A dropped
trigger leaves the tab showing pre-turn state with no indication it is stale, which is the specific
bug that behaviour was introduced to fix.

## Context Tab

Three sections, replacing the old Budget / Cache pill toggle.

### Usage

- **Budget gauge** — `totalTokens` against `maxTokens`, coloured by percentage, with the auto-compact
  threshold marked on the bar. The mark is the point: a gauge that shows only "68% full" does not
  answer "am I about to be compacted?"
- **Category bars** — a stacked proportional bar plus a legend, from `categories`, in the engine's
  colours. Deferred categories are marked as such rather than silently counted, since a deferred
  category is not currently occupying context.
- **Message breakdown** — conversation composition from `messageBreakdown`.

### Session

What this session is made of, and what each part costs:

- **Memory files** — every `CLAUDE.md` and memory file with its token cost, click-to-open in the
  viewer. This closes a long-standing blind spot: `CLAUDE.md` is the most-edited file in a Claude
  Code repo and its cost was previously unknowable.
- **System prompt sections** — what the engine prepended, and how big each part is.
- **Tools** — built-in, deferred built-in, and per-MCP-server, with token cost and health. Our own
  `ac-dc` server appears here like any other, which keeps us honest about what the bridge costs (see
  [mcp-bridge.md](mcp-bridge.md)).
- **Agents, skills, slash commands** — the inventory available to this session, sourced from project
  settings.

### Debug

Off by default, for diagnosing the engine rather than the code:

- Recent `HookEventMessage` traffic — which hooks fired, with what payloads.
- MCP server status detail from `get_mcp_status()`.
- Server info from `get_server_info()`.
- The raw `gridRows` payload.

## Usage HUD

The floating post-turn overlay survives; its content changes completely. Data comes from
`ResultMessage` rather than from a breakdown RPC, so it renders immediately without a second round
trip.

| Section | Source |
|---|---|
| Header | Model, context percentage, dismiss |
| This turn | `total_cost_usd`, duration, `num_turns`, terminal reason |
| Per-model usage | `model_usage` — input, output, cache read, cache creation, cost, context window, per model |
| Context | `totalTokens` / `maxTokens` with the auto-compact mark |
| Rate limits | `RateLimitEvent` state and reset time, when present |
| Permission prompts | Count for the turn |

Behaviour is unchanged: auto-hide after a few seconds, hover pauses, click dismisses, section collapse
state persisted, never shown for errored or empty turns.

### Cost under subscription billing

`total_cost_usd` may be absent or zero under a subscription, where per-token cost is not the billing
unit. The HUD must not render `$0.00` for a turn that consumed tokens — that reads as a broken HUD and
teaches users to ignore the number.

Instead: show a real figure when the engine reports one; otherwise label the billing mode and show the
usage figures that are always meaningful — tokens, context percentage, and rate-limit headroom.
`RateLimitEvent` is the subscription-mode analogue of a cost signal and gets first-class display for
exactly that reason.

`max_budget_usd`, when configured, appears as a spend-against-budget bar.

### Per-model usage is new information

`model_usage` is keyed by model, so a turn that delegated to a subagent on a cheaper model reports
both lines. The native engine could not express this — it had one model per request. It is worth
surfacing rather than summing, because "the expensive model did a little and the cheap model did a
lot" is the shape of a well-delegated turn and users should be able to see it.

## Terminal HUD

The server-side post-turn print survives in reduced form: model, per-model usage, cost or billing
mode, context percentage, and duration. Tier tables, cache-block boxes, promotion/demotion logs, and
the startup tier-distribution HUD are all deleted along with the tiering system they described.

## What Was Deleted

Named explicitly so their absence is understood as intentional rather than an oversight:

tier bars and the L0–L3 palette · N/threshold labels and stability bars · promotion and demotion logs ·
the manual cache-rebuild button · the measured-versus-unmeasured item split · synthetic `meta:` rows ·
the uncached synthetic tier · the tier-content fuzzy filter and sort toggle · the map-block modal ·
cache hit rate as a headline metric.

Cache hit rate deserves a note: the engine still caches, and `model_usage` still reports
`cacheReadInputTokens` and `cacheCreationInputTokens`, so the ratio is derivable and is shown in the
per-model rows. What is gone is its status as *the* headline number, because it was headline-worthy
only when AC⚡DC was the thing doing the caching.

## Invariants

- All context figures originate from `get_context_usage()` or `ResultMessage`; AC⚡DC never estimates
  or models context composition itself.
- Category colours come from the engine's payload, not from a local palette.
- The auto-compact threshold is always marked on the budget gauge when auto-compact is enabled.
- Overlapping refresh triggers collapse into one in-flight plus at most one queued fetch; no trigger
  is silently dropped.
- The HUD never displays a zero or absent cost as `$0.00`; it shows a real figure or the billing mode.
- The HUD never appears for errored or empty turns.
- Memory files, system prompt sections, and every MCP server — including `ac-dc` — appear in the
  Session section with their token cost.
- `gridRows` is never used for layout.
- The debug section is off by default and never required to understand normal usage.
