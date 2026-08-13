# History and Sessions

History splits cleanly in two, and keeping the halves separate is the whole design:

- **What the model sees** — owned entirely by Claude Code. AC⚡DC never assembles it, never counts
  its tokens, and never compacts it.
- **What the user browses** — owned by AC⚡DC. A mirrored transcript in `.ac-dc4/`, with the history
  browser, full-text search, session list, and per-message metadata that surface it.

The native engine conflated the two, and that conflation is what made its history layer complex: a
store that had to be simultaneously a browsable record and a prompt input ends up serving neither
well.

## Two Stores, One Source

| | Engine transcript | Mirrored store |
|---|---|---|
| Owner | Claude Code / SDK | AC⚡DC |
| Location | `.ac-dc4/sessions/` via our `SessionStore` | `.ac-dc4/history.jsonl` |
| Contents | The session record the SDK resumes from — the CLI's own transcript format, opaque to us | Rendered messages plus AC⚡DC metadata (image refs, selected files, request IDs, tool summaries) |
| Consumers | The engine; resume and fork | History browser, search, session list |
| Compaction | Engine-owned, automatic | Never compacted — it is an archive |

Both live under the repo's working directory, so a repo carries its own history and a clone does not
inherit someone else's.

## `SessionStore`

AC⚡DC implements the SDK's `SessionStore` protocol so a copy of the engine transcript lands inside
`.ac-dc4/` rather than only under `~/.claude/projects/`. Six methods: `append`, `load`,
`list_sessions`, `list_session_summaries`, `delete`, `list_subkeys`.

The store is a **mirror**, not the primary write path. The CLI writes its transcript to local disk
first and the SDK hands us a copy afterwards. What makes the mirror worth having is that resume reads
it back: when a session is resumed and the local file is gone, the SDK loads our copy and materialises
it for the subprocess. So the repo-local copy is what makes a session survive.

That matters because the CLI expires its own transcripts on a retention timer that knows nothing about
this repo. A session under a global per-user directory is not backed up with the repo, not removed with
it, not present on a second machine that clones it, and not inspectable alongside the rest of
`.ac-dc4/`. Putting it in the working directory makes session state a property of the repo, which is
what users already assume it is.

Implementation notes that are contracts rather than choices:

- **The mirror is flushed eagerly, and its gaps are visible.** Mirror appends are best-effort by
  construction — the SDK retries a failed batch and then reports a gap. A gap is surfaced as a banner
  and repaired by re-importing the local transcript, never left silent. Durability for the user's own
  message is the mirrored store's job, not the engine transcript's: `history.jsonl` is written before a
  turn is acknowledged.
- **The store is verified against the SDK's conformance suite**
  (`claude_agent_sdk.testing.session_store_conformance`). A store that passes locally but violates the
  protocol produces resume failures that look like context loss.
- **Summaries use the SDK's `fold_session_summary()` helper** rather than re-reading whole
  transcripts, so the session list stays fast as transcripts grow.
- **The store never rewrites history.** Append and delete only.
- **All six methods are implemented.** The SDK probes for the optional four by attribute presence; a
  missing one degrades a feature silently rather than failing.

On-disk schema in
[`../../specs-reference/3-engine/history.md`](../../specs-reference/3-engine/history.md).

## Mirrored Store

The mirror is written by the message pump as a turn progresses, one record per rendered element:

- User messages — text, image references, the framing's selected-file list, request ID, session ID.
- Assistant text and thinking blocks.
- Tool calls — name, input summary, result summary, status, duration. Summarised rather than verbatim:
  the mirror is for browsing, and a full `Read` result of a 2000-line file has no browse value.
- Turn results — cost, usage, duration, terminal reason.
- System events — mode changes, commits, resets, compaction boundaries, session switches.

Lines that fail to parse on load are skipped with a warning, as before; a partial write from a crash
must not make a session unreadable.

The mirror is what the existing `HistoryStore`, history browser, and message search already consume,
which is why they survive the conversion unchanged in shape.

## Turn Identity

Every record carries the **session ID** and the **request ID** of the turn that produced it. Together
they replace the native engine's `turn_id` — a request ID is already unique per turn and already the
correlation key everywhere else in the system, so introducing a third identifier would be gratuitous.

Records predating the conversion carry `turn_id` instead; the loader tolerates it and the browser
renders those sessions read-only. There is no migration: old sessions are historical records, and the
engine that could resume them no longer exists.

## Resume, Fork, and New

| User action | Mechanism | What the model sees |
|---|---|---|
| Server restart | Reconnect with `resume=<last session id>` | Its own prior context, including its own compactions |
| Open a session from the history browser | `resume=<that session id>` | That session's context |
| Branch from a past session | `fork_session` | A copy; the original stays intact |
| New Session | Connect without `resume` | Nothing |

Resumption is never a replay. AC⚡DC does not read the mirror and feed messages back into a prompt —
under the native engine that was the only option, and it is exactly the mechanism that produced
sessions which looked correct in the UI while the model's view had silently diverged. The SDK owns
resumption; the mirror is a record, not an input.

Consequently the history browser's "load session" button changes meaning slightly: it resumes an
engine session rather than repopulating a context. Sessions with no surviving engine transcript
(deleted, or pre-conversion) are browsable but not resumable, and are labelled as such rather than
failing on click.

**Fork is offered wherever resume is.** Forking is the safe choice when revisiting an old session —
it cannot damage the original — and the native engine had no equivalent, so it is worth surfacing
prominently rather than hiding behind a menu.

## Compaction

Claude Code compacts itself. AC⚡DC's compactor — topic-boundary detection on a smaller model,
verbatim windows, summarise-versus-truncate, minimum-exchange safeguards, tracker re-registration —
is deleted.

What remains is presentation:

- `PreCompact` broadcasts that compaction is starting, so a pause is explained rather than mysterious.
- `SystemMessage(subtype="compact_boundary")` renders as a divider in the transcript with before/after token counts and the
  trigger, so the user can see where the model's memory was condensed.
- The Context tab shows auto-compact state and the threshold, so an imminent compaction is
  predictable rather than a surprise. See [context-visibility.md](context-visibility.md).

Auto-compact can be disabled in project settings; AC⚡DC surfaces the state but does not own it.

## Subagent Transcripts

Subagent sessions are addressed via `list_subkeys` on the store (and `list_subagents()` on the
module surface), keyed by SDK agent ID. Each has its own transcript, read on demand when the user
opens a subagent tab.

The native engine's `agent_idx`-versus-`id` two-namespace problem, the `agent_blocks` field, and the
cross-turn reconstruction algorithm are all deleted: SDK agent IDs are stable, so there is nothing to
reconstruct.

Disk-usage monitoring carries over unchanged in mechanism — a one-shot warning when the session
directory crosses a threshold, dismissible, never blocking. Only the measured path moves.

## Search

Full-text search runs over the mirrored store, as before: persistent store first, in-memory fallback.
It searches user text, assistant text, and tool-call summaries. Tool *results* are excluded from the
index — searching them returns mostly file contents, which is what `Grep` is for, and it would drown
conversational hits.

## Invariants

- The model's context is never assembled by AC⚡DC; continuity is always `resume` or `fork_session`.
- Every message is durably appended to the mirrored store before the turn that produced it is
  acknowledged as complete.
- The `SessionStore` implementation passes the SDK's conformance suite.
- The store appends and deletes; it never rewrites an existing record.
- A gap in the mirrored engine transcript is always surfaced to the user; it is never tolerated
  silently, because a silent gap turns into a failed resume much later.
- Every mirrored record carries both a session ID and the request ID of its turn.
- A malformed line in the mirror is skipped on load and never makes a session unreadable.
- Sessions without a surviving engine transcript are browsable and labelled non-resumable; clicking
  them never produces an error.
- Fork is offered wherever resume is offered.
- Compaction boundaries are always visible in the transcript; a compaction never happens without a
  rendered marker.
- Subagent transcripts are keyed by SDK agent ID; no positional index appears in any path or record.
- The disk-usage warning fires at most once per server lifetime and never blocks work.
