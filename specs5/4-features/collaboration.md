# Collaboration

Collaboration mode allows multiple browsers to connect to a single backend. The first connection is
auto-admitted as the host; subsequent connections require explicit admission from any already-admitted
user via a toast prompt. Once admitted, non-localhost participants see the full UI and receive all
broadcast events but cannot send prompts, mutate engine state, perform git operations, **or answer a
permission prompt**. Disabled by default — enabled via an explicit CLI flag.

That last restriction is new and is the highest-stakes rule in this spec. Answering a permission prompt
authorises a specific tool call — including arbitrary `Bash` — so a participant who could answer one
would turn collaboration mode into a remote-code-execution grant. See
[decisions § CC-15](../plan/decisions.md#cc-15--permission-prompts-are-localhost-only) and
[`../3-engine/permissions.md` § Collaboration and Authority](../3-engine/permissions.md#collaboration-and-authority).
## Activation
- Disabled by default for security
- Enabled via a CLI flag
- When enabled — admission-aware server is used, collaboration service is registered as an RPC class, WebSocket and webapp servers bind to all interfaces
- When disabled — plain server, no admission flow, services bind to localhost only
## Connection Lifecycle
### First Connection (Auto-Admit)
- First WebSocket connection is auto-admitted with no screening
- This is the host — normally the user who started the process and opened their localhost browser
- No admission toast shown
### Subsequent Connections (Admission Required)
- All connections after the first are held in a pending state before JRPC setup completes
- Server overrides the JRPC handle-connection method to insert screening
- New WebSocket connects; server detects this is not the first connection
- Server sends a raw WebSocket admission-pending message with a generated client ID
- Server does not complete JRPC setup yet — no methods are exposed
- Server broadcasts an admission-request event to all admitted clients
- An admitted user clicks Admit or Deny in their UI
- On admit — JRPC setup completes, client becomes a full participant
- On deny — WebSocket closed with an error code, no JRPC state was created
### Disconnection
- Server removes the client from the registry
- If the host disconnects, the next admitted client (by admission order) becomes the host
- If the last client disconnects, the server resets — next connection will be auto-admitted
## Server Architecture
### Admission-Aware Server
- Subclass of the base RPC server
- Overrides handle-connection to insert admission screening and caller tracking
- Runs its own message receive loop after admission (mirroring the base server's loop) to set a current-caller identifier before each dispatch
### Client Registry
Per-client record:
- Client ID (UUID assigned on connection)
- IP address (peer IP from WebSocket)
- Role — host or participant
- Is-localhost flag
- Admission timestamp
- WebSocket reference
### Pending Queue
Per-pending-request record:
- Client ID
- IP
- Raw WebSocket (pre-JRPC)
- Future resolved by admit/deny
- Request timestamp
- Pending requests not acted on within a timeout (default 120 seconds) are auto-denied and the WebSocket is closed
- If a new connection arrives from the same IP while a previous request is still pending (e.g. browser refresh), the old pending request is auto-denied and its toast is removed before the new request is created
- Cancelled-by-replacement requests include a flag so frontends can distinguish from explicit deny
- Server monitors the pending client's WebSocket for closure — if the pending client disconnects before a decision, the request is cleaned up and an admission-result broadcast removes the toast from all admitted clients
### Localhost Detection
Connection is localhost if the peer IP matches:
- Loopback addresses (IPv4 and IPv6)
- Any IP address assigned to a local network interface
Handles the case where the host opens their browser to their LAN IP instead of loopback.
### Host Promotion
When the current host disconnects:
1. Next admitted client (by admission time) is promoted to host
2. Promoted client receives a role-changed event
3. All clients receive an updated client list
4. If the promoted client is non-localhost, they gain host role but still cannot send prompts — only localhost hosts can send prompts
### Role vs Localhost
Role and localhost are independent concepts:
| | Localhost | Non-localhost |
|---|---|---|
| Host | Full control | Can admit/deny, cannot mutate |
| Participant | Full control | Read-only: browse, search, view |
- The meaningful restriction is localhost vs non-localhost, not host vs participant
- Host role primarily determines who can admit/deny new connections when no localhost client is connected
## RPC Restrictions
### Localhost-Only Operations
Restricted to localhost connections (non-localhost participants get an error):
| Category | Operations |
|---|---|
| Turns | Chat streaming, commit-message turn, cancel streaming |
| Permissions | **Resolve permission**, set permission mode |
| Engine lifecycle | Connect the engine, shut the engine down |
| Session management | New session, resume session (with or without fork), delete engine session |
| Engine state | Set denied-read files, set model, rewind files, stop a subagent task |
| MCP control | Reconnect an MCP server, toggle an MCP server |
| Review mode | Start review, end review |
| Git operations | Commit, stage/unstage/discard files, rename/delete/create/write files, reset hard, stage all |
| Settings | Save config, reload engine config, reload app config |
| Doc convert | Convert files |

`resolve_permission` is the entry that is not obviously mutating and is nevertheless the most powerful
method in the inventory. `rewind_files` and `stop_task` are restricted for the ordinary reason: both
change state the host is responsible for. MCP toggles are restricted because they change what tools
exist for everyone.

The engine-lifecycle pair is restricted because starting and stopping the `claude` subprocess is not a
read: `shutdown` ends every other participant's view of the turn in progress, and `connect_engine`
launches a process against the host's repository and the host's credentials. They are easy to overlook
when auditing the surface — neither reads like a mutation of repository state, which is what the other
rows have in common.
### Read-Only Operations (Available to All)
- File content, file tree, search, engine state snapshot
- Flat file list, LSP queries, doc outlines
- Engine health, context usage, MCP server status, advertised server info
- Session list, session messages, history search, subagent transcripts
- Collaboration queries — admit/deny, connected clients

Read-only introspection is deliberately unrestricted. Watching a turn — its tool calls, its context
usage, its health — is the entire point of collaboration; a participant who can see less than the host
cannot review what the agent did.
### Enforcement Mechanism
- Service classes check caller identity via a shared collaboration reference
- A per-message context identifier is set before each dispatch (inside the receive loop)
- Service methods read the identifier and look up the client's localhost flag
- When no collaboration instance is attached (single-user mode), callers are always treated as localhost

**The identifier has to be a `ContextVar`, not an attribute.** An `async def` RPC method does not run its
body during dispatch: the dispatcher calls the method, gets a coroutine, wraps it in
`asyncio.create_task` and returns. The body runs a loop iteration later — by which time the receive
loop's `finally` has already cleared the caller. Held as an attribute the identifier reads as `None` by
then, and "no caller" means "trusted", so **every localhost gate on every async method passes for every
remote caller**, silently and with no failing test unless a test awaits across the dispatch boundary.
`create_task` copies the current context at creation, so a `ContextVar` set for the dispatch is inherited
by the task and the receive loop's reset cannot reach into that copy. This is not a Claude Code concern:
it applies to every async gate in the inventory, and the methods it was hiding predate the port.
### Restriction Response Shape
- Restricted methods return a specific error shape rather than raising
- Fields — error type (restricted), reason (human-readable)
- Frontend components track a mutation-allowed flag and hide or disable UI affordances for restricted actions
## Collaboration Service Methods
- Admit client — admits a pending client (callable by any admitted user)
- Deny client — denies a pending client, closes their WebSocket
- Get connected clients — returns list of currently connected clients with ID, IP, role, localhost flag
- Get own role — called by a client after JRPC setup to learn its own role
- Get share info — returns routable LAN IPs and WebSocket port for constructing share URLs
## Server → Client Events
- Admission request — broadcast to all admitted clients when a new connection is pending
- Admission result — broadcast when a pending request is resolved (admitted/denied/replaced)
- Client joined — broadcast when a client completes admission and JRPC setup
- Client left — broadcast when a client disconnects
- Role changed — sent to a specific client when their role changes
- Navigate file — broadcast when any client navigates to a file (all clients open the same file)
- Permission mode changed — broadcast when a localhost client changes the permission posture, with who changed it
- Permission request / resolved — broadcast to everyone; only localhost clients can answer
- Session changed — broadcast when a localhost client starts a new session or loads one
## Raw WebSocket Messages (Pre-JRPC)
Sent on the raw WebSocket before JRPC setup, for pending clients:
- Admission pending — sent immediately when a non-first connection is held
- Admission granted — sent when pending client is admitted; normal JRPC setup follows
- Admission denied — sent just before the WebSocket is closed
## Frontend Admission Flow
### Pending State
- Webapp intercepts raw WebSocket messages before JRPC processes them
- Root component overrides the WebSocket created hook to add a capturing message listener on the raw WebSocket
- All three admission message types are consumed before reaching JRPC
- On admission-pending — show a centered waiting screen with a cancel button
- On admission-granted — proceed with normal JRPC setup
- On admission-denied — show brief "Access denied" message and disconnect
### Admission Toast
- Persistent (non-auto-dismissing) toast shown when admission-request event arrives
- Shows connecting IP, admit button, deny button
- Multiple pending requests show multiple toasts, stacked
- New request from an IP matching a pending toast replaces the old toast (handles browser refresh)
- Admitted/denied toast is removed when admission-result arrives (self-action or someone else acted)
### Connected Users Indicator
- Small indicator in the dialog header shows count of connected clients
- Visible always (shows count even with one client)
- Clicking opens a collab popover
### Collab Popover
When collaboration is enabled:
- List of connected clients — role badge (host/participant, color-coded), IP, local label for localhost clients
- Share link section — copyable URL from server's LAN IP and WebSocket port
- Copy button with brief success indicator
- Share hint — instructional text for collaborators
When collaboration is disabled:
- Message explaining that collaboration mode is not enabled
- Instructions showing the CLI flag to enable it
## Participant UI Restrictions
When the calling client is non-localhost, the frontend applies restrictions:
- Chat input area replaced with a static "Viewing as participant" bar
- File picker context menu — git-mutating items hidden (rename, delete, new file). The deny-read items stay visible and are refused at the RPC with a `restricted` toast, because a participant reading that a file is off-limits to the agent is information, not a mutation
- Commit button hidden
- Settings tab editing disabled
- Permission-mode selector shown but read-only — a participant must be able to *see* the posture the agent is operating under, and must not be able to change it
- Permission dialog shown in a watch-only form: the request, the diff or command, and who is being asked, with the buttons absent and a line saying the host is deciding
- New session / resume session disabled
- Review mode controls hidden
Everything else works — browsing files, viewing diffs, searching, reading chat history, using tabs.
## Network Binding

When collaboration is enabled, the WebSocket server and webapp server bind to all interfaces. When disabled, both bind to loopback only.

- WebSocket server — binds all interfaces with collaboration flag, loopback otherwise
- Webapp server (default bundled static server) — same binding rules
- Vite dev server and preview server — same binding rules, via a host flag

Remote collaborators open the share link (which uses the host's LAN IP) to load the webapp and connect back to the host's WebSocket port over the LAN.

### WebSocket URI Derivation

- Webapp builds its WebSocket connection URI dynamically from the page URL
- URI uses the same hostname that served the page, with the WebSocket port
- When a remote client accesses the webapp via a LAN IP, the WebSocket connects to the same LAN IP
- When the page is loaded via loopback, the WebSocket also targets loopback (correct for the host but fails for remote clients)
- Remote clients must access the page using the host machine's LAN IP, not loopback
- The share link in the collab popover includes the correct port automatically

## Integration with Existing Systems

### Communication Layer

- Collaboration-aware server replaces the base RPC server when the flag is passed
- Collaboration instance is created and registered as a separate RPC class to expose admission methods
- Without the flag — plain server used, collaboration class is not registered
- Service classes receive references to the collaboration instance for localhost checks in both modes (the reference is null in single-user mode)

### Streaming

- The broadcast mechanism reaches all connected remotes automatically
- Streaming chunks, completions, `filesModified` events, and all server-push events reach all admitted clients
- No changes needed to the streaming pipeline

### Deny-Read Is Not Synced

A selection sync section stood here: a localhost client toggled a checkbox, the server broadcast
`filesChanged`, and every browser showed the same ticks. [CC-21](../plan/decisions.md#cc-21) removed
the selection, so there is nothing left to keep in agreement.

Deny-read, the per-path state that survived, is deliberately **not** given the same treatment:

- The setter is localhost-gated like every other mutation, and returns `restricted` to a participant
- It broadcasts nothing. A participant learns the current rules from `denied_read_files` in the state
  snapshot it fetches on load or reconnect
- So a rule the host writes mid-session does not reach an already-loaded participant's tree until they
  reload. Accepted: the rules are enforced by the CLI reading `.claude/settings.local.json`, not by any
  browser, so a stale strikethrough misinforms a spectator without weakening anything

### File Navigation Sync

- When any client navigates to a file, the server broadcasts a navigate-file event
- Each client opens the file in its diff viewer or SVG viewer
- Events originating from a remote broadcast carry a flag so the receiving client does not re-broadcast, preventing loops

### Permission Sync

Modes are gone, and with them the mode-sync protocol. What needs syncing now is the permission posture
and the permission dialog:

- When a localhost client changes the permission mode, the server broadcasts the change with the mode and who set it; every browser updates its indicator, and the change is recorded in the transcript as a system event
- A permission request is broadcast to everyone. Localhost clients get the dialog with buttons; participants get the same content, read-only
- Concurrent localhost clients race and the first decision wins; the dialog closes on the others with a note naming who answered
- If no localhost client is connected when a request arrives, it is denied after a short timeout with a reason that says nobody was there. A headless AIC⚡DC with only remote participants therefore degrades toward `plan`-like behaviour rather than toward something permissive
- See [`../3-engine/permissions.md`](../3-engine/permissions.md) for the decision protocol and timeouts

### Session Sync

- When a localhost client starts a new session or loads a previous one, the server broadcasts a session-changed event with the full message list
- All browsers clear their chat panel and display the new conversation state
- Collaborators always see the same conversation context

### Chat History and Mid-Turn Join

- On admission, the new client fetches the engine state snapshot as part of its normal setup, and sees every mirrored message exchanged so far
- If a turn is in flight, the snapshot's `active_streams` carries the turn's rendered blocks so far. A joining client replays them and then follows live chunks — it does not have to wait for completion to see what is happening
- Any pending permission request is in the same snapshot, so a joining client renders the dialog (or its watch-only form) immediately instead of missing the broadcast
- Blocks whose content is cumulative-within-block make this cheap: replay is the current state of each block, not a chunk log

### Context Visibility

- The context HUD reads `get_context_usage`, which is unrestricted, so every client sees the same numbers
- The numbers are the engine's own accounting, not ours, which makes them the same for every viewer by construction — there is no client-side estimate to drift

### Code Review Mode

- Review mode controls are restricted to localhost clients
- If the host enters review mode, participants see the review UI (banner, diffs) via the normal broadcast events

## Module Structure

- A dedicated collaboration module contains the admission-aware server subclass and the collaboration service class
- The split between server subclass and service class is necessary because exposing the server's inherited methods (start, stop, handle-connection) via RPC would be wrong
- The service class contains only the methods intended as RPC endpoints
- Server subclass holds a reference to the service instance and delegates admission state to it

## Limitations

### No Display Names

- Initial implementation shows IP addresses only
- A future enhancement could prompt for a display name on connect

### No Kick

- Admitted clients cannot be removed in the initial implementation
- The host can restart the server to clear all connections

### Single Turn

- Only one user-initiated turn is active at a time (existing constraint, now enforced by the engine service)
- Subagents the agent spawns are internal to that turn and do not count
- Participants cannot queue prompts

### Mid-Turn Join Is Approximate

- A client admitted mid-turn sees block state, not keystroke-level history: it gets each block's current content, not the order the chunks arrived in
- Thinking blocks that have already been superseded are not replayed

### No Follow Mode

- No synchronized navigation in the initial implementation
- Each client browses independently
- A planned future enhancement

## Future Enhancements

- Display names — prompt for a name on connect, show in admission toast and connected users list
- Kick / ban — allow host to remove admitted clients; ban by IP for the session
- Follow mode — synchronized navigation where one user leads and others' viewers follow
- Participant prompt queue — participants submit prompt suggestions that the host approves before sending
- Connection indicators — typing indicators, cursor positions, viewing-file status per connected user

## Invariants

- The first connection is always auto-admitted as host; subsequent connections are always screened
- Non-localhost participants cannot call any mutating RPC method — restricted calls always return an error without side effects
- No permission prompt is ever answered by a non-localhost client, in any mode, including when no localhost client is connected
- The permission posture is visible to every admitted client and settable by none but localhost
- Admission-pending clients have no JRPC state and no exposed methods
- Host disconnection always results in promotion of another admitted client, or a reset if no clients remain
- Same-IP pending requests never accumulate — new requests always replace older ones from the same IP
- Collaboration-aware server behaves identically to the base server in single-user mode (no flag)
- The single-active-turn policy applies across all connected clients, not per-client