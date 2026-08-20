# File Picker

Tree view of repository files with git status and a context menu. Left panel of the Files tab. Drives file navigation (which file is open in the viewer), path insertion into the prompt, and per-path read denial.

**There is no checkbox column.** The picker had one for as long as the native engine existed, because a checked file's content was placed in the prompt and an unchecked file contributed only its index block — selection was the context contract. The agent reads files itself now, which left the checkbox meaning nothing but a hint about what the user was looking at, in a channel the user could write more precisely by naming the path in the prompt. So it was removed, along with the whole hint pipeline behind it. See [decisions § CC-21](../plan/decisions.md#cc-21).

Two things the checkbox carried survive, moved off it:

- **Deny-read** — shift+click on the row, or a context-menu item. It was the checkbox's third state and it was never a hint: it writes a real `Read` deny rule the CLI enforces. See [decisions § CC-14](../plan/decisions.md#cc-14).
- **Pointing at a file** — middle-click inserts the path into the prompt, shift+middle-click inserts `@path`, and both are context-menu items. This is the picker's primary verb now, so it is no longer a gesture and nothing else.

The tree, the git status, the sorting, the context menu, the resizer, and every navigation affordance are untouched by the conversion.
## Tree Rendering
### Root Node — Branch Badge
- Root row displays the repo name and a compact pill showing the current git branch
- Branch name prefixed with a branch icon
- Normal branch — muted style, rendered as a clickable button that opens the branch switcher popover (see below)
- Detached HEAD — orange-tinted style, short SHA instead of branch name; non-interactive (switching out of detached HEAD goes through the commit graph, not the pill)
- Long branch names truncated with ellipsis, full name in tooltip
- Fetched via the current-branch RPC on every tree reload — stays current after commits, checkouts, and review entry/exit
- Shift+click on the root row denies the agent `Read` on every file in the repository, and allows it again when everything is already denied — the same gesture a directory row answers, scoped to the whole tree. It is the **only** gesture the root row answers: a plain click does nothing (there is no repo-wide open, and the branch pill handles its own clicks)
- Root row denial state renders like a directory's: strikethrough and muted when every file is denied, a `✕` badge when only some are

### Branch Switcher
- Clicking the branch pill opens a popover listing every local and remote branch
- Pill disabled during review mode, active streaming, and in-flight commits — branch switching during any of these would invalidate in-progress state; tooltip adapts to explain why
- Picker dispatches a branch-menu-requested event when the pill is clicked; files tab fetches branches via the list-all-branches RPC and populates the menu via the picker's public populate method
- Menu shows a "Loading…" state until the branch list arrives
- Each row tooltip leads with the full branch name so truncated rows are recoverable on hover
- Current branch row disabled; clicking it is a no-op that closes the menu
- Selecting a branch dispatches a branch-switch-requested event with the branch name and remote flag
- Files tab performs a clean-tree probe before the switch — dirty tree produces a toast and aborts before the RPC (backend also enforces this; the frontend check avoids a round-trip and gives a clearer message)
- Switch calls the checkout-branch RPC; on success, a toast confirms the new branch and the file tree reloads; on failure, the error message from the RPC surfaces as a toast
- Popover position clamps to viewport; outside-click or Escape closes it
- Remote-ref selection creates a local tracking branch via backend DWIM (see [repository.md](../1-foundation/repository.md#branch-operations))
### Directory Nodes
- Expandable toggle; a plain click anywhere on the row expands or collapses
- Shift+click denies (or allows) the agent `Read` across the whole subtree — see [Deny-Read](#deny-read-row-gesture)
- Middle-click inserts the directory path into the prompt
### File Nodes
- Name click opens in the diff viewer (or SVG viewer for SVG files)
- Shift+click denies (or allows) the agent `Read` on the path; middle-click inserts the path into the prompt
- Line count badge in neutral color (no size thresholds)
- Git status badge — modified, staged, untracked, deleted
- Diff stats for changed files (additions and deletions) — rendered in a reserved gutter to the left of the name so names stay aligned across sibling rows regardless of whether a given row has diff stats
### Tooltip
- Every row displays a native browser tooltip on hover
- Format — full path and node name, then the diff counts when git has some, then the gestures the row answers
- The gesture list is load-bearing, not decoration: with the checkbox gone the row *is* the control surface, and the tooltip is the only place that says a middle-click inserts the path or a shift+click denies the read. The verbs differ by row type — a file's plain click opens, a directory's expands, and a directory's deny covers everything inside
- Binary and denied rows replace the gesture list with an explanation instead. What a user wants from those rows is to know why they look that way
- Root node falls back to repo name
## Toolbar Layout

Top toolbar carries (left to right): a sort split-button, ⚙️ Settings, 📄 Doc Convert (when available), and a git action split-button. Buttons are icon-only — text labels were dropped when the dialog header was removed and the picker toolbar absorbed Settings. Tooltips remain for discoverability.

The Settings button dispatches `request-dialog-tab` with `{tab: 'settings'}`. The Doc Convert button (rendered only when the backend reports markitdown is installed) dispatches the same event with `{tab: 'doc-convert'}`. These are the sole entry points to those tabs now that the dialog header is gone.

### Active-State Halo

Active icon-only buttons across the picker toolbar — and across the rest of the chat-panel action bar (see [chat.md](chat.md) and [search.md](search.md)) — carry a consistent visual treatment: accent-coloured background tint, a 1px ring in the same accent, and a soft outer halo. The halo is the load-bearing affordance — icon-only buttons live on a dark background where a tint shift alone is hard to read at a glance. Blue accent (#58a6ff) for primary-mode, sort, selection, and option-toggle states; amber (#d29922) for cross-reference and review-active states. The shared treatment teaches a single "this is glowing therefore active" rule across every icon-only control in the dialog.

## Sorting

Sort UI is a split-button: a primary button showing the active mode's glyph plus a direction arrow (↑ ascending / ↓ descending), and a chevron (▾) that opens a dropdown menu. The primary button always carries the active-state halo (see above) since it represents the currently-selected sort mode.

| Mode | Glyph | Behavior |
|---|---|---|
| Name | A | Alphabetical by filename (default) |
| Modified | 🕐 | Most recently modified first |
| Size | # | Largest line count first |

- Clicking the primary button toggles ascending/descending for the current mode
- Clicking the chevron opens a dropdown listing all three modes; the active row carries the same halo as the primary button. Selecting a mode either switches to it (resetting to ascending) or, if already active, toggles direction
- Outside-click and Escape close the dropdown
- Directories always sort alphabetically regardless of mode
- Sort mode and direction persisted to localStorage

## Git Actions

Git UI in the toolbar is a split-button: a primary commit button (💾 idle, ⏳ in-flight) and a chevron (▾) opening a dropdown menu.

The primary button calls the commit-all RPC. Disabled during review mode (read-only), in-flight commits, and active LLM streaming. Tooltip adapts per state. While a commit is in flight the button picks up the active-state halo and swaps its glyph to ⏳.

Dropdown menu items:

| Item | Glyph | Behavior |
|---|---|---|
| Copy diff | 📋 | Copies the working-tree diff (staged + unstaged) to the clipboard |
| Start code review… | 🔍 | Opens the review selector modal. Hidden when a review is already active |
| Reset to HEAD… | ⚠️ | Discards all uncommitted changes (with confirm dialog). Destructive styling — red text, hover deepens to brighter red. Visually separated from the items above by a divider |

Outside-click and Escape close the dropdown. Each menu item carries its own enabled/disabled gate (Reset is gated on streaming + commit state; Start code review additionally gates on already-in-review state). The chevron is reachable even when the primary commit button is disabled, since Copy diff is always safe.

The split-button dispatches a bubbling `git-action` window event carrying `{action: 'copy-diff' | 'commit' | 'reset'}`. The app shell catches and routes to the appropriate handler. Start code review dispatches `open-review-selector` instead, since it opens a modal rather than firing an RPC.

## Filtering
- Text filter with fuzzy matching against the full path, anchored to a single-row bar at the **bottom** of the picker (below the tree scroll region). The toolbar at the top holds sort, Settings, Doc Convert, and git actions — the filter input is deliberately separated so the always-on toolbar controls don't compete with the input for the user's gaze when scanning the file list
- All characters in the query must appear in the path in order (not necessarily consecutive)
- Case-insensitive
- Directories auto-expand when filtered; a directory remains visible if any descendant matches
## @-Filter Bridge
- Typing an @-prefixed query in the chat input activates the file picker filter (see [chat.md](chat.md))
- Files tab receives filter-from-chat events and forwards them to the picker
## Keyboard Navigation
- Arrow keys move focus; ←/→ collapse and expand directory rows
- Space/Enter opens the focused file in the viewer, or expands/collapses a focused directory — the same verbs a plain click has. It toggled selection until CC-21; the keyboard follows the mouse rather than keeping a verb the mouse no longer has
- Auto-scroll to focused item
- F2 renames the focused file row via the inline-input flow. Click a file row first to give it focus, then press F2. No-op when nothing is focused or when focus is on a directory row — directory rename remains context-menu-only to prevent accidental subtree renames from a stray keystroke
## Git Status Badges
| State | Color | Badge |
|---|---|---|
| Clean | Grey | — |
| Modified | Amber | M |
| Staged | Green | S |
| Untracked | Green | U |
| Deleted | Red | D |
## Deny-Read (Row Gesture)
A path is either neutral or denied. There is no third state and no hinted state:
| State | Visual | Effect |
|---|---|---|
| Neutral (default) | Normal | Nothing. The agent may read the file if it decides to |
| Denied | Strikethrough, dimmed, `✕` badge | A `Read` deny rule is in force for the path. The agent's read fails and it is told why |

Denial is the picker's one *enforced* per-path state, and the only state it has left. The inverse — pointing
the agent **at** a file — is not a state at all: it is [path insertion](#path-insertion) into the prompt, which
is a thing the user writes rather than a thing the picker remembers.

### Interaction Model
- Shift+click on a file row — toggles between neutral and denied. `preventDefault` fires so the row's plain-click verb (open in viewer) does not also run
- Shift+click on a directory row — denies the whole subtree, or allows it when every descendant is already denied. `preventDefault` keeps the twisty from toggling too: being handed an expanded tree as a side effect of writing a permission rule reads as two things happening at once
- Shift+click on the root row — the same, scoped to every file in the repository
- Binaries are included in a subtree deny. A deny rule is about the path, and the agent can attempt a read on a PDF as readily as on a source file — a rule that quietly skipped `papers/*.pdf` would be a rule the user thinks they wrote and did not
- A directory with zero descendant files is a no-op
### Visual Treatment
- Denied files — strikethrough, muted opacity, `✕` badge, tooltip explaining shift+click to re-allow
- Directory rows reflect descendant denial state so the tree surfaces it without requiring every folder to be expanded:
  - All descendant files denied — strikethrough, muted opacity, tooltip explaining shift+click to re-allow all
  - Some descendants denied — `✕` badge at reduced opacity (no strikethrough), tooltip indicating partial denial. The badge is the distinguishing signal for this state alone, where strikethrough would be misleading
  - None denied — normal styling
- The root row uses the same three visual states as directory rows, aggregated over the entire repository
- A denied file whose rule came from a settings file the user wrote by hand, rather than from this picker, renders identically. The picker reflects the effective rule set; it does not distinguish rules by who authored them
### Context Menu
- File and directory context menus include allow/deny items as an alternative to shift+click, labelled "Deny agent read" and "Allow agent read". Only one of the pair is shown, chosen by the path's current state
- The menu route matters more than it used to: `shift` now means deny on a left-click and `@path` on a middle-click, so a user who is unsure which gesture they are about to make has a labelled way to say what they meant
### Backend Coordination
- A deny writes a `Read(<path>)` rule through the permissions layer, one per denied file. A directory deny expands to its descendant files at the gesture and writes one rule each; no `Read(<dir>/**)` glob is synthesised, so a file added to a denied directory afterwards is **not** covered by it. Shapes and destinations are in [`../../specs-reference/3-engine/permissions.md § PermissionUpdate`](../../specs-reference/3-engine/permissions.md)
- The list the RPC receives is authoritative, not additive: it replaces every `Read` rule the picker owns, which is what makes un-denying work without a second method. Deny rules in the same settings file that are not `Read(...)`, and every other settings key, are preserved
- The rule is real enforcement, applied by the CLI, not a filter we implement. Two consequences the UI must not obscure: it takes effect on the agent's very next read with no re-index and no cache invalidation, and when written to a settings file it applies to the `claude` CLI in the same repository too
- Removing a deny removes the rule. Nothing is rebuilt, because nothing was excluded from an index — the symbol and doc indexes are always whole-repo
- A denied read reaches the agent as a denial with a reason, so it can adapt. It does not look to the agent like a missing file, which would send it hunting
- One RPC, no per-tab dispatch. Rules are repo-wide by construction: a subagent inherits the session's settings sources, so there is no such thing as denying a path to one agent and not another. The frontend keys its denial *display* by tab (see [Files Tab Orchestration](#files-tab-orchestration)) purely so a dormant tab strip doesn't show one tab's rules as another's
- The picker's denial list is not authoritative and is not persisted by the frontend. It is re-read from the effective rule set on load — [state-fetch](../1-foundation/rpc-inventory.md) returns `denied_read_files` in the state snapshot — so a rule written by hand in a settings file shows up in the tree

Deleted with the tiering system: `set_excluded_index_files`, the cache-shaping filter, the three
application points, the dir-block seeding filter, and the per-turn dir-block refresh filter. An
exclusion is now one rule in one file.

### Denial Scope Prompt

**Not built.** Predates CC-21 and is untouched by it — recorded here because the design is still wanted,
not because it ships. What the code does today is write every denial straight to
`.claude/settings.local.json` (the "Save for this checkout" branch below) with no dialog and no
preference key, and report the one thing the dropped L0 dialog was honest about — that the rule is not
instant — through a `takes_effect` toast shown once per session. A reader comparing this section against
the picker will find no scope dialog and no `ac-dc-deny-read-scope` in `localStorage`.

- Denying a file or directory opens a small dialog asking where the rule should live: **This session only** (dropped when the session ends) or **Save for this checkout** (written to `.claude/settings.local.json`, git-ignored, survives restarts)
- Two buttons plus Cancel. A "Don't ask again" checkbox persists the chosen scope as the default for future denials; Cancel never persists a preference
- Removing a denial never prompts. The user explicitly wants the agent to see the file again, and there is no scope question to answer — every rule the picker wrote for that path is removed
- Preference stored in `localStorage` under `ac-dc-deny-read-scope` with values `ask` (default), `session`, or `local`. Resettable from Settings
- The dialog states the destination file by name. A rule the user cannot find is a rule the user cannot revoke

This replaces the old L0-invalidation prompt, which asked a question about our cache. The question is now
about durability and blast radius, which is a question about the user's repository — a better question to
be asking, and one with a consequence the user can inspect afterwards.

### Binary Files
- The picker rejects nothing on a binary row (xlsx, pdf, png, zip). Its path can be inserted and its read can be denied like any other, because neither operation puts content anywhere
- The consequence lands on the agent instead. A `Read` of a binary fails back to the agent with a reason, inside its own turn, and it adapts. AC⚡DC does not intervene. (An inserted `@path` on a binary is the one case where the failure is the CLI's expansion rather than the agent's tool call — still a message about that file, still not a turn AC⚡DC has to police)
- Because the failure is the agent's rather than ours, the old machinery is gone: no turn-start binary detection, no trimming of a selected list, no `binaryFilesSkipped` broadcast, and no toast
- What survives is the useful part. A binary row's tooltip explains that the agent cannot read this file directly and names the Doc Convert tab, in place of the usual gesture list. The explanation is on the row the user is already hovering, rather than in a toast after a turn began. (A per-row **Convert with Doc Convert** context item was specified here and never built; the toolbar's 📄 button, shown when the backend reports markitdown is installed, is the entry point that exists)
- After conversion the user points at the produced sibling markdown, which the agent can read

## Context Menu
### File Items
- **Insert path in prompt**, **Insert @path — agent reads it** — leading the menu, because path insertion is the picker's primary verb (CC-21) and was previously a middle-click gesture and nothing else, which made the most important thing the picker does its least discoverable one
- Stage, unstage, discard (confirm)
- Rename (inline input)
- Duplicate (inline input pre-filled with current path)
- Load in left panel, load in right panel — for ad-hoc comparison (see [diff-viewer.md](diff-viewer.md))
- Deny agent read / Allow agent read — whichever the path's current state makes available
- Delete (confirm)
### Directory Items
- Insert path in prompt, Insert @path — agent reads it
- Stage all, unstage all
- Rename (inline input)
- New file (inline input)
- New directory (inline input) — creates a placeholder file inside since git does not track empty directories
- Deny agent read on all / Allow agent read on all — gated on whether the subtree is fully, partly, or not at all denied

### Root Items
Right-clicking the repo-name row opens a reduced menu scoped to operations that make sense at the repository level:
- New file (inline input) — creates at repo root
- New directory (inline input) — creates at repo root

Stage-all / unstage-all / rename / deny-all are deliberately absent — the root is the repository itself, not a per-directory operand. Path insertion is absent for a plainer reason: the root's path is the empty string, so there is nothing to insert. (Repo-wide denial is still reachable, by shift+clicking the row.) Root-level new-file and new-directory use the same action IDs as the directory menu so the orchestrator routes through the same dispatcher.
### Inline Input Pattern
- Rename, new file, new directory operations render an inline text input at the correct indentation level (not a browser prompt)
- Input appears immediately below the target node (for rename) or as a child of the directory (for new file/dir)
- Enter submits, Escape or blur cancels
- Rename — input pre-filled with current name and auto-selected
- New directory — creates a placeholder file inside
- Auto-focus applied via lifecycle hook after render
### Load in Panel
- Load in left panel / load in right panel — fetch file content and dispatch an event that the diff viewer uses to load content into a panel for ad-hoc comparison
## First-Load Auto-Expand

- On first load, auto-expand directories containing files in the modified / staged / untracked / deleted lists. A changed file inside a collapsed subtree is a changed file the user does not know about
- A one-time guard ensures this runs exactly once per component lifetime — subsequent tree reloads (after commits, resets) do not re-trigger it, because by then the expand state is the user's
- Review entry is the deliberate exception: it re-expands even after the one-shot flag is spent, since the review's changed-file set is a different subject from whatever the user was last looking at

This was **auto-selection** until CC-21: the same directories were expanded, and every changed file was
also ticked and pushed to the server as a hint. The expansion was the half users read. Nothing merges
with a server-provided list any more, because the server holds no such list.

## File Mentions

- Files mentioned in assistant responses dispatch file-mention-click events; chips in the "Files Referenced" footer dispatch file-chip-click (see [chat.md](chat.md))
- Both do one thing: open the file in the viewer
- Neither changes any picker state and neither calls an RPC. A mention click used to toggle selection, which meant a click meant to *read* a file the agent had just talked about silently changed what the next turn claimed the user wanted. Chips carried `navigate: false` for the same reason — they were a context-curation surface, so opening the file would have been in the way. With nothing to curate, opening it is the only thing left to want

## Agent-Modified File Highlight

The old picker auto-selected files in two situations: when the LLM tried to edit a file that was not in
context, and when it created a new one. Both existed because an edit could only land on a file whose
content was in the prompt — selection was the mechanism, so selection had to be repaired.

Neither situation exists. The agent edits what it likes and creates what it likes, and its writes do not
consult the selection at all. Auto-selecting after the fact would be a hint the user never gave, about
work already done.

What replaces it is observation:

- `PostToolUse` broadcasts name the paths the agent just wrote. The picker refreshes those rows' git status and diff stats in place, without a full tree reload
- Modified paths get a turn-scoped **touched** marker — a small accent dot — so a user scanning the tree after a turn can see the blast radius without reading the transcript. The marker clears on the next user turn
- Parent directories of touched files auto-expand, which is the one behaviour worth keeping from the old auto-add: a file the agent changed in a collapsed subtree is a file the user will not notice
- A path the agent created appears in the tree on the next refresh like any other new file

Nothing is auto-selected and no retry prompt is composed. Both belonged to the edit protocol.

## Path Insertion

The picker's primary verb: it is how a user points the agent at a file, and since CC-21 it is the only way.
Two forms, because they cost different things:

| Gesture | Inserts | Meaning |
|---|---|---|
| Middle-click a row | `path/to/file.py` | A pointer. The agent reads it if the work needs reading it, and the turn costs one path's worth of tokens if it doesn't |
| Shift+middle-click a row | `@path/to/file.py` | A read. The CLI expands the mention into the file's full text before the turn starts, whether or not the agent would have asked for it |

- Both are also context-menu items, on file and directory rows. A gesture many trackpads cannot produce is not allowed to be the only way to reach the picker's primary verb
- `@` is applied in one place — the files tab's insertion handler — so the gesture and the menu item cannot drift apart on what an `@path` looks like
- The path lands at the cursor position, space-padded before and after only where it would otherwise jam against existing prose
- Insertion pushes through the chat panel's reactive input state as well as the textarea value, so send-button enablement and textarea auto-resize both respond
- Only button 1 (middle) triggers the gesture; the handler ignores every other `auxclick` button
- Browser's selection-buffer paste is suppressed via a one-shot flag on the chat panel (set by the path-insertion path, consumed by the paste handler)
- Cross-component flag pattern — the flag lives on the chat panel (which owns the textarea and paste event), not on the picker or a shared singleton
- Flag must be set before the textarea receives focus, or the browser may dispatch the paste before the handler sees the flag

`shift` therefore means two different things on one row — deny on a left-click, `@path` on a middle-click.
That is accepted with a mitigation rather than resolved: both insertion forms and both denial verbs are
context-menu items, so no gesture is load-bearing on its own.

## Active File Highlight

- Row highlighted when file is open in the viewer
- The viewer dispatches an active-file-changed event on tab switch, open, or close
- App shell relays to the dialog → files tab → picker
- Distinct background and left-border accent, independent of denial state

## Reveal from Diff Viewer

The picker exposes a public `revealFile(path)` method that makes the named file visible and draws the user's attention to it. Called by the files tab when the diff viewer dispatches `reveal-file-in-picker` (typically from a status-LED click — see [diff-viewer.md](diff-viewer.md#status-led)).

Behavior:

- Expand every ancestor directory of the target path so the row lands in the DOM
- Clear any active filter — a filter that would hide the target file makes reveal feel broken, so reveal wins unconditionally
- Set the focused-row highlight to the target path (same highlight channel file-search uses)
- Scroll the target row to the centre of the picker viewport with smooth scrolling
- Flash the row briefly with an accent-coloured animation, then remove the animation class so subsequent reveals restart cleanly

No-op when the path is missing, non-string, or not found in the current tree. Safe to call from outside the picker component; the files tab is the conventional caller but any consumer with a picker reference can invoke it.

## Left Panel Resizer

- Vertical 4px splitter between the file picker and chat panel, widening to a ~20px affordance strip with a `▸` glyph when the picker is collapsed
- Drag to resize: width clamped to [180px, 50% of the host width]. Minimum prevents the picker from collapsing below readable size; maximum keeps the chat pane at least half the dialog
- Double-click to toggle collapsed state. Collapsed renders at a fixed ~24px affordance width regardless of the stored drag width; the stored width survives so expand restores the user's prior size
- Width persists to `ac-dc-picker-width` in localStorage; collapsed flag persists to `ac-dc-picker-collapsed`
- Malformed stored values fall back to a sensible default rather than rendering at a sub-readable size
- In collapsed mode the splitter is a click target for expand (via double-click); pointerdown does not start a drag since the origin width would be meaningless

## Review Mode Banner

- When review mode is active, a banner at the top of the picker shows branch name, commit range, file/line stats, exit button
- Synchronized with review state from the review-state RPC
- See [code-review.md](../4-features/code-review.md)

## File Search Integration

When file search is active in the chat panel, the files tab swaps the picker tree to a pruned view containing only matching files.

- Search change event — triggers pruned tree build (from results) or full tree restore
- Search scroll event — syncs picker highlight to match panel scroll position

### Tree Swap

- Files tab builds a pruned tree from search results (splitting paths into nested directories, setting line count to match count)
- Calls the picker's set-tree method
- On exit, expanded state is restored before the full tree reload, so the user's previous expand/collapse state returns
- Focus state is cleared on exit

### Expand State Preservation

- Set-tree lazily snapshots the current expanded set on the first call (repeated search refinements do not re-snapshot)
- Restore method replaces the expanded set with the saved snapshot before the full tree reload
- Full tree reload does not reset the expanded set, so the restored state is used for rendering

### Picker Click Intercept

- During file search, file-clicked events from the picker are intercepted
- Instead of navigating to the diff viewer, the files tab calls the chat panel's scroll-to-file method to scroll the match overlay to the target file section

### Scroll Highlight Sync

- When the match overlay scrolls, the files tab receives search-scroll events and updates the picker's focused path, expands ancestor directories, and scrolls the picker to show the highlighted file row
- A brief pause flag prevents feedback loops between the two scroll directions

## Files Tab Orchestration

The files tab (parent of both picker and chat panel) coordinates all file-related state.

### Responsibilities

- Deny-read sync — receives the picker's exclusion-changed event (the internal name of the deny-read state, kept from the checkbox era), holds the authoritative set, pushes it back to the picker, and writes the whole list through the permissions RPC
- File mentions — receives file-mention-click and file-chip-click from chat and dispatches navigation. No state changes, no RPC
- Path insertion — routes insert-path from the picker's middle-click and its two menu items into the chat textarea, applying `@` when the event asks for the mention form
- Review lifecycle — refreshes the tree, re-runs the auto-expand pass over the review's changed files, updates the chat panel's review state
- Filter bridge — forwards filter-from-chat events to the picker's set-filter method
- File tree refresh — forwards files-modified from chat to picker's load-tree method and re-dispatches on window
- Touched markers — forwards the turn's modified paths from `PostToolUse` broadcasts to the picker, and clears them when the next user turn starts

A **selection sync** responsibility sat at the head of this list, and a **message preservation** one
below it existed to serve it. Both are gone with CC-21.

### Direct Update Pattern (Architectural)

When the deny-read set changes, the files tab updates the picker's property directly rather than relying on framework top-down reactive propagation.

Why this is necessary — framework reactive data flow means changing a property on the parent triggers a full re-render of its template, which would re-assign child component properties. For the file picker, this collapses interaction state (context menus, inline inputs, focus). For the chat panel, it resets scroll position and disrupts streaming state.

The pattern:

1. Short-circuit when the incoming set equals the current one — stops a broadcast loopback from doing another round-trip for our own update
2. Update the files tab's own deny-read state (the authoritative copy)
3. Directly set the picker's excluded-files property + request update
4. Notify server via the deny-read RPC, when the change originated locally

Where it's used — exclusion-changed handler, context-menu deny/allow handlers, state-loaded handler, active-tab-changed handler.

The pattern also had a **step 0**: sync messages from the chat panel back into the files tab's own state before touching anything. Selection lived on the chat panel as well as the picker, so a selection change re-rendered the files tab, and the re-render pushed the files tab's stale `messages` prop back down — losing whatever had streamed in since. Deny-read never touched the chat panel, so with selection gone the failure mode has no path: nothing the tab does on a denial re-renders the chat panel's message list.

### Review Entry Flow

When a review starts (via review-started event from the review selector):

1. Set review state to active with review details, and push it to the picker
2. Refresh picker's file tree (now shows staged changes from soft reset)
3. Re-run the auto-expand pass so the review's changed files are open — deliberately bypassing the one-shot first-load flag, since the user explicitly asked for this

Nothing is put into the turn on the user's behalf. The review used to also clear the hint set, on the
frontend and the server, on the reasoning that a review starts with nothing hinted; there is no hint set
to clear now, and the review's shape reaches the agent through the framing block's review facts — a
statement about the review rather than a claim about what the user picked.

Deny-read rules are **not** cleared on review entry and not restored on exit. They are the user's
standing policy about their repository, and a review is not a reason to hand the agent access it was
denied. The read-only permission posture the review installs is a separate mechanism and is layered on
top (see [code-review.md](../4-features/code-review.md#read-only-mode)).

## State Persistence

- Expanded directories — tracked in component state, propagated via events
- Panel width — localStorage
- Panel collapsed state — localStorage
- Branch name — fetched live on each tree reload, not persisted

## Invariants

- A row's denied rendering always reflects the effective rule set after any toggle, including rules the picker did not write
- The picker sends nothing with a turn. Every path the agent is pointed at is text the user can see in the composer before pressing send
- Removing a denial always removes every rule the picker wrote for that path
- Deny-read is repo-wide. The per-tab keying of the display list never produces a rule that applies to one agent and not another
- First-load auto-expand runs exactly once per component lifetime; review entry is the one event allowed to re-run it
- Middle-click path insertion always suppresses the subsequent browser selection-buffer paste
- Review entry never alters deny-read rules
- Direct-update pattern for deny-read changes never triggers a parent re-render that would reset child scroll or interaction state
- File search exit restores the previous expanded state of the full tree
- Touched markers are turn-scoped: they are never persisted and always cleared when the next user turn starts
- The picker never points the agent at a file in response to something the agent did