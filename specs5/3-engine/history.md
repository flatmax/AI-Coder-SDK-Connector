# History and Sessions

History splits cleanly in two, and keeping the halves separate is the whole design:

- **What the model sees** — owned entirely by Claude Code. AC⚡DC never assembles it, never counts
  its tokens, and never compacts it.
- **What the user browses** — owned by AC⚡DC. The same transcript, mirrored into `.ac-dc4/` by our
  `SessionStore`, surfaced by the history browser, full-text search and session list.

The split is about *roles*, not about copies. One transcript serves both: the engine resumes from it and
the browser reads it, and neither job is allowed to reshape it for the other's convenience. The native
engine conflated the roles instead — a store that was simultaneously a browsable record and a prompt
input served neither well, and reading it back into a prompt is precisely how a session came to look
correct in the UI while the model's view had diverged.

## One Store, One Index, One Events Log

An earlier draft of this spec described two transcripts: the engine's, and a rendered mirror of our own
that the message pump wrote in parallel. [CC-19](../plan/decisions.md#cc-19) collapses that to one
transcript, because the SDK documents `SessionStoreEntry` as a pass-through blob — a store cannot
impose a record shape, so a second writer buys a second shape that can disagree with the first.

| | Engine transcript | Derived index | Events log |
|---|---|---|---|
| Owner | Claude Code / SDK, stored by us | AC⚡DC | AC⚡DC |
| Location | `.ac-dc4/sessions/` via our `SessionStore` | `.ac-dc4/` | `.ac-dc4/events.jsonl` |
| Contents | The session record the SDK resumes from — the CLI's own transcript format, opaque to us, stored verbatim | Search terms, session summaries, request ID ↔ session mapping | Our operational events: commit, reset, review entry and exit, session resume and fork, permission-mode change, preset switch |
| Derived from | Nothing — it is the source | The transcript, entirely | Nothing — the transcript never held these |
| If deleted | The session is unresumable | Rebuilt on next start | Those events are gone; no session breaks |
| Compaction | Engine-owned, automatic | N/A | Never — it is an archive |

All three live under the repo's working directory, so a repo carries its own history and a clone does
not inherit someone else's.

**The store is never given an entry the CLI did not write.** `load()` hands the store's contents back
to a subprocess that parses its own union, so a record we invented would surface as a resume failure —
which presents as context loss, much later, in a session the user cares about. That is why our own
events are a separate file rather than namespaced entries.

**Each event is written by whoever performed it, at the moment it happens.** The alternative — one
observer deriving events from state changes it notices — cannot see what the change destroyed, and
that is the interesting half. A commit's record names the files it contained, read while they are
still staged; a reset's names the files it discarded, read before they are gone. So the ordering is
part of the record rather than an implementation detail, and the reset's is written *before* the caller
is told the reset succeeded: it is the only surviving trace of that work, and a fire-and-forget task
that lost a race would lose the work silently.

**An event that does not appear in the log is not visible anywhere.** The engine's transcript never
mentions a commit, a reset, a review, or a mode switch, so a session browsed without these records
shows an agent that went read-only and came back for no reason, or one that edited files without ever
asking. This is also why the permission-mode record names its `source`: "accept edits from now on"
checked in a permission dialog changes the posture for every later tool call, and it is the change a
reader is least likely to be able to account for.

**Nothing is archived that the transcript already states.** Compaction boundaries arrive as
`SystemMessage(subtype="compact_boundary")` and render from there. Written files are `Write`/`Edit`
tool calls the turn footer already reconstructs its file list from. A second account of either is a
second thing to keep true.

**Deleting a session is one operation across all three.** The store takes the transcript with its summary
sidecar and its subagent transcripts — and with them the pasted images, which live in the entries and
nowhere else. The events log drops that session's records, because an archived commit outliving the
session it describes renders in the browser as history for a session that no longer exists. The index
forgets it. In that order, so that a crash between steps leaves either something unreachable or something
self-healing; the reverse order leaves a browsable session whose events have silently gone.

**The session on screen is not deletable.** The store is a live mirror, so the CLI keeps appending to
whatever session it is attached to: the transcript would come straight back, and the next connect would
resume an ID with nothing behind it. That is refused rather than half-done, and starting a new session
first makes it deletable.

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

- **Entries are stored verbatim.** Round-tripping `json.dumps`/`json.loads` is the only invariant the
  protocol requires, and it is the only one we rely on. Pasted images therefore sit in the transcript as
  base64, which is why a session with several screenshots produces a multi-MB file; if that becomes a
  problem the fix is extraction on `append` with rehydration on `load`, inside the store and invisible
  to everything else.
- **The mirror is flushed eagerly, and its gaps are visible.** Mirror appends are best-effort by
  construction — the SDK retries a failed batch and then reports a gap. A gap is surfaced as a banner
  and repaired by re-importing the local transcript, never left silent.
- **Nothing here provides pre-acknowledgement durability.** The CLI writes locally first and the SDK
  hands us a copy afterwards, in eager batches at roughly 100 ms cadence, so the user's message becomes
  durable *during* the turn rather than before it is accepted. What covers the gap is the browser's own
  input history, which keeps typed text independently of any of this.
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

## What the Browser Reads

The history browser and the session list read the transcript through the SDK's own parsers —
`get_session_messages_from_store()` and the other `*_from_store` functions — never the raw entries. The
concrete entry shape is the CLI's internal discriminated union; it is exactly the kind of thing that
changes underneath a reader, and the SDK ships the parser for it.

Rendering happens at read time, not write time:

- User messages, assistant text and thinking blocks come from parsed `SessionMessage`s.
- Tool calls are summarised for display when the card is built — name, input summary, status, duration.
  A full `Read` result of a 2000-line file has no browse value, but summarising it *into storage* would
  be a second version of the truth; summarising it into a card is just rendering.
- Turn footers are **reconstructed**, not read. The transcript holds no result entry — verified against
  real CLI-written transcripts, and structurally guaranteed: the store is a mirror of the CLI's own
  transcript writes, so it receives exactly the entries the CLI writes and nothing else. So usage comes
  from each assistant message's own `usage` field, deduplicated by `message.id` (the CLI writes one
  entry per content block and repeats `usage` on every one of them) and summed per model; tool-call
  counts from the `tool_use` blocks; duration from the timestamp span between the prompt and the turn's
  last entry. **Cost and terminal reason are unavailable and are reported as absent** rather than
  guessed — a browsed turn shows no cost figure and no terminal badge. Cost is derived by the CLI from a
  pricing table we do not have and is null under subscription billing anyway; a "completed" badge on no
  evidence would be worse than no badge.
- Our own operational events come from `events.jsonl` and are interleaved by session ID and request ID.
- **Image blocks are rendered as pointers, never as bytes.** Entries hold pasted images verbatim as
  base64, so a loaded session that inlined them would send megabytes to every client on every open and
  again on every reconnect. A pointer names the session, the entry uuid and the block index, and
  `history_image` resolves one when the user actually looks at it.

A line that fails to parse is skipped with a warning: a partial write from a crash must not make a
session unreadable. This applies to all three files.

## The Derived Index

Search, the session list, and request-ID correlation are served by an index under `.ac-dc4/` built from
the transcript. It holds no content of its own, which is the point: it can be stale, and a stale index
is repaired by rebuilding it, whereas a second transcript that disagrees with the first has no repair
that does not involve choosing a winner.

- **Rebuildable on demand and on schema change.** Deleting it is a supported operation. It is not
  backed up, not migrated, and never the answer to "what happened in this session".
- **The session list is fed by `list_session_summaries`**, which the store maintains incrementally with
  `fold_session_summary()`; the index caches what the list needs to sort and filter without a read.
- **Request ID ↔ session mapping lives here** rather than in the store, because entries are
  pass-through and cannot carry a field of ours.

## Turn Identity

The **session ID** and the **request ID** together replace the native engine's `turn_id` — a request ID
is already unique per turn and already the correlation key everywhere else in the system, so introducing
a third identifier would be gratuitous.

Where each lives is now a consequence of the single store. Transcript entries carry the CLI's own
identifiers and nothing of ours, so the request ID is not *in* the transcript: the derived index maps a
request ID to its session and the entries it produced, and every record in `events.jsonl` carries both
IDs directly. Correlation is therefore a lookup, not a field — and if the index is lost, correlation is
rebuilt with it.

Pre-conversion history is not read at all. `history.jsonl` from the native engine is left on disk and
ignored; there is no loader for it, no migration, and no browsable view. Those sessions were resumable
only by an engine that no longer exists.

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

A subagent tab is rendered the same way the main transcript is, from the same code, so what the RPC
returns is a message list rather than the store's own entries. The session's operational events are not
interleaved into it: an event belongs to the session, and filing a commit under whichever subagent was
running at the time would say something that is not true. What the listing can offer alongside each
agent ID depends on how the session reached the store — the CLI's `agent-<id>.meta.json` sidecar
(`agentType`, `description`, `toolUseId`) reaches a live mirror as a synthetic `agent_metadata` entry
but is absent from the transcript it imports from disk, so those fields are reported when present and
omitted when not. The prompt the subagent was given is always available, and is the same information a
description summarises. See [`../../specs-reference/3-engine/history.md`](../../specs-reference/3-engine/history.md) § Subagents.

Disk-usage monitoring carries over unchanged in mechanism — a one-shot warning when the session
directory crosses a threshold, dismissible, never blocking. Only the measured path moves.

## Search

Full-text search runs over the derived index, with a scan of the transcript as the fallback when the
index is missing or being rebuilt. It searches user text, assistant text, and tool-call *names and
inputs*. Tool **results** are excluded — searching them returns mostly file contents, which is what
`Grep` is for, and it would drown conversational hits. The exclusion is now an indexing rule rather
than a storage one: the transcript holds results verbatim because it must, and the index declines to
read them.

## Invariants

- The model's context is never assembled by AC⚡DC; continuity is always `resume` or `fork_session`.
- **The store is never given an entry the CLI did not write**, and entries are stored byte-faithful:
  `json.loads(json.dumps(entry)) == entry`.
- There is exactly one transcript. Anything else under `.ac-dc4/` is either derived from it and
  rebuildable, or holds only what the transcript never contained.
- Every message reaches the store during the turn that produced it; typed text that has not reached it
  yet is held by the browser's input history, not by a second store.
- The `SessionStore` implementation passes the SDK's conformance suite.
- All six protocol methods exist, because the SDK probes the optional four by attribute presence.
- The store appends and deletes; it never rewrites an existing record.
- Deleting a session takes its events and its index rows with it, and the session the engine is attached
  to — or would attach to next — is never deletable.
- A gap in the mirrored engine transcript is always surfaced to the user; it is never tolerated
  silently, because a silent gap turns into a failed resume much later.
- Every record in `events.jsonl` carries both a session ID and the request ID of its turn; for
  transcript entries that correlation is a lookup in the derived index.
- An operational event is recorded by the action that performed it, and an action that failed records
  nothing. What a write destroyed or contained is read before the write, and a reset's record is on disk
  before the reset is reported as done.
- Nothing reaches `events.jsonl` that the transcript already states — no compaction boundaries, no
  written-file lists.
- A malformed line in any of the three files is skipped on load and never makes a session unreadable.
- Sessions without a surviving engine transcript are browsable and labelled non-resumable; clicking
  them never produces an error.
- Fork is offered wherever resume is offered.
- Compaction boundaries are always visible in the transcript; a compaction never happens without a
  rendered marker.
- Subagent transcripts are keyed by SDK agent ID; no positional index appears in any path or record.
- The disk-usage warning fires at most once per server lifetime and never blocks work.
