# Reference: Startup

**Supplements:** `specs5/6-deployment/startup.md`

This is the canonical owner for startup progress stage names, reconnect backoff schedule, and port selection constants. The browser startup overlay parses progress events by stage name — exact string match matters.

## Byte-level formats

### Progress event payload

Sent via `AcApp.startupProgress(stage, message, percent)` as a server-push RPC call. Three positional arguments, no dict wrapping:

- `stage: string` — fixed identifier from the table below
- `message: string` — human-readable progress text shown in the overlay
- `percent: int` — progress bar fill (0–100, clamped at 100)

### Startup progress stages

Exact stage name strings the browser handler dispatches on:

| Stage | When emitted | Typical percent |
|---|---|---|
| `symbol_index` | Symbol index initialization starts (tree-sitter parser construction) | 10 |
| `engine_wiring` | Symbol and doc indexes being wired into the `aic-dc` MCP server | 30 |
| `indexing` | Repository files being parsed in batches; message format `"Indexing repository... {N}/{M}"` | 50–90 |
| `references` | Reference graph being built from the completed symbol index | 90 |
| `ready` | Initialization complete; browser dismisses overlay after fade | 100 |
| `doc_index` | Background doc-index build progress (intercepted by shell, routed to dialog header bar instead of startup overlay) | 0–100 |
| `doc_index_error` | Structural extraction failed | — |
| `doc_enrichment_queued` | Keyword enrichment starting; message includes total count | 0 |
| `doc_enrichment_file_done` | Per-file enrichment progress | 0–100 |
| `doc_enrichment_complete` | All enrichment complete | 100 |

Stage name strings are matched exactly — a typo produces unhandled events that the browser silently drops. New stages should be added only when the browser shell is updated to handle them.

Two renames from the native engine, both because the stage no longer describes what happens:

| Old stage | New stage | Why |
|---|---|---|
| `session_restore` | `engine_wiring` | Nothing is restored *into context* at startup. The transcript is read from `.aic-dc/` in Phase 1 as a plain disk read, and no `claude` subprocess exists yet. What Phase 2 actually does at 30% is hand the indexes to the MCP bridge |
| `stability` | `references` | The step built cache tiers from the reference graph. The tiers are gone; the graph survives for Monaco's go-to-references |

Neither old name may be re-used for anything. A shell built against the old spec would show
"Restoring session…" over a step that touches no session, and "Building cache tiers…" over a step that
builds none — both wrong in the specific way that makes a user trust the overlay less than they should.

### Browser routing of doc-index stages

The startup overlay only handles the first five stages (`symbol_index`, `engine_wiring`, `indexing`, `references`, `ready`). The five doc-index stages are intercepted by the app shell's `startupProgress` handler and re-dispatched as `doc-index-progress` window events that the dialog header bar subscribes to.

This routing is necessary because doc-index work begins after `ready` fires — the startup overlay has dismissed. Without interception, these events would either re-show the overlay (jarring) or be ignored (loss of progress visibility during a multi-minute enrichment phase).

The interception check is a simple `stage` comparison:

```javascript
if (stage === 'doc_index' ||
    stage === 'doc_index_error' ||
    stage === 'doc_enrichment_queued' ||
    stage === 'doc_enrichment_file_done' ||
    stage === 'doc_enrichment_complete' ||
    stage === 'doc_enrichment_failed') {
  // re-dispatch to header bar, skip startup overlay update
  window.dispatchEvent(new CustomEvent('doc-index-progress', { detail: { stage, message, percent } }));
  return;
}
// Normal startup overlay handling
```

### Fade-out timing

After the `ready` stage fires, the startup overlay waits for a CSS transition to complete before unmounting:

```
400ms
```

The overlay sets `opacity: 0` immediately, the CSS transition `opacity 400ms` runs, and a `setTimeout(..., 400)` removes the element from the DOM. Shortening this produces visible flicker; lengthening it delays the first user interaction.

### Reconnection toast

On successful reconnection (not first connect), no startup overlay. Instead, a toast appears with:

- Message: `"Reconnected"`
- Type: `"success"`
- Auto-dismiss: per standard toast behavior (see general toast system)

The `_wasConnected` flag on the shell distinguishes first connect from reconnect — true means at least one `setupDone` has previously fired this session.

## Numeric constants

### Reconnection backoff schedule

```
[1000, 2000, 4000, 8000, 15000] milliseconds
```

After each disconnect, the shell waits for the next value in this list before attempting reconnection. The list is capped at 15000 — further disconnects keep retrying at 15-second intervals rather than doubling indefinitely.

The attempt counter resets to 0 on a successful `setupDone`. So a reconnection that succeeds clears the exponential backoff, and the next disconnect starts again at 1000ms.

| Attempt # | Delay before retry |
|---|---|
| 1 (first disconnect) | 1000ms |
| 2 | 2000ms |
| 3 | 4000ms |
| 4 | 8000ms |
| 5+ | 15000ms (capped) |

Reconnection logic:

```javascript
const delays = [1000, 2000, 4000, 8000, 15000];
const delay = delays[Math.min(this._reconnectAttempt, delays.length - 1)];
setTimeout(() => this._attemptReconnect(), delay);
this._reconnectAttempt++;
```

### Engine health probe budget

| Constant | Value |
|---|---|
| `claude --version` subprocess timeout | 3000 ms |
| Credential-source probe timeout | 3000 ms |
| Result caching | Once per server process; re-probed only on an explicit session restart from Settings |

The probe runs in Phase 1 and must not be allowed to delay the WebSocket server. On timeout the health
result is `unknown` with the timeout as its reason, the overlay proceeds, and the banner says the probe
timed out rather than claiming the CLI is missing — a slow filesystem and an absent binary warrant
different user actions.

The probe never starts a session. `claude --version` is a process that exits; a `ClaudeSDKClient` connect
is a process that stays, and doing that at startup would spawn a subprocess for every launch including
the many where no message is ever sent.

### Phase 1 browser-wait delay

```
500 milliseconds
```

Phase 2 of deferred initialization starts with `await asyncio.sleep(0.5)` to give the browser time to establish its WebSocket connection. During this window, Phase 1 is complete — the server is accepting connections — and the browser's first RPC call may already be in flight.

Shortening this produces races where `startupProgress` events fire before the browser has registered its `AcApp` class (events silently lost). Lengthening adds user-visible delay before progress updates start.

### File indexing batch size

```
20 files per batch
```

Phase 2's indexing step groups files into batches of 20, calls the symbol index for each batch in a `run_in_executor`, then `await asyncio.sleep(0)` to yield to the event loop. This allows WebSocket frames (pings, progress events, any RPC calls) to be processed between batches.

Smaller batches produce more responsive WebSocket handling but higher overhead. Larger batches index faster but can starve the event loop for hundreds of milliseconds at a time.

### Port probe defaults and range

| Constant | Value |
|---|---|
| WebSocket server default port | 18080 |
| Webapp static server default port | 18999 |
| Port probe increment | 1 |
| Maximum probe attempts | 100 |

Port probing logic:

```python
def find_available_port(start: int) -> int:
    for offset in range(100):  # max 100 attempts
        port = start + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available port in range {start}-{start + 99}")
```

Both the WebSocket port and the webapp port are probed independently. A second concurrent `aic-dc` instance scans past the first's ports rather than accidentally binding to them or cross-wiring.

Probing binds to `127.0.0.1` even when `--collab` is passed — a loopback bind is a strict superset check (if `0.0.0.0:N` is taken, `127.0.0.1:N` is also unavailable). Avoids needing different probe logic for different bind modes.

## Schemas

### CLI argument shape

Matches `argparse` parser in the CLI entry point:

| Flag | Default | Type |
|---|---|---|
| `--server-port` | 18080 | int |
| `--webapp-port` | 18999 | int |
| `--no-browser` | false | flag |
| `--repo-path` | current directory | path |
| `--dev` | false | flag (mutually exclusive with `--preview`) |
| `--preview` | false | flag (mutually exclusive with `--dev`) |
| `--verbose` | false | flag |
| `--collab` | false | flag |

The `--repo-path` argument accepts an absolute or relative path. Relative paths are resolved against the current working directory. If the resolved path is not a git repository, the server writes an instruction HTML page to a temp file, opens it as `file://` in the browser, and exits.

### Git-repo validation HTML

Served as a `file://` URL when validation fails. Exact layout:

- Dark theme (`#0d1117` background)
- Centered content, AIC⚡DC brand at top (4rem / 18% opacity)
- Offending path in accent blue (`#58a6ff`), monospace font
- Remediation commands in green (`#7ee787`) code blocks
- Two commands shown: `git init` and `cd /path && git init`

Terminal output mirrors the HTML content in plain text before exit. Both channels are best-effort — the goal is informing the user; the server exits regardless.

## Dependency quirks

### `asyncio.ensure_future` for Phase 2

Phase 2 runs as `asyncio.ensure_future(_heavy_init())`, NOT `await _heavy_init()`. Critical distinction:

- `ensure_future` — schedules the coroutine as a background task. The event loop returns to serving WebSocket traffic immediately. Progress events fire concurrently with request handling.
- `await` — blocks the calling coroutine (typically `main()`) until Phase 2 completes. The WebSocket server is accepting connections, but the event loop can't process any frames until Phase 2's synchronous CPU-bound work yields.

Implementers using `await` instead of `ensure_future` produce a subtle failure: connections succeed, but progress events don't arrive until Phase 2 finishes. The startup overlay stays at 5% for 30+ seconds, then jumps to 100%. The intermediate stages are invisible.

### GIL and run_in_executor

`run_in_executor` with the default `ThreadPoolExecutor` does not fully release the GIL during CPU-bound Python work (tree-sitter parsing, string processing). A long-running executor call still blocks other threads from running Python bytecode, even though the async framework believes the event loop is free.

Mitigation: the `asyncio.sleep(0)` between batches gives the GIL a chance to swap to other threads (including the WebSocket server's I/O threads). Without the sleep, batches indexed serially would hold the GIL for the full batch duration.

### Signal handling

On `SIGINT` or `SIGTERM`, the server shuts down gracefully:

1. Set a shutdown event (stops Phase 2 if still running)
2. Resolve every pending permission request as a denial, so no `can_use_tool` callback is left awaiting
3. Interrupt any turn in flight, then `await client.disconnect()` on the SDK client
4. Close the WebSocket server (existing connections receive close frames)
5. Stop the webapp HTTP server
6. Exit with status 0

| Constant | Value |
|---|---|
| Turn-interrupt grace before disconnect | 2000 ms |
| `disconnect()` timeout before falling back to a kill | 5000 ms |
| Vite child `terminate()` → `kill()` timeout | 5000 ms |

**Ordering is load-bearing, and step 2 comes first.** The `can_use_tool` callback runs on the SDK's read
loop (`specs-reference/3-engine/permissions.md` § The callback runs on the SDK's read loop). A pending
request means that loop is parked inside our coroutine, and `disconnect()` cannot drain a transport whose
reader is waiting on a decision that will now never arrive. Denying first releases the loop; disconnecting
first deadlocks the shutdown until the 5-second fallback kills the subprocess.

The interrupt-then-disconnect sequence exists so the CLI can flush its own session files. Those files are
the primary transcript and `.aic-dc/sessions/` is a mirror of them, so a hard kill mid-turn can leave the
authoritative record shorter than what the user watched stream into the browser — and our mirror shorter
again, since it only ever receives what the CLI has already written.

Child processes (Vite dev/preview in `--dev`/`--preview` mode) are terminated with `process.terminate()` followed by `process.wait(timeout=5.0)`. If the child is still alive after 5 seconds, `process.kill()` is called.

## Cross-references

- Behavioral two-phase flow, git validation, collaboration network binding: `specs5/6-deployment/startup.md`
- RPC methods called during init (`complete_deferred_init`, etc.): `specs-reference/1-foundation/rpc-inventory.md`
- Engine health shape, CLI discovery, and credential resolution: `specs-reference/3-engine/session.md` § `EngineHealth`
- Why the permission callback constrains shutdown ordering: `specs-reference/3-engine/permissions.md`
- Transcript-on-disk layout read during Phase 1: `specs-reference/3-engine/history.md` § Working-directory layout
- Build-time config and version baking: `specs-reference/6-deployment/build.md`
- Webapp static server specifics: `specs-reference/6-deployment/build.md` § Built-in Static File Server