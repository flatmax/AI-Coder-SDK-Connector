# Tool Surface and Hooks

Everything the agent *does* arrives as a tool call. This spec covers how tool activity is displayed,
how AC⚡DC reacts to it (file broadcasts, re-indexing, viewer refresh), and the hook contract that
makes those reactions possible.

The companion surfaces are [permissions.md](permissions.md) (which tool calls are *asked about*) and
[mcp-bridge.md](mcp-bridge.md) (the tools AC⚡DC itself provides).

## Display Comes From the Stream

Tool activity is rendered from two always-firing sources:

- **`ToolUseBlock` / `ToolResultBlock`** in the message stream — the authoritative record, correlated
  by `tool_use_id`.
- **The `PreToolUse` hook** — earlier than the stream in practice, so the card can appear the instant
  the agent commits to a call rather than after the block is emitted.

Never from `can_use_tool`, which only fires on the ask path. See
[permissions.md § can_use_tool Is Not a Display Channel](permissions.md#can_use_tool-is-not-a-display-channel).

### Tool cards

One card per call, in transcript order, collapsed by default:

- **Header** — tool name, a one-line summary of the input (the path for file tools, the command for
  `Bash`, the pattern for search tools), and a state LED: pending, running, succeeded, failed,
  denied.
- **Expanded** — full input, full result, and duration. Long results are truncated with a "show all"
  affordance; the full text is always retrievable, because a truncated `Bash` output that hid the
  actual error is worse than a long card.
- **Failures are expanded by default.** A failed call is the one the user needs to see.

File-touching cards carry a click affordance that opens the touched file in the viewer at the changed
region — the transcript becomes a navigation surface, not just a log.

### `TodoWrite`

The agent's todo list is not rendered as a tool card. It is hoisted into a persistent checklist
pinned near the top of the turn, updated in place as the agent revises it. A todo list rendered as
twelve sequential cards is unreadable; rendered as one live list it is the best available answer to
"what is it doing and how far through is it?"

### Thinking

`ThinkingBlock` content renders in a collapsed region, honouring the configured display mode
(`summarized` or `omitted`). It is visually distinct from assistant text so a user skimming the
transcript is never unsure whether they are reading a conclusion or a deliberation.

## Hooks

AC⚡DC registers hooks for **observation only**. They broadcast, re-index, and record. They never
return a `permissionDecision` — a `PreToolUse` hook returning `allow` skips `can_use_tool`
entirely, which would silently disable the permission dialog.

| Hook | AC⚡DC uses it to |
|---|---|
| `PreToolUse` | Create the tool card immediately; start a timer for the duration display. |
| `PostToolUse` | Detect file changes → broadcast, re-index, refresh the viewer. The main reaction point. |
| `PostToolUseFailure` | Mark the card failed and surface the error; expand it by default. |
| `UserPromptSubmit` | Record the turn boundary — the request-ID mapping in the derived index, and the turn's own events in `events.jsonl`. **Not** used to inject context — see [decisions § CC-6](../plan/decisions.md#cc-6--the-indexes-reach-claude-code-as-mcp-tools-not-as-prompt-text). |
| `PreCompact` | Broadcast that compaction is about to happen, so the UI can show it rather than presenting an unexplained pause. |
| `Stop` | Turn boundary housekeeping; triggers `postResponseComplete`. |
| `SubagentStart` / `SubagentStop` | Create and retire subagent tabs. |
| `Notification` | Surface engine notifications as toasts. |
| `PermissionRequest` | Debug view and the prompts-per-turn metric. |

**Two of those are registered, and the rest are covered better elsewhere.** The table is what each event
would be *for*; the implementation subscribes to `PostToolUse` and `PreCompact` only, because most of the
rows describe facts the message stream already carries — a tool card comes from the assistant message's
own tool-use block, a failure from the `ToolResultBlock`, the turn's end from `ResultMessage`, subagents
from the four `Task*` messages, and the prompt from the fact that we sent it. `PreToolUse` and
`PermissionRequest` are refused on top of that, because a decision returned from either shadows
`can_use_tool` and silently ungates the session. `PreCompact` is the one row with no equivalent in the
stream: `compact_boundary` arrives when compaction has *finished*. The per-event reasons are the
`HOOK_EVENTS` table in `sdk_surface.py`, which the suite checks against what `hooks.py` actually
registers, in both directions — see [`../plan/sdk-surface.md`](../plan/sdk-surface.md).

`include_hook_events` is enabled so hook activity arrives as `HookEventMessage` and is inspectable in
the Context tab's debug view. A hook system that cannot be observed is a hook system that gets
debugged by print statement. Note that a `HookEventMessage` arrives **twice** per hook run — once as
`hook_started`, once as `hook_response` — so a browser branch keyed on the event name fires twice, which
is why the compaction toast is driven by the hook's own broadcast and not by the hook-event stream.

## Reacting to File Changes

The agent's writes are the event the rest of the app cares about most. On `PostToolUse` for a
file-mutating tool:

1. **Resolve the paths touched.** From the tool input, normalised repo-relative.
2. **Broadcast `filesModified`** with those paths. The file tree refreshes git status; the diff
   viewer reloads the file if it is open; the status LED updates.
3. **Re-index incrementally.** Only the touched paths, dispatched to the symbol index or document
   index by extension. Debounced, because an agent mid-refactor writes many files in quick
   succession.
4. **Re-run doc enrichment** for touched document files, in the background — the same deferred
   enrichment behaviour as before, now triggered by tool calls rather than by edit blocks.

### Snapshot discipline moves to tool-call boundaries

The native engine re-indexed at request boundaries and treated the indexes as read-only snapshots
within a request. That contract cannot survive: a single agentic turn may rewrite twenty files, and
there is no request boundary between the rewrite and the agent's next question.

The replacement contract: indexes are consistent snapshots **within a tool call**, and are refreshed
between calls. Concretely — a `symbol_map` call made after an `Edit` call returns a map that includes
the edit. Without this, our own MCP tool misleads the agent about code the agent itself just wrote,
which is worse than having no tool.

The debounce must therefore be bounded by the next `ac-dc` tool invocation: a pending re-index is
flushed synchronously before any index-reading tool answers.

## Checkpointing and Undo

With file checkpointing enabled (and the replay-user-messages flag, which is also required), each
user message becomes a restore point. The UI exposes an undo affordance on user messages:
`rewind_files(user_message_id)` restores every file to its state before that message.

This is a genuinely new capability — the anchored-edit pipeline had no undo, only git. It is
presented as "revert files to before this message", scoped to files, and explicitly does not rewind
the conversation, because a UI implying it rewound both when it rewound one would be actively
misleading.

## Bash and the Terminal Question

`Bash` output appears in tool cards, not in a terminal emulator. Long-running commands stream their
output into the card; `BashOutput` and `KillShell` calls attach to the same card.

AC⚡DC does not ship a terminal. A user who wants a terminal has one — the point of the frontend is
the surfaces a terminal cannot provide. What it does provide is the thing a terminal makes hard:
clicking a path in command output to open it in the diff viewer.

## MCP Servers

Third-party MCP servers configured in the project are available to the agent and appear in the
Context tab's tool inventory. Their calls are gated by default (see
[permissions.md](permissions.md)) and rendered as tool cards with the server name shown, so a call
into an external system is never visually indistinguishable from a local file read.

Server health comes from `get_mcp_status()`; `reconnect_mcp_server()` and `toggle_mcp_server()` are
exposed as per-server controls, localhost-only.

## Invariants

- Tool cards are created from the message stream and `PreToolUse`, never from `can_use_tool`; the
  rendered card count for a given turn is independent of permission mode.
- No AC⚡DC hook returns a `permissionDecision`.
- Every tool card reaches a terminal state (succeeded, failed, denied) or is explicitly marked
  abandoned when a turn is interrupted; no card stays pending forever.
- `tool_use_id` is the only correlation key between a call and its result.
- Every file-mutating tool call results in a `filesModified` broadcast naming the paths it touched.
- A pending incremental re-index is flushed before any `ac-dc` index-reading tool returns; an
  index-reading tool never reports state older than the most recent completed file-mutating tool
  call.
- Failed tool cards are expanded by default; successful ones are collapsed.
- Truncated tool results are always retrievable in full.
- The undo affordance rewinds files only, and says so.
