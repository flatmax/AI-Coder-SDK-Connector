# Claude Agent SDK — Verified Surface

Ground truth for the conversion, read directly from the `claude-agent-sdk` **0.2.137** wheel
(`manylinux_2_17_x86_64`) rather than from documentation. Everything below was confirmed in
`claude_agent_sdk/types.py`, `client.py`, `_internal/sessions.py`, `_internal/transport/`, and
`_cli_version.py`.

Verified on 2026-08-12 against a local `claude` CLI at v2.1.227. The wheel pins
`__cli_version__ = "2.1.229"` and enforces `MINIMUM_CLAUDE_CODE_VERSION = "2.0.0"`. Both live in
private modules — `claude_agent_sdk._cli_version` and
`claude_agent_sdk._internal.transport.subprocess_cli` — not the public namespace, so reading them
is a deliberate coupling to SDK internals. `EngineHealth` needs the pin for skew warnings, so the
read is worth it, but it must be wrapped: a missing attribute reports "unknown pin" rather than
failing startup, because a private name can move in a patch release.

> **Do not re-derive this by guessing.** When implementing, re-read the installed wheel — the SDK
> moves fast and this file is a snapshot, not a contract the SDK owes us.

**Re-verified 2026-08-13** against 0.2.137 installed in the project venv (the first pass read an
unpacked wheel). The surface held: `ContextUsageResponse` carries every field CC-4 assumes,
`ResultMessage.terminal_reason` is real, `SessionStore` and the conformance harness exist, and the
session-management functions have the signatures recorded below. Two things were wrong here and are
now fixed — the four `Task*Message` classes were written without their `Message` suffix, and the CLI
pin had moved to 2.1.229. One thing was nearly "corrected" wrongly: `__cli_version__` is absent from
the *public* namespace but present in `claude_agent_sdk._cli_version`, so a `dir(claude_agent_sdk)`
check alone will tell you it does not exist. It does.

---

## Client

`ClaudeSDKClient(options: ClaudeAgentOptions)` — async context manager. Methods actually present:

| Method | Notes |
|---|---|
| `connect()` / `disconnect()` | Spawns / tears down the CLI subprocess. |
| `query(prompt: str \| AsyncIterable[dict], session_id="default")` | Message dicts are written **verbatim**, so multimodal content blocks (images) survive untouched. This is how AC⚡DC sends pasted images — see [`../4-features/images.md`](../4-features/images.md). |
| `receive_messages()` / `receive_response()` | The latter stops after `ResultMessage`. |
| `interrupt()` | Replaces our cancellation flag. |
| `set_permission_mode(mode)` | Live switch; no reconnect. |
| `set_model(model)` | Live switch. |
| `get_context_usage()` | **The `/context` data.** Basis of CC-4. |
| `get_mcp_status()` / `reconnect_mcp_server(name)` / `toggle_mcp_server(name, enabled)` | MCP server health surface. |
| `get_server_info()` | Advertised commands, tools, output styles. |
| `stop_task(task_id)` | Cancels a single background task / subagent. |
| `rewind_files(user_message_id)` | File-level undo to a checkpoint. Unreachable while a `session_store` is set — see below. |

### `rewind_files` has two prerequisites, and they cost the mirror

The origin brief mentions `enable_file_checkpointing=True`. That alone is insufficient — the client
also requires `extra_args={"replay-user-messages": None}`, because rewinding is keyed on a user
message ID that only exists if user messages are replayed back to the SDK. Missing the second flag
produces a runtime error at rewind time, not at connect time, so it will be found late unless
specified.

The third prerequisite is the one that decides the feature: **no `session_store`**.
`_internal/session_store_validation.py` raises `ValueError` from both `connect()` and `query()` for
that combination — "checkpoints are local-disk only and would diverge from the mirrored transcript" —
so a session with both starts nowhere. The mirror wins and undo goes
([CC-20](decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)); all three
flags are set together, only when there is no store.

The same validator has a second rule worth knowing: `continue_conversation` with a store requires the
store to implement `list_sessions()`. Ours does, and we pass `resume` explicitly rather than
`continue_conversation`, so it does not bite — but a minimal store would fail there too.

---

## Options — fields we set

From `ClaudeAgentOptions`. Fields AC⚡DC uses, and why:

| Field | Value | Reason |
|---|---|---|
| `cwd` | repo root | Every tool path resolves relative to the repo. |
| `permission_mode` | from config, default `"default"` | CC-1; live-switchable. |
| `can_use_tool` | our async callback | The permission dialog (CC-15). |
| `hooks` | see below | UI broadcasts and re-indexing (CC-7). |
| `include_partial_messages` | `True` | Token-level streaming → `StreamEvent`. |
| `include_hook_events` | `True` | Surfaces `HookEventMessage` so hook activity is visible in the transcript. |
| `setting_sources` | `["user", "project", "local"]` | CC-11 — `CLAUDE.md` and project settings apply. |
| `enable_file_checkpointing` | `True`, **only without `session_store`** | Undo. Requires the `extra_args` flag above, and excludes the mirror (CC-20). |
| `mcp_servers` | `{"ac-dc": <in-process server>}` | CC-6. |
| `max_budget_usd` | optional, from config | A hard stop the native engine never had. |
| `effort` / `thinking` | optional, from config | `thinking` is a TypedDict, not a class: `{"type": "adaptive", "display": "summarized" \| "omitted"}`. |
| `resume` / `fork_session` | on session load | CC-3. |
| `session_store` | our implementation | CC-3; mirrors the transcript into `.ac-dc4/`. |
| `session_store_flush` | `"eager"` | Batched flushing holds a turn's tail until the result message. |

`PermissionMode` is `Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
"auto"]` — six values, not the four the origin brief lists. `"plan"` and `"dontAsk"` both matter to
us: `"plan"` gives a read-only exploration mode worth exposing as a UI toggle, and `"dontAsk"` is a
distinct, less alarming middle ground than `"bypassPermissions"`.

---

## Message taxonomy

What comes out of `receive_response()`, and where each lands in the UI:

| Type | Contents | AC⚡DC surface |
|---|---|---|
| `SystemMessage(subtype="init")` | Session ID, model, tools, MCP servers, slash commands | Session banner; seeds the request-ID ↔ session-ID map |
| `AssistantMessage` | `TextBlock`, `ThinkingBlock`, `ToolUseBlock` | Chat message; thinking collapsed by default; tool-use cards |
| `UserMessage` | `ToolResultBlock` | Tool-result card attached to its call |
| `StreamEvent` | Partial deltas | Chunk coalescing at animation-frame rate |
| `ResultMessage` | `total_cost_usd`, `usage`, `model_usage`, `num_turns`, `duration_ms`, `is_error`, `terminal_reason` | Turn footer + usage HUD |
| `SystemMessage(subtype="compact_boundary")` | Pre/post token counts, trigger | A rendered divider in the transcript (CC-3) |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskUpdatedMessage` / `TaskNotificationMessage` | Background task and subagent lifecycle | Subagent tabs (CC-8) |
| `HookEventMessage` | Hook name, payload | Debug view in the Context tab |
| `RateLimitEvent` | Limit state and reset timing | HUD warning band |
| `MirrorErrorMessage` | Failed `SessionStore.append` batch | Health banner: the repo-local transcript has a gap |

### Only some system subtypes are typed

`SystemMessage` has dedicated subclasses for `task_started`, `task_progress`, `task_notification`,
`task_updated`, `mirror_error`, and hook events. **Everything else — including `init` and
`compact_boundary` — arrives as a generic `SystemMessage(subtype, data)`.** There is no
`CompactBoundary` class in 0.2.137; the origin brief and my own first pass both assumed one. The pump
dispatches on the subtype string and reads the raw `data` dict.

Assistant content blocks are also not just three kinds: the parser emits `server_tool_use` and
`advisor_tool_result` alongside `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, and `ToolResultBlock`. An
exhaustive three-way match drops content silently.

`ModelUsage` (per model, in `ResultMessage.model_usage`) carries `inputTokens`, `outputTokens`,
`cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`,
`contextWindow`, `maxOutputTokens`, and optionally `canonicalModel` / `provider`. This is a
strictly richer usage picture than the native engine's dual-mode getter chain, and it is
per-model — a turn that used a subagent on a cheaper model reports both.

`TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})` — the subagent
tab LED uses exactly this set to decide when to stop showing activity.

---

## `get_context_usage()` return shape

`ContextUsageResponse`, the basis of the rebuilt Context tab:

```python
categories: list[ContextUsageCategory]   # name, tokens, color, isDeferred?
totalTokens: int
maxTokens: int
rawMaxTokens: int
percentage: float
model: str
isAutoCompactEnabled: bool
memoryFiles: list[dict]          # CLAUDE.md + memory files, with per-file tokens
mcpTools: list[dict]
agents: list[dict]
gridRows: list[list[dict]]       # the CLI's visual grid, pre-laid-out
autoCompactThreshold: NotRequired[int]
deferredBuiltinTools: NotRequired[list[dict]]
systemTools: NotRequired[list[dict]]
systemPromptSections: NotRequired[list[dict]]
slashCommands: NotRequired[dict]
skills: NotRequired[dict]
messageBreakdown: NotRequired[dict]
apiUsage: NotRequired[dict | None]
```

Two things to note when building the tab. First, `categories` already carries a `color` — use the
engine's colours rather than inventing a palette, so the tab and the CLI agree visually. Second,
`gridRows` is a pre-laid-out grid; treating it as authoritative layout is tempting but couples our
UI to a CLI rendering decision. Read `categories` and lay out ourselves; keep `gridRows` for a
debug view.

---

## Hooks

Available hook events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`,
`Stop`, `SubagentStart`, `SubagentStop`, `PreCompact`, `Notification`, `PermissionRequest`.

`PreToolUseHookSpecificOutput.permissionDecision` accepts `"allow"`, `"deny"`, `"ask"`, **and
`"defer"`** — the fourth value is absent from the origin brief.

### The permission-shadowing hazard is real and the SDK warns about it

The SDK defines `CanUseToolShadowedWarning`, emitted when a `PreToolUse` hook returns a decision
that pre-empts the `can_use_tool` callback. This confirms hazard #1 in the origin brief and
sharpens it: **a `PreToolUse` hook returning `"allow"` also skips `can_use_tool`**, not just
`"deny"`. Our hooks must therefore be strictly observational — they broadcast and re-index, and
never return a `permissionDecision`. Any future hook that needs to influence permissions must do it
by feeding state into the `can_use_tool` callback, not by returning a decision.

There is also a `PermissionRequest` hook event, which the origin brief does not mention. It is a
better fit than `PreToolUse` for surfacing "a permission is being asked for" to the UI, because it
carries no risk of shadowing.

---

## Sessions

Module-level functions in `claude_agent_sdk` (`_internal/sessions.py`):

- `list_sessions(directory=None, limit=None, offset=0, include_worktrees=True) -> list[SDKSessionInfo]`
- `get_session_info(session_id, directory=None)`
- `get_session_messages(session_id, directory=None, limit=None, offset=0) -> list[SessionMessage]`
- `list_subagents(session_id, directory=None) -> list[str]`
- Mutations: `fork_session`, `rename_session`, `tag_session`, `delete_session`
- `*_from_store` variants of each, for use with a custom `SessionStore`

On-disk default locations (relevant even with a custom store, for the migration path):

- Main transcript — `~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl`
- Subagents — `~/.claude/projects/<project>/<sessionId>/subagents/agent-<agentId>.jsonl`

### `SessionStore` protocol

```python
async def append(key: SessionKey, entries: list[SessionStoreEntry]) -> None
async def load(key: SessionKey) -> list[SessionStoreEntry] | None
async def list_sessions(project_key: str) -> list[SessionStoreListEntry]
async def list_session_summaries(project_key: str) -> list[SessionSummaryEntry]
async def delete(key: SessionKey) -> None
async def list_subkeys(key: SessionListSubkeysKey) -> list[str]
```

Six methods, of which only `append` and `load` are required — the SDK probes for the rest by attribute
presence, so an unimplemented method degrades a feature silently. The SDK ships
`claude_agent_sdk.testing.session_store_conformance` — a conformance suite our implementation must
pass — and a `fold_session_summary()` helper for maintaining incremental summaries without re-reading
the whole transcript. This makes CC-3 substantially cheaper than it looks: the store is a first-class
extension point, not a hack.

**It is a mirror, not the primary write path.** `append()` is called *after* the subprocess's local
write succeeds, in batches at roughly 100 ms cadence; a failed batch is retried three times and then
dropped with a `MirrorErrorMessage`. So the store cannot provide write durability — but it can provide
resume: `_internal/session_resume.py` loads the store, materialises a temporary `~/.claude`-shaped
directory, and points the subprocess at it via `CLAUDE_CONFIG_DIR` when the local file is absent. That
is what makes a repo-local session survive the CLI's own `cleanupPeriodDays` sweep, and it is the real
argument for CC-3.

Two more options belong with it: `session_store_flush` (`"batched"` default, `"eager"` for
near-real-time appends) and `load_timeout_ms` (60 000 default, bounding each `load()` /
`list_subkeys()` during resume materialization). `import_session_to_store()` is the inverse path —
replay an existing local transcript into the store, idempotent on entry `uuid`, which makes it both a
migration tool and the repair for a reported mirror gap.

---

## Packaging: the wheel bundles a 295 MB CLI

`claude_agent_sdk/_bundled/claude` is a ~295 MB platform-specific binary, which is why the wheel is
`manylinux_2_17_x86_64` rather than `py3-none-any`. The transport prefers an explicitly configured
`cli_path`, then **the bundled copy, then a CLI on `PATH`** — `_find_cli` calls
`_find_bundled_cli()` before `shutil.which("claude")`. An earlier draft of this paragraph had those
last two the other way round; see [Corrections found while implementing phase 1](#corrections-found-while-implementing-phase-1).

Consequences for [`../6-deployment/packaging.md`](../6-deployment/packaging.md):

- AC⚡DC's wheel stops being pure-Python. Either we publish per-platform wheels, or we depend on
  the SDK and accept its platform constraint, or we ship an "external CLI" mode that requires
  `npm i -g @anthropic-ai/claude-code` and configure the path explicitly.
- A PyInstaller bundle that embeds the SDK embeds 295 MB. Current AC⚡DC bundles are a fraction of
  that.
- Version skew between a system CLI and the SDK's pin is a real failure mode. Startup must log
  which CLI was selected and its version.

See [`risks.md`](risks.md#r-7--bundled-cli-size-and-platform-specific-wheels).

---

## Environment notes

- **Resolved and locked.** `claude-agent-sdk>=0.2.136` is declared in `pyproject.toml` and locked at
  0.2.137. It adds nine packages (`mcp` 1.29.0 plus its HTTP/SSE transport stack) and changes the
  version of nothing already present. `doc_convert` 188 tests green; full suite 3 480 passed.
  Per-package accounting: [`../../specs-reference/6-deployment/build.md`](../../specs-reference/6-deployment/build.md)
  § What adding the SDK actually pulls in.
- **Correction to an earlier draft of this file.** It claimed the `mcp` floor collided with a
  `doc_convert` pin of 1.14.1 and that the bump needed co-testing. There was no such pin — `mcp` was
  not in `uv.lock` at all, and `markitdown[all]` does not depend on it. The 1.14.1 reading came from
  `litellm`'s `proxy` extra in `/home/flatmax/.venv`, a virtualenv that contains neither `ac_dc` nor
  `markitdown` and is not this project's environment. The repo had no materialised venv at the time.
  The surviving constraint is a packaging one, not a resolution one: six transport packages enter the
  dependency set that an in-process MCP server never serves.
- Authentication conflicts: the SDK/CLI authenticates via its own config (subscription login or
  `ANTHROPIC_API_KEY`). AC⚡DC's `llm.json` `env` block currently exports provider credentials into
  the process environment on startup, which can silently redirect the CLI to a different account or
  a Bedrock endpoint. The env-export step must be removed with the rest of the LiteLLM config
  (CC-11), and startup should report which credential source the CLI resolved.

---

## Corrections to the origin brief

Collected in one place, because the brief is otherwise a good design document and will be read.

| Brief says | Actually |
|---|---|
| Native engine remains the default; this is a mode | Total replacement (CC-1) |
| Four permission modes | Six: `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto` |
| `enable_file_checkpointing=True` enables `rewind_files` | Also requires `extra_args={"replay-user-messages": None}`, and is refused outright alongside `session_store` — so the mirror and undo cannot both exist (CC-20) |
| `PreToolUse` deny shadows `can_use_tool` | `allow` shadows it too; the SDK raises `CanUseToolShadowedWarning` |
| Permission decisions are allow / deny / ask | Also `defer` |
| Hook list | Missing `PermissionRequest` and `SubagentStart` |
| Client sketch | Missing `get_context_usage`, `get_mcp_status`, `get_server_info`, `stop_task`, `toggle_mcp_server`, `reconnect_mcp_server` |
| Context visibility is an open question | Answered: `get_context_usage()` (CC-4) |
| Packaging unaddressed | The wheel carries a 295 MB platform-specific CLI |
| A `CompactBoundary` message type | No such class — `SystemMessage(subtype="compact_boundary")` |
| `SessionStore` owns the transcript | It mirrors the CLI's local transcript, after the local write |
| Message list | Missing `MirrorErrorMessage`; assistant blocks also include `server_tool_use` and `advisor_tool_result` |

---

## Corrections found while implementing phase 1

**Verified 2026-08-14** against 0.2.137 in the project venv, bundled CLI 2.1.229, plus two live runs
of `scripts/engine_smoke.py`. Each row below is a place where a document in this repo contradicted the
installed wheel or the observed CLI. Per
[`../0-overview/implementation-guide.md`](../0-overview/implementation-guide.md) § Reading the SDK rule
2, the wheel wins and the spec was corrected rather than worked around.

| Where | Said | Actually | Fixed in |
|---|---|---|---|
| This file, § Packaging | `_find_cli` order is `cli_path` → `PATH` → bundled | `cli_path` → **bundled** → `PATH`; `_find_bundled_cli()` is checked before `shutil.which` | this file |
| `specs-reference/3-engine/session.md` § Options | `thinking=ThinkingConfig(display=…)` | `thinking` is a TypedDict union with no such constructor; correct value is `{"type": "adaptive", "display": …}` | that file |
| `specs-reference/3-engine/session.md` § Block identity | Partial-block key is `(StreamEvent.uuid, event["index"])` | `StreamEvent.uuid` is per-*event*, so every delta would open a new block. Key is `(parent_tool_use_id or "", message_start's message.id, index)` | that file |
| `specs-reference/3-engine/session.md` § RPC | `rewind_files` returns `{restored: list[str]}` | `ClaudeSDKClient.rewind_files()` returns `None`; the list cannot be populated from that call | that file |
| `specs-reference/3-engine/session.md` § CLI discovery | Implied the SDK enforces the version floor | The SDK only *warns* on skew. Refusing to start on an unusable CLI is ours to do | that file |

### Not a correction, but absent from every spec

- **`ConversationResetMessage`** is in the SDK's `Message` union (`new_conversation_id`, `uuid`,
  `session_id`) and appears in no spec. Routed to `systemEvent` rather than dropped.
- **`SystemMessage(subtype="status")`** — observed live, in no spec and in no SDK type. The CLI emits
  it before each model request, carrying `{"status": "requesting"}`. It arrived four times in a
  three-tool-call turn. Currently falls through to `systemEvent`, which is the designed behaviour for
  an unknown subtype; if the UI ever wants a "thinking…" indicator that is not tied to a thinking
  block, this is the signal to use.
- **`ThinkingConfigAdaptive.display`** is annotated `NotRequired[Literal["summarized", "omitted"]]`,
  so `typing.get_args` needs one layer unwrapped before it yields the two strings. Relevant only to
  the drift test that probes it.

### Observations from the live runs

Both runs used `--permission-mode plan`, so nothing was written.

- **The taxonomy the pump actually produced**, from one three-tool-call turn: `sessionStarted`,
  `systemEvent` ×4, `toolUse` ×3, `toolResult` ×3, `streamChunk` ×36, `thinkingChunk`, `streamComplete`.
  No channel went unexercised except the ones phase 1 has no source for yet (`subagentEvent`,
  `hookEvent`, `rateLimit`, `compactionEvent`, `engineHealth`).
- **`--replay-user-messages` works**: `user_message_id` came back populated
  (`07b76464-…`), which is the ID `rewind_files()` will need in phase 3.
- **The interrupt drain works against the real CLI.** Cancelling 4 s in produced a genuine
  `ResultMessage` with `terminal_reason: "aborted_streaming"`, `subtype:
  "error_during_execution"`, `is_error: true`, and a populated `errors` list carrying an
  `[ede_diagnostic]` string. The pump ran to the result rather than hanging, and `cancelled` was
  reported `true` — note that an interrupted turn is `is_error: true`, so the UI must read
  `cancelled` before deciding whether to show a failure.
- **`total_cost_usd` is populated on Bedrock** (0.273 and 0.0034 for the two runs). It is not
  subscription-only, so the "null means not priced" path stays untested here.
- **The credential warning fires as designed.** This machine has `$CLAUDE_CODE_USE_BEDROCK` set *and*
  a subscription login at `~/.claude/.credentials.json`; `detect_credentials()` reported
  `Amazon Bedrock (via CLAUDE_CODE_USE_BEDROCK)` and warned about the shadowed login. This is exactly
  the CC-11 pollution case, arriving from the ambient environment rather than from `llm.json`.

---

## Corrections found while implementing phase 2

**Verified 2026-08-14** against 0.2.137 in the project venv and the bundled CLI 2.1.229 — the latter read
directly (`strings` over `_bundled/claude`) where the Python surface does not describe the behaviour, as
with `AskUserQuestion`'s answer plumbing. Same rule as phase 1: the wheel and the CLI win, and the spec
was corrected rather than worked around.

| Where | Said | Actually | Fixed in |
|---|---|---|---|
| `specs-reference/3-engine/permissions.md` § Dependency quirks | "The callback runs on the SDK's read loop", so a slow decision stalls the turn | It does not. `Query._read_messages` hands each `control_request` to `_spawn_control_request_handler` → `spawn_task` → `spawn_detached`, tracked in `_inflight_requests`. Concurrent permission requests are genuinely concurrent | that file |
| `specs-reference/3-engine/permissions.md` § The callback signature | `ToolPermissionContext` has six fields | Nine in 0.2.137: `title`, `display_name` and `description` are the CLI's own prompt copy for the call, and are what a dialog should prefer over a summary of ours | that file |
| `specs-reference/3-engine/permissions.md` § Return types | `PermissionRuleValue` importable from the package root | Not re-exported — `"PermissionRuleValue" in dir(claude_agent_sdk)` is `False`. Import the permission types from `claude_agent_sdk.types` | that file |
| `specs-reference/3-engine/permissions.md` § `PermissionUpdate` | `destination: "session"` is how the file picker's deny-read rule is applied | There is **no runtime rule API** at all. A `PermissionUpdate` reaches the CLI only as `PermissionResultAllow.updated_permissions`, from inside a callback. The file picker's gesture happens outside any call, so the only mechanism is writing `.claude/settings.local.json` ourselves | that file |
| `specs-reference/3-engine/session.md` § Denied reads | `set_denied_read_files` applies rules "through a `PermissionUpdate`" | It writes them into `.claude/settings.local.json` and returns `{denied_read_files, settings_file}`. Same root cause as the row above | that file |
| `specs-reference/3-engine/permissions.md` § `QuestionPayload` | `AskUserQuestion` asks one question | It takes a **list** of 1–4, each with its own `header`, `options` (2–4) and `multiSelect` | that file, and `specs5/5-webapp/permission-dialog.md` |
| `specs-reference/3-engine/permissions.md` § resolve_permission | Allowing an `AskUserQuestion` call answers it | It does not. The tool reads `answers` off its own input; allow it without that key and the model is told "The user did not answer the questions". Needs `updated_input={**input, "answers": {question text: label}}`, multi-select joined with `", "` | that file (new § Answering an `interact` request) |
| `specs-reference/3-engine/permissions.md` § Decision mapping | "Allow with edited input" is offered for every write tool | `Edit` and `MultiEdit` cannot take one: their input is a list of `old_string` → `new_string` replacements and the editor works on whole-file content. Offered for `Bash`, `Write` and `NotebookEdit` only | that file, and `specs5/5-webapp/permission-dialog.md` |
| `specs-reference/3-engine/permissions.md` § Classification map | `TodoWrite` unclassified | Classified `read`. It writes nothing on disk, and gating it would prompt several times per turn — the click-through trainer R-12 exists to prevent | that file |
| `specs5/5-webapp/chat.md` § Thinking Regions | Thinking blocks carry a token count | No payload has one. `thinkingChunk` is `{block_id, seq, content, done}` and `usage` is a single total over text, tools and thinking together. The number would have to be invented | that file |
| `specs5/5-webapp/chat.md` § Card Anatomy | A truncated tool result gets a "show all" | The engine sends only the preview; the untruncated text never leaves the server, so the button would expand to what it already showed. It names the full byte count instead | that file |
| `specs5/5-webapp/chat.md` § Diff Rendering | A `Write` card diffs against the file on disk | The `toolUse` payload carries only the new content, and the panel has no read path that could fetch the old side without racing the write it is describing. It renders as an all-add diff; a real before/after belongs to the diff viewer | that file |
| `specs5/5-webapp/chat.md` § Invariants | Every client's history is identical, including compaction boundaries | `SessionStore` is a phase-5 deliverable and nothing constructs one, so the `compact_boundary` divider is client-side only and does not survive a reload. The rest of the invariant holds | that file |
| `specs5/4-features/collaboration.md` § Enforcement | A per-message caller attribute is enough | It must be a `ContextVar`. An `async def` method's body runs a loop iteration after dispatch, by which point the attribute is cleared — and "no caller" means "trusted", so every localhost gate on every async method passed for remote callers | that file |
| `specs5/4-features/collaboration.md` § Localhost-Only | Lifecycle methods unlisted | `connect_engine` and `shutdown` are localhost-only. Neither reads like a mutation of repository state, which is why they were missed | that file |
| `specs5/plan/inventory.md` § Frontend — DELETE | `edit-block-render.js` and `url-helpers.js` are deleted | `edit-block-render.js` draws the diff body of every write tool card; `url-helpers.js` is `main.js`'s WebSocket URI parser and has nothing to do with URL curation. Deleting either breaks phase 3 | that file |
| `specs5/plan/inventory.md` § Frontend — NEW | `permission-dialog.js`, `tool-card.js`, `todo-list.js` | A `permission-dialog/` directory; and cards and checklists are templates in `chat-panel/block-render.js` over the block model in `chat-panel/blocks.js`, not components of their own | that file |

### `AskUserQuestion`, read out of the CLI

The Python surface says nothing about how an answer gets back to the tool, and getting it wrong is silent
in both directions — the user sees an answered question, the agent hears "The user did not answer the
questions". From the bundled binary's tool definition:

- Input is `questions` (1–4; each `{question, header, options: 2–4 × {label, description, preview?},
  multiSelect}`), plus `answers: Record<str, str>` — "User answers collected by the permission
  component" — plus `annotations` and `metadata`.
- `checkPermissions` returns `{behavior: "ask"}` **unconditionally**, which is the mechanism behind
  "always gated by the SDK".
- `call` destructures `{questions, answers = {}, annotations, response, afkTimeoutMs}` from that input.
- The result string is built from `answers` keyed by question text; multi-select values are split on
  `", "` and checked back against the option labels. An answer that is not a known label is still
  delivered, prefixed with an instruction to read it carefully — that is the route the auto-provided
  "Other" reply takes.
- Question texts must be unique within a call and option labels unique within a question. The CLI
  enforces this before the callback, so the payload does not re-check it.
- There is an AFK path: the setting `askUserQuestionTimeout` (`60s` / `5m` / `10m` / `never`, default
  `never`) auto-continues with whatever is selected, and the CLI tells the model "No response after Ns —
  the user may be away from keyboard". Ours has its own 300 s decision timeout, so the two must not both
  be configured to fire.

Not built in phase 2, and additive: the freeform `response`, per-option `preview` (a block of
model-authored HTML — not forwarded into the dialog's shadow DOM incidentally), and `annotations`.

---

## Corrections found while implementing phase 4

**Verified 2026-08-15** against 0.2.137 and the bundled CLI 2.1.229, in live runs of
`scripts/bridge_smoke.py`. Same rule as before: the wheel and the CLI win.

| Where | Said / assumed | Actually |
|---|---|---|
| `get_mcp_status()` (§ Client, above) — "MCP server health surface" | The place to check whether our server registered | **An in-process SDK server does not appear in it.** A live run listed only the user's `chrome-devtools` from settings while all six `mcp__ac-dc__*` tools were being called successfully in the same turn. It reports *configured* stdio/http servers. What proves an SDK server registered is the model calling one of its tools |
| `specs5/3-engine/permissions.md` § classification table | `ac-dc` tools are ungated because they classify as read-only | Classification only shapes a dialog. The CLI raises a permission request for MCP tools in `acceptEdits` and `default` — not in `plan` — so they must be allowed in `can_use_tool` explicitly. Corrected in that file |
| `specs5/3-engine/mcp-bridge.md` § Tools | The tool names are what the model sees | It sees them prefixed: `mcp__ac-dc__<tool>`. And when the inventory is deferred it reaches them through `ToolSearch` first — all three live runs showed `ToolSearch{query: "select:mcp__ac-dc__file_symbols"}` before the call. A tool whose *name* is not guessable from the task will not be found that way, which is an argument for plain names over clever ones |

### `ToolAnnotations(readOnlyHint=True)` buys nothing at the gate

Worth stating because it looks like it should. The annotation is advisory metadata for the model and
for UI; it does not affect whether the CLI raises a permission request. Two read-only tools, one
built-in and one on an SDK server, get opposite treatment in `acceptEdits`.
