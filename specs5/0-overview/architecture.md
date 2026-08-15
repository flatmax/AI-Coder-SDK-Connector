# Architecture

The big-picture view of AC⚡DC. A new reader should understand the complete system from this file
alone, then dive into specific specs for detail.

## System Overview

AC⚡DC is a browser frontend for Claude Code. It runs as a local terminal process and presents its UI
in the browser. A single Python process hosts all backend services; a Lit-based single-page webapp
connects to it over one WebSocket. The process drives **one Claude Code session per repository**
through the Claude Agent SDK, which in turn owns a `claude` CLI subprocess.

The division of labour is the whole design. Claude Code owns the conversation, the context window,
prompt caching, tool use, bash execution, web fetch, subagents, compaction, file checkpointing, and
the application of edits. AC⚡DC owns everything a terminal cannot do: spatial navigation over the
code the agent touches, a permission dialog with a rendered diff in it, live context and cost
visualisation, document tooling, multi-client collaboration, and — its one piece of genuine
intelligence — tree-sitter symbol and document indexes, offered to the agent as MCP tools and to the
editor as language features.

```
┌──────────────────────────────────────────────────────────────────┐
│                         Browser (Lit SPA)                        │
│   Chat + tool cards · Files · Context · Settings · Doc Convert    │
│   Permission dialog (Monaco diff) · Subagent tabs · Usage HUD     │
│   ┌──────────────────────────┐  ┌───────────────────────────┐    │
│   │ Diff viewer (Monaco+LSP) │  │ SVG viewer / editor       │    │
│   └──────────────────────────┘  └───────────────────────────┘    │
└─────────────────────────────┬────────────────────────────────────┘
                              │ JSON-RPC 2.0 over WebSocket (jrpc-oo)
                              │ bidirectional — either side calls
┌─────────────────────────────┴────────────────────────────────────┐
│                     Python process (single)                      │
│  ClaudeCodeService · Repo (git) · Settings · Collab · DocConvert  │
│                                                                  │
│  ┌───────────────────────────┐   ┌───────────────────────────┐   │
│  │ Engine adapter            │   │ Symbol index (tree-sitter)│   │
│  │  options · message pump   │   │ Doc index (markdown, SVG) │   │
│  │  can_use_tool · hooks     │   │ Reference graph           │   │
│  │  SessionStore mirror      │   └─────────────┬─────────────┘   │
│  └────────────┬──────────────┘                 │                 │
│               │             ┌──────────────────┴─────────────┐   │
│               │             │ In-process MCP server "ac-dc"  │   │
│               │             └──────────────────┬─────────────┘   │
│               │  ClaudeSDKClient               │ tool calls      │
│               └───────────────┬────────────────┘                 │
└───────────────────────────────┼──────────────────────────────────┘
                                │ stdio subprocess
              ┌─────────────────┴───────────────┐
              │ claude CLI (bundled or on PATH) │──→ Anthropic API
              └─────────────────┬───────────────┘
                                │ its own tools: Read, Edit, Bash, Grep, Task, WebFetch …
       ┌────────────────┬───────┴─────────────┬─────────────────────┐
       │ git repo       │ filesystem          │ .ac-dc4/            │
       │ (worktree)     │ (agent tool calls)  │ (sessions, history) │
       └────────────────┴─────────────────────┴─────────────────────┘
```

Note what the diagram does *not* show: a path from AC⚡DC to a model provider. AC⚡DC never calls an
LLM. Everything that reaches a model goes through the CLI subprocess, which authenticates with its
own configuration.

### Component Responsibilities

| Component | Responsibility |
|---|---|
| **ClaudeCodeService** | The RPC surface replacing `LLMService`: chat, cancel, sessions, permission resolution, mode and model switches, context usage, MCP controls |
| **Engine adapter** | `ClaudeSDKClient` lifecycle, options assembly, the message pump, `can_use_tool`, hooks, the `SessionStore` mirror. The only code that knows SDK message types |
| **Repo** | Git operations, file I/O, file tree, search, per-path write mutex |
| **Settings** | Config read/write/reload with a whitelisted type set |
| **Collab** | Connection admission, client registry, role queries (optional, gated on `--collab`) |
| **DocConvert** | Document format conversion (docx, pdf, pptx, xlsx, … → markdown) |
| **Symbol index** | Tree-sitter parsing, cross-file reference graph, compact structural map |
| **Doc index** | Markdown + SVG outline extraction, keyword enrichment, doc reference graph |
| **MCP bridge** | In-process MCP server `ac-dc` exposing six read-only repo-intelligence tools to the agent |
| **History store** | Append-only JSONL mirror of the rendered transcript — what the user browses and searches |
| **SessionStore adapter** | Mirrors the engine's own transcript into `.ac-dc4/sessions/`, and is what resume reads back |
| **App shell** | WebSocket client, server-push routing, dialog host, global shortcuts |
| **Dialog tabs** | Chat, Context, Settings, Doc Convert |
| **Permission dialog** | The `can_use_tool` surface: tool name, input, a Monaco diff for edit tools, allow / deny / remember |
| **Viewers** | Monaco diff viewer (text), SVG viewer/editor (vector), file navigation grid |
| **Usage HUD** | Floating post-turn overlay: cost or billing mode, per-model usage, context percentage, rate limits |

## Process Model

One Python process hosts everything:

- A built-in HTTP static file server serves the bundled webapp on one port (default 18999)
- A jrpc-oo WebSocket server handles all RPC on another port (default 18080)
- An asyncio event loop drives both servers, the SDK client, and most coordination work
- One `ThreadPoolExecutor` handles the remaining CPU-bound work: tree-sitter indexing, keyword
  enrichment, and document conversion. The streaming executor is gone — the SDK is async, so there is
  no blocking provider call to isolate

The `claude` CLI runs as a child process owned by the SDK client. AC⚡DC never spawns it directly, and
never re-creates the client behind the user's back to recover from an error — a fresh session would
have no context, and the conversation would appear to develop amnesia.

The browser receives the webapp over HTTP, opens a WebSocket back to the server, and does all further
communication over that single connection. Either side can call methods on the other — the backend
pushes stream chunks, tool cards, permission requests, and broadcast events to the browser; the
browser calls everything else.

Bind addresses default to `127.0.0.1` (loopback only). Passing `--collab` binds to `0.0.0.0` and
activates the admission-gated collaboration mode — remote clients can connect, the first client is
auto-admitted as host, and all subsequent clients require explicit admission.

## Layered Dependency Model

The backend and frontend are organised in dependency layers. Each layer depends only on layers below
it. Specs are numbered to match.

**Layer 1 — Foundation.** RPC transport (jrpc-oo over WebSocket), configuration (file locations, hot
reload, managed vs user files), repository (git operations with per-path write mutex).

**Layer 2 — Indexing.** Symbol index (tree-sitter for Python/JS/TS/C/C++/MATLAB, compact map output),
document index (markdown + SVG outline extraction), reference graphs (code → code, doc → doc, doc →
code), keyword enrichment (optional KeyBERT for disambiguating similar headings). Consumers are Monaco
language features, the MCP bridge, and browser navigation surfaces — never prompt assembly.

**Layer 3 — Engine.** The Claude Code session: options assembly, the message pump that turns SDK
messages into server-push events, the permission callback and its browser dialog, the tool-surface and
hook contract, history as a mirrored transcript with SDK-owned continuity, context visibility from
`get_context_usage()`, and the MCP bridge that exposes layer 2 to the agent.

**Layer 4 — Features.** Image persistence (paste-to-chat, thumbnails, re-attach), code review mode
(read-only via git soft reset), collaboration (admission flow, restriction policy), document
conversion backend.

**Layer 5 — Webapp.** App shell (WebSocket owner, dialog host, startup overlay), chat panel (streaming
display, tool cards, todo list, thinking regions, input area), permission dialog, file picker (tree
with git status), diff viewer (Monaco + markdown/TeX preview + LSP), SVG viewer (pan/zoom + visual
editor), file navigation grid, context tab, settings tab, usage HUD, subagent browser, doc convert
tab.

**Layer 6 — Deployment.** Vite build for the webapp, packaged binaries per platform, startup
sequencing (fast phase + deferred heavy init including engine connect, graceful shutdown, config
directory upgrade on version change).

**Layer 7 — Future.** Speculative designs, plus the record of which earlier speculative designs the
platform implemented for us. Not for implementation.

Implementers working bottom-up complete and test each layer before starting the next.
[`implementation-guide.md`](implementation-guide.md) describes the build order and which contracts are
fixed by interop.

## Key Data Flows

### A User Turn

1. User types in the chat textarea and clicks Send
2. Browser generates a **request ID**, renders the user message optimistically, and calls
   `ClaudeCodeService.chat_streaming(requestId, message, files, images)`
3. Server returns `{status: "started"}` synchronously; the real work runs in a background task
4. Server broadcasts `userMessage` to all connected clients (collaborators see it immediately). It does
   **not** persist the message itself: the CLI writes the user entry and the SDK mirrors it to us during
   the turn, so persistence is a consequence of step 6, not a step of its own ([CC-19](../plan/decisions.md#cc-19))
5. Server builds the turn's **framing** block — selected file paths, the file open in the viewer, the
   cursor or selection range, review facts if review is active. Framing is bounded and never contains
   file content. Pasted images are attached as content blocks, passed through the SDK's verbatim dict
   path
6. Server calls `query()` and starts a message pump
7. Pump translates SDK messages into server-push events, every one carrying the request ID and a
   block identity: text and thinking chunks, tool cards, tool results, subagent lifecycle, compaction
   boundaries, rate-limit warnings
8. Tool calls that require approval suspend inside `can_use_tool` until the permission dialog answers
9. Hooks fire around each tool call: cards appear on `PreToolUse`, and `PostToolUse` drives
   `filesModified` broadcasts, incremental re-indexing, and viewer refresh
10. Pump drains to `ResultMessage` and emits `streamComplete` with cost, per-model usage, duration and
    terminal reason
11. Post-turn housekeeping (mirror flush, re-index settle, context-usage refetch) completes and emits
    `postResponseComplete`

Steps 5 and 6 are the whole of AC⚡DC's contribution to what the model sees. There is no prompt
assembly, no token budget, and no context reconstruction. See
[`../3-engine/session.md`](../3-engine/session.md).

### A Permission Request

1. The agent decides to call a tool; the SDK invokes `can_use_tool` with the tool name, input, and
   permission context
2. Server classifies the tool (auto-allowed, gated, or interaction), and for an edit tool computes the
   proposed diff
3. Server pushes `permissionRequest` to **localhost clients only**, and pushes a read-only notice to
   everyone else — a remote collaborator can watch but cannot answer
4. The dialog renders the tool, its input, and the diff. The user allows, denies, or
   allows-and-remembers; remembering returns a permission rule the SDK writes to project settings
5. `resolve_permission` delivers the decision; the callback returns allow or deny to the SDK, and the
   outcome is broadcast so every client's card agrees
6. No answer within the timeout, or no localhost client connected, resolves to deny with a reason the
   transcript records

See [`../3-engine/permissions.md`](../3-engine/permissions.md).

### The Agent Changes Files

1. `PostToolUse` fires for a file-mutating tool
2. Paths are resolved repo-relative and broadcast as `filesModified`; the file tree refreshes git
   status, the diff viewer reloads the file if it is open, the save LED updates
3. Touched paths are incrementally re-indexed, dispatched to the symbol or document index by
   extension, debounced because an agent mid-refactor writes many files in quick succession
4. Document files are queued for background keyword enrichment
5. A pending re-index is **flushed synchronously** before any `ac-dc` index-reading tool returns — our
   own tool must never describe code as it was before the agent's last edit

See [`../3-engine/tool-surface.md`](../3-engine/tool-surface.md).

### Session Continuity

| Trigger | Mechanism | What the model sees |
|---|---|---|
| Server restart | Reconnect with `resume=<last session id>` | Its own prior context, including its own compactions |
| Open a session in the history browser | `resume=<that session id>` | That session's context |
| Branch from a past session | `fork_session` | A copy; the original stays intact |
| New Session | Connect without `resume` | Nothing |
| Undo a file change | `rewind_files(user_message_id)` | Unchanged — files rewind, the conversation does not |

Continuity is never a replay. AC⚡DC does not read its own transcript back into a prompt; the SDK owns
resumption, and the mirrored transcript is a record rather than an input. See
[`../3-engine/history.md`](../3-engine/history.md).

### File Selection Sync

1. User toggles a checkbox in the file picker
2. Picker dispatches `selection-changed` up through the files tab
3. Files tab calls `ClaudeCodeService.set_selected_files(paths)`
4. Server updates its authoritative selected-files list
5. Server broadcasts `filesChanged` to all connected clients (including the originator)
6. Each client's files tab applies it via direct property assignment to the picker, bypassing Lit's
   reactive re-render, which would reset scroll and interaction state

The mechanism is unchanged; the *meaning* changed. Selection is a hint about what the user is pointing
at, carried in the turn's framing, not a promise that the files' contents are in the prompt. The
picker's third checkbox state now denies the agent read access to a path rather than excluding it from
a map.

### Collaboration Admission

1. New WebSocket connects; the server detects this is not the first connection
2. Server sends a raw `admission_pending` WebSocket message (pre-JRPC) to the connecting client; the
   client shows a waiting screen
3. Server broadcasts `admissionRequest` to all admitted clients; a toast appears for every admitted
   user
4. Any admitted user clicks Admit or Deny; the RPC runs on the host's behalf
5. On admit, the server completes the JRPC handshake and the new client becomes a full participant
6. On deny, the server closes the WebSocket with code 1008
7. 120-second timeout — unanswered requests auto-deny; same-IP requests replace older pending requests
   before they expire

## Cross-Cutting Concerns

### Path Handling

All file paths are relative to the repository root. The path validator at the Repo layer rejects `..`
traversal, symlinks that escape the repo, and absolute paths that resolve outside the repo. Every file
operation on the server side goes through this validator, and the frontend deals only in
repo-relative path strings.

The agent's tool calls are the exception worth naming: they are executed by the CLI, not by our Repo
layer, so our validator does not gate them. What gates them is the permission dialog and the
project's own permission rules. Paths arriving *from* the agent (in tool inputs, hook payloads, and
MCP arguments) are normalised and validated before they are used as broadcasts, index keys, or viewer
targets.

### Error Surfaces

Four places errors reach the user:

1. **Toasts** — transient notifications for brief, non-conversational feedback (save success,
   restricted-in-collab, clipboard copy). Auto-dismiss.
2. **Assistant message errors** — when a turn fails or is cancelled, the chat panel renders an error
   card inline with the conversation, in the message flow the user is already watching.
3. **System event messages** — operational events (commit, reset, session switch, compaction
   boundary, permission-mode change) appear as pseudo-user messages with distinct card styling and are
   persisted in the mirrored transcript.
4. **Engine health banners** — conditions that affect the session rather than a turn: the repo-local
   transcript mirror has a gap, our MCP server failed to start, the CLI version differs from the SDK's
   pin, the resolved credential source is unexpected. Persistent until resolved or dismissed, because
   each one silently degrades capability in a way the user would otherwise attribute to the model.

The old fourth category — LLM-visible system events fed back into the prompt — has no equivalent.
Claude Code owns its context; AC⚡DC cannot inject a note into it and does not try. Where the agent
genuinely needs to know an operational fact (that review mode is active, for instance), it arrives as
turn framing or through an MCP tool it can call.

### Localhost-Only vs Shared State

All state is shared across connected clients by default — selected files, the transcript, streaming
output, tool cards all broadcast to every admitted client. Every mutating operation is guarded:
non-localhost participants receive `{error: "restricted", reason: ...}` instead of the normal return
value. Read operations (file content, symbol queries, history browsing, search) work for everyone. The
effect is a shared read-only view with write privileges reserved to the host machine.

Permission requests are the sharpest case and get their own rule: they resolve against localhost
clients only. A permission dialog authorises arbitrary `Bash`, so allowing a remote participant to
answer one would turn collaboration mode into a remote-code-execution grant.

Single-user mode (no `--collab` flag) attaches no `Collab` instance; the localhost check
short-circuits to "allowed" and every caller is treated as localhost.

### Credentials Belong to the CLI

The CLI authenticates with its own configuration — a subscription login or an API key in its own
environment. AC⚡DC does not manage credentials, does not read them, and **never exports provider
credentials into the process environment**. The native engine's `env` block in `llm.json` did exactly
that, and under Claude Code it would silently redirect the CLI to a different account or a Bedrock
endpoint. Startup reports which CLI binary and which credential source were resolved, because a
misresolved account presents as inexplicable behaviour rather than as an error.

### Graceful Degradation

Optional dependencies can be missing without breaking core functionality:

| Dependency | Provides | Without it |
|---|---|---|
| **KeyBERT + sentence-transformers** | Doc index keyword enrichment | Outlines render without `(kw1, kw2)` annotations; one-time toast |
| **markitdown** | Document conversion (docx / pdf / xlsx / csv / rtf / odt / odp) | Doc Convert tab hidden |
| **PyMuPDF** (`fitz`) | PDF text extraction and SVG export | `.pdf` unavailable; pptx/odp fall back to python-pptx or markitdown |
| **LibreOffice** (`soffice` on PATH) | Primary pptx/odp → PDF pipeline | pptx falls back to python-pptx; odp falls back to markitdown |
| **python-pptx** | Pptx fallback when LibreOffice unavailable | Pptx conversion fails with an install-hint error |
| **openpyxl** | Colour-aware xlsx conversion | Xlsx falls back to markitdown (no cell colour preservation) |
| **make4ht** + LaTeX | TeX preview (`.tex`/`.latex` in the diff viewer) | Preview button replaced with an install-hint pane |
| **tree-sitter language grammars** | Per-language symbol extraction | That language produces no symbols; files still editable |
| **`ac-dc` MCP server** | Repo intelligence as agent tools | Session continues; a banner reports the loss, because the agent otherwise looks inexplicably worse at repo-wide questions |

The `claude` CLI is **not** in this table. It is the one hard prerequisite: without it there is no
engine, and startup fails with an actionable message naming the searched locations rather than
degrading into a UI that cannot answer anything.

### Per-Repo Working Directory

Each repository gets a `.ac-dc4/` directory at its root, auto-created on first run and added to the
repo's `.gitignore`:

| Path | Contents |
|---|---|
| `sessions/` | The engine's own transcripts, mirrored via the SDK `SessionStore`, plus summary sidecars and per-subagent transcripts. The one transcript: the engine resumes from it and the history browser reads it |
| `events.jsonl` | AC⚡DC's own operational events — commit, reset, review entry and exit, preset switch, permission-mode change. No message content |
| `index/` | Derived search / summary / request-ID index. Rebuildable from `sessions/`; safe to delete |
| `doc_cache/` | Keyword-enriched outline cache sidecars |
| `tex_preview/` | Transient TeX compilation workspace |

`history.jsonl` and `images/` are gone — [CC-19](../plan/decisions.md#cc-19) retired the second store,
so pasted images live in the transcript entries that carried them.

Per-repo rather than per-user, so history and sessions travel with the repository, survive the CLI's
own retention sweep of `~/.claude/projects/`, and can be audited alongside the code. The `agents/`
directory of the old parallel-agent design is gone; subagent transcripts live under `sessions/`, keyed
by SDK agent ID.

## What the Conversion Removed

Named explicitly, because their absence is a design position rather than an omission: prompt assembly
and cache-control placement · the four-tier stability cache, its N-values, and the membrane/flux
controller · the cache warmer · the token counter · the `🟧🟧🟧 EDIT` protocol and its anchored-match
apply pipeline · the LLM-driven history compactor · URL detection, fetching, caching, and
summarisation · the `🟧🟧🟧 AGENT` spawn protocol · per-mode and per-review system prompts · `litellm`
and its transitive dependency tree.

Each is replaced by a platform capability rather than dropped: see
[`../plan/decisions.md`](../plan/decisions.md) for the mapping, and
[`../plan/inventory.md`](../plan/inventory.md) for the file-by-file disposition.

## Further Reading

For the full specification suite, see [`../README.md`](../README.md). For the mechanical rule relating
specs5 to specs-reference, see [`../../specs-reference/README.md`](../../specs-reference/README.md).
For build order and the contracts fixed by interop, see
[`implementation-guide.md`](implementation-guide.md). For the verified SDK surface every layer-3 spec
is written against, see [`../plan/sdk-surface.md`](../plan/sdk-surface.md).

The layered diagram above is also drawn to scale, with the component set of each layer, in
[`../architecture.svg`](../architecture.svg). It is hand-maintained and therefore lags this document;
this document wins where they disagree.
