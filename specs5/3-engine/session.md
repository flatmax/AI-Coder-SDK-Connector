# Engine Session

AIC⚡DC drives one Claude Code session per repo, through the Claude Agent SDK. This spec covers the
session's lifecycle, how a user turn flows through it, and how the SDK's message taxonomy becomes
server-push events the browser can render.

The engine is not a provider behind an abstraction layer. There is no prompt assembly, no context
manager, no token budget, and no cache tiering — Claude Code owns all of it. AIC⚡DC's job is to
start a session, feed it user intent, and render what comes back.

See [`../plan/sdk-surface.md`](../plan/sdk-surface.md) for the verified SDK API this spec is written
against, and [`../../specs-reference/3-engine/session.md`](../../specs-reference/3-engine/session.md)
for exact option values and event payload shapes.

## Process Model

- One `ClaudeSDKClient`, connected for the process lifetime, `cwd` at the repository root.
- The client owns a `claude` CLI subprocess. AIC⚡DC never spawns the CLI directly.
- Connection happens during deferred startup, not on the fast path — the CLI takes a moment to
  come up and the browser must not wait on it. Requests arriving before the engine is ready are
  rejected with a user-facing "still starting" message, as they were under the native engine.
- Disconnect is part of graceful shutdown. A killed subprocess is detected and reported; the
  session is not silently re-created underneath the user, because a new session would have no
  context and the conversation would appear to develop amnesia.

## Session Options

Assembled once at connect time from engine config (see
[`../1-foundation/configuration.md`](../1-foundation/configuration.md)) plus AIC⚡DC's own
callbacks. The behaviourally load-bearing choices:

| Option | Behaviour it buys |
|---|---|
| `cwd` = repo root | Every tool path the agent produces is repo-relative, so the diff viewer and file tree resolve it without translation. |
| `permission_mode` | The user-visible safety posture. Live-switchable without reconnect. |
| `can_use_tool` | The browser permission dialog. See [permissions.md](permissions.md). |
| `hooks` | UI broadcasts and incremental re-indexing. See [tool-surface.md](tool-surface.md). |
| `include_partial_messages` | Token-level streaming. Without it the UI only updates per block, which reads as a stall on long responses. |
| `include_hook_events` | Hook activity becomes inspectable rather than invisible. |
| `setting_sources` = user, project, local | The repo's `CLAUDE.md`, `.claude/settings.json`, agents, skills, and custom slash commands all apply. A session in AIC⚡DC behaves like a session in the CLI in the same repo. |
| `env` = `CLAUDE_CODE_QUESTION_PREVIEW_FORMAT: markdown` | `AskUserQuestion` options may carry a `preview` — the example the dialog renders beside them. The field is in the tool's schema either way, but the CLI only documents its *format* for a terminal session — so unless the host names one, whether a preview arrives as markdown or as an HTML fragment is the model's guess, and the dialog renders markdown. See [`../5-webapp/permission-dialog.md` § `interact`](../5-webapp/permission-dialog.md#interact--real-choices). |
| `enable_file_checkpointing` + the replay-user-messages flag | `rewind_files()` — a real undo. **Both** are required; checkpointing alone raises at rewind time. Set **only when there is no `session_store`**, which means only a repoless run — see below. |
| `mcp_servers` = `{aic-dc: …}` | Repo intelligence as tools. See [mcp-bridge.md](mcp-bridge.md). |
| `session_store` | The transcript is mirrored into `.aic-dc/`. See [history.md](history.md). |
| `max_budget_usd` | An optional hard stop. Absent under subscription billing. |
| `effort`, `thinking` | Reasoning depth and whether thinking is shown, summarised, or hidden. |
| `max_buffer_size` = 16 MiB | The ceiling on one line of CLI stdout. The SDK's own default is 1 MiB and one line over it raises inside the reader, which kills the message pump for the rest of the session — so this is set unconditionally rather than left to the dependency. A returned inline screenshot is the case that reaches it. Overridable in `engine.json`; see [`../1-foundation/configuration.md`](../1-foundation/configuration.md). |
| `stderr` | A callback for the CLI's own diagnostics. Registering one *pipes* stderr instead of letting it inherit the server's, so the callback both logs the line and keeps the last 20 on `EngineHealth`, where the health banner shows them. Without it a CLI that explains its own failure explains it to nobody. |

Options that AIC⚡DC deliberately does **not** set:

- **`allowed_tools`** — allow rules approve a call before `can_use_tool` runs, which would make
  gated tools silently ungated. Tool-level allowances belong in project settings, where the user can
  see them, not in our options dict.
- **`agents`** — subagent definitions come from the project (`.claude/agents/`), so they are shared
  with the CLI rather than being AIC⚡DC-only.
- **`system_prompt`** — prompt customisation is `CLAUDE.md`'s job. Injecting our own system prompt
  would fork behaviour between AIC⚡DC and the CLI for the same repo, and would be invisible to the
  user in a file they do not know exists.

### The mirror and file checkpointing exclude each other

The SDK refuses `session_store` together with `enable_file_checkpointing`, and refuses it *at
connect* — "checkpoints are local-disk only and would diverge from the mirrored transcript". Setting
both costs the whole session, not just the undo, so one has to go and it is the undo: the mirror is
what history, resume, and the session browser are built on
([decisions § CC-20](../plan/decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)).

Consequences a reader of this file needs:

- Every run with a repo mirrors, so **`rewind_files()` is unavailable in practice**. The RPC refuses
  it with a message that names git rather than letting the SDK raise about local disks.
- A repoless run has nothing to mirror into, so it *does* get checkpointing. Undo is not a reason to
  refuse a session.
- The replay-user-messages flag goes with it. Its only job is the user-message ID `rewind_files()`
  takes, so `streamComplete.user_message_id` is `null` whenever the mirror is on.

## Request Flow

Unchanged in shape from the native engine, which is why the transport and reconnect logic survive:

1. Browser renders the user message immediately and generates a **request ID**.
2. Browser calls the streaming RPC; the server returns synchronously with a started status.
3. Server broadcasts `userMessage` to all clients. Persistence is not a step here — the CLI writes the
   user entry and mirrors it to our store during step 4 ([CC-19](../plan/decisions.md#cc-19)).
4. Server sends the turn to the engine via `query()` and starts a message pump.
5. Pump translates SDK messages into server-push events, all carrying the request ID.
6. Pump runs to `ResultMessage`, then finalises.
7. If that result arrived with background tasks still running, a **drain** keeps consuming the stream
   past it — see below.

### A result message ends a turn, not the run

A background subagent — `Task` with `run_in_background` — outlives the turn that spawned it, and the
SDK is explicit about it: a result frame arriving with tasks in flight leaves stdin open and logs
`Result received with N task(s) in flight`. It then goes on emitting for those tasks, and for main's
own reply once a task notification wakes it.

`receive_response()` stops at the result regardless, so step 6 must not be where consumption ends.
Instead the session keeps a **background drain**: a task that consumes `receive_messages()` with the
same `TurnTranslator`, and ends on a result message that arrives with nothing left in flight — the
SDK's own definition of the run ending.

Stopping at the turn's result instead left a background subagent's entire life unread, which showed
up as four unrelated-looking bugs: an empty subagent tab, an activity LED stuck on "status unknown at
turn end" for a subagent that had succeeded, a permission request attributed to no turn at all
(`_active_turn` was already gone), and main's closing answer missing. The CLI wrote all of it to the
transcript meanwhile, so it appeared if — and only if — the user reloaded the page, rendered from the
mirror as a malformed user turn.

What made it read as a rendering bug is that **nothing was lost**. The SDK's message stream is a
100-slot buffer the client owns, so the messages sat in it and whichever turn read next got them.
What was lost was *when*: they arrived attributed to a later turn, long after they meant anything
live. Two consequences the implementation depends on — a second iterator resumes rather than
replaying, and there must never be two: the drain is stopped, not merely signalled, before the next
turn's pump starts.

The result message carries `background_tasks`, the ids it did *not* end. The browser settles live
subagent tabs at the result and has no other way to tell "its terminal event never arrived" from "it
is still working" ([`../5-webapp/subagent-browser.md`](../5-webapp/subagent-browser.md) § Status
LEDs).

A cancelled turn is not followed. Stop is the user saying they are done with this work.

#### Every result the drain reads is emitted, flagged `continuation`

Main's reply to a task notification is a *further* turn on the same request, and it ends in a result
of its own. The drain emits those too, with `continuation: true` added.

Swallowing them looked defensible — the browser has already rendered this turn's footer, and a second
one would finish a finished turn — and it cost main's closing answer, which was the fourth symptom
above still standing after the drain fixed the other three. The answer was consumed, folded into the
turn's blocks, and rendered nowhere: the browser freezes a turn's blocks onto a settled message at its
result, and with no further result nothing ever re-froze it. The subagent's own blocks escaped only
because the subagent tab holds them by reference rather than frozen
([`../5-webapp/subagent-browser.md`](../5-webapp/subagent-browser.md) § Block mirroring).

The flag exists because the browser's two available readings of a result are both wrong for this one:
appending a second message repeats the whole turn, since every field on the payload is cumulative over
the request, and ignoring it drops the answer. What it does instead is **revise** the message it
already settled ([`../5-webapp/chat.md`](../5-webapp/chat.md) § A turn can end more than once).

Cumulative is a property the engine has to *maintain*, not just observe. Two kinds of field need work
for it to hold, and both are handled where the pump and the drain meet — `_fold_session_state` — so the
two cannot disagree about the arithmetic:

- **Differences the engine takes.** `turn_cost_usd` and `turn_model_usage` are derived against a
  baseline, and taking them per result would put a background subagent's spend — most of a delegated
  turn's cost, since its tokens are spent after the first result — into a footer nothing renders. The
  baseline is anchored **once per turn**, at admission
  ([`context-visibility.md` § Cost is cumulative](context-visibility.md#cost-is-cumulative-and-a-turn-is-a-difference)).
- **Counters the engine reports per result.** `duration_ms`, `duration_api_ms` and `num_turns` describe
  the result carrying them, so they are summed over the turn. Passed through as they arrive, the footer
  read *"2.7s · 1 engine turn"* for a turn that spent four times that across two of them. The sum is the
  engine's *working* time, not the wall clock — the gap while a background agent worked and main slept
  belongs to neither, and wall clock is what the browser's own run timer reports. A counter the engine
  did not send stays absent rather than becoming a zero.

A turn that ends once gets its own figures back unchanged, either way.

If the prompt carried pasted images, a `userMessageImages` follow-up joins step 4 — pointers to the
image blocks, sent when the mirror writes the user entry, because the entry `uuid` a pointer is built
from does not exist at step 3 ([`../4-features/images.md`](../4-features/images.md) § Engine Service
Integration).

### Request IDs remain the multiplexing primitive

The browser's request ID — not the SDK's `session_id` — correlates every server-push event with the
UI element that should receive it. SDK session IDs are per-session, not per-turn, so they cannot
distinguish two turns; and the collaboration, reconnect, and passive-adoption behaviours are all
built on request IDs already.

The server maintains a map from request ID to `(session_id, in-flight state, accumulated
transcript)`. The map is the resume point for a reconnecting client and the drain target for a
cancelled turn.

### Turn framing

A turn is not only the user's typed text. The server prepends a small, deterministic framing block
describing UI state the agent cannot otherwise see:

- The active file in the viewer, and the cursor or selection range when the user invoked from an
  editor gesture.
- Review-mode facts when review is active (branch, merge-base) — see
  [`../4-features/code-review.md`](../4-features/code-review.md).

Framing is small, bounded, and never file content. Everything the agent might want to *read* it
reads with its own tools; framing exists only to answer "what is the user looking at?", which no
tool can answer.

The test a candidate fact has to pass is narrower than that, though: **could the user reasonably have
typed it?** A list of paths from the file picker could — and did, as the framing block's first entry,
until [CC-21](../plan/decisions.md#cc-21) removed both the checkbox and the block. A path the user
wants named is named in the prompt, where `@path` makes the CLI actually read it, rather than out here
where a paragraph asked the model to consider it. What is left is the viewer's live cursor and the
review's shape, neither of which the user could sensibly retype every turn.

Images are not framing — they are content blocks in the message, passed through `query()`'s verbatim
dict path. See [`../4-features/images.md`](../4-features/images.md).

### Concurrency guard

One user-initiated turn at a time, as before. Subagents spawned by the `Task` tool are internal to
the turn and are not gated — the guard counts user intent, not engine activity.

A second user turn arriving mid-turn is rejected with an explanatory message rather than queued.
Queuing reads as a hang, and the user's intent is usually "stop and do this instead", which is a
cancel followed by a send.

**A routed slash command is not a turn and is not gated by this.** It never reaches `query()` — the
service answers it from the route table and the webapp opens a surface — so there is no second turn
for the guard to count. Which routes stay available mid-turn is a per-command property, not a
property of being routed; see [§ Mid-turn availability](#mid-turn-availability).

## Message Taxonomy → UI

The pump is the only component that knows SDK message types. Everything downstream sees AIC⚡DC
events.

| SDK message | Becomes | Rendered as |
|---|---|---|
| `SystemMessage(subtype="init")` | `sessionStarted` | Session banner: model, tool inventory, MCP server health. Seeds the request-ID → session-ID map. |
| `AssistantMessage` → `TextBlock` | `streamChunk` | Markdown in the assistant bubble. |
| `AssistantMessage` → `ThinkingBlock` | `thinkingChunk` | A collapsed "thinking" region, expandable. Respects the configured thinking display mode. |
| `AssistantMessage` → `ToolUseBlock` | `toolUse` | A tool card: name, summarised input, pending state. |
| `AssistantMessage` → `usage` | `turnUsage` | The live token counter under the streaming card ([`../5-webapp/chat.md`](../5-webapp/chat.md#live-token-counter)). The message carries what its own API call used; the pump sums those into a per-model running total for the turn and pushes `{turn_model_usage}` — the same shape and scope the result message ends with, so one renderer draws the running figure and the final one. Summed and not replaced, because each step of an agentic turn is its own API call and the turn is their sum. Subagent messages are counted too, under their own model. Nothing is emitted when a message reports no usable counter: the payload is the total, so an unchanged one would repaint the counter to say the same thing. |
| `UserMessage` → `ToolResultBlock` | `toolResult` | Result attached to its card by `tool_use_id`; collapsed by default, expandable, truncated with a "show all" affordance. |
| `StreamEvent` | `streamChunk` (partial) | Same coalescing path as full blocks. |
| `SystemMessage(subtype="compact_boundary")` | `compactionEvent` | A divider in the transcript recording that the engine compacted itself, with before/after token counts. |
| `SystemMessage(subtype="status")` | `compactionEvent` when it is about compaction, `systemEvent` otherwise | The engine's live compaction report: `status: "compacting"` starts the indicator, a later frame's `compact_result` / `compact_error` ends it. A failed compaction writes no boundary, so this is its only report. `requesting` and permission-mode changes ride the same subtype and stay generic. See [history.md § Compaction](history.md#compaction). |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskUpdatedMessage` / `TaskNotificationMessage` | `subagentEvent` | Subagent tabs. See [`../5-webapp/subagent-browser.md`](../5-webapp/subagent-browser.md). |
| `HookEventMessage` | `hookEvent` | Debug view only; not in the main transcript. |
| `RateLimitEvent` | `rateLimit` | A warning band in the usage HUD with reset timing. |
| `MirrorErrorMessage` | `engineHealth` | A banner: the repo-local transcript copy has a gap. Non-fatal — the turn continues. See [history.md](history.md). |
| `ResultMessage` | `streamComplete` | Turn footer: cost, per-model usage, duration, turn count, terminal reason. |

Only some system-message subtypes have dedicated SDK classes; `init`, `compact_boundary` and `status`
arrive as generic `SystemMessage` and are dispatched on the subtype string. Assistant content is likewise not
limited to text, thinking, and tool-use blocks. The pump must route unknown subtypes and unknown block
kinds to a generic rendering path rather than dropping them — a CLI upgrade that adds a block kind
should degrade to "shown but not specially styled", never to silence.

### Chunk semantics change

The native engine sent **full accumulated content** in every chunk, which made dropped or reordered
chunks harmless. That is no longer possible: a turn is now a sequence of heterogeneous blocks
(text, thinking, tool calls, results), and re-sending the whole turn on every token would be
quadratic in a way that actually matters for long agentic turns.

The new contract: each event carries a **block identity** plus its content, and content for a given
block is cumulative within that block. The browser keys rendered elements by block identity, so
ordering within a block is safe and ordering across blocks is preserved by arrival order. Frame-rate
coalescing is unchanged — the browser still batches to one render per animation frame.

### Two completion events survive, with new meanings

- `streamComplete` fires on `ResultMessage` — on *every* one for the request, not only the first, since
  a result ends a turn rather than the run. It finalises the assistant turn in the chat panel and
  carries the usage and cost figures. All but the first are flagged `continuation` and revise the turn
  the browser has already settled (§ *A result message ends a turn, not the run*).
- `postResponseComplete` fires after post-turn housekeeping (transcript mirroring flushed,
  re-indexing of touched files settled, context-usage refetched). Consumers that need consistent
  derived state — the Context tab, the file tree — wait for it. It fires a second time when a turn's
  background work finishes, because that housekeeping is exactly as stale then as it was at the first
  result; consumers must therefore be idempotent, and are (the browser re-dispatches it as a window
  event and the listeners refetch).

The split exists for the same reason it did before: the chat panel wants immediacy, the derived
views want consistency. Only the housekeeping behind the second event has changed.

## Cancellation

- The Send button becomes Stop during a turn, as before.
- Stop calls `interrupt()`.
- The pump **must drain to `ResultMessage`** before the next turn is read. `terminal_reason` will be
  `aborted_streaming` or `aborted_tools`. Skipping the drain routes the interrupted turn's tail into
  the next turn's UI.
- The pump never `break`s out of iteration. Cancellation is a flag plus `interrupt()`; the loop runs
  to completion. Breaking out causes asyncio cleanup failures, and a client disconnecting mid-turn is
  AIC⚡DC's normal case rather than an edge case.
- The turn's pump lifetime is independent of any WebSocket. A disconnected client's turn keeps
  running and accumulating server-side; the client re-attaches on reconnect and replays from the
  accumulated transcript.

## Session Continuity

| Action | Mechanism |
|---|---|
| Server restart | Reconnect with `resume=<last session_id>`. The engine restores its own context. |
| Load a previous session from the history browser | `resume=<that session_id>`. |
| Branch from a point in history | `fork_session` — leaves the original session intact. |
| New session | Connect without `resume`; a fresh session ID is issued. |
| Undo a file change | `rewind_files(user_message_id)` back to a checkpoint — unavailable while the transcript is mirrored, which is every run with a repo (CC-20). Git is the answer there. |

Context continuity is entirely the SDK's. AIC⚡DC never reconstructs a conversation by replaying
messages into a prompt — the failure mode of that approach is a session that looks right in the UI
and is subtly wrong in the model's view. See [history.md](history.md).

## Slash Commands

**A slash command goes to the CLI unless this deployment answers it better.** The CLI dispatches its
own built-ins in-process, before a model turn is billed — `/compact`, `/config`, `/effort`, `/doctor`
and the rest return real answers for zero turns and zero dollars, and `.claude/commands/`, skills and
plugin commands arrive the same way. Passthrough is therefore the default, and the two tables below
are the exceptions.

An unknown `/command` is passed through too. The CLI answers `Unknown command: /contxt`, which names
the mistake; guessing on its behalf is how the refusal table this replaced came to refuse commands
that had since shipped and would have answered.

**Routed** — passing through would reach a real command whose effect this deployment has a better
surface for, or would desynchronise the session store. Answered with `{status: "routed", target}`;
the webapp opens the surface named by `target`.

| Command | Target | Mid-turn | Surface |
|---|---|---|---|
| `/context` | `tab:context#usage` | Yes | The Context tab — live, not a one-shot print. See [context-visibility.md](context-visibility.md). |
| `/usage`, `/cost` | `tab:context#usage` | Yes | The same tab's cost and per-model breakdown ([`../5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md)). AIC⚡DC computes these figures itself from every result message, so the tab knows more than the CLI's one-shot print does — and unlike the print, it keeps being true. |
| `/mcp` | `tab:context#session` | Yes | The Context tab's MCP section: per-server connection state from `get_mcp_status()`, plus each server's tools and their token cost. |
| `/agents` | `tab:context#session` | Yes | The Context tab's subagent list — the definitions this session can delegate to, with what they have spent. |
| `/model` | `tab:settings#model` | Yes | The Settings tab's model panel: the alias in force, what the CLI resolves it to, and a switch that calls `set_model` for this session. See [`../5-webapp/settings.md` § Model Panel](../5-webapp/settings.md#model-panel). |
| `/permissions` | `tab:settings#permission-mode` | Yes | The Settings tab's `engine.json` card, opened with the `permission_mode` line selected — that field is the mode the *next* session starts in. The running session's mode is the selector beside the composer, which is always visible and deliberately not routable. |
| `/clear` | `new-session` | No | New Session. The CLI's own `/clear` would mint a session the store never saw, leaving every other client rendering the old transcript. |
| `/resume` | `history` | No | The history browser. Not in the CLI's command list at all. |

#### Target grammar

A target is `surface` or `surface#section`. The webapp opens the surface, then — when a section is
named — asks it to show that section.

**Every `tab:` target names a section.** A tab is not a destination, and each of the two has its own
reason. The Context tab remembers the section its reader last chose, so a bare `tab:context` opens
onto whatever that was: `/mcp` would land on Usage, which carries no MCP status at all. The Settings
tab has no memory to land wrong in — it opens onto a card grid, with the model panel above it and
every config field one click inside a file. Either way the command would have opened the right tab
and still not shown the thing it names.

**What a section means is the surface's business, not the grammar's.** Three answers so far, all to
the same question: on the Context tab it picks a segment; on Settings, `#model` scrolls a panel into
view and marks it, while `#permission-mode` opens a config card and selects the line that sets the
field. The webapp splits the fragment off and forwards it without knowing which of the three it will
get. That is what lets a surface whose "control" is a line of text still be a routable destination —
the alternative was building a duplicate control on the tab just so a route would have something to
point at, which is the drift `/permissions` came from.

Being *shown* a section is not the same as *choosing* it, so an arriving command does not overwrite
the remembered choice — the reader's own next visit still opens where they left off. An unknown
section is ignored rather than fatal: the surface still opens, on whatever it would have shown
anyway. A section is never a substitute for the surface being right, only for where inside it the
answer lives.

A section that names a field the file does not set is answered rather than mimed: `#permission-mode`
against an `engine.json` with no such key opens the card and says the key is absent and the engine's
own default is in force. Marking an empty spot would read as "here it is".

#### Mid-turn availability

**Mid-turn** is carried on the reply and on the `list_commands` entry as `during_turn`. The question it
answers is **"does answering this need a model turn?"** — not "is it routed?", and not "does it reach
the engine?". Those are three different questions, and conflating the first two is how an earlier
version of this section came to claim that nothing but local UI could run beside a live stream.

The SDK client exposes fifteen methods and **only `query()` starts a turn**. The rest are control
requests on a channel that runs *concurrently with* a streaming turn — `can_use_tool` is the proof,
since a turn blocks waiting for the browser to answer a permission dialog on exactly that channel. So:

| Class | Mid-turn | Why | Examples |
|---|---|---|---|
| Local UI | Yes | Nothing is asked of the engine at all. | `/permissions` → the Settings tab |
| Control request | Yes | Answered on the concurrent channel. | `/mcp` → `get_mcp_status()`, `/agents` and `/context` → `get_server_info()` / `get_context_usage()`, `/model` → `set_model()` |
| Needs a model turn | No | It is a turn, and the guard above allows one. | `.claude/commands/`, skills, anything answered in prose |
| Session swap | No | Would orphan the stream the user is watching. | `/clear`, `/resume` |

**Measured, not reasoned.** A prompt was sent on an isolated instance, and three seconds into the turn
`get_context_usage` and `get_mcp_status` were fired at it. Both answered 4.8s later with the stream
still running; the turn itself ran 42s. So the concurrency is real — and so is the latency. These
answer in seconds, not instantly, which is a fact the surfaces have to carry: the Context tab reads
"Reading context…" for several seconds when a command opens it mid-turn, and that is the feature
working. `get_server_info` came back in 2ms because it is answered from what `initialize` already
returned, without a control request at all.

The mechanism that makes a control-request command available mid-turn is **routing it**. Routing is what
keeps a command off `query()`; the surface it opens then issues the control request itself, which the
Context tab already does for all three of `get_context_usage`, `get_mcp_status` and `get_server_info`.
A control-request command left as passthrough is not mid-turn-capable, because passthrough means
`query()` — so `/mcp` as a `send` entry would be rejected by the guard however concurrent
`get_mcp_status()` is.

That is why the table above routes `/usage`, `/cost`, `/mcp` and `/agents` even though the CLI answers
all four for free. They are not *refused* passthrough; they are commands whose answer already exists
here, live and cumulative, in the one surface the user would otherwise have opened by hand.

Two consequences worth stating:

- **A command that is not routed is `during_turn: false`, without exception.** This is a fact about
  passthrough, not about the engine's concurrency: the CLI also processes its input stream serially, so
  such a command could at best answer *after* the turn rather than beside it.
- **`during_turn` gates the palette, not the RPC.** The two commands it withholds are already refused
  where they land — `new_session` and `resume_session` both answer `turn_in_progress` — so a second
  guard in the route table would only duplicate an answer that is better phrased at the surface that
  owns it. What `during_turn` buys is a palette that does not offer them.

`/model` is routed, and was the last control-request command that was not. What held it back was the
absence of a surface, not any doubt about `set_model`: routing a command to a tab with no model
control would have opened the wrong thing confidently. The Settings tab now has that control — its
own panel, above the card grid and outside it, because every card there applies to the *next*
session and this one applies now.

**A routed command's argument does not travel.** `/model sonnet` is a reasonable thing to type — the
CLI takes it that way — but routing opens a surface, and a surface takes no argument. The reply
therefore names what it dropped (`What follows it — 'sonnet' — was not applied`) rather than letting
the user believe they had switched models. This is a general property of routing, stated here because
`/model` is the command most likely to be typed with one.

#### What the model surface has to read

The model *in force* is in no file. `engine.json` holds a **request** — an alias like `opus`, or
nothing at all — the CLI resolves, and a `set_model` since then overrides it. So the surface reads
`get_model()`, which answers the alias, the CLI's own `alias → resolvedModel` mapping from the
handshake, and the list of models this engine offers. One call rather than two: the alternative was
`get_current_state`, which carries the whole rendered transcript, for one string.

Three answers it is careful about:

- **A null alias means nothing is pinned**, and is reported as null rather than as the string
  `"default"`. "Nothing is pinned" and "`default` is pinned" reach the same model today and need not
  after a CLI upgrade.
- **A resolution is never derived here.** This repo does not resolve aliases; if the handshake does
  not list the alias in force, `resolved` is null. A guessed model id is one the browser would quote
  back to somebody deciding what to spend.
- **An empty model list before the first turn is not an error.** The engine connects lazily, so there
  is no handshake to read yet — the same window that makes `list_commands` answer `partial: true`.
  The surface says so rather than showing an empty menu.

#### A mid-turn switch lands on the *next* turn

**Measured.** `set_model('haiku')` was fired 22.8s into a live 34s turn on an isolated instance. It
answered in **252ms** — an order of magnitude faster than `get_context_usage`, which gathers something —
and the turn kept streaming. The turn then went on billing `claude-opus-5`, including a `turnUsage`
report 124ms *after* the switch had landed and been broadcast. The next turn billed
`claude-haiku-4-5-20251001`.

So a mid-turn `set_model` is **accepted** mid-turn and **applies** to the turn after. Both halves are
load-bearing and neither is obvious: "the control request answers beside a live stream" is what makes
`during_turn: true` correct, and "the running turn keeps its model" is what the surface has to say out
loud. The person most likely to switch models mid-turn is the person watching an expensive turn run
away, and letting them believe they had just made it cheaper would be the worst thing this panel could
do.

`set_model` broadcasts `modelChanged` for the reason `set_permission_mode` broadcasts its own: the
model in force is session-wide, and another window would otherwise go on naming the model the session
started on. Unlike the permission mode, though, the *reply* is authoritative on its own — `Session.set_model`
records the new alias only after the control request came back — so the calling window may flip its
own control on the reply without waiting for the event.

**Denied** — passing through would reach for something this deployment does not have, or act on the
CLI host rather than the conversation. Answered with `{status: "unsupported"}` and the reason; never
forwarded as prose, which would turn a command into a question.

| Command | Reason |
|---|---|
| `/rewind` | No file checkpoints while the transcript is mirrored, which is every run with a repo (CC-20). Names git. |
| `/heapdump` | Writes a heap snapshot to the CLI host's desktop. |
| `/login`, `/logout` | Credentials are resolved from the environment at startup. |
| `/vim`, `/terminal-setup` | Terminal editing modes; there is no terminal here. |
| `/__remote-workflow`, `/workflow-launch-exec` | Belong to server-launched CLI sessions. |

### The `/` Palette

`list_commands()` answers the composer's autocomplete: the CLI's advertised list, minus denied
commands and minus names starting `_` (the CLI's marker for session plumbing), plus any routed
command the CLI does not advertise. Each entry carries `action` — `route` or `send` — a `target`, and
`during_turn`, so the webapp holds no second copy of the mapping and no second copy of the mid-turn
rule. Read from the initialize handshake rather than a table in the service, which is the only way a
newly-authored skill can appear without the engine being told it exists.

The palette opens when `/` is the first non-whitespace character in the composer and the cursor is
still inside that token — the same rule the engine uses to decide a message is a command, and the rule
a composer button satisfies by typing the `/` rather than working around it (see
[`../5-webapp/chat.md` § Slash Palette](../5-webapp/chat.md#slash-palette)). Selecting a
`send` entry completes the text in place; selecting a `route` entry clears the token and opens the
surface. With nothing matching, Enter is not consumed: a stray `/typo` stays sendable, because the
CLI's answer about it is better than the palette's.

**Before the engine connects, the answer is the routed commands and `partial: true` — not an error.**
The engine connects lazily on the first turn, so the entire pre-first-turn window has no handshake to
read, and that window is exactly when the palette is most wanted: the user is composing that first
turn, and two of the routes (`/resume`, `/clear`) are what somebody who has not started yet is
reaching for. Connecting from here instead would spend a 295 MB subprocess on a keystroke, and
`list_commands` is read-only, so a remote participant typing `/` would spawn the host's engine.
`partial` is the webapp's signal that the cache is worth replacing once the engine reports itself
connected; the flag is on the reply rather than inferred from the list's length, since a deployment
whose CLI advertises nothing is not the same condition.

**During a turn the palette still opens, and rows the turn blocks are visibly disabled with the reason
on them** rather than filtered out. A list that silently shrinks while streaming teaches the user that
commands come and go; a list where `/context` is live and `/compact` is greyed out with "available when
the turn ends" teaches them the actual rule, which is the one they need in order to predict it. Enter
and click are both refused on a disabled row, and focus skips it — an overlay that highlights a row it
will not act on is worse than one that does not offer it.

Most of the list is disabled while a turn runs, since most of it is passthrough. That is the honest
picture rather than an unfortunate one: the palette's job during a turn is to show which of these the
user can reach *now*, and the routed handful — context, cost, MCP, subagents, permissions — is exactly
the set someone watching a turn go by reaches for.

## Errors and Degradation

| Condition | Behaviour |
|---|---|
| CLI not found | Startup fails with an actionable message naming the searched locations and the install command. Not a silent degradation — nothing works without it. |
| CLI version below the SDK floor | Startup fails with both versions named. |
| CLI version differs from the SDK's pin but is above the floor | Warn, log both versions, continue. |
| Auth conflict or missing credentials | Visible banner naming the resolved credential source; the SDK's own warning is surfaced rather than logged. |
| Subprocess dies mid-turn | The turn ends with an error result. The session is **not** silently re-created; the user is told the session was lost and offered resume. |
| Budget exceeded | The turn stops with the SDK's terminal reason; the HUD shows the budget state and the settings control. |
| Rate limited | `RateLimitEvent` is surfaced with reset timing. The turn continues if the engine retries internally. |
| MCP server (ours) fails to start | Session continues without the `aic-dc` tools; a banner reports the loss, because the agent will otherwise appear inexplicably worse at repo-wide questions. |
| The CLI writes to stderr | Logged, and the last 20 lines are kept on `EngineHealth` and rendered in the health banner. Deliberately **not** a health problem in itself: the CLI writes routine chatter there, so the tail can neither open the banner nor undo a dismissal — it is context underneath whatever did open it. The case it was added for is a connect that fails, where the CLI's own words are the only diagnosis and the banner is already open. |

## Invariants

- Exactly one connected `ClaudeSDKClient` per process; it is never re-created behind the user's
  back to recover from an error.
- Every server-push event carries the request ID of the turn it belongs to; the transport never
  assumes a singleton turn.
- The message pump runs to `ResultMessage` for every turn, including cancelled and errored turns,
  and never exits the iterator via `break`.
- Consumption of the message stream outlives the turn whenever the turn's result arrives with
  background tasks in flight, and ends on a result that arrives with none. A background subagent's
  events are never first read by a later turn.
- Every result message for a request reaches the browser. All but the first carry `continuation`, and
  every one of them reports the *turn's* figures rather than the step since the previous result — the
  cost differenced from one per-turn anchor, the duration and engine-turn counters summed. The browser
  renders the last result it receives, and a background subagent's work all lands after the first.
- Exactly one consumer of the message stream at a time. A background drain is stopped and awaited
  before the next turn's pump starts, and on disconnect.
- A result message reports which background tasks it did not end.
- After `interrupt()`, the interrupted turn's messages are fully drained before any later turn's
  messages are read; a message arriving for a finalised request ID is dropped and logged, never
  re-routed.
- A turn's lifetime is independent of client connectivity; a client that disconnects and reconnects
  sees the complete turn.
- Only one user-initiated turn is in flight at a time; engine-internal subagent activity is not
  gated by that guard.
- Turn framing never contains file content — only paths, ranges, and mode facts.
- `streamComplete` always precedes `postResponseComplete` for the same turn. Housekeeping runs after the
  turn's first result, and **again when background work ends** — the drain flags its last continuation
  `background_finished`, and the service repeats the housekeeping on it. So a turn with no background
  work fires one `postResponseComplete`, and one that outlived itself fires two. Before the second, the
  derived views kept describing the session as it was before the background work and only picked the
  change up on the next turn.
- No component outside the message pump references an SDK message type.
- AIC⚡DC never constructs a conversation history to hand to the model; resumption is always via
  `resume` or `fork_session`.
