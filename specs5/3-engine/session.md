# Engine Session

AC⚡DC drives one Claude Code session per repo, through the Claude Agent SDK. This spec covers the
session's lifecycle, how a user turn flows through it, and how the SDK's message taxonomy becomes
server-push events the browser can render.

The engine is not a provider behind an abstraction layer. There is no prompt assembly, no context
manager, no token budget, and no cache tiering — Claude Code owns all of it. AC⚡DC's job is to
start a session, feed it user intent, and render what comes back.

See [`../plan/sdk-surface.md`](../plan/sdk-surface.md) for the verified SDK API this spec is written
against, and [`../../specs-reference/3-engine/session.md`](../../specs-reference/3-engine/session.md)
for exact option values and event payload shapes.

## Process Model

- One `ClaudeSDKClient`, connected for the process lifetime, `cwd` at the repository root.
- The client owns a `claude` CLI subprocess. AC⚡DC never spawns the CLI directly.
- Connection happens during deferred startup, not on the fast path — the CLI takes a moment to
  come up and the browser must not wait on it. Requests arriving before the engine is ready are
  rejected with a user-facing "still starting" message, as they were under the native engine.
- Disconnect is part of graceful shutdown. A killed subprocess is detected and reported; the
  session is not silently re-created underneath the user, because a new session would have no
  context and the conversation would appear to develop amnesia.

## Session Options

Assembled once at connect time from engine config (see
[`../1-foundation/configuration.md`](../1-foundation/configuration.md)) plus AC⚡DC's own
callbacks. The behaviourally load-bearing choices:

| Option | Behaviour it buys |
|---|---|
| `cwd` = repo root | Every tool path the agent produces is repo-relative, so the diff viewer and file tree resolve it without translation. |
| `permission_mode` | The user-visible safety posture. Live-switchable without reconnect. |
| `can_use_tool` | The browser permission dialog. See [permissions.md](permissions.md). |
| `hooks` | UI broadcasts and incremental re-indexing. See [tool-surface.md](tool-surface.md). |
| `include_partial_messages` | Token-level streaming. Without it the UI only updates per block, which reads as a stall on long responses. |
| `include_hook_events` | Hook activity becomes inspectable rather than invisible. |
| `setting_sources` = user, project, local | The repo's `CLAUDE.md`, `.claude/settings.json`, agents, skills, and custom slash commands all apply. A session in AC⚡DC behaves like a session in the CLI in the same repo. |
| `enable_file_checkpointing` + the replay-user-messages flag | `rewind_files()` — a real undo. **Both** are required; checkpointing alone raises at rewind time. |
| `mcp_servers` = `{ac-dc: …}` | Repo intelligence as tools. See [mcp-bridge.md](mcp-bridge.md). |
| `session_store` | The transcript is mirrored into `.ac-dc4/`. See [history.md](history.md). |
| `max_budget_usd` | An optional hard stop. Absent under subscription billing. |
| `effort`, `thinking` | Reasoning depth and whether thinking is shown, summarised, or hidden. |

Options that AC⚡DC deliberately does **not** set:

- **`allowed_tools`** — allow rules approve a call before `can_use_tool` runs, which would make
  gated tools silently ungated. Tool-level allowances belong in project settings, where the user can
  see them, not in our options dict.
- **`agents`** — subagent definitions come from the project (`.claude/agents/`), so they are shared
  with the CLI rather than being AC⚡DC-only.
- **`system_prompt`** — prompt customisation is `CLAUDE.md`'s job. Injecting our own system prompt
  would fork behaviour between AC⚡DC and the CLI for the same repo, and would be invisible to the
  user in a file they do not know exists.

## Request Flow

Unchanged in shape from the native engine, which is why the transport and reconnect logic survive:

1. Browser renders the user message immediately and generates a **request ID**.
2. Browser calls the streaming RPC; the server returns synchronously with a started status.
3. Server persists the user message to the mirrored store and broadcasts `userMessage` to all
   clients.
4. Server sends the turn to the engine via `query()` and starts a message pump.
5. Pump translates SDK messages into server-push events, all carrying the request ID.
6. Pump runs to `ResultMessage`, then finalises.

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

- The files currently selected in the picker, as paths — a hint about what the user is pointing at,
  not a content injection (see
  [decisions § CC-14](../plan/decisions.md#cc-14--file-selection-becomes-a-hint-not-a-context-contract)).
- The active file in the viewer, and the cursor or selection range when the user invoked from an
  editor gesture.
- Review-mode facts when review is active (branch, merge-base) — see
  [`../4-features/code-review.md`](../4-features/code-review.md).

Framing is small, bounded, and never file content. Everything the agent might want to *read* it
reads with its own tools; framing exists only to answer "what is the user looking at?", which no
tool can answer.

Images are not framing — they are content blocks in the message, passed through `query()`'s verbatim
dict path. See [`../4-features/images.md`](../4-features/images.md).

### Concurrency guard

One user-initiated turn at a time, as before. Subagents spawned by the `Task` tool are internal to
the turn and are not gated — the guard counts user intent, not engine activity.

A second user turn arriving mid-turn is rejected with an explanatory message rather than queued.
Queuing reads as a hang, and the user's intent is usually "stop and do this instead", which is a
cancel followed by a send.

## Message Taxonomy → UI

The pump is the only component that knows SDK message types. Everything downstream sees AC⚡DC
events.

| SDK message | Becomes | Rendered as |
|---|---|---|
| `SystemMessage(subtype="init")` | `sessionStarted` | Session banner: model, tool inventory, MCP server health. Seeds the request-ID → session-ID map. |
| `AssistantMessage` → `TextBlock` | `streamChunk` | Markdown in the assistant bubble. |
| `AssistantMessage` → `ThinkingBlock` | `thinkingChunk` | A collapsed "thinking" region, expandable. Respects the configured thinking display mode. |
| `AssistantMessage` → `ToolUseBlock` | `toolUse` | A tool card: name, summarised input, pending state. |
| `UserMessage` → `ToolResultBlock` | `toolResult` | Result attached to its card by `tool_use_id`; collapsed by default, expandable, truncated with a "show all" affordance. |
| `StreamEvent` | `streamChunk` (partial) | Same coalescing path as full blocks. |
| `SystemMessage(subtype="compact_boundary")` | `compactionEvent` | A divider in the transcript recording that the engine compacted itself, with before/after token counts. |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskUpdatedMessage` / `TaskNotificationMessage` | `subagentEvent` | Subagent tabs. See [`../5-webapp/subagent-browser.md`](../5-webapp/subagent-browser.md). |
| `HookEventMessage` | `hookEvent` | Debug view only; not in the main transcript. |
| `RateLimitEvent` | `rateLimit` | A warning band in the usage HUD with reset timing. |
| `MirrorErrorMessage` | `engineHealth` | A banner: the repo-local transcript copy has a gap. Non-fatal — the turn continues. See [history.md](history.md). |
| `ResultMessage` | `streamComplete` | Turn footer: cost, per-model usage, duration, turn count, terminal reason. |

Only some system-message subtypes have dedicated SDK classes; `init` and `compact_boundary` arrive as
generic `SystemMessage` and are dispatched on the subtype string. Assistant content is likewise not
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

- `streamComplete` fires on `ResultMessage`. It finalises the assistant turn in the chat panel and
  carries the usage and cost figures.
- `postResponseComplete` fires after post-turn housekeeping (transcript mirroring flushed,
  re-indexing of touched files settled, context-usage refetched). Consumers that need consistent
  derived state — the Context tab, the file tree — wait for it.

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
  AC⚡DC's normal case rather than an edge case.
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
| Undo a file change | `rewind_files(user_message_id)` back to a checkpoint. |

Context continuity is entirely the SDK's. AC⚡DC never reconstructs a conversation by replaying
messages into a prompt — the failure mode of that approach is a session that looks right in the UI
and is subtly wrong in the model's view. See [history.md](history.md).

## Slash Command Equivalents

Claude Code's built-in slash commands are terminal interface, not SDK features. Custom commands from
`.claude/commands/` pass through to the engine untouched; the built-ins are mapped here. An unmapped
`/command` returns an explicit unsupported response — it is never forwarded to the model as prose,
which would silently turn a mistyped command into a question.

| Command | AC⚡DC equivalent |
|---|---|
| `/context` | The Context tab — live, not a one-shot print. See [context-visibility.md](context-visibility.md). |
| `/compact` | Not exposed as a command; auto-compact is engine-owned and its boundaries are rendered. A manual compact affordance may be added later. |
| `/clear` | New Session. |
| `/model` | `set_model()`, from the Settings tab and a chat-panel model picker. |
| `/cost` | The usage HUD. |
| `/rewind` | The undo affordance on user messages (`rewind_files`). |
| `/permissions` | The Settings tab's permission-mode control plus the rules list. |
| `/mcp` | MCP server health in the Context tab, backed by `get_mcp_status()`. |
| `/agents` | Subagent inventory in the Context tab; live subagents in the tab strip. |
| `/resume` | The history browser. |
| `/login`, `/logout`, `/doctor`, `/bug`, `/help`, `/vim`, `/terminal-setup` | Not supported. Reported as CLI-only. |

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
| MCP server (ours) fails to start | Session continues without the `ac-dc` tools; a banner reports the loss, because the agent will otherwise appear inexplicably worse at repo-wide questions. |

## Invariants

- Exactly one connected `ClaudeSDKClient` per process; it is never re-created behind the user's
  back to recover from an error.
- Every server-push event carries the request ID of the turn it belongs to; the transport never
  assumes a singleton turn.
- The message pump runs to `ResultMessage` for every turn, including cancelled and errored turns,
  and never exits the iterator via `break`.
- After `interrupt()`, the interrupted turn's messages are fully drained before any later turn's
  messages are read; a message arriving for a finalised request ID is dropped and logged, never
  re-routed.
- A turn's lifetime is independent of client connectivity; a client that disconnects and reconnects
  sees the complete turn.
- Only one user-initiated turn is in flight at a time; engine-internal subagent activity is not
  gated by that guard.
- Turn framing never contains file content — only paths, ranges, and mode facts.
- `streamComplete` always precedes `postResponseComplete` for the same turn.
- No component outside the message pump references an SDK message type.
- AC⚡DC never constructs a conversation history to hand to the model; resumption is always via
  `resume` or `fork_session`.
