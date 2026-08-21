# Context Visibility

What does the agent currently know, what is it costing, and how close is it to compacting? Under the
native engine those questions were answered by AIC⚡DC's own model of the prompt it had assembled.
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

### Verified field shapes

The table above names the fields; it does not say what is inside them, and the first round of this work
guessed — then wrote tests that asserted the guess and passed while the app rendered transparent bars.
The shapes below are not guesses and not a single capture either. The bundled `claude` binary carries
the zod schema it validates this response against, and the element shapes are read from it:

| Field | Element shape |
|---|---|
| `categories` | `{name, tokens, color, isDeferred?}` |
| `memoryFiles` | `{path, type, tokens}` |
| `mcpTools` | `{name, serverName, tokens, isLoaded?}` |
| `deferredBuiltinTools` | `{name, tokens, isLoaded}` |
| `systemTools`, `systemPromptSections` | `{name, tokens}` |
| `agents` | `{agentType, source, tokens}` |
| `slashCommands` | `{totalCommands, includedCommands, tokens}` |
| `skills` | `{totalSkills, includedSkills, tokens, skillFrontmatter: [{name, source, tokens}]}` |
| `messageBreakdown` | `{toolCallTokens, toolResultTokens, attachmentTokens, assistantMessageTokens, userMessageTokens, redirectedContextTokens, unattributedTokens, toolCallsByType: [{name, callTokens, resultTokens}], attachmentsByType: [{name, tokens}]}` |
| `apiUsage` | `{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` or `null` |

Four consequences a capture alone could not have given us:

- **The reserve category has two names.** With autocompact on the engine calls the row "Autocompact
  buffer"; with it off it still holds tokens back and calls the row "Compact buffer". Under a window
  sized `auto` there is no reserve row at all. All of them are *room*, not content — a reader that only
  knows the first name counts the reserve as content, the sum overshoots `totalTokens`, and every
  autocompact-off session silently loses its segmented bar.
- **Two content rows carry theme tokens no capture showed**: `cyan_FOR_SUBAGENTS_ONLY` for "MCP tools"
  and `permission` for "Custom agents". A session with every MCP tool deferred and no custom agents —
  which is what got captured — reports neither, so both rows would have rendered uncoloured, including
  the one naming our own bridge.
- **`unattributedTokens` is derived and floored.** It is the Messages category minus the other six
  parts, clamped at zero, so the parts can legitimately sum to *more* than the category they belong to.
  A message bar must be drawn against the parts' own sum, and the discrepancy reported rather than
  hidden.
- **`percentage` is `round(totalTokens / rawMaxTokens * 100)`**, and `maxTokens` and `rawMaxTokens` are
  assigned from one variable — equal by construction, not merely equal in the capture.

One field is ours rather than the engine's. `memoryFiles[].path` is absolute, and the repo layer takes
paths relative to the root and rejects absolute ones outright, so `ClaudeCodeService.get_context_usage`
adds **`relPath`** to each entry inside the root — resolving symlinks on both sides, as the read itself
will — and leaves entries outside it unmarked. That is the difference between a clickable row and a row
that would offer to open a file the read path refuses. `session.get_context_usage` stays a pure
pass-through; the annotation happens in the layer that knows where the root is.

The engine also bands this pressure by *distance*, not proportion: its own reader warns within 20 000
tokens of the effective ceiling and treats the last 3 000 before the raw window as blocked. Worth
knowing before anyone tunes our percentage bands for a 1 M-token window, where 20 000 tokens is 2%.

### Verified field shapes — the result footer

Read from the same binary, for the same reason, and worth its own table because three of these fields
answer questions that look identical and are not:

| Field | Scope | Lifetime |
|---|---|---|
| `usage` | Main agent loop **only** — excludes Task subagents, sidechains, auxiliary calls | **Per turn** |
| `modelUsage` | **Every** model call in the query pipeline — main loop, subagents, sidechains, compaction, Workflow agents | **Cumulative** across the session |
| `total_cost_usd` | The same calls `modelUsage` covers | **Cumulative** across the session |

The schema's own instruction is *"Prefer modelUsage for token/cost accounting"* — but reading it as a
turn's usage is what put the session's running total under a heading that said "This turn". The two
fields differ in both scope and lifetime, in opposite directions, and neither of them is "what this turn
used". `turn_model_usage` and `turn_cost_usd` are; see § Cost is cumulative below.

The zod schema types `total_cost_usd` as a plain number. The SDK's `ResultMessage` dataclass declares it
`float | None = None` — that null is the *SDK's* default for a field the CLI did not send, not a value
the engine produces, and it is why a null has to mean something in our code at all.

`ModelUsage` element shape, camelCase on the wire (`claude_agent_sdk/types.py`, whose own docstring says
the value is "passed through verbatim from the CLI's `modelUsage` field"):

```
{inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens,
 webSearchRequests, costUSD, contextWindow, maxOutputTokens,
 canonicalModel?, provider?}
```

Two consequences, both of which were live defects:

- **There is no `modelName`.** The footer renderer preferred it over the map key, so its "friendly
  name" branch was dead. `canonicalModel` is the field that exists, and it earns the preference: on
  Bedrock or Vertex the key is a provider id like `us.anthropic.claude-opus-5-v1:0`.
- **The transcript on disk uses snake_case for the same counters.** A reader that knows one spelling
  works on exactly half its inputs — which is how a live turn came to render no per-model usage at all
  while the same turn, browsed back later, rendered correctly.

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
  `aic-dc` server appears here like any other, which keeps us honest about what the bridge costs (see
  [mcp-bridge.md](mcp-bridge.md)).
- **Agents, skills, slash commands** — the inventory available to this session, sourced from project
  settings.

### Debug

Off by default, for diagnosing the engine rather than the code:

- Which binary is running, from `EngineHealth` — path, version, source, the SDK's pin, the credential
  source, and the mirror-gap verdict. Pushed on `engineHealth`, and a push wins over a fetch because
  `mirror_gaps` moves during a turn. `get_server_info()` does not carry this: the resolution is ours.
- Server info from `get_server_info()` — the initialize reply, summarised by key and printed verbatim,
  since no schema for its shape has been read.
- Recent `HookEventMessage` traffic — which hooks fired, with what payloads, newest first and bounded.
- MCP server status detail from `get_mcp_status()`.
- The raw `gridRows` payload.

Two of these are pushes and two are fetches, and the split is the point: the fetches happen on the way
into the section, so a session that never opens Debug pays nothing for it, while the pushes are collected
from the moment the panel mounts — a hook log that begins when the reader arrives has already missed the
turn they came to ask about.

## Usage HUD

The floating post-turn overlay survives; its content changes completely. Data comes from
`ResultMessage` rather than from a breakdown RPC, so it renders immediately without a second round
trip.

| Section | Source |
|---|---|
| Header | Model, context percentage, dismiss |
| This turn | `turn_cost_usd` / `turn_cost_basis`, duration, `num_turns`, terminal reason |
| Per-model usage | `turn_model_usage` — input, output, cache read, cache creation, cost, context window, per model |
| Context | `totalTokens` / `maxTokens` with the auto-compact mark |
| Rate limits | `RateLimitEvent` state and reset time, when present |
| Permission prompts | Count for the turn |

Behaviour is unchanged: auto-hide after a few seconds, hover pauses, click dismisses, section collapse
state persisted. Not shown for an empty turn — but see the invariant below: an errored turn carrying
real usage is not an empty turn, and a turn that failed late is the most expensive kind there is.

### Cost is cumulative, and a turn is a difference

**Corrected in phase 6.** This section previously said `total_cost_usd` may be absent or zero under a
subscription. The CLI's own wire schema, read out of the bundled binary, says otherwise on both counts.

`total_cost_usd` is typed as a plain number with **no null branch**, and its `describe()` reads:
*"Cumulative estimated cost in USD for this query() call, covering the same query-pipeline calls as
modelUsage and sharing its lifecycle: cumulative across turns in streaming-input sessions — each result
carries the running total so far, so read the latest result rather than summing across results.
Crash/startup-error results may carry zeroed values, resumed sessions start fresh, and a mid-session
/clear resets the running total. An estimate, not a billing statement."*

So a null cost never comes from the engine — every one in this codebase is a footer AIC⚡DC wrote itself,
or a replayed turn whose cost the transcript does not record. Billing mode is not visible here at all;
`EngineHealth.credential_source` is the only signal for it.

Because the figure is cumulative, **this turn's cost is a difference against the previous result**. The
baseline outlives the turn, so it lives in `EngineSession` (`aic_dc/claude_code/cost.py`), folded into
`streamComplete` by the pump exactly as `engineHealth` already is — a `TurnTranslator` is one turn's
worth of state and could not hold it, and a browser holding it would lose it on reconnect and disagree
between clients. Three per-turn fields are added beside the engine's cumulative ones, never replacing
them: `turn_cost_usd`, `turn_cost_basis`, `turn_model_usage`.

`turn_cost_basis` records why there is no figure when there is none:

- `measured` — a difference in hand. **Zero is a real answer**: the turn cost nothing extra.
- `reset` — the reported total fell below the running one (a `/clear`, a resume, a fabricated footer).
- `unpriced` — no usable number: our own synthetic footers, or an errored footer the CLI zeroed. The
  schema's "crash/startup-error results may carry zeroed values" is exactly why a zero on an error is
  *no evidence* rather than free.

An `unpriced` result deliberately does **not** advance the baseline, so spend we could not attribute
lands on the next turn we can price rather than going missing or turning a later delta negative.
Genuine late failures — `error_max_turns`, `budget_exhausted`,
`structured_output_retry_exhausted`, `tool_deferred_unavailable` — carry the real running total, so
they stay `measured`; only fabricated footers fall through.

`max_budget_usd`, when configured, appears as a spend-against-budget bar.

### Per-model usage is new information

`modelUsage` is keyed by model, so a turn that delegated to a subagent on a cheaper model reports both
lines. The native engine could not express this — it had one model per request. It is worth surfacing
rather than summing, because "the expensive model did a little and the cheap model did a lot" is the
shape of a well-delegated turn and users should be able to see it.

It carries the same cumulative warning as the cost, and one more distinction worth reading twice. Its
`describe()`: *"Per-model totals for every model call made through the query pipeline during this
query() call — main loop, Task subagents, sidechains, and internal calls such as compaction and Workflow
agents. Cumulative across turns in streaming-input sessions."* By contrast `usage` is *"MAIN AGENT LOOP
ONLY — excludes Task subagent, sidechain, and auxiliary model calls, and is per-turn in streaming-input
sessions. Prefer modelUsage for token/cost accounting."*

So the two fields differ in **both** scope and lifetime, in opposite directions: `usage` is this turn but
not the whole turn; `modelUsage` is the whole session but every call in it. Neither is "what this turn
used", which is what `turn_model_usage` is for — the counters differenced, with `contextWindow`,
`maxOutputTokens`, `canonicalModel` and `provider` passed through verbatim, since differencing a context
window would report a 0-token window for every turn after the first. A model whose counters did not move
is dropped rather than reported as a row of zeroes, which would read as "answered, cost nothing".

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

Cache hit rate deserves a note: the engine still caches, and the per-model usage still reports
`cacheReadInputTokens` and `cacheCreationInputTokens`, so the ratio is derivable and is shown in the
per-model rows. What is gone is its status as *the* headline number, because it was headline-worthy
only when AIC⚡DC was the thing doing the caching.

## Invariants

- All context figures originate from `get_context_usage()` or `ResultMessage`; AIC⚡DC never estimates
  or models context composition itself.
- Category colours come from the engine's payload, not from a local palette.
- The auto-compact threshold is always marked on the budget gauge when auto-compact is enabled, and its
  absence is labelled when it is disabled. A gauge already measured *against* the threshold satisfies
  this at its own end and takes no second mark.
- Overlapping refresh triggers collapse into one in-flight plus at most one queued fetch; no trigger
  is silently dropped.
- No surface reads `total_cost_usd` or `model_usage` as a single turn's; they are the session's running
  totals and appear only labelled as such.
- A turn whose cost cannot be established is never displayed as `$0.00` and never as a claim about the
  billing plan; the reason is named. A turn that genuinely cost nothing extra reads differently from one
  whose cost is unknown.
- The HUD never appears for an empty turn. An errored turn carrying real usage is not an empty turn — a
  turn that fails late has usually spent the most.
- Memory files, system prompt sections, and every MCP server — including `aic-dc` — appear in the
  Session section with their token cost.
- `gridRows` is never used for layout.
- The debug section is off by default and never required to understand normal usage.
