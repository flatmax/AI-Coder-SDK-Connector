# Reference: Chat Panel

**Supplements:** `specs5/5-webapp/chat.md`

## Byte-level formats

### Request ID format

Generated browser-side for every user-initiated streaming request:

```
{epoch_ms}-{6-char-alnum}
```

Example: `1736956800000-a1b2c3`

- `epoch_ms` — `Date.now()` at request origination, base-10 integer
- `6-char-alnum` — random lowercase letters + digits

Request IDs are the multiplexing primitive for streaming state. Canonical definition:
`specs-reference/3-engine/session.md` § Request ID format. The format is unchanged from the native
engine — it is browser-generated and never seen by the CLI.

### Block identity and the seq discard rule

A chunk is `{request_id, agent_id, block_id, seq, content, done}`. `content` is cumulative **within a
block**, not within a turn. Canonical shapes: `specs-reference/3-engine/session.md` § Block identity.

Browser-side rendering rule, restated here because it is a frontend invariant:

| Condition | Action |
|---|---|
| `seq` > highest seen for this `block_id` | Replace that block's content; record `seq` |
| `seq` <= highest seen for this `block_id` | Discard the chunk entirely |
| `block_id` not yet seen | Create the block at the end of its scope's block list |
| `done: true` | Mark the block sealed; later chunks for it are discarded regardless of `seq` |

Blocks are ordered by first-arrival, not by `block_id` sort order. `agent_id` selects the scope (null =
main); `block_id` is unique only within a scope.

### Terminal-reason badge labels

`terminal_reason` comes from the engine (`ResultMessage.subtype` normalized —
`specs-reference/3-engine/session.md` § Service: AcApp). There is no `finish_reason` and no provider-level
normalization step, so the badge set is four values plus null:

| `terminal_reason` | Badge label | CSS class |
|---|---|---|
| `completed` | (no badge rendered) | — |
| `aborted_streaming` | `⏹ stopped` | `.terminal-reason.muted` (opacity 0.6) |
| `aborted_tools` | `⏹ stopped during tool use` | `.terminal-reason.muted` |
| `max_turns` | `↻ hit turn limit` | `.terminal-reason.warn` (amber border, tinted bg) |
| `null` / missing | (no badge rendered) | — |

`completed` renders no badge: the overwhelmingly common case must be visually silent, or the badge becomes
noise the user stops reading.

Neither abort value fires a toast — the user pressed Stop and knows. `max_turns` fires one:
`Turn limit reached — send another message to continue`.

The native engine's `length` / `content_filter` / `tool_calls` / `function_call` badges have no successor.
`length` was a provider truncation the CLI now handles internally by continuing; `tool_calls` was a
finish reason because the native engine returned control to us on every tool call, and the CLI does not.

### Typed error toast catalog

Errors arrive as a `ResultMessage` with `is_error: true` plus, when the SDK classified it, an
`error_info`. The classification set is the SDK's, not litellm's — canonical in
`specs-reference/3-engine/session.md` § Service: AcApp.

| `error_type` | Icon | Toast message | Severity |
|---|---|---|---|
| `context_window_exceeded` | `📏` | `Context full — the engine will compact and retry` | `warning` |
| `rate_limit` (with `retry_after`) | `⏱️` | `Rate limited — retry in {N} s` or `Rate limited — retry in {N} min` when `retry_after >= 60` | `warning` |
| `rate_limit` (no `retry_after`) | `⏱️` | `Rate limited — wait and retry` | `warning` |
| `authentication` | `🔑` | `Authentication failed — check engine health in Settings` | `error` |
| `not_found` (with `model`) | `❓` | `Model not found: {model}` | `error` |
| `not_found` (no `model`) | `❓` | `Model not found — check engine config` | `error` |
| `cli_not_found` | `🔌` | `claude CLI not found — check the resolved path in Settings` | `error` |
| `cli_exited` | `💥` | `The engine process exited ({code})` | `error` |
| `api_connection` | `🌐` | `Connection failed — check network / proxy` | `warning` |
| `service_unavailable` | `🔧` | `Provider unavailable — retry later` | `warning` |
| `timeout` | `⏱️` | `Request timed out` | `warning` |
| `budget_exceeded` | `💰` | `Session budget reached — raise max_budget_usd or start a new session` | `error` |
| `engine_error` (or missing info) | `❌` | Message verbatim, or fallback `Engine error` | `error` |

`context_window_exceeded` is a **warning**, not an error, and its wording differs from the native
engine's `Context too large — compact or remove files`. Neither remedy exists: there is no file
selection to trim and no manual compaction button. Auto-compact is the engine's own recovery, so the
toast tells the user what is about to happen rather than asking them to fix it.

`cli_not_found`, `cli_exited`, and `budget_exceeded` are new. All three are failures of *our* process
management or configuration rather than of the model, and all three route the user to Settings rather
than offering a retry.

The toast is in addition to the assistant-card error body. The card is the persistent record; the toast
catches attention when the card is out of view.

### Assistant-card error body format

`_formatErrorBody` renders a three-part card body when `error_info` is present:

```
**Error:** {error type label}

{message}

*(model: {model}, session: {session_id})*
```

| `error_type` | Label |
|---|---|
| `context_window_exceeded` | `Context window exceeded` |
| `rate_limit` | `Rate limit exceeded` |
| `authentication` | `Authentication failed` |
| `not_found` | `Model not found` |
| `cli_not_found` | `Engine binary not found` |
| `cli_exited` | `Engine process exited` |
| `api_connection` | `Connection failed` |
| `service_unavailable` | `Service unavailable` |
| `timeout` | `Request timed out` |
| `budget_exceeded` | `Session budget exceeded` |
| (anything else) | `Engine error` |

The metadata line carries `session_id` where the native engine carried `provider`. There is one provider
now and naming it says nothing; the session ID is what a user needs to resume, to find the transcript on
disk, or to quote in a bug report. It is suppressed when both `model` and `session_id` are null.

`bad_request` is gone from both tables: it meant a malformed request body, and we no longer construct
request bodies.

## Numeric constants

### Scroll behavior

| Constant | Value | Purpose |
|---|---|---|
| Scroll-up disengage threshold | 30px | Minimum upward scroll distance during active streaming to disengage auto-scroll |
| IntersectionObserver re-engage margin | 0px (at-bottom) | Observer fires when sentinel becomes fully visible; re-engages auto-scroll |
| Double-rAF scroll settle | 2 animation frames | After DOM updates, wait two frames before setting scrollTop so layout has committed |
| Streaming chunk coalescing | 1 animation frame | rAF callback drains the pending block map; rapid chunks across any number of blocks collapse to one re-render |
| Code block scroll preservation | Pre-update snapshot + post-update restore | `scrollLeft` of every `<pre>` captured by index before content replace, restored after |

Coalescing now drains a **map of pending blocks** rather than a single latest chunk. A turn with
interleaved text, thinking, and three parallel tool calls produces chunks for several blocks between
frames, and keeping only the newest would starve every block but one.

### Content-visibility thresholds

| Constant | Value |
|---|---|
| Forced-render window | Last 15 messages |
| Intrinsic size hint (user card) | 80px |
| Intrinsic size hint (assistant card) | 200px |
| Intrinsic size hint (tool card, collapsed) | 44px |
| CSS property (off-screen) | `content-visibility: auto` with `contain: layout style paint` |
| CSS property (force-visible) | `content-visibility: visible` with `contain: none` |

Last 15 messages force full render to keep scroll heights accurate near the bottom where the user is most
likely scrolling. Tool cards get their own hint because an agentic turn produces many of them and a
200px guess per card compounds into visibly wrong scroll geometry.

### Tool card limits

| Constant | Value | Purpose |
|---|---|---|
| Result preview lines (collapsed) | 12 | Lines of tool output shown before the expander |
| Command display cap | 4 000 chars | Matches the permission dialog's cap (`specs-reference/3-engine/permissions.md` § Numeric constants) so the same command renders identically in both surfaces |
| Diff ceiling | 2 MiB | Above this a card shows stats only; same ceiling as the permission dialog |
| Todo list identity | Latest `TodoWrite` per turn wins | Earlier lists in the same turn are replaced, not stacked |

### Input history

| Constant | Value |
|---|---|
| Maximum entries | 100 |
| Duplicate handling | Move to end of list rather than create a second entry |
| Visible batch (no filter) | 20 newest entries rendered; remainder reachable via filter |
| Entries scroll max-height | 40vh (list scrolls independently of filter input and hint strip) |
| Overlay max-height | 50vh |
| Row min-height | 1.75rem (prevents flex squashing when many entries fit the cap) |
| Long-entry disclosure | Native `title` attribute on each entry — full text (including newlines) shown on hover |

### Image limits

See `specs5/4-features/images.md`. Pinned here because they affect chat panel paste handling:

| Constant | Value |
|---|---|
| Max size per image | 5 MB |
| Max images per message | 5 |
| Accepted MIME types | `image/png`, `image/jpeg`, `image/gif`, `image/webp` |
| Encoding | base64 data URI |

### Two-level diff highlighting

Unchanged algorithm, new consumers: Edit/Write tool cards and the permission dialog's `write` body,
where it previously served `🟧🟧🟧 EDIT` blocks parsed out of the response text.

| Constant | Value |
|---|---|
| Diff algorithm | Myers line diff (via `diff` npm package, `diffLines`) |
| Character diff algorithm | Word-level diff (`diffWords`) |
| Pairing rule | Adjacent `remove` runs followed by `add` runs of the same length are paired 1:1 for character-level diff |

### Post-turn timing

| Constant | Value |
|---|---|
| Delay after `streamComplete` before `postResponseComplete` | 500ms |
| `compactionEvent` delivery retry (max attempts) | 3 |
| `compactionEvent` delivery retry delay | 1 second |

The 500ms delay survives, but what happens in it changed: the native engine ran a compaction check, and
this one gathers `context_usage` and `files_modified` for the footer, HUD, and Context tab. Compaction is
the engine's, announced via `compactionEvent` stage `compact_boundary` whenever the engine decides —
which can be mid-turn, not only after one.

### Compaction event stage routing

The `compactionEvent` channel no longer carries URL progress (URL fetching is deleted). Stages:

| Stage | Feedback type | Action |
|---|---|---|
| `compact_boundary` | System-event message + toast | Insert a compaction marker message; refresh the Context tab |
| `doc_index` | Not handled by chat panel | Intercepted by app shell → doc-index progress overlay |
| `doc_enrichment_queued` | Not handled by chat panel | Routed to header progress bar |
| `doc_enrichment_file_done` | Not handled by chat panel | Updates header progress bar |
| `doc_enrichment_complete` | Not handled by chat panel | Dismisses header progress bar |
| `doc_enrichment_failed` | Debug log | Logged for diagnostic; no user-visible notification |

`compacting` / `compacted` / `compaction_error` are gone. Compaction is not a request/response we drive,
so there is no "in progress" state to show and no error path of ours to report. `compacted` replaced the
whole message list from our own compactor's output; `compact_boundary` inserts a marker and leaves the
transcript alone.

The handler accepts events for both the current streaming request ID and the most recently completed one,
since `compact_boundary` can arrive just after `streamComplete`.

## Schemas

### localStorage keys

| Key | Type | Purpose |
|---|---|---|
| `ac-dc-snippet-drawer` | `"true"` / `"false"` | Snippet drawer open/closed state |
| `ac-dc-search-ignore-case` | `"true"` / `"false"` | Search toggle: ignore case (default `"true"`) |
| `ac-dc-search-regex` | `"true"` / `"false"` | Search toggle: regex mode (default `"false"`) |
| `ac-dc-search-whole-word` | `"true"` / `"false"` | Search toggle: whole word (default `"false"`) |
| `ac-dc.chat.draft` | string | In-progress textarea content for the main tab. Written on every input event; removed on send. Global rather than per-repo — drafts are short-lived and the chat panel does not currently receive `repoName`. Restored at `connectedCallback` time when `_input` is empty so an existing in-memory draft is not clobbered. Subagent tabs have no input, so nothing else needs a draft slot |
| `ac-dc-preset` | string | Selected prompt preset name. **Per-client and never sent to the server** — a preset is a local text template, not an engine state (`specs5/plan/decisions.md#cc-12--modes-become-prompt-presets-not-engine-states`) |
| `ac-dc-thinking-collapsed` | `"true"` / `"false"` | Whether thinking regions start collapsed (default `"true"`) |
| `ac-dc-tool-cards-collapsed` | `"true"` / `"false"` | Whether tool cards start collapsed (default `"true"`) |

Input history is NOT persisted — session-scoped only.

There is no `ac-dc-mode` key. Mode was server state broadcast as `modeChanged`; a preset is a browser-local
string, so two windows on the same repo can hold different presets without contradiction.

### System event message content templates

Operational events rendered as `role: "user"` + `system_event: true` messages:

**Commit:**
```
**Committed** `{sha}`

```
{commit_message}
```
```

**Reset to HEAD:**
```
**Reset to HEAD** — all uncommitted changes have been discarded.
```

**Rewind:**
```
**Rewound files** to the state before this message. {n} file(s) restored.
```

**Compaction boundary:**
```
**Context compacted** — {pre_tokens} → {post_tokens} tokens.
```

**Unsupported slash command:**
```
`/{name}` is not supported here. {reason}
```

**Session resumed:**
```
**Resumed** session `{session_id}` — {n} message(s) restored.
```

The mode-switch template (`Switched to {mode} mode.`) is deleted: presets are client-local and produce no
server event, so there is nothing to record in a transcript that other clients read.

The unsupported-slash-command note is rendered locally and **never appended to the transcript sent to the
model** — the text was never a message, and recording it would make the agent believe the user said
something they did not.

Canonical template set: `specs-reference/3-engine/history.md` § Mirrored store record schema.

### Cross-component flag

| Flag | Owner | Purpose |
|---|---|---|
| `_suppressNextPaste` | Chat panel instance | Set to `true` by the files-tab's middle-click path insertion BEFORE calling `chatPanel.focus()`. Consumed (cleared + `preventDefault()` called) on the very next `paste` event. Ensures the browser's selection-buffer paste from middle-click doesn't duplicate the inserted path |

### Tool card object

The one card object per `tool_use_id`, built browser-side by folding three sources:

```
{
  tool_use_id: string,
  agent_id: string | null,     // routes the card to a scope
  name: string,                // "Edit", "Bash", "mcp__ac-dc__symbol_search"
  server: string | null,       // MCP server chip, parsed from an mcp__ name
  input: object,               // from the assistant message's tool_use block
  gated: boolean,              // set when a permissionRequest carried this id
  decision: string | null,     // set from permissionResolved
  status: "running" | "ok" | "error" | "unknown",
  is_error: boolean,           // from the tool_result block's flag, never sniffed from content
  result: string | object | null,
  duration_ms: number | null
}
```

`status` derives from `is_error` — the **flag on the result block**, not a substring search for "error"
in the output. A `Grep` that legitimately finds the word `error` in the codebase must not render as a
failed tool call.

`unknown` is the status of a card whose turn ended with no matching `tool_result`, which happens when the
turn was interrupted mid-call.

Cards are keyed by `tool_use_id` and are idempotent: a repeated block for a known id updates in place.

## Dependency quirks

### `marked` library — two separate instances

The chat panel uses a dedicated `Marked` instance (`markedChat`) with custom renderers. A completely
independent instance (`markedSourceMap`) is used by the diff viewer's markdown preview for source-line
tracking. They share KaTeX math rendering via a shared extension but do NOT share renderer overrides.

### Scroll listener attachment for streaming

The IntersectionObserver alone is insufficient for scroll-up detection during active streaming — content
reflows can briefly push the sentinel out of view, which would falsely disengage auto-scroll. A separate
passive scroll listener tracks `_lastScrollTop` and only disengages when the user scrolls UPWARD by more
than 30px. The observer only re-engages; it never disengages during active streaming.

Agentic turns make this worse, not better: a tool card expanding on arrival is a reflow the user did not
cause, and treating it as a scroll-up would strand them mid-turn.

### Shadow-DOM textarea undo

Native `document.execCommand('undo')` is broken inside shadow-DOM textareas when the component
programmatically sets `value`. The chat panel's keydown handler intercepts Ctrl+Z and Ctrl+Shift+Z /
Ctrl+Y and explicitly calls `execCommand('undo')` / `execCommand('redo')` to work around this.

### Global shortcuts are inert while the permission dialog is open

Every keybinding in this panel — the Escape chain, Ctrl+Enter send, the history overlay, the @-filter —
is suppressed for the dialog's lifetime, including its settling interval. The dialog traps focus, and a
shortcut that leaked through could send a message or dismiss an overlay the user cannot see. See
`specs5/5-webapp/permission-dialog.md` § Anti-Click-Through.

### A tool result can precede its tool_use block

Block ordering within a turn is arrival ordering, and the SDK emits a `tool_result` for a fast tool
before the assistant message carrying the corresponding `tool_use` has finished streaming. The card
folder must therefore accept a result for an unknown `tool_use_id` and hold it until the input arrives,
rather than dropping it or creating a card with no name.

## Cross-references

- Behavioral specification (message display, streaming, tool cards, input area, search integration): `specs5/5-webapp/chat.md`
- Request ID format, block identity, stream multiplexing, and every server → browser payload shape: `specs-reference/3-engine/session.md`
- Permission payloads, tool classification, numeric caps: `specs-reference/3-engine/permissions.md`
- System event message templates and mirrored record schema: `specs-reference/3-engine/history.md`
- Permission dialog UI: `specs5/5-webapp/permission-dialog.md`
- Subagent tabs and status LEDs: `specs5/5-webapp/subagent-browser.md`
- Image persistence (paste format, storage): `specs5/4-features/images.md`
