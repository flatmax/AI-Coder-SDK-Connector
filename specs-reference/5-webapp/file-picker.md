# Reference: File Picker

**Supplements:** `specs5/5-webapp/file-picker.md`

## Schemas

### localStorage keys

| Key | Type | Default | Purpose |
|---|---|---|---|
| `ac-dc-sort-mode` | `"name"` / `"mtime"` / `"size"` | `"name"` | Sort mode for file tree |
| `ac-dc-sort-asc` | `"1"` / `"0"` | `"1"` | Sort direction: 1 = ascending, 0 = descending |
| `ac-dc-picker-width` | integer px (string) | `280` | Picker pane width within the Files tab |
| `ac-dc-picker-collapsed` | `"true"` / `"false"` | `"false"` | Picker collapsed state |
| `ac-dc-deny-read-scope` | `"ask"` / `"session"` / `"local"` | `"ask"` | Remembered answer to the denial-scope prompt. `session` writes the rule for this session only; `local` writes it to `.claude/settings.local.json`. Resettable from Settings |

Malformed values fall back to defaults. Storage errors (private-browsing quirks, quota) are swallowed silently.

The hint set is **not** in this table. It is in-memory browser state forwarded to the server for the next
turn's framing, and it is deliberately not persisted: a hint is "what I am looking at right now", and a
stale one restored from a previous browsing session would be a lie told to the agent on the user's behalf.
Deny-read rules do persist, but in `.claude/settings*.json` where the CLI can also see them — not here.

### Panel width constraints

| Constant | Value |
|---|---|
| Default width | 280 px |
| Minimum width | 180 px (enforced by drag clamp) |
| Maximum width | 50% of host element width (enforced by drag clamp) |
| Collapsed width | ~24 px (affordance strip, widens from splitter hover) |
| Splitter width (normal) | 4 px |
| Splitter width (collapsed mode) | ~20 px (shows `▸` glyph) |

### Context menu action IDs

Dispatched via `context-menu-action` events with `detail: { action, type, path, ... }`:

**File row actions** (`type: "file"`):

| Action ID | Label | Trigger |
|---|---|---|
| `stage` | Stage | Runs `Repo.stage_files([path])` |
| `unstage` | Unstage | Runs `Repo.unstage_files([path])` |
| `discard` | Discard Changes… | Confirm → `Repo.discard_changes([path])` |
| `rename` | Rename… | Opens inline input pre-filled with current name |
| `duplicate` | Duplicate… | Opens inline input pre-filled with full path |
| `load-left` | Load in Left Panel | Fetches content and dispatches `load-diff-panel` event |
| `load-right` | Load in Right Panel | Fetches content and dispatches `load-diff-panel` event |
| `deny-read` | Deny agent reads | Shown only if the path has no deny rule. Writes `Read(<path>)` via the permissions layer |
| `allow-read` | Allow agent reads | Shown only if the path has a deny rule. Removes it |
| `doc-convert` | Convert to Markdown… | Shown only for convertible binary types. Opens the Doc Convert tab prefilled with the path |
| `delete` | Delete… | Confirm → `Repo.delete_file(path)` (destructive class) |

**Directory row actions** (`type: "dir"`):

| Action ID | Label | Trigger |
|---|---|---|
| `stage-all` | Stage All | Collects descendant files, runs `Repo.stage_files(paths)` |
| `unstage-all` | Unstage All | Collects descendants, runs `Repo.unstage_files(paths)` |
| `rename-dir` | Rename… | Opens inline input pre-filled with current name |
| `new-file` | New File… | Opens inline input as child |
| `new-directory` | New Directory… | Opens inline input; creates directory + `.gitkeep` |
| `deny-read-all` | Deny agent reads | Writes one glob rule, `Read(<dir>/**)` — not one rule per descendant |
| `allow-read-all` | Allow agent reads | Removes the subtree's glob rule and any per-file rules underneath it |

**Root row actions** (`type: "root"`):

| Action ID | Label | Trigger |
|---|---|---|
| `new-file` | New File… | Inline input creates file at repo root |
| `new-directory` | New Directory… | Inline input creates directory at repo root |

### Inline input modes

When the user triggers rename / duplicate / new-file / new-directory, the picker renders an inline text input at the appropriate tree position:

| Mode | Rendering position | Pre-fill |
|---|---|---|
| `rename` | Replaces the target row | Current name |
| `duplicate` | Below the target row | Full current path |
| `new-file` | At the top of the target directory's children | Empty |
| `new-directory` | At the top of the target directory's children | Empty |

Key handling: Enter commits, Escape cancels, blur cancels. After commit, the input dispatches `rename-committed` / `duplicate-committed` / `new-file-committed` / `new-directory-committed` events with payload shape documented in `specs5/5-webapp/file-picker.md`.

## Dependency quirks

### Shift+click vs regular click — `preventDefault()` asymmetry

Regular click on a checkbox: do NOT call `event.preventDefault()`. The browser's native checkbox toggle runs, updating the visual state. Our reactive `.checked` binding re-renders with the authoritative state on the next frame. Result: user sees the expected toggle with no visual glitch.

Shift+click on a checkbox: DO call `event.preventDefault()` immediately. Without this, the browser's native toggle fires first, producing a one-frame visual flip, then our state change applies and the checkbox flips back. The glitch is ~16ms but visually obvious.

### Regular click on a denied file

One gesture performs two state changes:
1. Remove the `Read` deny rule for the path
2. Add the path to the hint set

Dispatches `deny-changed` then `hint-changed`. The orchestrator fires both RPCs (restricted guard per
call; either may fail independently). The ordering matters and is the reverse of the intuitive one:
removing the rule is the operation with a durable side effect on a settings file, so it goes first. A hint
that lands against a still-denied file is a harmless inconsistency for one frame; a rule removal that
silently failed while the checkbox ticked is a user believing they re-allowed a file they did not.

### Directory click with denied descendants

Regular click on a directory row whose subtree carries deny rules:
1. First removes every deny rule in the subtree — the directory's own glob rule and any per-file rules underneath it
2. Then applies the normal hint-all-descendants logic

Prevents the confusing state where ticking a parent hints most descendants while some remain unreadable.
The failure mode is worse than the old excluded-file version: an excluded file still contributed an index
block, whereas a denied file makes the agent's read fail outright, so a half-applied parent click produces
a turn where the agent reports it cannot read files the user believes it was just pointed at.

### Three-state checkbox cycle

Shift+click cycles through states based on current state:

| Current | Shift+click result |
|---|---|
| Neutral | Denied |
| Hinted | Denied (also clears the hint) |
| Denied | Neutral (back to no rule, NOT hinted) |

The "denied → neutral" direction deliberately does NOT jump to hinted, for the same reason as the native
engine's version: the shift+click gesture meant "change what the agent may read", not "point the agent at
this". The regular-click-on-denied path covers the "allow AND hint" case in one gesture.

### The denial-scope prompt runs before the first write, not on every write

The first deny in a session prompts for scope (session-only or `.claude/settings.local.json`) and the
answer is remembered per `ac-dc-deny-read-scope`. The prompt is modal on the gesture: the checkbox does
not change state until it resolves, and cancelling leaves the file neutral. Optimistically striking the
row and then reverting on cancel would make the safer answer look like a failure.

### Deny rule cleanup on delete

When a file is deleted via the context menu:
1. If the path carried a deny rule, remove it
2. If the path was in the hint set, drop it locally

Both steps are local-first. The server does not broadcast rule changes, so without step 1 re-creating a
file at the same path would find it mysteriously unreadable — and unlike the old excluded state, the
symptom would be a failed agent read rather than a missing index block, which is far harder to attribute
to a file that was deleted an hour ago.

### Touched markers are turn-scoped and never become hints

`PostToolUse` marks a row as touched for the remainder of the turn and auto-expands its parents. The
marker is cleared on the next user message. It never adds the path to the hint set: the agent already
read and wrote the file, so a hint would tell it something it knows, about work it has finished.

## Cross-references

- Behavioral specification: `specs5/5-webapp/file-picker.md`
- Files tab orchestration and direct-update pattern: `specs5/5-webapp/file-picker.md` § Files Tab Orchestration
- Middle-click path insertion + chat panel flag: `specs-reference/5-webapp/chat.md` § Cross-component flag
- `PermissionUpdate` shape for deny rules and their destination files: `specs-reference/3-engine/permissions.md`
- Why selection is a hint rather than a context contract: `specs5/plan/decisions.md#cc-14--file-selection-becomes-a-hint-not-a-context-contract`