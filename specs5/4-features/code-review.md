# Code Review

A review mode that leverages git's staging mechanism to present branch changes for AI-assisted code
review. By performing a soft reset, all review changes appear as staged modifications — so the file
picker, the diff viewer, and every git tool the agent has work unchanged, on a repository whose state
*is* the review.

The conversion changed how the agent learns about the review, not the review itself. There is no longer
a review system prompt, no injected review context block, and no pre-change symbol map held in memory.
Instead the repository is arranged so that `git diff --cached` **is** the review, the `review_state` MCP
tool tells the agent which arrangement it is looking at, and a read-only permission posture keeps a
reviewer from becoming an editor by accident. Everything the old design assembled into a prompt, the
agent can now fetch itself — and fetch at the moment it needs it, rather than once per turn.

## Architecture
- User selects branch and base commit via an interactive git graph
- Server verifies clean working tree, computes merge-base, performs a controlled sequence of checkouts and a soft reset
- Result — files on disk match branch tip, git HEAD at merge-base, all review changes appear as staged modifications
- Existing file picker, diff viewer, and context engine work unchanged
- Review preset activated — review snippets, review turn framing, and a read-only permission posture. No system prompt is involved
- The `review_state` MCP tool starts answering with review facts instead of "not in review"
- Exit reverses the state — soft reset to branch tip, checkout original branch
## Prerequisites
### Clean Working Tree
- Review mode requires a clean working tree — no staged or unstaged changes to tracked files
- Uses git status porcelain with untracked-ignore flag — untracked files are tolerated
- If dirty, user is shown an error with remediation commands
- See [repository.md](../1-foundation/repository.md) for the cleanliness check
### Dedicated Review Clone
- Recommended workflow is to use a separate clone for reviews
- Avoids disrupting active development — the soft reset changes the git state in ways that would be confusing alongside uncommitted work
## Git State Machine — Entry Sequence
Ordered operations to transform the repository into review state:
1. Verify clean working tree
2. Compute merge-base between branch tip and original branch (the branch HEAD was on before review — typically master/main)
3. Checkout the original branch (ensures a known starting point)
4. Checkout the merge-base commit (detached HEAD, disk at pre-review state)
5. Checkout the branch tip by SHA (detached HEAD, disk at reviewed state)
6. Soft reset to merge-base — HEAD moves, all feature branch changes become staged modifications
7. Re-index — the symbol and doc indexes now reflect the reviewed code on disk
8. Switch the permission posture to read-only (see § Read-Only Mode) and remember the previous mode

A ninth step cleared the file selection, on both sides for defence in depth. It went with the selection
([CC-21](../plan/decisions.md#cc-21)). The picker's deny-read rules are deliberately *not* cleared: they
are about paths, and a checkout does not make a file the user forbade the agent to read readable.

The old sequence had a step between 4 and 5: build a **pre-change symbol map** while disk sat at the
merge-base, hold it in memory, and inject it into every prompt. It is gone. The agent reaches the
pre-change state the same way a human reviewer does — `git show`, `git diff`, `git log` — and an index
of files that are no longer on disk cannot be served by `file_symbols` without lying about paths. What
the map bought (topology comparison across the change) is now the agent's own work, done on demand and
only when the review actually calls for it.

After step 6, the repository state:
| Aspect | State | Effect |
|---|---|---|
| Files on disk | Branch tip content | User sees reviewed code; symbol map reflects it |
| Git HEAD | Merge-base (detached) | `git diff --cached` shows only feature branch changes |
| Staged changes | Feature branch changes only | File picker shows M/A/D badges naturally |
| Working tree | Clean (matches disk) | No unstaged changes |
### Merge-Base Computation
- Use git merge-base rather than the parent of the user-selected base commit
- Matches GitLab/GitHub merge request semantics — excludes changes that arrived via merge commits from the target branch
- Cascades through candidates — original branch, then main, then master
- Falls back to parent of user-selected commit if all candidates fail
### Branch Tip Checkout by SHA
- Step 5 checks out by SHA, not by branch name
- Handles local and remote refs uniformly — remote refs like `origin/foo` would leave HEAD at the ref pointer rather than the actual commit
## Git State Machine — Exit Sequence
1. Soft reset to branch tip — HEAD moves, staging clears
2. Checkout original branch — HEAD reattaches to the branch the user was on before review
3. Rebuild symbol and doc indexes — reflects restored state
4. Restore the permission mode remembered at entry, and the non-review preset
If the original branch no longer exists, HEAD remains detached at branch tip SHA and the user is informed.

The permission restore happens even when the git restore fails. A half-exited review that leaves the
agent read-only is recoverable; one that silently re-arms writing against a detached HEAD is not.
## Error Recovery
- If any entry step fails, the repo module attempts to return to the original branch
- If that fails, the error is reported as-is
- If the server crashes during review, manual recovery via checkout of the original branch, or (if needed) a soft reset to branch tip followed by checkout
- Disk files already match branch tip — soft reset just moves HEAD
## Commit Selection via Git Graph
Replaces the two-step (branch dropdown → commit search) flow with a single visual interaction.
### Git Graph Display
- SVG rendered within a scrollable container
- Each branch occupies a stable vertical lane — branches do not shift lanes as the user scrolls
- Commit nodes are clickable — clicking selects that commit as the review base
- Lazy loading — initial batch of commits, more fetched on scroll-to-bottom
### Lane Assignment
- Each branch tip assigned a lane (column index), ordered by most recent commit date
- Commits follow first-parent links downward within the same lane
- Merge commits show a connecting line from the second parent's lane to the merge point
- When a branch's history joins another branch (the fork point), the lane ends
### Commit Node Display
- Colored circle matching its branch color
- Short SHA (7 characters)
- Commit message (first line, truncated)
- Author and relative date
- Branch tip commits show the branch name as a label badge
- Hover tooltip shows full SHA, parent SHAs, list of branches reaching the commit, author, ISO+relative date, and the full commit message
### Branch Legend
- Fixed header above the scrollable graph — does not scroll
- Branch chips colored to match their lanes
- Ordered by most recent commit
- Chips toggleable to filter branches in the graph — clicking a chip hides every commit and edge reachable only through that branch. Commits also reachable from a still-visible branch remain rendered. The chip itself dims and strikes through to indicate the hidden state; clicking again restores. Lane assignments don't reflow on toggle so the graph layout stays stable.
- Remote branches toggle button (default: local only)
### Disambiguation
- A commit can be reachable from multiple branches
- On click, perform full parent-walk from each branch tip to determine reachability
- Candidate branches are those whose walk reached the selected commit
- Disambiguation popover at the click position lists candidate branches
- The branch whose lane matches the selected commit's lane is pre-selected
- Scrolling the graph dismisses the popover
### Clean-Tree Check
- When the review selector opens, check cleanliness before rendering the graph
- If dirty, show an inline message with remediation commands instead of the graph
- Prevents selections that will fail at start-review time
### Review Summary and Action
- After selection, the area below the graph shows the review summary — branch name, commit range, commit count
- A single Start Review button initiates the review

### Review History Graph (Read-Only)
- Available during an active review via the "View graph" button on the review banner
- Uses the same commit-graph component as the selector, but in read-only mode — clicks emit `commit-inspected` rather than `commit-selected`
- No branch-disambiguation popover — clicks immediately inspect the commit
- Two commits are highlighted with coloured rings around their nodes:
  - **Amber ring** (`BASE` label) — the review's merge-base, which is the current git HEAD during the review
  - **Green ring** (`TIP` label) — the branch tip being reviewed
- Clicking any commit fetches its diff via `Repo.get_diff_to_branch(sha)` and routes it to the diff viewer's left panel via `load-diff-panel`
- The modal closes on commit click so the diff viewer takes focus — the fetch completes asynchronously and populates the panel when it lands
- Escape, backdrop click, and the close button all dismiss the modal
- Remote branches are included by default (the `includeRemote` flag is true) so the user can see where their review sits relative to remote tracking branches

## Commit Graph Data
- Paginated fetch with limit and offset
- Each commit carries SHA, short SHA, message, author, date, relative date, parent SHAs
- Branch data — name, SHA, is-current flag, is-remote flag
- Has-more flag for pagination
- Branch filtering — remove symbolic refs, arrow entries, bare remote aliases (e.g. `origin` when `origin/master` exists)
## Preset Swap

- On review entry the review preset activates: review snippets, the review turn-framing hint, and the read-only posture
- No system prompt is swapped, because AC⚡DC has none. A user who wants standing review instructions puts them in `CLAUDE.md`, which the CLI reads through `setting_sources` and which therefore applies in review exactly as it does elsewhere — see [decisions § CC-11](../plan/decisions.md#cc-11--setting_sources-includes-the-project-so-claudemd-is-live)
- A project that wants a genuinely different reviewer persona uses a Claude Code **agent** or **skill**, named in the preset. That is the platform's mechanism for the job and it is versioned in the repo, which the old `review.md` never was
- Preset state is not persisted. After a crash the next start comes up in the default preset, matching the fact that review git state is not persisted either

## How the Agent Learns It Is in a Review

Three channels, in order of how much they carry:

**1. The repository itself.** This is the load-bearing one, and it needs no protocol. Files on disk are
the reviewed code; `git diff --cached` is exactly the branch's change; `git log merge-base..tip` is the
commit list. Every one of the agent's own tools sees this without being told.

**2. The `review_state` MCP tool.** Reviewed branch, target branch, merge-base, and the changed-file
list with status and diff stats. Its real job is narrower than it looks: it tells the agent that the
repository is in AC⚡DC's *soft-reset* arrangement, which changes what `git status` means. A detached
HEAD with a hundred staged modifications is otherwise indistinguishable from a user mid-disaster. See
[`../3-engine/mcp-bridge.md` § `review_state`](../3-engine/mcp-bridge.md#review_state).

**3. Turn framing.** The user's turn carries the review's identity as a short set of facts — branch,
merge-base — not a payload. It never carries diffs. This is now the only list-shaped thing framing
carries besides the viewer's position ([CC-21](../plan/decisions.md#cc-21) removed the selected-files
block that used to head it).

### What is no longer injected

The old design built a review context block and re-injected it on every message: summary, commit list,
pre-change symbol map, and forward diffs for every selected file. All of it is gone.

The reason is not economy, it is staleness and duplication. The block was rebuilt every turn against
the current selection, which meant the model saw diffs it had not asked for, in an order chosen by the
picker, for as long as the review lasted. An agent that fetches `git diff -- path` when it decides to
look at `path` reads the same bytes, once, at the moment they are relevant — and can read the surrounding
file, the history of the hunk, or the test that covers it, none of which the block could offer.

### Diffs and selection

- Selecting a file in the picker is a **hint** that the user is looking at it, nothing more (see [decisions § CC-14](../plan/decisions.md#cc-14--file-selection-becomes-a-hint-not-a-context-contract))
- The agent decides what to read. A selected file it judges irrelevant costs nothing; an unselected file it needs is one tool call away
- Deleted files are the case that used to need special handling — no content on disk, diff only. The agent's `git show base:path` covers it without a rule
- Diffs the agent runs against the object database are unaffected by working-tree or index state during the session, which is the same property the old block relied on

### No token budget to manage

The old spec asked the user to manage a token budget through file selection, and showed "N of M diffs
in context" to support it. Both are gone: AC⚡DC does not assemble the context and cannot count it.
What the status bar shows instead is review shape — changed files, additions, deletions — and what
guards the window is the engine's own compaction. The context HUD reports usage after the fact; see
[`../3-engine/context-visibility.md`](../3-engine/context-visibility.md).

A large review is still a large review. The difference is that the agent paces itself through it with
tool calls instead of the user pacing it through batches of files — a pacing control the picker no
longer offers in any form ([CC-21](../plan/decisions.md#cc-21)).

## UI Components
### Review Mode Banner
- Displayed at the top of the file picker when review active
- Shows branch name, commit range, file/line stats, exit button
- Synchronized with review state from the review-state RPC
- A "View graph" button opens the Review History Graph modal — the same commit-graph component as the selector, rendered in read-only mode with the review's merge-base (amber) and branch tip (green) highlighted
### Git Graph Selector
- Floating resizable dialog, not modal-blocking
- File picker and chat panel remain usable underneath
- Draggable by header, resizable from edges
- Close button dismisses without starting a review
- Three zones — frozen branch legend, scrollable graph, review info / action bar
### File Picker in Review Mode
- Operates unchanged
- Staged files appear with their normal status badges
- Filter, context menu, keyboard navigation, deny-read and path insertion all work as normal
### Review Status Bar
- Slim bar above chat input
- Shows review summary — branch, commit count, files changed, additions/deletions
- Changed-file count, additions, deletions
- No "N of M diffs in context" counter — nothing about the review is injected, so there is no in-context set to count. A counter reporting a count of *picked* files under that label would be a lie about what the model can see, which is why neither the counter nor the picking survived
- Exit Review button
### Pointing at a File During Review
- No chip-per-file UI, no per-file diff-inclusion switch, and since [CC-21](../plan/decisions.md#cc-21) no checkbox either
- The user names a file in the prompt — typed, or inserted by the picker's middle-click. `@path` makes the CLI read it; a bare path names it without forcing the read
- Scales naturally to large reviews because nothing about the review grows with what the user points at
### Diff Viewer in Review Mode
- Operates unchanged
- Left side (original) — file content from HEAD (pre-review state)
- Right side (modified) — file content from disk (reviewed code)
- Standard staged-diff view

### Commit Inspection from Review History Graph
- Commit clicks in the read-only graph modal dispatch `commit-inspected` events with the commit dict
- Files-tab catches these and calls `Repo.get_diff_to_branch(sha)` to fetch the commit's diff
- The diff is routed to the diff viewer's left panel via a `load-diff-panel` window event (same mechanism the history browser uses for ad-hoc comparisons)
- The modal closes before the fetch resolves so the user sees the diff arrive in the viewer immediately
- Empty diffs surface as an info toast ("No diff available for that commit")
- The diff shown is `commit..working-tree`, not a pure parent-diff — useful during review because the user is asking "what did this commit touch in my current view's context"
### Review Snippets
- Review snippets stored alongside the code and doc preset groups in the unified snippets file
- The snippet RPC checks review state first and returns review snippets when a review is active, ahead of the active preset
- Frontend does not need to distinguish — always calls the single RPC and renders whatever is returned
- Examples — full review, security review, commit walkthrough
## Review State
Held in memory on the engine service:
- Active flag
- Branch being reviewed
- Branch tip SHA (for restoration)
- Base commit SHA
- Parent SHA (merge-base, current git HEAD during review)
- Original branch (HEAD before entry, for restoration)
- Commit list
- Changed file list with status
- Aggregate stats — commit count, files, additions, deletions
- Permission mode at entry, for restoration on exit
State is not persisted across server restarts. It is also the source for both the `get_review_state`
RPC and the `review_state` MCP tool, which project the same in-memory record for two different
audiences.

### Broadcast Events
- On successful `start_review`, the backend broadcasts `reviewStarted` with the full review-state payload, matching `get_review_state()`
- On `end_review` the backend broadcasts `reviewEnded` with the empty-state review shape (`active: false`, null fields)
- Both events are broadcast to every connected client — the frontend shell re-dispatches as `review-started` / `review-ended` window events, which the files-tab listens for
- Files-tab's handlers populate / clear the picker's `reviewState` prop (driving the banner) and trigger a file-tree reload so the picker reflects the soft-reset state
## Integration with Existing Systems
### File Picker
- No changes — staged files appear naturally with their normal badges and diff stats
### Diff Viewer
- No changes — pre-review HEAD vs reviewed disk is a standard staged-changes diff
### Symbol and Document Indexes

- Both are re-indexed on entry and on exit, because disk content changes underneath them
- The indexes always describe what is on disk, which during a review is the reviewed code. There is no second index of the pre-change state, and `symbol_map` never mixes the two
- The agent reaches pre-change structure through git, not through us

### History

- Review conversations use the same mirrored history as any other turn. Nothing about a review is special in the transcript except the system event that records entry and exit
- Compaction is the engine's, and it may compact away the early part of a long review. This is a real behaviour change worth stating plainly: the old design re-injected review context every turn precisely so compaction could not lose it. Now it can. What makes that acceptable is that the facts are recoverable — `review_state` and `git` are both one call away, and a compacted agent that needs the branch name asks for it

### Streaming

- Chat operates normally during review. No special-casing in the pump, no post-response edit step to skip, because there is no edit step

## Read-Only Mode

Review is read-only, and enforcing that is now a **permission** problem rather than a pipeline one.
This is the sharpest single consequence of the conversion in this feature.

The old enforcement was structural and total: edits reached disk only through AC⚡DC's apply step, so
skipping that step during review made writes impossible. The agent writes to disk itself now, through
the CLI, and no flag of ours sits between it and the filesystem.

So review entry sets the permission posture to a read-only one — `plan` mode, the platform's own
"read and reason, no writes, no commands" state (see
[`../3-engine/permissions.md` § Permission Mode](../3-engine/permissions.md#permission-mode)) — and
remembers the previous mode for exit. Consequences:

- The enforcement is the CLI's, which is stronger than a rule list of ours could be: it covers `Bash`, which can write files by other means, and any future write-capable tool we have not enumerated
- It is **overridable**, and honestly so. A user who switches out of `plan` mid-review can let the agent edit. The banner shows the posture next to the review branch for exactly this reason, and a mode change during review is recorded as a system event in the transcript
- The user's own writes are not restricted. Diff-viewer saves and SVG edits go through the repository layer, not the engine, and remain available — a reviewer annotating a file is not the failure mode this guards against
- Commit-message generation and the commit button stay disabled during review, as before. HEAD is at the merge-base; a commit there would be wrong regardless of who asked for it

The old "edit blocks still appear for reference" behaviour has no successor and needs none: an agent in
`plan` mode proposes changes in prose, which is what a reviewer wanted from those blocks anyway.

## Limitations

### Single Review Session

- Only one review can be active at a time
- Starting a new review exits the current one first

### No Concurrent Editing

- Since git HEAD is at a different commit during review, committing new changes is not supported
- The user should exit review mode before making commits

### Root Commits

- If the base commit is the first commit in the repository (no parent), the pre-review state is an empty tree
- Every file appears as a new addition, and `git diff` against the merge-base shows the whole repository

### Large Reviews

- Reviews with hundreds of changed files will not fit in one context window, and no arrangement of ours changes that
- The agent paces itself: `review_state` for the file list, then diffs in batches of its choosing, with the engine compacting behind it
- The user's lever is the request ("review the auth changes first"), and naming the files it should start with

### Branch Switching During Review

- Not supported
- User must exit review mode before switching branches

## Invariants

- Review entry sets a read-only permission posture and records the previous one; exit restores it even if the git restore fails
- The read-only guarantee is the engine's, and is visible in the UI rather than assumed — a user who overrides it can see that they have
- No index ever describes the pre-change state; the indexes describe disk
- Exit always restores the original branch, or leaves HEAD detached with an informative error
- Clean working tree is enforced before entry — a dirty tree can never enter review mode
- Review entry leaves the picker's deny-read rules alone; there is no selection for it to clear
- `review_state` answers with review facts while a review is active and with an explicit not-in-review result otherwise; it never fabricates
- Review state is never persisted across server restarts
- `reviewStarted` and `reviewEnded` are broadcast to every connected client whenever the server-side review state changes — frontends never infer entry/exit from the RPC return value alone
- The review-history graph is read-only — clicking a commit never mutates review state or triggers selection changes; it only drives the diff viewer's ad-hoc comparison panel