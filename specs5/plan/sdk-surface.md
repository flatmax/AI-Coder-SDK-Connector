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

**Since 2026-08-18 the snapshot has a machine-checked half** — see § [The probe](#the-probe). This file
still carries the reasoning, which no reflection can produce; what it no longer has to carry alone is the
*inventory*, which drifts silently every time the wheel moves.

**Re-verified 2026-08-13** against 0.2.137 installed in the project venv (the first pass read an
unpacked wheel). The surface held: `ContextUsageResponse` carries every field CC-4 assumes,
`ResultMessage.terminal_reason` is real, `SessionStore` and the conformance harness exist, and the
session-management functions have the signatures recorded below. Two things were wrong here and are
now fixed — the four `Task*Message` classes were written without their `Message` suffix, and the CLI
pin had moved to 2.1.229. One thing was nearly "corrected" wrongly: `__cli_version__` is absent from
the *public* namespace but present in `claude_agent_sdk._cli_version`, so a `dir(claude_agent_sdk)`
check alone will tell you it does not exist. It does.

---

## The probe

`src/aic_dc/claude_code/sdk_surface.py` asks the installed wheel what it offers and this repo's own source
what it uses, and reports the difference in five sections: **options**, **hooks**, **messages**, **client
methods**, **betas**. `tests/test_claude_code_sdk_surface.py` is the gate; `ClaudeCodeService.get_sdk_surface`
is the RPC; `webapp/src/sdk-surface-tab.js` is the reader's view of it (Alt+5, linked from the Context tab's
Debug section).

Everything below this heading is what building it cost to learn.

### It fails on *untriaged*, not on *unimplemented*

Every name the SDK exposes is one of three things:

| Bucket | Meaning | Where it comes from |
|---|---|---|
| `handled` | this repo demonstrably uses it | **derived** from our own syntax trees |
| `declined` | we know about it and chose not to, with the reason recorded | a declared table |
| `pending` | we know about it, it is not built, and there is an argument for it | a declared table |

A `pending` name is a **finding, not a failure** — 22 options are pending right now and the suite is green.
What fails the gate is a name in *none* of the three, because that is the only state that means the SDK
moved and nobody looked. Closing it costs one table entry with a sentence in it, which is the smallest
action that leaves a reader better off than a passing test would have.

The distinction matters because the alternative was tried on paper and rejected: a gate that fails on
unimplemented surface would have failed on 24 things the day it landed, got an ignore-list within a week,
and the ignore-list is the thing nobody reads. Two of those 24 were built the same day *because the report
argued for them* — which is the finding bucket working, and is not something a red test would have
achieved faster.

### Coverage is read out of the AST, and two naive versions had to fail first

`handled` is computed by walking this package's own syntax trees — `ast.Subscript` targets for
`kwargs["model"]`, `ast.Assign` / `ast.AnnAssign`, `ast.Return` dict literals, `ast.Call` attribute names.
Both cheaper designs were built and both lied, and their failures are now pinned as regression tests:

- **A runtime diff of `build_option_kwargs()` reported 36 false gaps.** `model`, `hooks`, `resume` and
  `thinking` are set *conditionally*, so a probe that inspects one call's output sees whichever branch that
  call took and calls the rest missing. `test_conditionally_set_options_count_as_handled`.
- **A text grep of `options.py` reported `skills` as handled.** The word appears in a comment about
  `.claude/skills/`. A grep cannot tell a mention from a use, and the failure direction is the dangerous one
  — it marks unbuilt surface as done. `test_options_named_only_in_comments_are_not_handled`.

The payoff is that adding an option to `options.py` needs **no edit here**: it moves from `pending` to
`handled` on its own. The tables only ever grow for surface the SDK itself added.

The leftover `PENDING_OPTIONS` entry does still have to go, and one edit of this file used to say `stale`
caught it. It cannot: `stale` is `(declined | pending) − fields`, so it only fires for names the *SDK*
removed, and a note about an option we just implemented names a field that still exists. That is what the
`resolved` bucket is for — `(declined | pending) ∩ assigned`, reported next to `stale` in the tab and
asserted empty by `test_nothing_we_set_is_still_argued_against`. Both directions matter for the same reason:
a pending note is an argument *for not doing* the thing, so once the thing is done the note reads as a
reason to undo it.

### This is the other direction from the existing tripwire

`test_claude_code_options.py` asserts every key we emit exists on the installed dataclass — it catches the
SDK **removing** something under us. This one asserts every field on the dataclass is accounted for — it
catches the SDK **adding** something. Neither subsumes the other, and the removal one is the more urgent of
the two; it stays where it is.

### What it cannot see

Reflection reads shape, never semantics. `include_partial_messages=True` is `handled` because the key is
set; that a `StreamEvent`'s deltas are keyed correctly is [phase 1's correction](#corrections-found-while-implementing-phase-1)
and no probe would have found it. The same goes for every row in the correction tables below — each is a
case where the *types* were satisfied and the *behaviour* was not. A green gate means nobody is unaware of a
name. It does not mean the name is used properly.

Two more limits worth stating:

- **`get_server_info()` is diffed separately** (`diff_server_info`), because the CLI ships on its own
  release train and can gain a command or a tool without any Python type changing. It is a live read, so it
  is absent when no engine is connected — the static half renders regardless, which is the point, since a
  diagnostic panel is most often opened when something is broken.
- **`get_server_info()` does not say which model is in force.** It carries a `models` list, which is the
  CLI's alias→id *mapping* rather than its current selection. The panel reads four keys off each entry —
  `value`, `displayName`, `resolvedModel`, `description` — and treats every one but `value` as optional,
  since the list arrives from the CLI's own release train. A live read on CLI 2.1.229 returned seven
  entries mixing bare aliases (`default`, `opus`, `haiku`) with fully-qualified ids
  (`au.anthropic.claude-sonnet-4-6`, and a `[1m]`-suffixed long-context variant), with `displayName` and
  `resolvedModel` populated throughout. Worth stating because it reads like the obvious place to look: building the
  model panel started there, found no such field, and had to take the alias from `Session.model` (the
  value `set_model` records) and pair it against this list for the resolution. The mapping half is still
  only available here, so the two reads belong together in one RPC — hence
  `ClaudeCodeService.get_model()`. Answered from the stored `initialize` reply, so it costs ~2ms and no
  control request even mid-turn.
- **`KNOWN_BETAS` exists so the gate fails only on a *new* beta.** Without it, the one beta we have declined
  (`context-1m-2025-08-07`) failed the gate as untriaged — a test that goes red for a thing already decided
  is a test that gets deleted.

### What it says today

Read on 2026-08-18 against SDK 0.2.137 / CLI pin 2.1.229, nothing untriaged:

| Section | handled | declined | pending |
|---|---|---|---|
| Options | 23 | 2 | 22 |
| Hooks | 2 | 8 | 0 |
| Messages | 7 | 0 | 0 |
| Client methods | 14 | 1 | 0 |
| Betas | 0 | 1 | 0 |

The message taxonomy and the client surface are fully consumed, which is the part that would have been
guessed wrong: this repo renders every message type the union carries and calls every client method.

`receive_messages` was originally declined here, on the reasoning that `receive_response` bounds
itself on `ResultMessage` and that bound was the one we wanted. That reasoning was wrong, and the
correction is the reason the count above has no exception left: a result frame ends a *turn*, not the
run, so a background subagent's messages arrive after `receive_response()` has already returned. The
session consumes `receive_messages` past the result while tasks are in flight
([`../3-engine/session.md`](../3-engine/session.md) § A result message ends a turn, not the run). The
convenience wrapper is still what a turn itself uses; it is simply not the whole of what has to be
read.

The hook column reached zero pending the same day the probe first reported it. `PreCompact` was the single
entry there, and the argument for it was already written down; see below.

Three of the pending options were worth doing rather than merely knowing about, and the first was already
this plan's own [open item 1](README.md#open-items-carried-forward-as-of-2026-08-18) — the probe found it
independently, which is the best evidence it is reading something real. Two of the three are now set, and
the counts above are after that:

- **`max_buffer_size`** — one oversized stdout line ends a session permanently. **Done:** set
  unconditionally at 16 MiB, overridable through `engine.json`. See
  [`../1-foundation/configuration.md`](../1-foundation/configuration.md).
- **`stderr`** — a callback for the CLI's stderr. **Done:** the last 20 lines ride on `EngineHealth` and
  render in the health banner. See [`../3-engine/session.md`](../3-engine/session.md).
- **`resume_session_at` / `resume_drops_turn`** — resume from a chosen point rather than the end, which is
  the SDK-side half of an undo story this repo gave up when the mirror won ([CC-20](decisions.md)). Still
  pending: unlike the other two this is a feature with a UI, not a constructor argument.

And `PreCompact` — the hook the same reading called out — is registered, broadcasting a `systemEvent`
before the pause rather than after it. The stream's own `compact_boundary` arrives when compaction has
*finished*, so it can only explain a stall the user has already read as a hang.

And one is a trap worth naming: **`sandbox`**. It reads like a security win and it is not ours to enable
casually — it changes what the agent can do to the machine, and the permission dialog is this repo's answer
to that question.

---

## Client

`ClaudeSDKClient(options: ClaudeAgentOptions)` — async context manager. Methods actually present:

| Method | Notes |
|---|---|
| `connect()` / `disconnect()` | Spawns / tears down the CLI subprocess. |
| `query(prompt: str \| AsyncIterable[dict], session_id="default")` | Message dicts are written **verbatim**, so multimodal content blocks (images) survive untouched. This is how AIC⚡DC sends pasted images — see [`../4-features/images.md`](../4-features/images.md). |
| `receive_messages()` / `receive_response()` | The latter stops after `ResultMessage` — which ends a turn, not the run, so a turn with background tasks in flight is followed with the former. Both read one buffer the client owns: a second iterator resumes rather than replaying, and two at once would split the stream. |
| `interrupt()` | Replaces our cancellation flag. |
| `set_permission_mode(mode)` | Live switch; no reconnect. |
| `set_model(model)` | Live switch, and "live" needed measuring. Fired 22.8s into a live 34s turn it answered in **252ms** without interrupting the stream — but the running turn kept billing its original model, including a usage report 124ms after the switch returned; the *next* turn used the new one. So it is accepted mid-turn and applies to the turn after. See [`../3-engine/session.md` § A mid-turn switch lands on the next turn](../3-engine/session.md#a-mid-turn-switch-lands-on-the-next-turn). |
| `get_context_usage()` | **The `/context` data.** Basis of CC-4. |
| `get_mcp_status()` / `reconnect_mcp_server(name)` / `toggle_mcp_server(name, enabled)` | MCP server health surface. `get_mcp_status` is wired to the Context tab; the other two are exposed as RPCs with **no browser caller** — see [`../impl-history/work-log.md` § Specified but not yet built](../impl-history/work-log.md#specified-but-not-yet-built). |
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

From `ClaudeAgentOptions`. Fields AIC⚡DC uses, and why:

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
| `mcp_servers` | `{"aic-dc": <in-process server>}` | CC-6. |
| `max_budget_usd` | optional, from config | A hard stop the native engine never had. |
| `effort` / `thinking` | optional, from config | `thinking` is a TypedDict, not a class: `{"type": "adaptive", "display": "summarized" \| "omitted"}`. |
| `resume` / `fork_session` | on session load | CC-3. |
| `session_store` | our implementation | CC-3; mirrors the transcript into `.aic-dc/`. |
| `session_store_flush` | `"eager"` | Batched flushing holds a turn's tail until the result message. |
| `max_buffer_size` | 16 MiB, overridable in `engine.json` | The SDK's own 1 MiB default is per *line* of CLI stdout, and one line over it raises inside the reader and kills the session's message pump. An inline screenshot reaches it. Set unconditionally — the one place where deferring to the dependency's default is the broken choice. |
| `stderr` | `EngineSession._note_cli_stderr` | Registering a callback is what *pipes* the CLI's stderr; unset, it is inherited. So the callback logs the line **and** keeps the last 20 on `EngineHealth`, where the health banner renders them. |

`PermissionMode` is `Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk",
"auto"]` — six values, not the four the origin brief lists. `"plan"` and `"dontAsk"` both matter to
us: `"plan"` gives a read-only exploration mode worth exposing as a UI toggle, and `"dontAsk"` is a
distinct, less alarming middle ground than `"bypassPermissions"`.

---

## Message taxonomy

What comes out of `receive_response()`, and where each lands in the UI:

| Type | Contents | AIC⚡DC surface |
|---|---|---|
| `SystemMessage(subtype="init")` | Session ID, model, tools, MCP servers, slash commands | Session banner; seeds the request-ID ↔ session-ID map |
| `AssistantMessage` | `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `usage` | Chat message; thinking collapsed by default; tool-use cards. `usage` is this API call's four token counters — the only mid-turn source of them — summed per model into the streaming card's live counter (`turnUsage`) |
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

**A task can end without ever naming one of them.** `stop_task` is its own control subtype
(`SDKControlStopTaskRequest`, sibling of the interrupt request, sent by `query.py:848`), and
`client.py:454` promises that after it resolves "a `task_notification` system message with status
`'stopped'` will be emitted by the CLI in the message stream" — the terminal word arrives as an event, not
as a return value. But that promise covers only *that* request. Watched live on 2026-08-17: a subagent
killed through the CLI's **own** `TaskStop` tool produced no terminal task message whatsoever — the
engine's record of it stayed `{status: null, terminal: false}`, holding only the `usage` patch that had
arrived while it was alive. Two routes to the same task, and only one of them reports. Any surface that
waits for a terminal status therefore needs a fallback that does not (ours is the turn-end sweep, which
shows a still-live subagent as status-unknown).

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

- AIC⚡DC's wheel stops being pure-Python. Either we publish per-platform wheels, or we depend on
  the SDK and accept its platform constraint, or we ship an "external CLI" mode that requires
  `npm i -g @anthropic-ai/claude-code` and configure the path explicitly.
- A PyInstaller bundle that embeds the SDK embeds 295 MB. Current AIC⚡DC bundles are a fraction of
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
  `litellm`'s `proxy` extra in `/home/flatmax/.venv`, a virtualenv that contains neither `aic_dc` nor
  `markitdown` and is not this project's environment. The repo had no materialised venv at the time.
  The surviving constraint is a packaging one, not a resolution one: six transport packages enter the
  dependency set that an in-process MCP server never serves.
- Authentication conflicts: the SDK/CLI authenticates via its own config (subscription login or
  `ANTHROPIC_API_KEY`). AIC⚡DC's `llm.json` `env` block currently exports provider credentials into
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
| `specs5/5-webapp/chat.md` § Thinking Regions | Thinking blocks carry a token count | No payload has one. `thinkingChunk` is `{block_id, seq, content, done, agent_id}` and `usage` is a single total over text, tools and thinking together. The number would have to be invented | that file |
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
  the user may be away from keyboard". Ours has no decision timeout at all while a localhost client is
  connected, so nothing of ours races it — and the CLI's default is `never`, so left alone neither side
  answers for the user.

Not built in phase 2, and additive: the freeform `response`, per-option `preview` (a block of
model-authored HTML — not forwarded into the dialog's shadow DOM incidentally), and `annotations`.

---

## Corrections found while implementing phase 4

**Verified 2026-08-15** against 0.2.137 and the bundled CLI 2.1.229, in live runs of
`scripts/bridge_smoke.py`. Same rule as before: the wheel and the CLI win.

| Where | Said / assumed | Actually |
|---|---|---|
| `get_mcp_status()` (§ Client, above) — "MCP server health surface" | The place to check whether our server registered | **An in-process SDK server does not appear in it.** A live run listed only the user's `chrome-devtools` from settings while all six `mcp__aic-dc__*` tools were being called successfully in the same turn. It reports *configured* stdio/http servers. What proves an SDK server registered is the model calling one of its tools |
| `specs5/3-engine/permissions.md` § classification table | `aic-dc` tools are ungated because they classify as read-only | Classification only shapes a dialog. The CLI raises a permission request for MCP tools in `acceptEdits` and `default` — not in `plan` — so they must be allowed in `can_use_tool` explicitly. Corrected in that file |
| `specs5/3-engine/mcp-bridge.md` § Tools | The tool names are what the model sees | It sees them prefixed: `mcp__aic-dc__<tool>`. And when the inventory is deferred it reaches them through `ToolSearch` first — all three live runs showed `ToolSearch{query: "select:mcp__aic-dc__file_symbols"}` before the call. A tool whose *name* is not guessable from the task will not be found that way, which is an argument for plain names over clever ones |

### `ToolAnnotations(readOnlyHint=True)` buys nothing at the gate

Worth stating because it looks like it should. The annotation is advisory metadata for the model and
for UI; it does not affect whether the CLI raises a permission request. Two read-only tools, one
built-in and one on an SDK server, get opposite treatment in `acceptEdits`.
