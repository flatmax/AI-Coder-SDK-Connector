# Shell

The root component of the webapp. Owns the WebSocket connection, routes server-push events to child components, hosts the dialog and viewer background, manages global keyboard shortcuts, and orchestrates startup and reconnection. All child components receive RPC access through a shared singleton rather than holding their own WebSocket.

## Role

- Single WebSocket client for the whole webapp
- Publishes a shared RPC proxy that child components consume via a mixin
- Hosts the draggable dialog (foreground) and the viewer background (full viewport)
- Hosts the permission dialog, at viewport scope, above every other surface
- Routes server-push events to child components via window-level custom events
- Manages startup overlay and reconnection UI

## Connection Management

- Extract WebSocket port from URL query parameter
- Build WebSocket URI from the current page hostname (ensures remote collaborators connect to the LAN IP they loaded the page from, not loopback)
- Connect on mount; reconnect with exponential backoff on disconnect (1s, 2s, 4s, 8s, capped at 15s)
- First-connect shows the startup overlay; subsequent reconnects show only a transient "Reconnected" toast

## Shared RPC Publishing

- On setup-done, publish the call proxy to a shared singleton
- Child components using the RPC mixin subscribe and receive ready notifications
- Some components defer their first RPC call to the next microtask so sibling components finish receiving the proxy before requests fire

## Server Callbacks

Methods the server calls on the client (registered at connection time):

- Turn events — chunk, thinking chunk, tool card, tool result, subagent event, stream complete, post-response complete
- Permissions — permission request, permission resolved, permission mode changed
- Engine state — compaction event, rate limit, hook event, engine health
- State sync — files changed, session changed, user message, commit result
- Startup — progress (with special filtering; see below)
- Navigation — navigate file (carries a flag to prevent echo re-broadcast)
- Collaboration — admission request, admission result, client joined, client left, role changed
- Doc convert — progress

`modeChanged` is gone; nothing replaced it, because there are no modes. `permissionModeChanged` is a
different thing that happens to sound similar — it carries the permission posture, and it is the one
broadcast the shell must never drop, since a client showing the wrong posture is a client showing the
user the wrong authority.

All dispatch to window-level custom events that the relevant child components listen for.

## Startup Overlay

- Full-screen overlay driven by startup-progress events
- Shows the AIC⚡DC brand, a status message, and a progress bar
- Message and percent update per stage (config load, symbol index init, engine connect, session restore, doc index, ready)
- Fades out shortly after the ready signal

### Doc Index Stage Filtering

- Progress stage indicating doc-index work is intercepted and routed to the dialog header progress bar instead of the startup overlay
- Only in-progress updates (percent below 100) are forwarded; completion arrives via the enrichment-complete event
- Prevents the background doc index build from re-showing or stalling the startup overlay

### First Connect vs Reconnect

- First connect — show startup overlay, drive it from progress events
- Reconnect — skip overlay, show success toast, re-fetch state, re-subscribe to events

## State Restoration Cascade

- On setup-done, fetch a full current-state snapshot via a single RPC call
- Dispatch a state-loaded event with the full state as detail
- Browser tab title updated from the repo name in state (no prefix, no branding)
- The snapshot's `repo_root` — the repo's absolute path — is published for path normalisation (see [Viewer Background](#viewer-background)). It is the only absolute path the browser is given, and it is given it so it can stop sending them back. Guarded rather than defaulted: a snapshot without it leaves paths untouched instead of measuring them against an empty root, and cannot un-set a root an earlier snapshot established
- **It is not kept on the shell.** It goes to the module that holds the conversion rule, for the reason the call proxy is a singleton ([Shared RPC Publishing](#shared-rpc-publishing)): the renderers that need it take a path and no host, so the alternative is threading a string down to every chip. The publisher is the state-snapshot handler and there is no second one — one repo, one answer to "where is the repo"
- Files tab restores messages, hinted files, active-stream blocks, permission posture, and any pending permission requests
- File picker sync deferred so the picker has loaded its tree before selection is applied
- Chat panel detects bulk message load and triggers scroll-to-bottom

## File and Viewport Persistence

- Last-opened file path and viewport state persisted to localStorage
- Keys are repo-scoped so opening a different repo never restores the wrong file
- Legacy bare keys migrated to scoped keys on first recognition of the repo name
- File path saved on every navigate-file event
- Viewport state (scroll position, cursor, and any view-mode toggles) saved on `beforeunload`, before navigating away from the current file, and whenever a viewer reports a new active file via `active-file-changed`. The last case captures the `type` discriminator (svg vs diff) at the moment the viewer is ready, so a reload immediately after opening a file always has a correct viewer-routing record — without it, the stored viewport would still describe the previous file and restore would short-circuit on path mismatch
- View-mode toggles covered:
  - Markdown files — whether the preview pane was open and the preview's scroll position
  - TeX files — whether the preview pane was open and the preview's scroll position
  - SVG files — the current viewBox (pan/zoom), presentation-mode flag, and which viewer was active (visual SVG vs. Monaco text diff via `toggle-svg-mode`). Persisting the active-viewer choice means a user who switched to text diff for precise editing, then reloaded, returns to the text diff rather than having to re-toggle every session.
- Not persisted: editor find-widget state, focused side of the diff editor. Adding either is an additive `aic-last-viewport` schema change.

### Restore Flow

- On startup, after state-loaded completes, defer file reopen until the startup overlay dismisses (prevents file-fetch RPC calls from blocking the server during heavy init)
- On reconnect (init already complete), reopen immediately
- After the reopen, restore viewport state once the viewer is ready
- Preview toggles restore before scroll — the user's last view (raw editor vs preview) is the one they see, and the preview's own scroll position is restored against the preview pane rather than the editor
- For SVG files: if the stored `type` is `svg`, route through the SVG viewer and apply presentation-mode flag before writing the viewBox — presentation mode changes the right pane's width (no left pane), and the viewBox rectangle is only meaningful against its actual container. If the stored `type` is `diff` for an `.svg` path, the user toggled to text diff; restore via the diff viewer and dispatch `toggle-svg-mode` with target `diff` after navigate so the visibility flip lands on the diff viewer. The visual ↔ text toggle buttons remain available; persistence only affects the initial restore.
- A timeout cancels restoration if the file never opens (e.g., deleted)

## Permission Dialog Hosting

The permission dialog is a top-level element in the shell's shadow DOM, sibling of the dialog container
and the viewer background, with the highest z-index in the application — above the dialog panel, above
every progress overlay, above the startup overlay, above the toast layer.

- Mounted for the shell's whole lifetime, not created on demand. A request that arrives while the browser is mid-reconnect must find a listener already there
- Fed by the permission-request and permission-resolved callbacks, plus the `pending_permissions` list in the state snapshot
- Its Escape binding is registered ahead of every other Escape handler in the application
- Non-localhost clients mount the same component; it renders without decision controls

See [permission-dialog.md](permission-dialog.md). It is hosted here rather than inside the dialog panel
because the panel can be minimized, docked, dragged mostly off-screen, or showing Settings, and a
permission request has stalled the turn.

## Viewer Background

- Background layer hosts the diff viewer and SVG viewer as siblings, filling the viewport to the right of the docked dialog
- Only one is visible at a time — CSS class toggle with a short opacity transition
- Routing by file extension determines which viewer receives each navigate-file event
- Both viewers keep independent tab state; switching between file types just toggles the layer
- **Every `navigate-file` path is normalised against `repo_root` before anything else happens.** Claude Code's file tools take absolute paths, so a tool card's file chip, a turn footer's "files modified" list and the context tab all carry absolute paths, while every `Repo` method takes a repo-relative one and rejects an absolute path outright (it resolves nothing, because resolving would be a way around the containment check). One normalisation here rather than one per dispatcher, and it covers the two side effects as well: the path that gets persisted as last-open and registered with the navigation grid is the relative one, so a bad path cannot survive a reload
- A path outside the repo root is passed through unchanged rather than rewritten. It has no repo-relative name, and the backend refusing it is the correct outcome — a `../..` walk would ask for a different file

#### The Same Rule Names Files On Screen

**A file inside the repo is displayed relative to the root; one outside it keeps its absolute path,
because that is the only name it has here.** The engine's path stays on the element's tooltip and
accessible name. This is the house rule for every view in the app that shows a file — the Context tab's
memory-file table, the permission dialog, tool-card footers, the "Files Referenced" chips — and it is the
same rule as the navigation conversion above, applied to a label rather than to a request. So it is the
*same function*: there is no second helper for display, and no way for the two to disagree about where a
file is.

Two consequences worth stating, because both look like bugs from one side:

- **Shortening a label must never shorten what the click sends.** The normalisation at the
  `navigate-file` choke point is the one converter; a chip dispatches the path it was given. A display
  concern that became the navigation contract would put the conversion in two places, which is what
  having one choke point was for
- **Where the backend already relativises, it stays relativised.** The permission dialog's paths and the
  context tab's `relPath` are computed server-side and arrive short. Converting an already-relative path
  is a no-op, so those views need no change and get none — the rule holds without a second code path

### Telling the Server What Is Open

The shell is the one place both viewers' `active-file-changed` events converge, so it is where the server
is told what the user is looking at — `ClaudeCodeService.set_viewer_state`, which feeds the turn framing
and the `ui_state` tool ([`../3-engine/session.md` § Turn framing](../3-engine/session.md#turn-framing)).
It is the **only** writer: `chat_streaming`'s `viewer` argument stays null and the service falls back to
the last push, so the field cannot be told two different things. The alternative — answering at send time
from the chat panel — knows only about turns that start in this browser, which is the case the `ui_state`
tool exists for.

- `active-file-changed`, not `navigate-file`: it reports what a viewer *has* open rather than what it was
  asked to open. A fetch can fail, and routing diverges (an SVG with a scroll hint goes to the diff
  viewer), so the request and the result are not the same fact
- Repeats for one path are deduped against the last value pushed. The event is deliberately re-emitted on
  a same-file `openFile` so the shell can re-run its visibility routing, so without the dedupe every
  redundant open would cost a round trip to say nothing
- A `null` from the viewer that is *not* visible is ignored. Both viewers emit on their own file changing,
  and the hidden one emptying does not mean nothing is on screen
- The SVG viewer's synthesised `virtual://svg-compare/…` path is reported as nothing open. It is on
  screen, but it is not a file anything can read, and leaving the previous path standing would point the
  agent at a file that is no longer shown
- A reconnect re-pushes. Server-side viewer state is in memory and a reconnect usually means a restarted
  process, so the file still in front of the user is one the new process has never heard of
- The **selection range is never sent** — only the file. See [`../next.md`](../next.md) § C7

### Reserved Strip

The background layer's left edge is inset by the width the docked dialog occupies, published as the
`--viewer-inset-left` custom property on the layer.

Without the inset the layer spanned the whole viewport and lost its left half. Both viewers divide their
width in two — Monaco's original / modified sides, the SVG viewer's Original / Modified panes — so the
"before" side of every side-by-side view landed exactly underneath the opaque dialog. It was rendered,
correctly sized, and holding the right content; it was simply invisible. Presentation mode had the mirror
version of the same bug: a full-width right pane spilled its left half under the dialog.

- The inset is **measured** from the dialog's own rect, not recomputed from the stored docked width. The measured right edge already accounts for the CSS `min-width`, the 1px border, and a resize drag that has written an inline width without yet committing it to state — three ways a recomputation drifts
- Zero whenever the dialog isn't occluding a full-height strip at the left edge: undocked (it floats over the layer, and insetting for it would make viewer content jump on every drag frame) or minimized. Minimizing therefore hands the whole viewport to the viewer, which is what a user reaching for presentation mode wants
- A dialog whose rect has left the viewport edge is treated as floating even if the `.floating` class hasn't landed yet, so a mid-drag frame can't inset the layer by a strip the dialog no longer covers
- Synced on first render (the docked width restored from localStorage applies on that same render), on any state change that can move the dialog's right edge, and ahead of every viewer relayout. Renders that can't move that edge — toasts, tab switches, stream flags — don't re-measure, because each sync forces a layout read
- The sync reports whether the value changed, and a relayout is scheduled only when it did. The viewers' relayout refits viewBoxes and Monaco layout, which is wasted work at an unchanged width

## Dialog Container

The dialog is a draggable, resizable foreground panel hosting the tab bar and tab bodies. It sits above the viewer background layer (z-index 10 vs 0–1) so it always renders on top regardless of what the viewer's internal positioning does. Left-docked by default; first drag or first bottom/corner resize undocks it into an explicit rectangle.

### Layout

The dialog has no header bar. The chat panel's tab strip sits directly at the top of the dialog body; messages, input, and a compact LED strip follow.

**Tab strip** (top of chat panel): Main + one per subagent in the current turn. Text labels with horizontal scroll overflow and a ⋯ direct-jump menu when the strip exceeds available width. Always rendered, even with only Main present. The Main tab carries a 📊 Context icon (visible on hover/active/focus) that opens the Context overlay. Subagent tabs carry a live indicator and, while live, a ⏹ stop icon. The strip is the dialog **drag handle** — pointerdown on its empty background or the gap between buttons begins a drag; pointerdown on any tab button, Context icon, stop icon, or overflow button skips drag via the `closest('button')` guard.

The 📊 icon is on Main only, and no tab carries a ✕. Context is session-scoped now — there is one
snapshot to show, not one per conversation — and a subagent tab cannot be closed because closing it never
meant "hide a view", it meant "kill a scope AIC⚡DC owned". See
[subagent-browser.md](subagent-browser.md).

**LED strip** (below the textarea, above the context-capacity bar): one small dot per tab, centered horizontally. Each dot reflects that tab's live / outcome state (cyan flashing while working, green for a clean finish, red for failure, amber for stopped or unknown). Clicking a dot activates the corresponding tab. The strip takes minimal vertical space — no background, no border, just the dots floating below the input. Tooltip on hover gives the description, status, and state-specific diagnostic per [subagent-browser.md](subagent-browser.md#status-leds).

**Doc Convert**: lives in the file picker's top toolbar (a 📄 button rendered only when the backend reports markitdown is installed). Clicking dispatches `request-dialog-tab` with `{tab: 'doc-convert'}`. Same toolbar pattern as Settings — both buttons replaced earlier dialog-header / FAB iterations and now live in the picker so the dialog has no header at all.

**Minimize button**: ▾ button rendered at the right edge of each dialog tab's toolbar — the chat panel's tab strip (after the overflow ⋯ menu), and each overlay tab's toolbar/nav-bar (Context, Settings, Convert, SDK Surface). Right-edge placement is consistent across all five tabs so the affordance lives in the same spatial location regardless of which tab is active.

All five dispatch `request-dialog-minimize` which the shell catches and routes through `_toggleMinimize`. Each tab carries its own minimize button rather than relying on a single top-right FAB (an earlier FAB iteration shadowed the Context tab's refresh button) — overlay tabs are sibling tab-panels inside `dialog-body`, so the chat panel's tab strip is unreachable when an overlay is active.

**Expand FAB**: ▴ button at the dialog's top-right, rendered ONLY when the dialog is minimized. The minimized state hides the dialog body, so all in-tab minimize buttons are unreachable; the expand FAB takes over as the only way to restore the dialog.

**Settings**: lives in the file picker's top toolbar (a ⚙️ button between the sort split-button and the Doc Convert / git split-button row). Clicking dispatches `request-dialog-tab` with `{tab: 'settings'}`.

**Drag detection**: the dialog as a whole listens for pointerdown. Drag is initiated only when the pointer's `composedPath()` walks through an element with `data-drag-handle="true"` AND no button. Today only the tab strip carries that attribute. This means:

- Pointerdown on a tab button, the per-tab 📊 Context icon, the per-tab ✕ close icon, or the overflow button — `closest('button')` matches, no drag.
- Pointerdown on the tab strip's background or the gap between buttons — drag begins.
- Pointerdown anywhere else in the dialog (LEDs, message area, picker, input) — no drag, normal click handling.

**Context-capacity bar**: thin 4px strip at the very bottom of the dialog (above the resize handles), rendered once a context snapshot has been seen. The fill width tracks `totalTokens / maxTokens` from the engine's own accounting, with a 1px marker at the auto-compact threshold when auto-compact is enabled. Colour follows the same tri-state convention as the Context tab's gauge: green ≤75%, amber 75–90%, red >90%. Hidden in minimized mode along with the body and reconnect banner. Tooltip on hover gives exact token counts, percent, and the distance to the threshold.

Fed by the `context_usage` payload on post-response-complete and by the Context tab's own fetches — never
by a bar-specific RPC. There is no `get_history_status` any more, and no local model of a compaction
threshold to compare against: the threshold is the engine's, reported alongside the totals. A user who
sees the fill cross the marker knows compaction is imminent, which is what the old bar was trying to say
and could only approximate.

**Doc index progress overlay**: an `aic-doc-index-progress` component rendered inside the dialog body. Owns its own visibility lifecycle keyed on the doc-index stages the shell intercepts from the startup-progress channel and re-dispatches as `doc-index-progress` window events. It also carries the doc-enrichment stages, which arrive on the compaction-event channel during a session rather than at startup. Exists so background indexing surfaces without re-showing the startup overlay or stalling chat interaction.

**Compaction progress indicator**: an `aic-compaction-progress` component rendered inline in the dialog
directly above the context-capacity bar, and the pairing is the point — the bar says how close a
compaction is, the indicator says when one is happening. Appears on the `pre_compact` `systemEvent` our
own `PreCompact` hook broadcasts, holds a spinner, a label naming the trigger, an indeterminate sweeping
bar and an elapsed-seconds counter for as long as the pause lasts, then reports the boundary's token
counts — phrased by the same `compactionSummary` builder the transcript divider uses — and fades. Unlike
the body and the capacity bar it is **not** hidden in minimized mode: a user who collapsed the dialog and
is waiting on a turn has no other way to tell a compaction from a hang.

**Indeterminate by construction.** The engine reports the start (the hook) and the end
(`compact_boundary`) and nothing in between, so there is no percentage to be had and the component never
implies one — no `aria-valuenow`, no modelled fill. The earlier viewport-scope overlay of this name was
deleted with the native engine on the reasoning that a progress bar over someone else's compaction would
be an animation rather than a measurement. The reasoning holds; the conclusion did not. The pause is real
and the only thing announcing it was a 3-second toast, which expires long before the condition it
describes — so the honest half of a progress bar came back, and the toast went (see
[chat.md § Engine Event Routing](chat.md#engine-event-routing)).

**It survives a page reload, because a broadcast is not a record.** Every signal driving the indicator
is live — it says what the engine is doing *now*, to whoever happens to be connected — so a refresh
during the pause used to reconnect into a session that looked idle while the engine was still
summarising. Tens of seconds of apparently hung UI: the exact failure the indicator exists to prevent,
reintroduced by the one action a user watching a silent screen is most likely to take. Same class as the
compaction divider phase 2 shipped client-side only, and the same fix. `get_current_state` now carries a
`compaction` key — `null`, or `{elapsed_seconds}` — and `state-loaded` restores the indicator from it.

Three things about that key are decisions rather than details:

- **The server computes the elapsed seconds; it does not send a start timestamp.** A timestamp would
  make the browser difference two clocks, and a collaborating client can be on another machine.
- **It is set from the engine's status frame, never from the `PreCompact` hook**, so a restored
  indicator can never be a speculative background precompute — which is why it restores as *confirmed*
  and gets the long ceiling rather than the short unconfirmed one.
- **The ceiling budgets the whole compaction, not this component's view of it.** A restore arriving 170
  seconds in gets the remaining 10, not a fresh 180; otherwise a compaction that died before the refresh
  would sit there for three more minutes claiming to work.

The trigger is deliberately absent from the restored state: it belongs to the hook, not the frame. A
restored indicator says how long, not why.

A boundary with no start ahead of it is **ignored**, not flashed: microcompaction can report one without
the hook ever firing, and by the time it lands there is no pause left to explain — the divider records it.
The reverse case is bounded rather than trusted: a spinner is a claim only `compact_boundary` can retract,
so after 3 minutes with no boundary the component says it lost track and gets out of the way, because a
spinner that runs forever is worse than the toast it replaced.

**Read-aloud transport overlay**: an `aic-speech-controls` component rendered at viewport scope. Unlike the progress overlays, it is **draggable** and remembers its position across sessions. It listens for the text-to-speech player's state-change window event and is visible only while a message is being read aloud, offering play/pause, a speed slider, and a per-sentence position bar. It holds no playback state — it is a remote control for the shared synthesis player and reflects its state. See [speech.md § Floating Transport](speech.md#floating-transport-controls-overlay) for the full specification.

Returning to chat from an overlay tab: each overlay tab's body carries a back-arrow (`← Chat`) at top-left. Clicking it dispatches `request-dialog-tab` with `{tab: 'files'}` — legacy storage key, retained for migration safety. The shell's `_switchTab` handles the rest.

**`request-dialog-tab` may also name a `section`**, for a tab that holds a segmented control — a routed slash command uses this to land on the part of the tab that answers it (see [chat.md § Slash Commands](chat.md#slash-commands)). The shell switches the tab, then offers the section to whatever view is now active over a duck-typed `showSection(id)`, the same shape as the existing `onTabVisible()` hook: a view that has no such method is left alone, and no event names which tabs have sections. The shell does not know what a section *is*, and should not — every question about which ids are valid, whether an id is remembered, and what an unknown one means belongs to the view that owns the control.

**The back-arrow is load-bearing, and the dialog gives it no backup.** There is no rendered tab bar — the
strip at the top of the dialog body belongs to the *chat panel* and is unreachable while an overlay is
active — so an overlay tab without its own `← Chat` is a dead end escapable only by knowing Alt+1. The SDK
Surface tab shipped that way and was reported as such within the hour. Two rules follow: the arrow is part
of a toolbar rendered **outside** any state branch, because a failed fetch or an empty panel is exactly
when a reader wants out; and a new overlay tab is not finished when its content renders, it is finished
when it can be left.

**Layout history note**: the journey here started from a draft that kept a dialog header and tried to project the chat tab strip up into it via absolute positioning — that failed due to shadow-DOM stacking-context constraints. A second iteration removed the header but kept a full-width LED row at the top with Context/minimize icons attached. The current layout is a third pass: the LED row collapses into a compact strip at the bottom of the chat panel, Context lives per-tab, and minimize joins Convert as a corner FAB. The tab strip absorbs the drag-handle role.

### Layout Modes

Two mutually-exclusive modes:

- **Docked** — top, left, bottom anchored to the viewport edges; width is a percentage (with an optional stored override in pixels). This is the default on first run and stays in effect until the user drags the header or bottom/corner-resizes.
- **Undocked** (floating) — all four edges set from a stored pixel rectangle. The CSS `bottom: 0` anchor is disabled; shadow gives visual separation from the viewer background. Produced by dragging the header past the drag threshold, or by resizing from the bottom / corner handle.

Minimize applies to both modes — collapses the dialog to the header row only, hiding the body, the reconnect banner (if visible), and the compaction capacity bar. Resize handles on the bottom and corner are also hidden when minimized since they'd be meaningless (no body to resize). Minimized state is preserved across reload in both modes.

### Resize Handles

Three handles — invisible hit zones at the edges that grow a subtle accent line on hover:

| Handle | Location | Axis | Behaviour |
|---|---|---|---|
| Right | Right edge, 8px hit zone extending 4px past the border | Horizontal | Adjusts width only. In docked mode, writes the new width to `aic-dc-dialog-width` and stays docked. In undocked mode, writes to the full undocked rectangle. |
| Bottom | Bottom edge, 8px hit zone | Vertical | Adjusts height only. **Always undocks** — the docked mode's height comes from `bottom: 0`, so expressing a smaller height requires an explicit rectangle. |
| Corner | Bottom-right, 14×14px hit zone | Both | Adjusts width and height simultaneously. Always undocks for the same reason. |

The right handle's behaviour is asymmetric: while docked, only `aic-dc-dialog-width` is persisted, leaving the undocked rectangle alone. This lets a user widen the docked dialog without committing to floating mode.

Mid-drag the `.dialog.resizing` class is active, suppressing the width transition so the pane tracks the pointer 1:1. The class is removed on pointerup.

### Minimum Dimensions

- Width: **300px**
- Height: **200px**

Below 300 wide the tab buttons wrap to a second row; below 200 tall the body collapses to an unusable slit. The resize handlers clamp against these floors at the JS level — CSS `min-width` / `min-height` aren't applied because flexbox interactions with the docked-mode percentage width cause occasional drift.

### Dragging

The tab strip is the drag handle — `cursor: grab` on the background, `cursor: grabbing` during an active drag. Buttons inside it (tab buttons, the Context icon, subagent stop icons, the overflow menu, minimize) override the cursor and don't initiate drags; the pointerdown handler skips when `event.target.closest('button')` matches.

**Drag threshold: 5px.** Below this, a header pointerdown + pointerup pair is treated as a click (no-op today, since minimize has its own button). Above the threshold, the `.dialog.dragging` class activates, the dialog undocks if still docked, and subsequent pointermove events track the pointer by applying the stored delta to the drag-start rectangle.

The threshold prevents accidental undocks from imprecise clicks — users clicking the header edge without meaning to drag shouldn't see the dialog jump into floating mode.

### Off-Screen Recovery

Both during drag and at restore time, the dialog is constrained so that **at least 100px remains visible on both the X and Y axes**. Specifically:

- During drag: the new left is clamped to `[100 - width, viewportWidth - 100]`, the new top to `[0, viewportHeight - 100]`. The left can go negative (part of the dialog hanging off the left edge) as long as 100px sticks out into the viewport; the top cannot go negative because the header must remain reachable as the drag handle.
- At restore: a stored position where fewer than 100px would be visible — typically after a monitor disconnect or resolution change that stranded the dialog off-screen — is discarded and the dialog reverts to docked mode. Valid-but-too-big rectangles are clamped to viewport dimensions rather than rejected.

The margin is a "findable handle" guarantee: however the user maimed their window, the dialog always has a visible edge they can grab to drag it back into view.

### Proportional Rescaling

On window resize, the dialog keeps the same approximate fraction of the viewport the user last chose — the user's intent was "half the window" or "this rectangle of screen real estate", not "exactly 600 pixels". Holding pixel values static across browser resizes makes the dialog drift away from its intended size.

Three cases:

- **Docked, default width** — The stylesheet's percentage rule (`width: 50%`) tracks the viewport automatically. No JS action needed. This is the state on first run, before the user ever drags the right edge.
- **Docked, user-resized width** — Once the user drags the right edge, an inline pixel width overrides the percentage rule. Rescale it by `newViewport / baselineViewport` on every resize so the fraction stays constant. Without this, a user who set half-width on a 1200px-wide window would see the dialog become a quarter of the viewport when the browser grows to 2400px.
- **Undocked** — Scale `width`, `height`, `left`, and `top` independently by the corresponding viewport-axis ratio. Left and top scale so a right-anchored or centred dialog stays pinned; width and height scale so the dialog keeps its fraction of the viewport on each axis.

In every case the result is clamped to the dialog's minimum width / height and to the visible-margin safety rule (at least 100px of the dialog must remain inside the viewport on both axes).

**Baseline viewport.** The scaling ratio needs a remembered "viewport at last commit" baseline for each state. Without one, every resize event would scale from the original stored pixel-literal and the dialog's fraction of the viewport would slowly drift.

The baseline is updated at three points:

- **User commit** — pointerup after a right-edge resize (docked width), drag (undocked position), or bottom/corner resize (undocked rectangle).
- **After each resize-driven rescale** — the just-captured viewport becomes the new baseline, so subsequent resize events chain correctly.
- **First render** — initialised to the current viewport, so the very first resize after a fresh load scales from "now" rather than from whatever viewport was active when the stored geometry was originally written.

**Throttling.** Resize handling is throttled to one call per animation frame. Rapid resize events (drag the window corner, laptop lid reopen) can fire dozens of times per frame; without throttling the reflow math produces visible jank. The viewer relayout uses a separate RAF handle from the dialog rescale so a window resize during a dialog-resize drag doesn't cancel the drag's pending viewer relayout.

### Persistence

Four localStorage keys, all repo-scoped implicitly via the URL-derived WebSocket port (the dialog state is frontend-only, but the user's chrome preferences are stable across repo switches):

| Key | Type | Purpose |
|---|---|---|
| `aic-dc-active-tab` | string | Last-selected tab — one of `files`, `context`, `settings`, `doc-convert`, `sdk-surface`. Unknown values fall back to `files`. A stored `search` value (from a pre-integrated-search-tab build) also falls back to `files`. |
| `aic-dc-minimized` | string `"true"` / `"false"` | Minimize state. |
| `aic-dc-dialog-width` | string (integer px) | Docked-mode width override. Absent until the user resizes the right edge while docked. Ignored while undocked. |
| `aic-dc-dialog-pos` | JSON `{left, top, width, height}` | Full undocked rectangle. Absent until the user drags the header past the drag threshold or resizes from the bottom / corner. |

Keys are read synchronously in the constructor (not in `connectedCallback`) so first paint doesn't flash the defaults before jumping to the stored values.

Width and position are independent — resizing the right edge while docked writes only `aic-dc-dialog-width`, leaving any stored undocked rectangle alone. This is deliberate: a user who occasionally floats the dialog shouldn't lose their preferred floating geometry just because they widened the docked view in between.

Malformed values (non-JSON, wrong shape, width below minimum, finite-number check fails) are treated as absent. Invalid keys don't propagate into the UI state.

## Preset and Permission Controls

The preset selector and the permission-mode indicator live in the chat panel's action bar — not the
dialog header. See [chat.md § Preset Selector](chat.md#preset-selector) and
[chat.md § Permission Mode Selector](chat.md#permission-mode-selector) for the full UI specification.

The shell's only stake in either is the permission-mode broadcast, which it routes like any other
server-push event, and the rule that the posture indicator is never hidden by a collapse or a layout
state. A dialog minimized to its tab strip still shows the posture; the indicator moves into the strip
rather than disappearing with the body.

The old cross-reference overlay toggle is gone with the modes it overlaid. Both indexes are always
available to the agent as tools, so there is nothing to switch.

## Global Keyboard Shortcuts

- Alt+1 returns to Chat (the default body)
- Alt+2 opens Context
- Alt+3 opens Settings
- Alt+4 opens Convert (when available; the keystroke is consumed but no-op when Convert is unavailable)
- Alt+5 opens SDK Surface — a maintenance view, reached in normal use from the Context tab's Debug
  section rather than from a keystroke ([`../plan/sdk-surface.md`](../plan/sdk-surface.md#the-probe))
- Alt+M toggles dialog minimize
- Ctrl+Shift+F activates file search in the chat panel, prefilling from the current selection

Alt+1 always returns to Chat regardless of which overlay is currently shown — same effect as clicking the back arrow. Alt+3 is fixed on Settings regardless of whether Convert is installed, so muscle memory survives stripped-down deployments.

Alt+5 is bound even though nothing in the chrome advertises it, for the same reason Alt+4 is consumed when
Convert is absent: a bound-but-obscure key is a stable escape, and an unbound digit that silently does
nothing is indistinguishable from a broken one. Alt+6 and above are unmapped and pass through.

Every shortcut is suppressed while the permission dialog is open. It is modal, its focus is trapped, and
Alt+2 opening the Context tab behind a pending `Bash` approval would be a distraction at the worst
possible moment.

### Ctrl+Shift+F Selection Capture

- The selection must be read synchronously as the very first operation in the keydown handler, before any asynchronous work
- Focus changes during tab switching would clear the selection if read later
- The captured string is passed as an explicit parameter down to the search activator — never re-read from `window.getSelection()` inside a later callback
- Multi-line selections are discarded (file search is single-line by design)
- Captured string is trimmed before use

## Window Resize Handling

- Window resize triggers two actions — proportional dialog rescaling and viewer relayout
- Both throttled to one call per animation frame
- Without throttling, rapid resize events cause feedback loops (layout shift → resize event → layout call → forced reflow → visible jank)
- Throttle handle cancelled on component unmount to prevent stale callbacks
- Viewer relayout is also scheduled on every dialog-resize pointermove frame, and on the frame an undock drag adds the `.floating` class — a dialog getting wider shrinks the strip left for the viewer, and undocking hands it back. Monaco caches scrollbar / minimap dimensions; the SVG viewer's editors run with `preserveAspectRatio="none"` and rely on explicit `fitContent()` calls. Without this hook, both viewers leave stale layout until the user clicks into them.
- Every relayout re-syncs the background's reserved strip first, then calls each viewer. The viewers measure their own containers, so the layer's new width has to be committed before they read it.
- Window-resize and dialog-resize relayouts use separate RAF handles so they don't cancel each other's pending frames.

## Toast System

Two independent toast layers:

- **Chat panel local toast** — rendered inside the chat panel, positioned near the input; used for chat-specific feedback (copy, commit results, turn errors, rate-limit warnings, permission timeouts)
- **App shell global toast** — rendered in the app shell at the bottom-left of the viewport (z-index above the dialog so toasts remain visible when the dialog is docked left), supports multiple simultaneous toasts with independent fade-out; used by components outside the chat panel.

Components dispatch toast events; the shell catches and renders them. Chat panel's local toast does not dispatch global events.

## Invariants

- Only the shell holds a WebSocket connection; child components use the shared RPC proxy
- First-connect always shows the startup overlay; reconnect never does
- The captured `window.getSelection()` at Ctrl+Shift+F is passed by parameter, never re-read downstream
- File and viewport state is restored after the ready signal on first connect, or immediately on reconnect — never before
- Window resize handlers run at most once per animation frame
- Browser tab title reflects the current repo name with no prefix, except while permission requests are pending, when it carries the pending marker and count
- The startup overlay is dismissed exactly once per connection lifecycle (first connect)
- The permission dialog is mounted for the shell's entire lifetime and renders above every other surface, including the startup overlay
- Global keyboard shortcuts are inert while the permission dialog is open
- The permission-mode indicator remains visible in every dialog layout state, including minimized
- The context-capacity bar is fed only by pushed or tab-initiated context snapshots; it never issues its own RPC
- The compaction indicator never displays a percentage or an `aria-valuenow`, and never stays active longer than its ceiling: the engine reports only the start and the end of a compaction, so anything between the two would be invented, and a spinner nothing retracts is worse than no notice at all
- One writer publishes the repo root, the state-snapshot handler, and one function answers "absolute engine path → the name to use" for both navigation and display
- No chip, row or label sends the path it displays. What is dispatched is the path as received; the shortening is a label