# Reference: File Picker

**Supplements:** `specs5/5-webapp/file-picker.md`

## Schemas

### localStorage keys

| Key | Type | Default | Purpose |
|---|---|---|---|
| `aic-dc-sort-mode` | `"name"` / `"mtime"` / `"size"` | `"name"` | Sort mode for file tree |
| `aic-dc-sort-asc` | `"1"` / `"0"` | `"1"` | Sort direction: 1 = ascending, 0 = descending |
| `aic-dc-picker-width` | integer px (string) | `280` | Picker pane width within the Files tab |
| `aic-dc-picker-collapsed` | `"true"` / `"false"` | `"false"` | Picker collapsed state |
| ~~`aic-dc-deny-read-scope`~~ | — | — | **Declined 2026-08-29 and will never exist.** Belonged to the denial-scope prompt, which is declined rather than deferred — `specs5/5-webapp/file-picker.md` § *Denial Scope Prompt — declined*, `specs5/next.md` § E. Every denial goes to `.claude/settings.local.json` unconditionally, which is now the whole design rather than the unbuilt half of one. The row stays struck through so a reader who meets the key in an old branch or an old spec knows it was decided against, not forgotten |

Malformed values fall back to defaults. Storage errors (private-browsing quirks, quota) are swallowed silently.

Neither a hint set nor a selection is in this table, because neither exists (CC-21). The picker holds one
per-path list — the denied-read set — and does not persist it here either: it is re-read on load from the
state snapshot's `denied_read_files`, which reflects the effective rules in `.claude/settings*.json` where
the CLI can also see them. A rule the user wrote by hand therefore shows up in the tree, and a rule the
picker wrote survives a browser refresh without the frontend storing anything.

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
| `insert-path` | Insert path in prompt | Dispatches `insert-path` with `{path, mention: false}` |
| `insert-mention` | Insert @path — agent reads it | Dispatches `insert-path` with `{path, mention: true}` |
| `stage` | Stage | Runs `Repo.stage_files([path])` |
| `unstage` | Unstage | Runs `Repo.unstage_files([path])` |
| `discard` | Discard changes… | Confirm → `Repo.discard_changes([path])` |
| `rename` | Rename… | Opens inline input pre-filled with current name |
| `duplicate` | Duplicate… | Opens inline input pre-filled with full path |
| `load-left` | Load in left panel | Fetches content and dispatches `load-diff-panel` event |
| `load-right` | Load in right panel | Fetches content and dispatches `load-diff-panel` event |
| `exclude` | Deny agent read | Shown only if the path has no deny rule. Adds it to the denied set → `Read(<path>)` |
| `include` | Allow agent read | Shown only if the path has a deny rule. Removes it from the denied set |
| `delete` | Delete… | Confirm → `Repo.delete_file(path)` (destructive class) |

The deny/allow IDs are `exclude` / `include`, not `deny-read` / `allow-read`: they were the third
checkbox state's name throughout the picker and its tests, they outlived both the index they named and
(under CC-21) the checkbox itself, and renaming them would touch a dozen files to say the same thing.
Only the labels say "deny".

No `doc-convert` row action exists. It appears in no menu-item table in the code; the Doc Convert entry
point is the picker toolbar's 📄 button.

**Directory row actions** (`type: "dir"`):

| Action ID | Label | Trigger |
|---|---|---|
| `insert-path` | Insert path in prompt | Dispatches `insert-path` with `{path, mention: false}` |
| `insert-mention` | Insert @path — agent reads it | Dispatches `insert-path` with `{path, mention: true}` |
| `stage-all` | Stage all | Collects descendant files, runs `Repo.stage_files(paths)` |
| `unstage-all` | Unstage all | Collects descendants, runs `Repo.unstage_files(paths)` |
| `rename-dir` | Rename… | Opens inline input pre-filled with current name |
| `new-file` | New file… | Opens inline input as child |
| `new-directory` | New directory… | Opens inline input; creates directory + `.gitkeep` |
| `exclude-all` | Deny agent read on all | Shown unless every descendant is already denied. Adds every descendant **file** to the denied set — one `Read(<path>)` rule each, no `Read(<dir>/**)` glob |
| `include-all` | Allow agent read on all | Shown when any descendant is denied. Removes every descendant file from the denied set |

Binaries are included in both subtree operations. A deny rule is about a path, and the agent can attempt
a read on a PDF as readily as on a source file.

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

### `shift` denies, `ctrl` mentions

Four gestures on one row, and each modifier means exactly one thing across both buttons:

| Gesture | Effect |
|---|---|
| click | Open the file / expand the directory |
| shift+click | Toggle the `Read` deny rule (subtree, on a directory or the root) |
| middle-click (button 1) | Insert the bare path into the composer |
| ctrl+middle-click | Insert `@path` into the composer |

`shift`+middle-click is *not* a fifth gesture: `_emitInsertPath` reads `event.ctrlKey` and nothing else,
so a `shift`-held middle-click inserts the bare path. CC-21 shipped the mention on `shift` and had
`shift` meaning two things split by mouse button, on the reasoning that `ctrl`/`cmd`+click belongs to the
browser — true of the *primary* button, which is the one the browser spends `ctrl` on. The middle button
is free, so the collision is resolved rather than mitigated. All four verbs remain context-menu items, so
no gesture is the only route to anything.

### `preventDefault()` on the deny gestures

Shift+click on a file or directory row: DO call `event.preventDefault()` before mutating state. Without
it the row's plain-click verb also runs — a file opens in the viewer, or a directory's twisty toggles —
and being handed an expanded tree as a side effect of writing a permission rule reads as two things
happening at once. The checkbox era needed this call for a different reason (suppressing the browser's
native toggle to avoid a one-frame visual flip); the call survived the control it was written for.

Middle-click handlers likewise `preventDefault()` **and** `stopPropagation()`, and bail out unless
`event.button === 1`.

### The root row answers exactly one gesture

`shift`+click denies or allows the whole repository. A plain click on the root row is a deliberate no-op:
there is no repo-wide open, and the branch pill inside the row handles its own clicks (the handler
`stopPropagation()`s so the pill never reaches the row). The root context menu carries no insert-path
item because the root's path is the empty string.

### Denial is all-or-nothing across a subtree

`_toggleSubtreeDenial` reads the current state of every descendant file and picks one direction for all
of them: if every descendant is already denied it allows them all, otherwise it denies them all. There is
no per-descendant toggling, and a subtree with zero files is a no-op.

The resulting set is dispatched whole on `exclusion-changed`, and the RPC replaces every `Read` rule the
picker owns with that set. Consequence worth knowing: a file created inside a denied directory afterwards
is **not** denied, because there is no glob covering it.

### The denial-scope prompt is declined

**Decided 2026-08-29** — previously "not built". There is no dialog asking where a deny rule should live,
`aic-dc-deny-read-scope` is never read or written and never will be, and every denial goes straight to
`.claude/settings.local.json`. That is the design now, not a gap in one.

The short version of why: both offered scopes wrote the same bytes to the same file, differing only in
whether AIC⚡DC deleted the rule again at session end — and that cleanup needs a clean exit, which
Windows never gets and no crash gets anywhere. A genuine in-memory session scope has no mechanism either,
because **the CLI never routes `Read`, `Glob` or `Grep` through `can_use_tool`**, so our own permission
callback cannot see the tool the rule is about. Full reasoning in
`specs5/5-webapp/file-picker.md` § *Denial Scope Prompt — declined*; the decision is `specs5/next.md` § E.

What does reach the user is a `takes_effect` toast — the string comes from the RPC's return value, not
from an assumption in the frontend — shown once per session on the first denial, plus a `restricted`
warning toast when a non-local collaborator tries to deny (CC-15). Those carry the disclosure the dialog
was actually for: which file the rule went into, and that it applies from the CLI's next read of its
settings sources.

### Deny rule cleanup on delete

When a file is deleted via the context menu, and the path carried a deny rule, the rule is removed.
Local-first: the server does not broadcast rule changes, so without it, re-creating a file at the same
path would find it mysteriously unreadable — and the symptom is a failed agent read rather than a missing
index block, which is far harder to attribute to a file that was deleted an hour ago.

A second step used to sit alongside it — dropping the path from the hint set — and went with CC-21.

### Deny rules follow a rename

Renaming a file is not permission to read it. `onRenameCommitted` rewrites the rule onto the new path:
a file rename swaps one entry, a directory rename calls `migrateSubtreeState`, which rewrites every entry
whose path is `oldDir` or starts with `oldDir/` to the equivalent path under `newDir`. Both paths call
`_applyExclusion(next, /* notifyServer */ true)`, so the `Read` rules in `.claude/settings.local.json` are
rewritten too.

Without it the rule would be left behind on a path that no longer exists — silently stopping applying,
with nothing in the UI to say it had lapsed.

`migrateSubtreeState` still wraps its logic in a `migrateSet` closure over a single set. Two sets went
through it (selection and denial) until CC-21 removed the first.

### Touched markers are turn-scoped and point at nothing

`PostToolUse` marks a row as touched for the remainder of the turn and auto-expands its parents. The
marker is cleared on the next user message. It writes into no other channel: there is no list for a
touched path to join, and nothing about it reaches the next turn.

## Cross-references

- Behavioral specification: `specs5/5-webapp/file-picker.md`
- Files tab orchestration and direct-update pattern: `specs5/5-webapp/file-picker.md` § Files Tab Orchestration
- Middle-click path insertion + chat panel flag: `specs-reference/5-webapp/chat.md` § Cross-component flag
- `PermissionUpdate` shape for deny rules and their destination files: `specs-reference/3-engine/permissions.md`
- Why there is no selection at all: `specs5/plan/decisions.md#cc-21`
- The hint it superseded, for the history: `specs5/plan/decisions.md#cc-14`