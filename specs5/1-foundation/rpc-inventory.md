# RPC Method Inventory

Authoritative catalog of every RPC method in the system. Other specs reference methods by name rather
than re-listing them.

## Structure

- Methods are grouped by registered service (Repo, ClaudeCodeService, Settings, Collab, DocConvert, AcApp)
- Each entry records: name, direction (browser→server or server→browser), purpose, arguments, return shape, localhost-only flag
- Restricted methods (localhost-only in collaboration mode) are flagged explicitly
- Argument and return shapes live in [`specs-reference/1-foundation/rpc-inventory.md`](../../specs-reference/1-foundation/rpc-inventory.md); engine-specific shapes live in the [`specs-reference/3-engine/`](../../specs-reference/3-engine/) twins

## Service: Repo (browser → server)

- File I/O — get content (optionally at a version), write, create, exists, is-binary, base64 encode, delete
- Git staging — stage, unstage, discard changes
- File manipulation — rename file, rename directory
- Tree and listing — full file tree with git status, flat sorted file list
- Diffs — staged, unstaged, to-branch (two-dot), for a single review file
- Branches — current, list, list-all (local + remote), resolve ref, checkout (switch local, or create tracking branch from remote ref via DWIM; refuses dirty tree), commit graph (paginated), commit log range, parent of commit, merge-base
- Commits — commit, reset-hard, search commits, stage-all
- Review support — setup soft reset, exit review mode, changed files in review
- Clean check — working tree cleanliness
- Search — grep with regex / whole-word / ignore-case / context-lines flags
- TeX preview — make4ht availability check, compile to HTML

Unchanged by the conversion. The agent does not call these — it has its own file and git tools,
executed by the CLI (see [`../3-engine/tool-surface.md`](../3-engine/tool-surface.md)). Repo serves the
browser.

## Service: ClaudeCodeService (browser → server)

Replaces `LLMService`. The service class name is the RPC namespace, so the rename is visible on the
wire; nothing forwards the old namespace.

- State — engine state snapshot (mirrored messages, selected files, denied-read files, session id, repo name, init and engine-ready flags, streaming flag, active streams with per-block replay, permission mode, model, pending permissions, doc-index flags, review state, engine health)
- Engine health — connected flag, CLI path and version, SDK version and its CLI pin, credential source, MCP server health, mirror-gap count, last error
- File selection — get, set. Selection is a hint carried in turn framing, not a context contract
- Denied-read files — get, set. The picker's third state, written as `Read(path)` deny rules
- Turns — start streaming chat (request id, message, optional files, images, viewer framing), cancel streaming
- Live controls — set permission mode, set model, rewind files to a checkpoint, stop a subagent task, resolve a permission request
- Sessions — new session, resume session (optionally forking)
- History — list sessions, load a session's messages, search history, delete a session (localhost-only), fetch one image's bytes by pointer. All read the one mirrored transcript through the SDK's parsers, never raw entries. There are no separate engine-session listing or deletion RPCs: with one store each would be a second answer to a question the history surface already answers
- Subagents — list subagent transcripts, get a subagent transcript
- Introspection — context usage, MCP server status, reconnect an MCP server, toggle an MCP server, server info (advertised commands, tools, output styles), the SDK surface report
- Slash commands — list the commands the `/` palette offers: the CLI's advertised list filtered and annotated with what selecting each one does. See [`../3-engine/session.md` § Slash Commands](../3-engine/session.md#slash-commands)
- Commit workflow — generate commit message, commit-all (background)
- Reset — reset-to-HEAD (records a system event)
- Snippets — current snippets for the active preset, review-specific snippets
- Review — check ready, start, end, get state, get file diff
- LSP — hover, definition, references, completions (coordinates are 1-indexed). Served from the surviving symbol index
- Navigation — broadcast file navigation to all clients
- TeX — availability, compile

Deleted with the native engine: context breakdown by tier, manual cache rebuild, file map block,
mode get/switch/cross-reference, the seven URL methods, `load_session_into_context`,
`get_history_status`, and the four agent-turn methods (`get_turn_archive`, `get_agent_history`,
`close_agent_context`, `set_agent_selected_files`).

## Service: Settings (browser → server)

- Config read — get content for a whitelisted config type
- Config write — save content (triggers reload for reloadable types)
- Explicit reload — engine config, app config
- Info — current model name and config paths
- Snippets — get standard or review-specific (direct access, bypassing preset logic)

The whitelist shrank with the prompt files it used to gate; see
[`configuration.md`](configuration.md).

## Service: Collab (browser → server, registered only with collaboration flag)

- Admission — admit pending client, deny pending client
- Registry — list connected clients
- Self-query — get own role (host/participant, localhost flag, client id)
- Share info — LAN IPs and WebSocket port for share URL construction

## Service: DocConvert (browser → server)

- Availability — dependency status (markitdown, LibreOffice, PyMuPDF, combined PDF pipeline)
- Scan — list convertible files with status badges (new, stale, current, conflict)
- Convert — start batch conversion (returns `{status: "started"}`, streams progress)

## Service: AcApp (server → browser)

Every turn-scoped event carries the originating request id. The authoritative payload shapes are in
[`specs-reference/3-engine/session.md` § Service: AcApp](../../specs-reference/3-engine/session.md#service-acapp--server--browser).

- Session lifecycle — session started (model, tool inventory, MCP health, resolved permission mode)
- Streaming — text chunk, thinking chunk, tool use, tool result. Each carries a block identity; content is cumulative within a block
- Turn completion — stream complete (usage, cost, duration, terminal reason), post-response complete (re-indexed files, fresh context usage)
- Engine state — compaction event, rate limit, hook event (debug view only), engine health
- Permissions — permission request, permission resolved, permission mode changed
- Subagents — subagent event (started, progress, updated, notification)
- Broadcast — files changed, user message (with request id and framing's file list), user message images (the pasted images' pointers, once the CLI has written the entry they name — see [`../4-features/images.md`](../4-features/images.md) § Engine Service Integration), commit result, session changed
- Startup — progress
- Navigation — navigate file
- Collaboration — admission request, admission result, client joined, client left, role changed
- Doc convert — progress updates

Deleted: `modeChanged`. The old `streamChunk` contract — full accumulated content per chunk — is
replaced by the block-identity contract; the event name survives, its semantics do not. See
[`../3-engine/session.md` § Chunk semantics change](../3-engine/session.md#chunk-semantics-change).

## Restriction Policy

- Every mutating method on ClaudeCodeService, Repo, Settings, DocConvert checks the caller's localhost status
- Non-localhost participants in collaboration mode receive `{error: "restricted", reason: ...}` for restricted calls
- Single-user mode (no collaboration flag) treats all callers as localhost
- **`resolve_permission` is restricted even though it is not obviously mutating.** It authorises arbitrary `Bash`, which makes it the most powerful method in the inventory. See [`../3-engine/permissions.md` § Collaboration and authority](../3-engine/permissions.md#collaboration-and-authority)
- Read-only introspection is unrestricted: a remote participant may watch a turn, read engine health, and inspect context usage. Watching is the point of collaboration

## Method Naming Convention

- Service class name becomes the RPC namespace
- Method name follows after a dot (e.g., `Repo.get_file_content`, `ClaudeCodeService.chat_streaming`)
- Registered-in-browser methods follow the same pattern (e.g., `AcApp.streamChunk`)
- The namespace is derived from the Python class name, so renaming a service class is a wire-visible change and must be done deliberately

## Invariants

- Every method listed here is implemented exactly once on exactly one service
- No mutating method, and no permission resolution, is callable by a non-localhost participant
- Every server→browser event that belongs to a turn carries that turn's request id
- Adding a new RPC method requires updating this inventory
