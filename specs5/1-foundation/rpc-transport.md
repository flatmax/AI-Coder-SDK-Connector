# RPC Transport

Bidirectional JSON-RPC 2.0 over a single WebSocket connection, using the jrpc-oo library. Either side can call methods exposed by the other.

## Connection Model

- Single WebSocket carries all traffic, multiplexed by JSON-RPC request IDs
- Browser is the client; Python process is the server
- Localhost-only binding by default; all-interfaces binding when collaboration is enabled
- Port passed to browser via URL query parameter

## Transport Configuration

- Default server port selection and override
- Default webapp port selection and override
- Remote timeouts (server-side and browser-side)
- Protocol is plain `ws://` — no TLS (local tool)
- Maximum WebSocket frame size is raised from the `websockets` library default (1 MiB) to 64 MiB so data-URI image payloads in chat args don't trip code 1009 disconnects. The limit still provides back-pressure against pathological payloads.

## Registering Services

- Server-side: objects have their public methods auto-exposed; namespace derived from class name
- Browser-side: root component registers methods the server can call
- Underscore-prefixed methods are never exposed

## When a Service Method Raises

jrpc-oo catches every exception a service method raises, returns its text to the caller, and prints one line: `Failed: {e}`, or `Async method failed: {e}` from a coroutine. That line carries no method name, no arguments and no timestamp, which is how a real refusal — `Absolute paths not accepted: /home/you/repo/a.py` — reached the operator as a sentence naming neither the call that produced it nor the view that would now render nothing.

The registration facade wraps every exposed method so a failure is also logged with the call that caused it: the qualified name, the arguments, and the message. Three decisions shape it, and each one is a thing the obvious version would have got wrong.

**The seam is the callback, not the method.** Registration leaves a `{qualified name: wrapper}` dict on the inner server; the wrapper signals failure by invoking a callback. Substituting that callback sees every error jrpc-oo swallows while leaving the method itself untouched — which matters because service methods have Python callers too, and wrapping the method would log a `RepoError` that an internal caller deliberately catches as though it were a fault.

**The arguments are summarised, not rendered.** A string is clipped before it is repr'd, and a list or dict is described by shape rather than contents. Users paste screenshots as data URIs into chat arguments, so the naive version copies megabytes to build a log line and then throws most of it away. A clip that reports how much it dropped is the difference between a bounded line and a silent one.

**The error the caller receives is unchanged.** Only the callback is substituted, so the browser sees exactly the string it saw before. That is what makes the wrapping safe to install on every service rather than on the ones somebody remembered.

What it does not recover is the traceback: the exception object is gone by the time the callback runs, so the record holds the message and the call, not the stack. Getting the stack means intercepting the method, which is the thing above that this declines to do. The library's own bare print also stays, because jrpc-oo is a dependency rather than vendored code.

This is one half of a failure being reportable. The other half is the browser: see [diff-viewer.md](../5-webapp/diff-viewer.md#when-neither-side-can-be-read), where a viewer that renders a failed fetch as empty content hides the same event from the other end.

## Calling Conventions

- Server → browser: uses bracket-notation proxy on a `call` attribute
- Browser → server: uses bracket-notation proxy on a `call` attribute
- Response envelope unwrapping (single-key object → direct value)
- Multi-remote responses are keyed by client UUID; first value wins for read ops
- Broadcasts reach all connected admitted remotes

## Streaming Pattern

- Browser initiates a streaming request with a generated request ID
- Server returns synchronously with `{status: "started"}`, streams via server-push
- Chunk payloads carry full accumulated content, not deltas — dropped/reordered chunks are harmless
- A completion event signals end of stream
- Progress events (e.g., compaction, URL fetch) share the same channel

## Connection Lifecycle

- Handshake hook (before JRPC setup) — used by collaboration admission
- `remoteIsUp` — connection confirmed
- `setupDone` — call proxy populated, ready to invoke methods
- Disconnect and reconnect triggers
- Reconnection uses exponential backoff (1s, 2s, 4s, 8s, cap 15s)

## Threading

- Server event loop reference must be captured on the event loop thread before launching worker threads
- Worker threads schedule callbacks via `run_coroutine_threadsafe` using the captured loop
- Callers must never acquire a new event loop inside a worker thread

## Concurrency

- Only one user-initiated turn is active at a time (enforced by the engine service, see [session.md](../3-engine/session.md#concurrency-guard))
- The guard counts user intent, not engine activity: subagents spawned by the agent's `Task` tool are internal to the turn and are never blocked by it. Their events are correlated by agent ID inside the parent turn's request ID (see [session.md](../3-engine/session.md#message-taxonomy--ui))
- Request IDs are the multiplexing primitive — the transport never assumes a singleton stream; every server-push event carries the exact ID of the stream it belongs to
- Non-streaming calls are served concurrently
- All state is global across connected clients; streaming and turn events are broadcast to all admitted remotes

## Reconnection Behavior

- On reconnect, the client fetches current state via a single RPC call and rebuilds its UI
- First connect shows a startup overlay driven by progress events
- Subsequent reconnects show only a transient "Reconnected" toast

## Invariants

- Every server-push event reaches all admitted clients unless explicitly filtered
- Every server-push event carries the exact request ID of the stream it belongs to; the transport never assumes a singleton stream
- A captured event-loop reference is always usable from a worker thread
- A reconnecting client never receives duplicate state that would double-apply history or selections
- Methods on registered objects must return a value (server awaits every browser-side call)
- Every exposed method carries failed-call logging; a service reaches the wire through one registration path and that path installs it, so "which services are covered" is not a list anybody maintains
- Adding that logging never changes what a caller receives — the wrapper substitutes a callback and nothing else