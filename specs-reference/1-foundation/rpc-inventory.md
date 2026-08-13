# Reference: RPC Method Signatures

**Supplements:** `specs5/1-foundation/rpc-inventory.md`

The behavioral inventory in specs5 lists every RPC method with a one-line purpose. This twin pins the argument and return shapes — what the caller passes, what comes back, and the envelope conventions that surround every call.

**Division of labour with the layer-3 twins.** Engine-facing shapes — `EngineState`, `EngineHealth`,
turn methods, live controls, permissions, sessions, history, context usage, and every turn-scoped
`AcApp` event — are pinned in `specs-reference/3-engine/{session,permissions,history}.md`. They are not
repeated here. What this twin owns is the transport envelope, `Repo`, `Settings`, `Collab`,
`DocConvert`, the non-engine half of `ClaudeCodeService` (review, snippets, LSP, navigation, TeX,
commit workflow), and the `AcApp` events that are not turn-scoped.

## Byte-level formats

### Request ID format

Browser-generated, correlates streaming callbacks to the originating request.

```
{epoch_ms}-{6-char-alphanumeric}
```

See `specs-reference/3-engine/session.md` § Request ID format for the full format specification. The RPC layer treats these as opaque strings.

### Response envelope shape

Every jrpc-oo call returns a wrapped object. Single-remote responses have exactly one key:

```json
{"<method_name>": <actual_return_value>}
```

Multi-remote responses (broadcasts to multiple connected clients) have one key per remote, keyed by the remote's UUID:

```json
{
  "uuid-client-1": <return_value_from_client_1>,
  "uuid-client-2": <return_value_from_client_2>
}
```

The `rpcExtract` helper takes the first and only key from a single-key response or the first key from a multi-key response when broadcast is in play. Callers that explicitly need multi-remote results bypass the helper and read all values directly.

### Restricted error shape

Methods guarded by `_check_localhost_only()` return this exact dict shape to non-localhost callers in collaboration mode:

```json
{
  "error": "restricted",
  "reason": "<human-readable explanation>"
}
```

The `error` field is always the literal string `"restricted"`. The `reason` varies by method (e.g., `"Participants cannot perform this action"`, `"Only the host can commit"`). Frontend components check `result?.error === "restricted"` to decide whether to show a warning toast or hide the UI affordance.

In single-user mode (no collab instance attached), `_check_localhost_only()` returns `None` and methods proceed normally. All callers are treated as localhost.

## Schemas

### Service: Repo — browser → server

File I/O:

| Method | Arguments | Return |
|---|---|---|
| `Repo.get_file_content` | `path: str, version?: str` | `str` — file content; `version` is e.g. `"HEAD"` for committed content |
| `Repo.write_file` | `path: str, content: str` | `{status: str}` |
| `Repo.create_file` | `path: str, content: str` | `{status: str}` or error if file exists |
| `Repo.file_exists` | `path: str` | `bool` |
| `Repo.is_binary_file` | `path: str` | `bool` |
| `Repo.get_file_base64` | `path: str` | `{data_uri: str}` — full `data:{mime};base64,{content}` URI |
| `Repo.delete_file` | `path: str` | `{status: str}` |

Git staging:

| Method | Arguments | Return |
|---|---|---|
| `Repo.stage_files` | `paths: list[str]` | `{status: str}` |
| `Repo.unstage_files` | `paths: list[str]` | `{status: str}` |
| `Repo.discard_changes` | `paths: list[str]` | `{status: str}` |
| `Repo.stage_all` | — | `{status: str}` |

Rename:

| Method | Arguments | Return |
|---|---|---|
| `Repo.rename_file` | `old_path: str, new_path: str` | `{status: str}` |
| `Repo.rename_directory` | `old_path: str, new_path: str` | `{status: str}` |

Tree and listing:

| Method | Arguments | Return |
|---|---|---|
| `Repo.get_file_tree` | — | `{tree: FileNode, modified: list[str], staged: list[str], untracked: list[str], deleted: list[str], diff_stats: dict[str, {additions: int, deletions: int}]}` |
| `Repo.get_flat_file_list` | — | `str` — newline-separated sorted file paths |

The `FileNode` shape:

```pseudo
FileNode:
    name: string
    path: string
    type: "file" | "dir"
    lines: integer          // 0 for binary and directories
    mtime: float?            // files only
    children: FileNode[]?   // directories only
```

Diffs:

| Method | Arguments | Return |
|---|---|---|
| `Repo.get_staged_diff` | — | `str` — unified diff |
| `Repo.get_unstaged_diff` | — | `str` — unified diff |
| `Repo.get_diff_to_branch` | `branch: str` | `{diff: str}` or `{error: str}` |

Commits:

| Method | Arguments | Return |
|---|---|---|
| `Repo.commit` | `message: str` | `{sha: str, message: str}` |
| `Repo.reset_hard` | — | `{status: str}` |
| `Repo.search_commits` | `query: str, branch?: str, limit?: int` | `list[{sha, short_sha, message, author, date}]` |

Branches:

| Method | Arguments | Return |
|---|---|---|
| `Repo.get_current_branch` | — | `{branch: str \| null, sha: str, detached: bool}` |
| `Repo.list_branches` | — | `{branches: list[{name, sha, message, is_current}], current: str}` |
| `Repo.list_all_branches` | — | `list[{name, sha, is_current, is_remote}]` sorted by recency, deduplicated |
| `Repo.resolve_ref` | `ref: str` | `str \| null` — full SHA or null if unresolvable |
| `Repo.checkout_branch` | `branch: str` | `{status: str, branch: str, sha: str}` or `{error: str}` — DWIM on remote refs; refuses a dirty tree |
| `Repo.is_clean` | `include_untracked?: bool` | `bool` — untracked files ignored by default |

Commit graph:

| Method | Arguments | Return |
|---|---|---|
| `Repo.get_commit_graph` | `limit?: int, offset?: int, include_remote?: bool` | `{commits: list[...], branches: list[...], has_more: bool}` |
| `Repo.get_commit_log` | `base: str, head?: str, limit?: int` | `list[{sha, short_sha, message, author, date}]` |
| `Repo.get_commit_parent` | `commit: str` | `{sha: str, short_sha: str}` |
| `Repo.get_merge_base` | `ref1: str, ref2?: str` | `{sha: str, short_sha: str}` or `{error: str}` |

Commit graph entry shape:

```pseudo
CommitGraphEntry:
    sha: string
    short_sha: string
    message: string
    author: string
    date: string                 // ISO timestamp
    relative_date: string        // "2 days ago"
    parents: list[string]        // parent SHAs
```

Review:

| Method | Arguments | Return |
|---|---|---|
| `Repo.checkout_review_parent` | `branch: str, base_commit: str` | `{branch, branch_tip, base_commit, parent_commit, original_branch, phase: "at_parent"}` or `{error}` |
| `Repo.setup_review_soft_reset` | `branch_tip: str, parent_commit: str` | `{status: "review_ready"}` |
| `Repo.exit_review_mode` | `branch_tip: str, original_branch: str` | `{status: "restored"}` or `{error}` |
| `Repo.get_review_changed_files` | — | `list[{path, status, additions, deletions}]` |
| `Repo.get_review_file_diff` | `path: str` | `{path: str, diff: str}` |

Search:

| Method | Arguments | Return |
|---|---|---|
| `Repo.search_files` | `query: str, whole_word?: bool, use_regex?: bool, ignore_case?: bool, context_lines?: int` | `list[{file: str, matches: list[SearchMatch]}]` |

Search match shape:

```pseudo
SearchMatch:
    line_num: integer
    line: string
    context_before: list[{line_num: integer, line: string}]
    context_after: list[{line_num: integer, line: string}]
```

TeX preview:

| Method | Arguments | Return |
|---|---|---|
| `Repo.is_make4ht_available` | — | `bool` |
| `Repo.compile_tex_preview` | `content: str, file_path?: str` | `{html: str}` or `{error: str, log?: str, install_hint?: str}` |

### Service: ClaudeCodeService — browser → server

The service that replaced `LLMService`. The RPC namespace is the Python class name (see § Dependency
quirks), so the rename is visible on the wire and nothing forwards the old namespace.

The engine-facing half of this service is pinned elsewhere and deliberately not duplicated:

| Group | Twin |
|---|---|
| State (`get_current_state` → `EngineState`), engine health, file selection, denied-read files, turns, live controls, introspection | `specs-reference/3-engine/session.md` § Service: ClaudeCodeService |
| `resolve_permission` and the permission payloads | `specs-reference/3-engine/permissions.md` |
| Sessions, mirrored history, subagent transcripts | `specs-reference/3-engine/history.md` § RPC surface |

The rest is browser-facing plumbing with no engine involvement, and lives here.

Commit workflow:

| Method | Arguments | Return |
|---|---|---|
| `ClaudeCodeService.generate_commit_message` | `diff_text: str` | `{status: "started", request_id: str}` — the message arrives as a normal turn in the transcript |
| `ClaudeCodeService.commit_all` | — | `{status: "started"}` — commit runs in background; result broadcast via `commitResult` |
| `ClaudeCodeService.reset_to_head` | — | `{status: str, system_event_message: str}` |

`generate_commit_message` changed shape with the conversion. It no longer calls an auxiliary model and
returns a string; it sends `commit.md` plus the diff as a **user turn** on the live session, so the
answer streams like any other turn and is visible in history. See
`specs5/1-foundation/configuration.md` § Config File Set.

Review:

| Method | Arguments | Return |
|---|---|---|
| `ClaudeCodeService.check_review_ready` | — | `{clean: bool, message?: str}` |
| `ClaudeCodeService.get_commit_graph` | `limit?: int, offset?: int, include_remote?: bool` | Same shape as `Repo.get_commit_graph` — a delegation endpoint |
| `ClaudeCodeService.start_review` | `branch: str, base_commit: str` | `{status: "review_active", branch, base_commit, commits, changed_files, stats}` or `{error: str}` |
| `ClaudeCodeService.end_review` | — | `{status: "restored"}` or `{error: str, status?: "partial"}` |
| `ClaudeCodeService.get_review_state` | — | `ReviewState` or `{active: false}` |
| `ClaudeCodeService.get_review_file_diff` | `path: str` | `{path: str, diff: str}` |

`ReviewState` shape — also the type of `EngineState.review_state`, and the payload the `review_state`
MCP tool projects for the agent:

```pseudo
ReviewState:
    active: bool
    branch?: string
    base_commit?: string
    branch_tip?: string
    commits?: list[CommitGraphEntry]
    changed_files?: list[{path, status, additions, deletions}]
    stats?: {commit_count, files_changed, additions, deletions}
```

Snippets:

| Method | Arguments | Return |
|---|---|---|
| `ClaudeCodeService.get_snippets` | — | `list[{icon: str, tooltip: str, message: str}]` — for the active preset |
| `ClaudeCodeService.get_review_snippets` | — | `list[{icon: str, tooltip: str, message: str}]` |

`get_snippets` is preset-aware where it used to be mode-aware; the return shape is unchanged. See
`specs5/plan/decisions.md` § CC-12.

LSP:

| Method | Arguments | Return |
|---|---|---|
| `ClaudeCodeService.lsp_get_hover` | `path: str, line: int, col: int` | `{contents: str}` — 1-indexed coordinates |
| `ClaudeCodeService.lsp_get_definition` | `path: str, line: int, col: int` | `{file: str, range: Range}` |
| `ClaudeCodeService.lsp_get_references` | `path: str, line: int, col: int` | `list[{file: str, range: Range}]` |
| `ClaudeCodeService.lsp_get_completions` | `path: str, line: int, col: int, prefix?: str` | `list[{label: str, kind: str, detail: str}]` |

All four are served from the surviving symbol index (`specs5/2-indexing/symbol-index.md`) and never
reach the engine. `Range` uses 1-indexed line/column:

```pseudo
Range:
    start: {line: integer, character: integer}
    end: {line: integer, character: integer}
```

Navigation and TeX:

| Method | Arguments | Return |
|---|---|---|
| `ClaudeCodeService.navigate_file` | `path: str` | `{status: str, path: str}` — broadcasts to all clients |
| `ClaudeCodeService.is_tex_preview_available` | — | `{available: bool, install_hint?: str}` |
| `ClaudeCodeService.compile_tex_preview` | `content: str, file_path?: str` | `{html: str}` or `{error: str, log?: str, install_hint?: str}` |

#### Deleted methods and their shapes

Removed with the native engine, listed so a reader of old code knows nothing replaced them:
`get_context_breakdown` (and the `ContextBreakdown` / `TierBlock` / `CategoryBreakdown` shapes),
`rebuild_cache`, `get_file_map_block`, `get_mode` / `switch_mode` / `set_cross_reference`,
`load_session_into_context`, `get_history_status`, the seven URL methods (`detect_urls`, `fetch_url`,
`detect_and_fetch`, `get_url_content`, `invalidate_url_cache`, `remove_fetched_url`,
`clear_url_cache`), and the four agent-turn methods (`get_turn_archive`, `get_agent_history`,
`close_agent_context`, `set_agent_selected_files`).

The nearest surviving thing to `get_context_breakdown` is `get_context_usage`, and it is not the same
shape or the same idea — it reports the engine's own categories, not our tiers. See
`specs5/3-engine/context-visibility.md`.

### Service: Settings — browser → server

| Method | Arguments | Return |
|---|---|---|
| `Settings.get_config_content` | `type: str` | `str` — raw file content |
| `Settings.save_config_content` | `type: str, content: str` | `{status: str}` |
| `Settings.reload_engine_config` | — | `{status: str}` |
| `Settings.reload_app_config` | — | `{status: str}` |
| `Settings.get_config_info` | — | `{model: str \| null, config_dir: str, cli_path: str}` — `model: null` means the CLI's default |
| `Settings.get_snippets` | — | `list[{icon: str, tooltip: str, message: str}]` |
| `Settings.get_review_snippets` | — | `list[{icon: str, tooltip: str, message: str}]` |

The `type` argument is a whitelisted identifier — not a file path. The whitelist is `engine`, `app`, `snippets`. See `specs5/1-foundation/configuration.md` for what each one maps to and which changes need a new session.

### Service: Collab — browser → server

Only registered when `--collab` is passed on the command line.

| Method | Arguments | Return |
|---|---|---|
| `Collab.admit_client` | `client_id: str` | `{ok: true, client_id: str}` |
| `Collab.deny_client` | `client_id: str` | `{ok: true, client_id: str}` |
| `Collab.get_connected_clients` | — | `list[{client_id, ip, role, is_localhost}]` |
| `Collab.get_collab_role` | — | `{role: "host" \| "participant", is_localhost: bool, client_id: str}` |
| `Collab.get_share_info` | — | `{ips: list[str], port: int}` |

### Service: DocConvert — browser → server

| Method | Arguments | Return |
|---|---|---|
| `DocConvert.scan_convertible_files` | — | `list[{path, name, size, status, output_path}]` |
| `DocConvert.convert_files` | `paths: list[str]` | `{status: "started"}` — progress via `docConvertProgress` events; results via final event |
| `DocConvert.is_available` | — | `bool` |

### Service: AcApp — server → browser (client-side callbacks)

Methods the server calls on connected browsers. Each returns `true` as an acknowledgement unless
otherwise noted.

Every turn-scoped event — `sessionStarted`, `streamChunk`, `thinkingChunk`, `toolUse`, `toolResult`,
`subagentEvent`, `hookEvent`, `rateLimit`, `compactionEvent`, `streamComplete`,
`postResponseComplete`, `permissionRequest`, `permissionResolved`, `permissionModeChanged`,
`engineHealth`, `userMessage` — is pinned in `specs-reference/3-engine/session.md` § Service: AcApp,
with the permission pair in the permissions twin. Note in particular that `streamChunk` kept its name
but changed its contract: the payload is now a `{block_id, seq, content, done}` object whose `content`
is cumulative **within a block**, not within the turn.

What remains here are the events that are not turn-scoped and did not change:

| Method | Arguments | Return |
|---|---|---|
| `AcApp.filesChanged` | `selected_files: list[str]` | `true` |
| `AcApp.commitResult` | `result: {sha, short_sha, message, status, error?}` | `true` |
| `AcApp.sessionChanged` | `data: {session_id: str, messages: list[MessageDict]}` | `true` |
| `AcApp.startupProgress` | `stage: str, message: str, percent: int` | `true` |
| `AcApp.navigateFile` | `data: {path: str}` | `true` |
| `AcApp.admissionRequest` | `data: {client_id, ip, requested_at}` | `true` |
| `AcApp.admissionResult` | `data: {client_id, ip, admitted, replaced?}` | `true` |
| `AcApp.clientJoined` | `data: {client_id, ip, role, is_localhost}` | `true` |
| `AcApp.clientLeft` | `data: {client_id, ip, role}` | `true` |
| `AcApp.roleChanged` | `data: {role, reason}` | `true` |
| `AcApp.docConvertProgress` | `data: {...}` — shape varies by progress stage | `true` |

`MessageDict` is a record from the mirrored store; its schema is in
`specs-reference/3-engine/history.md` § Mirrored store record schema. In-memory uses may omit `id`,
`session_id`, and `timestamp`; the core triad is `{role, content, system_event?}`.

Deleted: `AcApp.modeChanged`. Nothing replaced it — there are no modes.

## Dependency quirks

### RPC prefix derivation from Python class name

`server.add_class(instance)` derives the RPC namespace from `type(instance).__name__` — the Python class name, not the variable name. So `server.add_class(engine_service)` where `engine_service` is an instance of `ClaudeCodeService` produces RPC endpoints like `ClaudeCodeService.chat_streaming`, not `engine_service.chat_streaming`.

This is why the `LLMService` → `ClaudeCodeService` rename is a wire-visible change that has to be made in the browser call sites at the same time, and why it cannot be softened by keeping a variable name.

This differs from the browser side's `addClass(this, 'AcApp')` which takes an explicit namespace string as the second argument. On the server, passing a second argument to `add_class()` either raises or silently overrides the derived name — the codebase never passes a second argument.

### jrpc-oo `this.server` vs `this.call` on the browser

Browser-side has two calling mechanisms:

- `this.server['ClassName.method'](args)` — calls one remote, returns the direct result. Fails with "More than one remote has this RPC" when multiple remotes expose the same method.
- `this.call['ClassName.method'](args)` — calls every connected remote that has the method, returns `{uuid: result, ...}`.

The AC⚡DC codebase uses `this.call` exclusively via the `rpcExtract` helper. The `this.server` path is legacy and unreliable when collaboration mode has multiple connected clients (which is always, from the server's perspective — it sees one remote per browser).

### `addClass(this, 'AcApp')` name is load-bearing

The browser-side registration name must match exactly what the server-side call site uses:

```python
# server calls:
await call["AcApp.streamChunk"](request_id, content)
```

```javascript
// browser registers:
this.addClass(this, 'AcApp');
```

A mismatch means the server's call resolves to no handler and silently fails (jrpc-oo does not error on unknown method names from server→browser calls — it just drops the call).

### Arguments wrapping

When the browser calls `this.server['MyApi.add'](3, 5)`, jrpc-oo serializes the arguments as:

```json
{"args": [3, 5]}
```

The Python side's `ExposeClass` unwraps this — the handler sees normal parameters `def add(self, a, b)`. No manual unwrapping needed on either end.

## Cross-references

- Behavioral inventory with method purposes and grouping: `specs5/1-foundation/rpc-inventory.md`
- Connection lifecycle, reconnection, streaming patterns: `specs5/1-foundation/rpc-transport.md`
- Engine method and event shapes (`EngineState`, `ChunkPayload`, `StreamCompleteResult`, `compactionEvent` stages): `specs-reference/3-engine/session.md`
- Permission request, decision, and rule shapes: `specs-reference/3-engine/permissions.md`
- Mirrored-store record schema, session listing, subagent keys: `specs-reference/3-engine/history.md`
- MCP tool argument and result shapes for the `ac-dc` server: `specs5/3-engine/mcp-bridge.md`
- Collaboration restriction policy and admission flow: `specs5/4-features/collaboration.md`