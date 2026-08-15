# Reference: Engine Session

**Supplements:** `specs5/3-engine/session.md`

Option values, event payload shapes, and the SDK-dispatch details the pump needs. The behavioural
contracts — one client, drain-to-result, request-ID multiplexing — are in the parent spec.

Verified against `claude-agent-sdk` **0.2.137**. See `specs5/plan/sdk-surface.md` for how that surface
was established and re-read the installed wheel before implementing.

## Byte-level formats

### Request ID format

Unchanged from the native engine. Browser-generated, opaque to the server:

```
{epoch_ms}-{6-char-alphanumeric}
```

Example: `1736956800000-a1b2c3`. See `specs-reference/1-foundation/rpc-inventory.md` § Request ID
format.

Child request IDs for agent-internal streams are gone — subagents are addressed by SDK task and agent
IDs instead (see `specs-reference/3-engine/history.md` § Subagent keys).

### SDK session ID format

Canonical lowercase hyphenated UUID v4, issued by the CLI and first observed in the init system
message:

```
c3f1a2b4-5d6e-4f70-8a91-b2c3d4e5f607
```

AC⚡DC never generates one. The SDK validates the format on resume paths (`_validate_uuid`), so a
malformed ID fails at the SDK boundary rather than silently starting a new session.

Native-engine session IDs (`sess_{epoch_ms}_{uuid6}`) still appear in pre-conversion mirror records.
Readers must treat the session ID as an opaque string and must not parse either format.

### Block identity

Assigned by the pump, opaque to the browser:

```
{request_id}:b{n}          — text, thinking, and other content blocks; n is a per-turn counter
{tool_use_id}              — tool cards; the SDK's own `toolu_…` id
```

`n` is a monotonic per-turn integer, incremented when a block first appears — whether from a
`content_block_start` stream event or from an unseen block in a completed assistant message. The
map from SDK identity to `block_id` is pump-internal state, which is what keeps SDK message types
out of the transport.

The partial-block key is `(parent_tool_use_id or "", streaming message id, event["index"])`, where
the message id comes from the `message_start` event's `message.id`. **Not** `StreamEvent.uuid`: that
is a per-event identifier, unique to each delta, so keying on it makes every delta open a new block.
`parent_tool_use_id` is in the key because a subagent streams concurrently with its parent and both
number their content blocks from 0. Full blocks key on `(parent_tool_use_id or "", message_id or
uuid, content index)`, which is what lets a completed assistant message reuse — and correct — the
block its own partials already rendered.

Tool cards use `tool_use_id` directly because the tool result references it, so no correlation table
is needed on either side.

### Timestamp format

ISO 8601 UTC with full microsecond precision, `datetime.now(timezone.utc).isoformat()` — as in
`specs-reference/3-engine/history.md`. Exception: SDK-sourced timestamps are passed through in the
SDK's own units and are documented per field below (`RateLimitInfo.resets_at` is Unix **seconds**;
`SessionStoreListEntry.mtime` is Unix **milliseconds**).

## Numeric constants

| Constant | Value | Notes |
|---|---|---|
| Engine connect timeout | 60 s | CLI cold start. The bundled 295 MB binary's first exec is the slow case. |
| Interrupt drain timeout | 30 s | Time allowed for the interrupted turn to reach its result message. On expiry the client is disconnected and the session reported lost, rather than reading the next turn over an undrained buffer. |
| `load_timeout_ms` | 60 000 | SDK default, kept. Per `session_store.load()` / `list_subkeys()` call during resume materialization. |
| Re-index debounce | 250 ms | After a file-mutating tool call. Flushed synchronously before any index-reading tool returns. |
| Tool input summary | 200 chars, single line | Card header. Full input is available expanded. |
| Tool result preview | 4 000 chars or 120 lines, whichever is hit first | Card body; `truncated: true` and `full_bytes` accompany it. |
| Chunk coalescing | one render per animation frame (~16 ms) | Browser-side, unchanged. |
| Concurrent user turns | 1 | Engine-internal subagents are not counted. |
| `MINIMUM_CLAUDE_CODE_VERSION` | `"2.0.0"` | SDK-enforced floor; startup fails below it. |
| SDK CLI pin (`__cli_version__`) | `"2.1.229"` | Read from the private `claude_agent_sdk._cli_version`; mismatch above the floor warns, does not fail. A missing attribute reports `"unknown"`. |

## Schemas

### Options assembly

Built once at connect time. Fields whose config value is null are omitted from the call rather than
passed as `None`, so SDK defaults apply:

```python
ClaudeAgentOptions(
    cwd=str(repo_root),
    permission_mode=cfg.permission_mode,            # default "default"
    can_use_tool=permissions.can_use_tool,
    hooks=hooks.spec(),                             # see tool-surface.md
    include_partial_messages=True,
    include_hook_events=True,
    setting_sources=["user", "project", "local"],
    enable_file_checkpointing=True,
    extra_args={"replay-user-messages": None},      # mandatory partner of the line above
    mcp_servers={"ac-dc": bridge.server},
    session_store=store,
    session_store_flush="eager",
    model=cfg.model,                                # omitted when null → CLI default
    max_budget_usd=cfg.max_budget_usd,              # omitted when null
    effort=cfg.effort,                              # omitted when null
    thinking={"type": "adaptive", "display": cfg.thinking_display},  # "summarized" | "omitted"
    resume=session_id,                              # omitted for a new session
    fork_session=True,                              # only when branching
)
```

`extra_args` maps a flag name to its value; a value of `None` emits a bare `--replay-user-messages`.
Never set `allowed_tools`, `agents`, or `system_prompt` — see the parent spec.

`PermissionMode` is `Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
"auto"]`.

`thinking` is a TypedDict union (`ThinkingConfigAdaptive | ThinkingConfigEnabled |
ThinkingConfigDisabled`), not a class — there is no `ThinkingConfig(display=…)` constructor.
`"adaptive"` leaves the token budget to the model, which is the CLI's own posture; `display` is the
only part the user chose. Verified against 0.2.137 on 2026-08-14.

`cli_path` should be set to the binary AC⚡DC already resolved and version-checked. Left unset, the
SDK re-runs its own discovery, which has no fallback where ours does — so the binary named in the
engine-health record would not necessarily be the binary that runs.

### Service: ClaudeCodeService — browser → server

The RPC namespace is the Python class name (see § Dependency quirks), so every method below is
`ClaudeCodeService.*`.

State:

| Method | Arguments | Return |
|---|---|---|
| `get_current_state` | — | `EngineState` (below) |
| `get_engine_health` | — | `EngineHealth` (below) |
| `get_selected_files` | — | `list[str]` |
| `set_selected_files` | `files: list[str]` | `list[str]` — filtered; non-existent paths removed |
| `get_denied_read_files` | — | `list[str]` |
| `set_denied_read_files` | `files: list[str]` | `list[str]` — the resulting deny set after the rules are written |

`get_denied_read_files` / `set_denied_read_files` replace the native engine's
`get_excluded_index_files` / `set_excluded_index_files`. The rename is deliberate: nothing is excluded
from an index any more, and a method whose name describes a subsystem that no longer exists is worse
than a rename cost paid once. The state is a permission fact rather than an in-memory filter, and the
CLI honours it in the same repo.

The setter writes `Read(path)` deny rules **into `.claude/settings.local.json` directly**, and returns
`{denied_read_files: list[str], settings_file: str}`. An earlier draft said it writes them "through a
`PermissionUpdate`"; there is no API to hand one to. `PermissionUpdate` only reaches the CLI as
`PermissionResultAllow.updated_permissions` from inside a `can_use_tool` callback, and this call happens
outside any tool call — see `specs-reference/3-engine/permissions.md` § There is no runtime rule API.
The CLI reads the settings file itself, so the effect is the same and the mechanism is a file write.

Turns:

| Method | Arguments | Return |
|---|---|---|
| `chat_streaming` | `request_id: str, message: str, files?: list[str], images?: list[str], viewer?: ViewerFraming` | `{status: "started"}`, `{status: "unsupported", command, message, equivalent?}`, or `{error: str, reason: str}` |
| `cancel_streaming` | `request_id: str` | `{status: "interrupting"}` or `{error: str}` |

`images` are base64 data URIs, as before. `viewer` carries the active file and range for turn framing:
`{path: str, start_line?: int, end_line?: int}`. Framing never carries file content.

`{status: "unsupported"}` is the synchronous answer to an unmapped built-in slash command; the message
is never forwarded to the model.

Live controls:

| Method | Arguments | Return |
|---|---|---|
| `set_permission_mode` | `mode: PermissionMode` | `{mode: str}` or `{error: str}` |
| `set_model` | `model: str` | `{model: str}` or `{error: str}` |
| `rewind_files` | `user_message_id: str` | `{restored: list[str], user_message_id: str}` or `{error: str}` |
| `stop_task` | `task_id: str` | `{status: str}` or `{error: str}` |
| `resolve_permission` | `permission_id: str, decision: PermissionDecision` | see `specs-reference/3-engine/permissions.md` |

`rewind_files`'s `restored` list is **always empty**. The SDK's `ClaudeSDKClient.rewind_files()`
returns `None`, so the set of restored paths cannot be derived from that call — an earlier draft of
this table promised a list nothing can populate. The frontend should refresh the file tree on success
rather than trusting the list. Echoing `user_message_id` back lets a client correlate the answer with
the undo affordance it came from.

Introspection:

| Method | Arguments | Return |
|---|---|---|
| `get_context_usage` | — | `{usage: ContextUsageResponse, fetched_at: str}` or `{error: str}` |
| `get_mcp_status` | — | SDK passthrough |
| `reconnect_mcp_server` | `name: str` | SDK passthrough |
| `toggle_mcp_server` | `name: str, enabled: bool` | SDK passthrough |
| `get_server_info` | — | SDK passthrough — advertised commands, tools, output styles |

`ContextUsageResponse` is passed through unmodified; its shape is in
`specs5/plan/sdk-surface.md` § `get_context_usage()` return shape. `fetched_at` is ours, so the tab
can show staleness.

Sessions and history: see `specs-reference/3-engine/history.md` § RPC surface.

### `EngineState`

Returned by `get_current_state`. Replaces the native engine's `CurrentState`.

```pseudo
EngineState:
    messages: list[MessageDict]           // mirrored-store records, see history twin
    selected_files: list[string]
    denied_read_files: list[string]
    session_id: string | null             // null before the init message arrives
    repo_name: string
    init_complete: bool                   // AC⚡DC startup
    engine_ready: bool                    // client connected and init message seen
    streaming_active: bool
    active_streams: list[ActiveStream]    // empty when no turn in flight
    permission_mode: PermissionMode
    model: string | null
    pending_permissions: list[PermissionRequestPayload]
    doc_index_ready: bool
    doc_index_building: bool
    doc_index_enriched: bool
    enrichment_status: object             // the four doc-index fields arrive together
    review_state: ReviewState             // always present; check `active`
    engine_health: EngineHealth
    doc_convert_available: bool           // server capability probe, not engine state
    disk_warning: string | null           // one-shot; see below

ActiveStream:
    request_id: string
    session_id: string
    started_at: string                    // ISO 8601 UTC
    blocks: list[RenderedBlock]           // replay payload for a reconnecting client

RenderedBlock:
    block_id: string
    kind: "text" | "thinking" | "tool" | "system"
    seq: integer                          // highest seq emitted for this block
    content: string                       // cumulative text; empty for tool blocks
    tool: ToolCard | null                 // present when kind == "tool"
```

`blocks` replaces `accumulated_content`. A reconnecting client renders the list in order and then
resumes applying chunks, which is the same re-attach mechanism as before at block granularity.

`pending_permissions` lets a client that connects mid-prompt render the dialog immediately instead of
waiting for a broadcast it already missed.

`disk_warning` is the session-directory size warning, and it is the *same* one-shot as
`PostResponsePayload.disk_warning` — one flag behind two carriers, so it is delivered exactly once per
server lifetime whichever channel notices first. This snapshot is the one that covers "checked at
startup": first paint is the earliest moment a warning has somebody to reach.

`doc_convert_available` is the odd one out: a probe for whether `markitdown` imports on the server,
which has nothing to do with the engine. It is here because this snapshot is the only one the shell
fetches once the chat path leaves `LLMService`, and document conversion survives the conversion
unchanged. The shell gates the Doc Convert tab on it, so an `EngineState` without it would silently
retire a feature nothing in the plan retires. Absent field means unavailable.

### `EngineHealth`

```pseudo
EngineHealth:
    connected: bool
    cli_path: string                      // which binary was selected
    cli_version: string
    sdk_version: string
    sdk_cli_pin: string                   // __cli_version__; compare for skew warnings
    version_warning: string | null
    credential_source: string             // e.g. "subscription (…/.claude)", "ANTHROPIC_API_KEY", "bedrock"
    auth_warning: string | null
    mcp: list[{name: string, status: string, tool_count: integer}]
    mirror_gaps: integer                  // count of MirrorErrorMessage events this session
    last_error: string | null
```

### Service: AcApp — server → browser

Each returns `true` as acknowledgement. All turn-scoped events carry the originating `request_id`.

| Method | Arguments |
|---|---|
| `sessionStarted` | `request_id: str, data: SessionStartedPayload` |
| `streamChunk` | `request_id: str, chunk: ChunkPayload` |
| `thinkingChunk` | `request_id: str, chunk: ChunkPayload` |
| `toolUse` | `request_id: str, data: ToolCard` |
| `toolResult` | `request_id: str, data: ToolResultPayload` |
| `subagentEvent` | `request_id: str, data: SubagentEventPayload` |
| `hookEvent` | `request_id: str \| null, data: HookEventPayload` |
| `rateLimit` | `request_id: str \| null, data: RateLimitPayload` |
| `compactionEvent` | `request_id: str, event: {stage, …}` |
| `streamComplete` | `request_id: str, result: StreamCompleteResult` |
| `postResponseComplete` | `request_id: str, data: PostResponsePayload` |
| `permissionRequest` / `permissionResolved` | see permissions twin |
| `permissionModeChanged` | `data: {mode: str, by: str}` |
| `engineHealth` | `data: EngineHealth` |
| `userMessage` | `data: {content: str, request_id: str, files: list[str], image_refs: list[str]}` |

`userMessage` gains the three metadata fields so a collaborator's transcript matches the sender's.

Retained unchanged: `filesChanged`, `commitResult`, `sessionChanged`, `startupProgress`,
`navigateFile`, the collaboration callbacks, `docConvertProgress`. See
`specs-reference/1-foundation/rpc-inventory.md` § Service: AcApp.

#### `SessionStartedPayload`

From the init system message's raw `data` dict:

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | Seeds the request-ID ↔ session-ID map |
| `model` | string | Resolved model, not the requested alias |
| `cwd` | string | Should equal the repo root; a mismatch is a bug worth surfacing |
| `tools` | list[string] | Full advertised inventory including MCP tools |
| `mcp_servers` | list[{name, status}] | Per-server connection status |
| `slash_commands` | list[string] | Custom commands discovered from settings sources |
| `permission_mode` | string | The mode the CLI actually resolved |
| `raw` | object | The untouched `SystemMessage.data`, for the debug view |

Keys other than `raw` are read from `data` with `.get()`. The init payload is a CLI-owned dict, not a
typed SDK object (see § Dependency quirks), so unknown keys are expected and preserved in `raw`.

#### `ChunkPayload`

| Field | Type | Notes |
|---|---|---|
| `block_id` | string | See § Block identity |
| `seq` | integer | Monotonic per block from 0. The browser discards a chunk whose `seq` is not greater than the highest seen for that block |
| `content` | string | **Cumulative within the block**, not a delta. The latest chunk for a block supersedes earlier ones |
| `done` | bool | Optional; `true` on the block's final chunk |

Cumulative-within-block preserves the native engine's drop-tolerance at block granularity while
avoiding re-sending the whole turn on every token.

#### `ToolCard` and `ToolResultPayload`

```pseudo
ToolCard:
    tool_use_id: string          // also the block_id
    name: string                 // e.g. "Edit", "Bash", "mcp__ac-dc__symbol_map"
    server: string | null        // MCP server name when the tool is an MCP tool
    input_summary: string        // ≤ 200 chars, single line
    input: object                // full tool input
    status: "pending"
    gated: bool                  // whether a permission dialog was shown for this call
    agent_id: string | null      // non-null when the call came from a subagent

ToolResultPayload:
    tool_use_id: string
    status: "ok" | "error"
    preview: string              // truncated per § Numeric constants
    truncated: bool
    full_bytes: integer
    duration_ms: integer
    files_modified: list[string] // paths this call changed, empty for read-only tools
```

`status: "error"` results are expanded by default in the UI; the flag is what the frontend keys on.

#### `SubagentEventPayload`

| Field | Type | Notes |
|---|---|---|
| `type` | `"started"` \| `"progress"` \| `"updated"` \| `"notification"` | Source message |
| `task_id` | string | |
| `agent_id` | string \| null | SDK agent ID; the transcript key |
| `tool_use_id` | string \| null | The `Task` call that spawned it |
| `description` | string | |
| `task_type` | string \| null | `started` only |
| `status` | string \| null | `updated` (from `patch.status`) and `notification` |
| `last_tool_name` | string \| null | `progress` only |
| `usage` | `{total_tokens, tool_uses, duration_ms}` \| null | `progress`, and `notification` when present |
| `summary` | string \| null | `notification` only |
| `output_file` | string \| null | `notification` only |
| `terminal` | bool | Computed: `status in TERMINAL_TASK_STATUSES` |

`TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})`. A task can reach a
terminal status via `updated` with **no** `notification` — see § Dependency quirks.

#### `RateLimitPayload`

| Field | Type | Notes |
|---|---|---|
| `status` | `"allowed"` \| `"allowed_warning"` \| `"rejected"` | |
| `rate_limit_type` | string \| null | `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `overage` |
| `resets_at` | integer \| null | **Unix seconds**, not ISO and not milliseconds |
| `utilization` | float \| null | 0.0–1.0 |
| `overage_status` | string \| null | |
| `overage_resets_at` | integer \| null | Unix seconds |
| `overage_disabled_reason` | string \| null | |
| `raw` | object | Full CLI dict, camelCase keys |

#### `HookEventPayload`

| Field | Type | Notes |
|---|---|---|
| `phase` | `"hook_started"` \| `"hook_response"` | The SDK subtype |
| `hook_event_name` | string | `PreToolUse`, `PostToolUse`, `PermissionRequest`, … |
| `tool_name` | string \| null | When the event carries one |
| `outcome` | string \| null | `hook_response` only |
| `exit_code` | integer \| null | `hook_response` only |
| `raw` | object | Full event dict |

Debug view only; never rendered in the main transcript.

#### `compactionEvent` stages

The channel name is retained; the stage set changes. Stages that were native-engine-only
(`compacting`, `compacted`, `compaction_error`, `url_fetch`, `url_ready`) are gone.

| Stage | Extra fields |
|---|---|
| `pre_compact` | `trigger: str`, `custom_instructions: str \| null` — from the `PreCompact` hook |
| `compact_boundary` | `pre_tokens: int`, `post_tokens: int`, `trigger: str` — from the compact-boundary system message |
| `reindex` | `files: list[str]`, `pending: int` |
| `doc_enrichment_queued` | `files: list[str]`, `total: int` |
| `doc_enrichment_file_done` | `file: str`, `remaining: int` |
| `doc_enrichment_complete` | — |
| `doc_enrichment_failed` | `file: str`, `error: str` |

The four `doc_enrichment_*` stages are unchanged from the native engine — the document index survives
the conversion and so does its progress channel.

#### `StreamCompleteResult`

Assembled from the result message plus pump-local accounting:

| Field | Type | Present when | Notes |
|---|---|---|---|
| `session_id` | string | Always | |
| `response` | string | Always | Final assistant text, blocks concatenated in order |
| `subtype` | string | Always | SDK result subtype |
| `terminal_reason` | string \| null | Always | `completed`, `max_turns`, `aborted_streaming`, `aborted_tools`, … Null on older CLIs and local slash-command results |
| `is_error` | bool | Always | |
| `num_turns` | integer | Always | Engine-internal turns, not user turns |
| `duration_ms` / `duration_api_ms` | integer | Always | |
| `usage` | object \| null | Always | Aggregate token counts; snake_case keys from the API |
| `model_usage` | `{model: ModelUsage}` \| null | Always | Per-model; camelCase keys — see below |
| `total_cost_usd` | float \| null | Always | **Null under subscription billing.** Never rendered as `$0.00` |
| `tool_calls` | integer | Always | Pump-counted |
| `permission_prompts` | integer | Always | Pump-counted; feeds the click-through metric |
| `files_modified` | list[string] | Always | Union of tool-result paths; may be empty |
| `permission_denials` | list[object] \| null | When denials occurred | SDK passthrough |
| `deferred_tool_use` | object \| null | A `PreToolUse` hook returned `defer` | Should never occur — AC⚡DC hooks are observational |
| `api_error_status` | integer \| null | On API failure | HTTP status, e.g. 429, 500, 529 |
| `errors` | list[string] \| null | On error | |
| `cancelled` | bool | On interrupt | Derived from `terminal_reason` |
| `mirror_gap` | bool | A mirror append failed this turn | Sourced from `MirrorErrorMessage` |

`ModelUsage` fields: `inputTokens`, `outputTokens`, `cacheReadInputTokens`,
`cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`, `maxOutputTokens`, and
optionally `canonicalModel`, `provider`.

#### `PostResponsePayload`

| Field | Type | Notes |
|---|---|---|
| `files_reindexed` | list[string] | Paths whose symbol or doc entry was refreshed |
| `context_usage` | object \| null | Fresh `ContextUsageResponse`, or null when the fetch failed |
| `disk_warning` | string \| null | One-shot session-directory size warning |

## Dependency quirks

### Only some system subtypes are typed

`SystemMessage` subclasses exist for `task_started`, `task_progress`, `task_notification`,
`task_updated`, `mirror_error`, and hook events (`hook_started` / `hook_response` →
`HookEventMessage`). **Everything else falls through to a generic `SystemMessage(subtype, data)`** —
including `init` and `compact_boundary`. The pump must therefore dispatch on the `subtype` string for
those two and read their fields out of the raw `data` dict. There is no `CompactBoundary` class in
0.2.137.

### Assistant content blocks are not just three kinds

The parser emits `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`, and also
`server_tool_use` and `advisor_tool_result` blocks. A pump that matches exhaustively on three kinds
drops content silently. Unknown block kinds must fall through to a generic rendering path rather than
being discarded.

### A task can finish without a notification

`TaskUpdatedMessage` with a terminal `patch.status` may arrive with no `TaskNotificationMessage` — a
task stopped via `stop_task()` reports `status="killed"` this way. Subagent tab state must clear on a
terminal status from *either* message.

### `extra_args` values of `None` mean bare flags

`extra_args={"replay-user-messages": None}` emits `--replay-user-messages` with no value. This is the
mandatory partner of `enable_file_checkpointing=True`; without it `rewind_files()` fails at call time,
not at connect time.

### Interrupt semantics

`interrupt()` does not empty the message buffer. The pump must keep iterating to the result message,
and must not exit the iterator with `break` — breaking triggers asyncio cleanup failures, and a client
disconnecting mid-turn is the normal case here rather than an edge case.

### CLI discovery and version skew

The SDK transport resolves, in order: an explicitly configured `cli_path`, the bundled
`claude_agent_sdk/_bundled/claude`, then `claude` on `PATH`. **The bundled binary wins over `PATH`** —
verified in `SubprocessCLITransport._find_cli` at 0.2.137 on 2026-08-14, which checks
`_find_bundled_cli()` before `shutil.which("claude")`. An earlier draft of this section had `PATH`
second; it did not, so a machine with a newer system CLI still runs the wheel's copy unless
`cli_path` says otherwise.

Startup must log which one was selected and its version, because a system CLI newer or older than
`__cli_version__` changes behaviour in ways that are otherwise invisible. The SDK itself only *warns*
on skew; refusing to start on an unusably old CLI is AC⚡DC's job, not the SDK's.

### RPC namespace derives from the Python class name

`server.add_class(instance)` derives the namespace from `type(instance).__name__`. The service class is
`ClaudeCodeService`, so the RPC surface is `ClaudeCodeService.chat_streaming` and friends. Renaming the
class renames every RPC and breaks every frontend call site — the name is interface, not
implementation detail. See `specs-reference/1-foundation/rpc-inventory.md` § RPC prefix derivation.

### Credential resolution must not be polluted

The CLI authenticates through its own config (subscription login or `ANTHROPIC_API_KEY`). The native
engine's `llm.json` `env` block exported provider credentials into the process environment at startup,
which silently redirects the CLI to a different account or a Bedrock endpoint. That export is deleted
with the rest of the LiteLLM config; startup reports the resolved source in `EngineHealth`.

### `mcp` version floor

`claude-agent-sdk` 0.2.137 requires `mcp` ≥ 1.29.0. The dependency is now declared and locked at
`mcp` 1.29.0; nothing in the pre-existing set depended on `mcp`, so it resolved without touching any
other version. An earlier note here claimed a collision with a `doc_convert` pin of 1.14.1 — there was
no such pin. Full accounting of what the SDK pulls in:
`specs-reference/6-deployment/build.md` § What adding the SDK actually pulls in.
