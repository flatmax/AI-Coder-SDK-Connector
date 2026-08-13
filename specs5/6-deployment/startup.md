# Startup

The startup sequence is split into two phases to give the user early feedback. The browser connects and shows a startup overlay while heavy initialization runs in the background with progress updates. The WebSocket server is accepting connections within the fast phase.
## Running Modes
| Mode | Description | URL pattern |
|---|---|---|
| Bundled (default) | Built-in static server serves bundled webapp | `http://localhost:{webapp_port}/?port={server_port}` |
| Local dev (`--dev`) | Vite dev server + RPC server | Same pattern |
| Local preview (`--preview`) | Vite production build + preview server | Same pattern |
## Phase 1: Fast (under a second)
1. Validate git repository — if not a repo, write a self-contained instruction HTML to a temp file, open as a file URL in the browser, print terminal banner with remediation commands, exit
2. Find available ports for WebSocket and webapp servers
3. Initialize lightweight services:
   - Construct config manager
   - **Resolve the `claude` CLI** — locate the binary, read its version, and determine the credential source. This is a fast local probe, not a session start, and it happens here so a missing or unauthenticated CLI is reported through the startup overlay rather than as a failed first turn. See [`../1-foundation/configuration.md`](../1-foundation/configuration.md) § Engine health.
   - Construct repo, settings, doc convert availability check
   - **No environment writing.** Nothing in this phase sets `os.environ`. The CLI resolves its own credentials, and a config that appeared to inject them would silently redirect billing away from the account the user authenticated (see [`../../specs-reference/1-foundation/configuration.md`](../../specs-reference/1-foundation/configuration.md) § The environment must not be written).
4. Start webapp server — bundled static server (default), Vite dev, or Vite preview
5. Construct the engine service with a deferred-init flag — no symbol index yet, and **no SDK client**
6. **Load the last session's transcript from `.ac-dc4/`** — a disk read, before starting the WebSocket server, so `get_current_state` returns previous messages to the first browser connection
7. Register services with the RPC server and start the WebSocket server
8. Open browser (unless `--no-browser` flag passed) — user sees startup overlay immediately

Step 6 is a read, not a resume. Restoring the transcript makes the browser show the conversation; the
engine has no session until one is needed. Resuming is a `ClaudeSDKClient` connect with `resume`, and
doing it eagerly at startup would spawn a `claude` subprocess for every launch — including the many where
the user opens AC⚡DC to read a diff and never sends a message. See
[`../3-engine/history.md`](../3-engine/history.md) and
[chat.md § Resume Is Not Load](../5-webapp/chat.md#resume-is-not-load).
## Phase 2: Deferred (non-blocking background task)
Phase 2 runs as a background task so the event loop stays free to handle WebSocket frames (pings, RPC calls) throughout. Each CPU-bound step uses the executor to avoid GIL starvation.
1. Wait briefly for browser WebSocket connection
2. Initialize symbol index via executor — progress ~10%
3. Complete deferred engine init via executor (wire the symbol index into the MCP bridge) — progress ~30%
4. Index repository in small batches (around 20 files per batch) via executor, with event-loop yield between batches — progress 50–90%
5. Build reference index once after all files indexed — progress ~90%
6. Signal ready — browser dismisses startup overlay — progress 100%
7. Start background doc index build (structural extraction → enrichment)

The stability-tracker step is deleted. It assigned cache tiers from the reference graph, and there are no
tiers. The reference index survives because Monaco's go-to-references needs it (see
[`../2-indexing/reference-graph.md`](../2-indexing/reference-graph.md)); what it no longer feeds is a
prompt-assembly decision.
### Progress Reporting
- Progress sent via a server-push progress callback
- Each stage is best-effort — if the browser isn't connected yet, the call is silently dropped
- An init-complete flag gates chat requests — requests before phase 2 completes are rejected with a user-friendly message

The gate is narrower than it was. It exists now only because the `ac-dc` MCP server would advertise search
tools backed by a half-built index; the conversation itself needs nothing from phase 2. A future refinement
could let a turn start immediately with the search tools withheld until the index lands, and the reason
that is not specified as the behaviour is honesty about sequencing: it wants the tool-availability
signalling in [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md) to be built first.
### Doc Index Stage Filtering
- Progress stage indicating doc-index work is intercepted by the shell and routed to the dialog header progress bar instead of the startup overlay
- Only in-progress updates forwarded; completion signal arrives via the enrichment-complete event
- Prevents the background doc index build from stalling or re-showing the startup overlay
### File Reopen Deferral
- Browser delays reopening the last-viewed file until the startup overlay dismisses (after the ready signal)
- Prevents file-fetch RPC calls from blocking the server's event loop during heavy initialization
- On reconnect (when init is already complete), the file reopens immediately
## Startup Overlay
The browser shows a full-screen overlay with the brand mark, a status message, and a progress bar. The overlay updates as progress events arrive:
| Stage | Message | Percent |
|---|---|---|
| Connected | Connected — initializing… | ~5% |
| Symbol index | Initializing symbol parser… | ~10% |
| Engine wiring | Preparing the agent's tools… | ~30% |
| Indexing | Indexing repository… N/M | 50–90% |
| References | Building the reference graph… | ~90% |
| Ready | Ready | 100% |
- Overlay fades out shortly after ready
- On reconnection (not first connect), overlay is not shown — only a "Reconnected" success toast appears
- Document index enrichment progress is communicated separately via the persistent header progress bar in the chat panel (not the startup overlay)
- **The permission dialog renders above the overlay.** A resume that replays a pending `can_use_tool` can produce a request while startup is still running, and a dialog underneath the overlay would deadlock the turn behind a prompt the user cannot see (see [`../5-webapp/permission-dialog.md`](../5-webapp/permission-dialog.md#placement))

### Engine Health in the Overlay

When the `claude` CLI could not be resolved, or resolved without usable credentials, the overlay does not
fail the startup. Phase 1 continues, the overlay reaches ready, and the shell shows a persistent health
banner naming what is wrong and where to fix it.

The reasoning is that everything except the conversation still works: the file tree, the diff viewer, git
operations, the symbol index, search. A user whose `claude` install is broken should land in a working
editor with one clear problem stated, not on an error page. The one thing that must not happen is a
send button that looks live and produces an opaque subprocess failure — so the chat input is disabled with
the banner's reason as its placeholder.
## Git Repository Validation
- When the target path is not a git repository, the server writes a self-contained HTML page to a temp file and opens it in the browser via a file URL
- HTML is dark-themed, centered, shows the brand mark, highlights the offending path and remediation commands (`cd /path && git init`) in a visually distinct style
- Terminal banner mirrors the information in plain text
- Server then exits without starting the WebSocket server
## CLI Arguments
| Flag | Default | Description |
|---|---|---|
| `--server-port` | 18080 | RPC WebSocket port |
| `--webapp-port` | 18999 | Webapp server port |
| `--no-browser` | false | Don't auto-open browser |
| `--repo-path` | current dir | Git repository path |
| `--dev` | false | Run local dev server (Vite) |
| `--preview` | false | Build and preview (Vite) |
| `--verbose` | false | Debug logging |
| `--collab` | false | Enable collaboration mode (listen on all interfaces, admission-gated) |
## Port Selection
- Find-available-port helper tries binding to loopback on consecutive ports starting from the configured default
- Scans up to a reasonable number of attempts
- First successfully bound port is used
- If no port available in the range, server exits with an error
- **Both ports are probed independently** — the WebSocket port AND the webapp port. Skipping the probe on either port is a correctness bug: a second instance launched with defaults would fail to bind the taken port in one of two silent ways (static-server constructor raises `OSError` inside a daemon thread and gets swallowed; or the browser-open call still fires and the user sees "their" app in the tab title while the loaded JS comes from the first instance). Users have no clear signal that something is wrong — the tab loads, looks right, and silently talks to the wrong backend
- The CLI flags `--server-port` and `--webapp-port` specify the *starting* port for the probe, not a required port. This matches the "just works" principle — running two AC⚡DC instances back to back should never require the user to remember port arithmetic

## Browser Tab Title

- Set to the repo name (e.g., `my-project`)
- Repo name comes from the state snapshot returned by the current-state RPC
- Updated on initial state load
- No prefix or branding — just the bare repo name
- Helps users distinguish multiple AC⚡DC sessions across different repos

## Logging

Structured to stderr. Default level INFO. Verbose flag enables DEBUG.

| Level | Usage |
|---|---|
| ERROR | Exceptions, fatal failures |
| WARN | Recoverable issues |
| INFO | Turn start and end, tool calls and their decisions, session id changes, startup |
| DEBUG | RPC calls, chunks, hook payloads, symbol timing, config |

Tool-call logging is deliberately at INFO. A turn's tool calls are the record of what the agent did to the
repository, and that belongs in the default log rather than behind a verbose flag. Credentials, prompt
contents, and file contents are never logged at any level.

## Graceful Shutdown

- SIGINT / SIGTERM handler triggers clean exit
- Child processes (Vite dev/preview) terminated with a timeout, then killed if needed
- **Live `claude` subprocess disconnected via `ClaudeSDKClient.disconnect()`**, with a timeout and a kill fallback. A turn in flight is interrupted first so the CLI can flush its own transcript — the engine's session files are the primary record, and a hard kill mid-turn can leave them short of what the user saw
- WebSocket server stopped cleanly
- Pending background tasks allowed a brief grace period before forced termination
- Pending permission requests resolved as denials, so nothing is left blocked
- Temp directories (TeX preview working dirs) cleaned up where possible

## Security Considerations

| Area | Policy |
|---|---|
| File access | Paths resolved relative to repo root; parent-directory traversal rejected |
| Git operations | Local only (except shallow clones for URL fetching) |
| WebSocket binding | Loopback by default; all interfaces only when collaboration flag is set |
| Agent tool calls | Gated by the permission layer; write and exec classes always reach a human on localhost unless a rule or mode says otherwise ([`../3-engine/permissions.md`](../3-engine/permissions.md)) |
| Permission decisions | Localhost clients only; non-localhost participants see requests read-only ([`../4-features/collaboration.md`](../4-features/collaboration.md)) |
| Credentials | Never read, written, logged, or forwarded. The CLI owns them; AC⚡DC reports only which source was resolved |
| Settings writes | Only `.claude/settings.json` and `.claude/settings.local.json`, only through the permission layer's rule writer, only on an explicit "always allow" |

Path validation moved rather than vanished. AC⚡DC's own file RPCs still resolve against the repo root and
still reject traversal, because those serve the browser. The agent's file access is the CLI's to police
via `cwd` and its own path rules — and unlike an edit-block parser we wrote, it is enforced by the process
that performs the write.

## Graceful Degradation

| Failure | Behavior |
|---|---|
| Tree-sitter parse failure | Skip file in symbol index, log warning; file still in tree and navigable |
| `claude` CLI not found | Startup completes; health banner names the resolved search path; chat input disabled with the reason |
| CLI present but unauthenticated | Same as above, with the credential source and the `claude` command that would fix it |
| CLI subprocess exits mid-turn | Turn ends with `cli_exited` and its code; the transcript keeps what streamed; the next send reconnects a fresh client |
| Provider down / rate limited | Error result in chat with a typed toast; the session survives, so the next send is a normal send |
| Git operation fails | RPC returns an error shape; toast shown; file tree doesn't update |
| Commit fails | Error shown in chat; files remain staged |
| WebSocket disconnect | Reconnecting banner with attempt count, auto-retry with exponential backoff |
| Config file corrupt/missing | Use built-in defaults; log warning; settings panel displays the error |
| Symbol cache corrupt | Clear in-memory cache, rebuild from source |
| Transcript mirror append fails | `MirrorErrorMessage` → health banner: the engine's turn succeeded, our repo-local copy has a gap ([`../3-engine/history.md`](../3-engine/history.md)) |
| Permission request expires unanswered | Denied for want of an answer; the transcript records the timeout; the agent may adapt and continue |
| Review mode crash | Manual recovery via checkout of the original branch; detached HEAD state is safe |

Two entries are gone rather than reworded. **URL fetch failure** has no successor — URL fetching is
deleted, and a URL in a prompt is now the agent's `WebFetch` call, failing in its own tool result where the
agent can react. **Compaction LLM failure** likewise: compaction is the engine's, and a failure there is
its recovery to attempt, visible to us only as whatever it reports.

## Invariants

- Phase 1 completes before the WebSocket server accepts connections
- The last session's transcript is loaded from disk before the WebSocket server starts, so the first browser connect returns previous messages immediately
- Startup never spawns a `claude` subprocess. The SDK client is constructed on the first turn, or on an explicit resume
- Phase 2 never blocks the event loop — all CPU-bound work goes through the executor with event-loop yields
- Chat requests arriving before phase 2 completes are rejected with a user-friendly message
- Startup overlay appears on first connect only; reconnects show a transient toast
- Doc-index progress during phase 2 never re-shows or stalls the startup overlay
- Git repository validation failure always produces both the HTML instruction page and the terminal banner
- Port selection always succeeds or exits with a clear error — never silently binds to an unexpected port
- Both the WebSocket port and webapp port are probed before the server starts; a second concurrent instance probes past the first's ports rather than cross-wiring into it
- Browser tab title always matches the repo name; no branding prefix
- SIGINT / SIGTERM always trigger clean shutdown with child process termination, including any live `claude` subprocess
- No startup path writes `os.environ`, and no config reload does either
- A missing or unauthenticated `claude` CLI degrades to a working editor with a health banner and a disabled chat input — never a failed startup and never a live-looking send button