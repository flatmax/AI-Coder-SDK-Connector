# File Picker

Tree view of repository files with checkboxes, git status, and context menu. Left panel of the Files tab. Drives file navigation (which file is open in the viewer), the selection hint sent with a turn, and per-path read denial.

What a checkbox means changed. It used to be a context contract: a checked file's full content was placed in the prompt, and an unchecked file contributed only its index block. The agent reads files itself now, so a checked file is a **hint** — it tells the agent what the user is looking at, and nothing more. The third state is no longer an index filter but a real permission rule: the agent is denied `Read` on that path. See [decisions § CC-14](../plan/decisions.md#cc-14--file-selection-becomes-a-hint-not-a-context-contract).

The tree, the git status, the sorting, the context menu, the resizer, and every navigation affordance are untouched by the conversion. Only the checkbox semantics and their backend wiring changed.
## Tree Rendering
### Root Node — Branch Badge
- Root row displays a checkbox, the repo name, and a compact pill showing the current git branch
- Branch name prefixed with a branch icon
- Normal branch — muted style, rendered as a clickable button that opens the branch switcher popover (see below)
- Detached HEAD — orange-tinted style, short SHA instead of branch name; non-interactive (switching out of detached HEAD goes through the commit graph, not the pill)
- Long branch names truncated with ellipsis, full name in tooltip
- Fetched via the current-branch RPC on every tree reload — stays current after commits, checkouts, and review entry/exit
- Root checkbox aggregates over every file in the repo — same semantics as a directory checkbox applied to the whole tree: regular click toggles select-all (un-excluding any excluded descendants), shift+click toggles exclude-all (deselecting any selected descendants). Checked / indeterminate / unchecked reflect aggregate selection; strikethrough + dimmed checkbox reflect all-excluded; `✕` badge reflects partial exclusion

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
- Expandable toggle
- Checkbox selects/deselects all children
- Indeterminate state when partially selected
### File Nodes
- Checkbox for selection (three states — see below)
- Name click opens in the diff viewer (or SVG viewer for SVG files)
- Line count badge in neutral color (no size thresholds)
- Git status badge — modified, staged, untracked, deleted
- Diff stats for changed files (additions and deletions) — rendered in a reserved gutter to the left of the checkbox column so the checkbox column stays aligned across sibling rows regardless of whether a given row has diff stats
### Tooltip
- Every row displays a native browser tooltip on hover
- Format — full path and node name
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
- Arrow keys move focus
- Space/Enter toggles selection
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
## Three-State Checkbox (Hint and Deny-Read)
Files have three states controlled via the picker checkbox:
| State | Checkbox | Visual | Effect |
|---|---|---|---|
| Neutral (default) | Unchecked | Normal | Nothing. The agent may read the file if it decides to |
| Hinted | Checked | Normal | The path is listed with the next turn as "what the user is looking at" |
| Denied | Unchecked | Strikethrough, dimmed | A `Read` deny rule is in force for the path. The agent's read fails and it is told why |

The middle state is advisory and the third is enforced, which is the opposite of the old arrangement —
selection used to be the enforced thing and exclusion the advisory one. It is worth internalising,
because it inverts what a careful user does: to keep the agent away from a file you deny it, and to point
the agent at a file you hint it and say so in the prompt.

### Interaction Model
- Regular click — toggles between neutral and hinted
- Shift+click — toggles between neutral and denied; suppresses the native checkbox toggle via `preventDefault` to avoid a visual glitch
- Shift+click on a hinted file — denies (clears the hint and writes the rule)
- Regular click on a denied file — removes the deny rule and hints the file
- Shift+click on a directory — writes or removes a glob deny rule covering the subtree
- Regular click to hint directory children — removes any deny rules on those children
- Regular and shift+click on the root checkbox apply the same rules as a directory checkbox, but scoped to every file in the repository
### Visual Treatment
- Denied files — strikethrough and muted opacity, reduced checkbox opacity, tooltip explaining shift+click to re-allow. No badge — the strikethrough alone carries the signal.
- Directory rows reflect descendant denial state so the tree surfaces it without requiring every folder to be expanded:
  - All descendant files denied — strikethrough, muted opacity, reduced checkbox opacity, tooltip explaining shift+click to re-allow all. No badge.
  - Some descendants denied — `✕` badge at reduced opacity (no strikethrough), tooltip indicating partial denial. The badge is the distinguishing signal for this state alone, where strikethrough would be misleading.
  - None denied — normal styling
- The root row uses the same three visual states as directory rows, aggregated over the entire repository
- A directory with zero descendant files is treated as "none denied"
- Checkbox tooltip adapts to state — the prompts differ for neutral, hinted, and denied
- A denied file whose rule came from a settings file the user wrote by hand, rather than from this picker, renders identically. The picker reflects the effective rule set; it does not distinguish rules by who authored them
### Context Menu
- File and directory context menus include allow/deny items as an alternative to shift+click, labelled "Deny agent reads" and "Allow agent reads"
### Backend Coordination
- A deny writes a `Read` rule through the permissions layer — `Read(<path>)` for a file, `Read(<dir>/**)` for a directory. Shapes and destinations are in [`../../specs-reference/3-engine/permissions.md § PermissionUpdate`](../../specs-reference/3-engine/permissions.md)
- The rule is real enforcement, applied by the CLI, not a filter we implement. Two consequences the UI must not obscure: it takes effect on the agent's very next read with no re-index and no cache invalidation, and when written to a settings file it applies to the `claude` CLI in the same repository too
- Removing a deny removes the rule. Nothing is rebuilt, because nothing was excluded from an index — the symbol and doc indexes are always whole-repo
- A denied read reaches the agent as a denial with a reason, so it can adapt. It does not look to the agent like a missing file, which would send it hunting
- The hint set is browser state, forwarded to the server so it can be included in the next turn's framing and broadcast to collaborators. It is not persisted across server restarts and nothing depends on it being accurate

Deleted with the tiering system: `set_excluded_index_files`, the cache-shaping filter, the three
application points, the dir-block seeding filter, and the per-turn dir-block refresh filter. An
exclusion is now one rule in one file.

### Denial Scope Prompt
- Denying a file or directory opens a small dialog asking where the rule should live: **This session only** (dropped when the session ends) or **Save for this checkout** (written to `.claude/settings.local.json`, git-ignored, survives restarts)
- Two buttons plus Cancel. A "Don't ask again" checkbox persists the chosen scope as the default for future denials; Cancel never persists a preference
- Removing a denial never prompts. The user explicitly wants the agent to see the file again, and there is no scope question to answer — every rule the picker wrote for that path is removed
- Preference stored in `localStorage` under `ac-dc-deny-read-scope` with values `ask` (default), `session`, or `local`. Resettable from Settings
- The dialog states the destination file by name. A rule the user cannot find is a rule the user cannot revoke

This replaces the old L0-invalidation prompt, which asked a question about our cache. The question is now
about durability and blast radius, which is a question about the user's repository — a better question to
be asking, and one with a consequence the user can inspect afterwards.

### Binary File Selection
- The picker accepts a checkbox on a binary file (xlsx, pdf, png, zip). It is a hint, so there is nothing to reject: no content is being placed anywhere
- The consequence lands on the agent instead. A `Read` of a binary fails back to the agent with a reason, inside its own turn, and it adapts. AC⚡DC does not intervene
- Because the failure is the agent's rather than ours, the old machinery is gone: no turn-start binary detection, no trimming of the selected list, no `binaryFilesSkipped` broadcast, and no toast
- What survives is the useful part. A binary row's context menu offers **Convert with Doc Convert** when the extension is supported, and the tooltip explains that the agent cannot read this file directly. The affordance is where the user already is, rather than in a toast after a turn began
- After conversion the user hints the produced sibling markdown, which the agent can read

## Context Menu
### File Items
- Stage, unstage, discard (confirm)
- Rename (inline input)
- Duplicate (inline input pre-filled with current path)
- Load in left panel, load in right panel — for ad-hoc comparison (see [diff-viewer.md](diff-viewer.md))
- Exclude from index / include in index
- Delete (confirm)
### Directory Items
- Stage all, unstage all
- Rename (inline input)
- New file (inline input)
- New directory (inline input) — creates a placeholder file inside since git does not track empty directories
- Exclude from index / include in index

### Root Items
Right-clicking the repo-name row opens a reduced menu scoped to operations that make sense at the repository level:
- New file (inline input) — creates at repo root
- New directory (inline input) — creates at repo root

Stage-all / unstage-all / rename / exclude-all are deliberately absent — the root is the repository itself, not a per-directory operand. Root-level new-file and new-directory use the same action IDs as the directory menu so the orchestrator routes through the same dispatcher.
### Inline Input Pattern
- Rename, new file, new directory operations render an inline text input at the correct indentation level (not a browser prompt)
- Input appears immediately below the target node (for rename) or as a child of the directory (for new file/dir)
- Enter submits, Escape or blur cancels
- Rename — input pre-filled with current name and auto-selected
- New directory — creates a placeholder file inside
- Auto-focus applied via lifecycle hook after render
### Load in Panel
- Load in left panel / load in right panel — fetch file content and dispatch an event that the diff viewer uses to load content into a panel for ad-hoc comparison
## Auto-Selection

- On first load, auto-select files appearing in the modified / staged / untracked / deleted lists
- Merge with any server-provided selection (e.g., after a browser refresh while server is still running) rather than replacing
- Auto-expand directories containing changed files
- A one-time guard ensures this runs exactly once per component lifetime — subsequent tree reloads (after commits, resets, review entry) do not re-trigger auto-selection

## File Mention Selection

- Files mentioned in assistant responses toggle selection via file-mention-click events (see [chat.md](chat.md))
- On add — file added to selected set, picker checkbox checked, parent directory auto-expanded, chat input text accumulated
- On remove — file removed from selected set, picker checkbox unchecked
- In both cases — file opened in diff viewer

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

Nothing is auto-hinted and no retry prompt is composed. Both belonged to the edit protocol.

## Middle-Click Path Insertion

- Middle-click on any row inserts the path into chat input at cursor position, space-padded before and after
- Browser's selection-buffer paste is suppressed via a one-shot flag on the chat panel (set by the path-insertion path, consumed by the paste handler)
- Cross-component flag pattern — the flag lives on the chat panel (which owns the textarea and paste event), not on the picker or a shared singleton
- Flag must be set before the textarea receives focus, or the browser may dispatch the paste before the handler sees the flag

## Active File Highlight

- Row highlighted when file is open in the viewer
- The viewer dispatches an active-file-changed event on tab switch, open, or close
- App shell relays to the dialog → files tab → picker
- Distinct background and left-border accent, independent of selection state

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

- Selection sync — receives selection-changed from picker, updates server and chat panel directly
- Deny-read sync — receives the picker's denial-changed event, resolves the scope prompt, and writes or removes the rule through the permissions RPC
- File mentions — receives file-mention-click from chat, toggles selection, updates picker and chat panel
- Message preservation — syncs messages from chat before selection updates to prevent stale message overwrites
- Review lifecycle — clears selection on review entry, refreshes tree, updates chat panel's review state
- Filter bridge — forwards filter-from-chat events to the picker's set-filter method
- Path insertion — routes insert-path from picker middle-click to chat textarea
- File tree refresh — forwards files-modified from chat to picker's load-tree method and re-dispatches on window
- Touched markers — forwards the turn's modified paths from `PostToolUse` broadcasts to the picker, and clears them when the next user turn starts

### Direct Update Pattern (Architectural)

When selection changes, the files tab updates both the picker's selected-files property and the chat panel's selected-files property directly, rather than relying on framework top-down reactive propagation.

Why this is necessary — framework reactive data flow means changing a property on the parent triggers a full re-render of its template, which would re-assign child component properties. For the chat panel, this resets scroll position and disrupts streaming state. For the file picker, it collapses interaction state (context menus, inline inputs, focus).

The pattern (used consistently across all selection-changing operations):

1. Sync messages from chat back into the files tab's own state — prevents stale data from overwriting the chat panel's current state on any future re-render
2. Update the files tab's own selected-files state
3. Directly set the chat panel's selected-files + request update
4. Directly set the picker's selected-files + request update
5. Notify server via the selected-files RPC

Where it's used — selection-changed handler, file-mention-click handler, files-changed handler, review-started handler, state-loaded handler, denial-changed handler.

Without the message sync step, the following failure occurs: user sends message → chat panel updates its messages array → user clicks file mention → files tab re-renders → chat panel receives the files tab's stale messages prop → latest messages are lost.

### Review Entry Flow

When a review starts (via review-started event from the review selector):

1. Set review state to active with review details
2. Clear the hint set (a review starts with nothing hinted — the diff is the subject, not a file list)
3. Reset picker's selected files to empty set
4. Refresh picker's file tree (now shows staged changes from soft reset)
5. Update chat panel's selected files and review state

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

- The checkbox's denied state always reflects the effective rule set after any toggle, including rules the picker did not write
- A denial always names its destination before it is written; no rule is written from a guess about scope
- Removing a denial never prompts and always removes every rule the picker wrote for that path
- The hint set never persists across server restart (only across browser reloads while the server is running), and nothing depends on its accuracy
- Auto-selection runs exactly once per component lifetime
- Middle-click path insertion always suppresses the subsequent browser selection-buffer paste
- Review entry clears the hint set on both server and frontend — defense in depth — and never alters deny-read rules
- Direct-update pattern for selection changes never triggers a parent re-render that would reset child scroll or interaction state
- File search exit restores the previous expanded state of the full tree
- Touched markers are turn-scoped: they are never persisted and always cleared when the next user turn starts
- The picker never auto-hints a file in response to something the agent did