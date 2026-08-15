# Reference: History and Sessions

**Supplements:** `specs5/3-engine/history.md`

On-disk layout, the `SessionStore` adapter's key-to-path mapping, the events-log and derived-index
schemas, and the history RPC surface. The behavioural contracts — one transcript,
resume-never-replays, append-only — are in the parent spec.

Verified against `claude-agent-sdk` **0.2.137**.

## Byte-level formats

### Working-directory layout

```
.ac-dc4/
  sessions/
    <project_key>/
      <session-uuid>.jsonl              — engine transcript, mirrored from the CLI
      <session-uuid>.summary.json       — SessionSummaryEntry sidecar
      <session-uuid>/
        subagents/
          agent-<agent-id>.jsonl        — subagent transcript
  events.jsonl                          — AC⚡DC's own operational events
  index/                                — derived search / summary / request-ID index (rebuildable)
```

`history.jsonl` and `images/` are gone — [CC-19](../../specs5/plan/decisions.md#cc-19). A directory
written by the native engine keeps both; nothing reads them.

The `<project_key>` level exists even though AC⚡DC is single-repo: the SDK's key includes it, and
worktrees of the same repo produce different keys. Flattening it would make two worktrees collide.

### `project_key` derivation

Use the SDK's own helper — `claude_agent_sdk._internal.sessions.project_key_for_directory(cwd)` — not
a sanitiser of our own. The key is a sanitised absolute path; paths over 200 characters are truncated
and suffixed with a portable djb2 hash so the same path yields the same key across runtimes. A
hand-rolled equivalent that disagrees by one character makes live-mirrored and imported sessions land
in different buckets and silently halves the session list.

### `SessionKey` → path mapping

```pseudo
SessionKey:
    project_key: string
    session_id: string          // canonical lowercase UUID v4
    subpath: string?            // omitted for the main transcript; "subagents/agent-<id>" for subagents
```

```
subpath absent  → sessions/<project_key>/<session_id>.jsonl
subpath present → sessions/<project_key>/<session_id>/<subpath>.jsonl
```

`subpath` is opaque to the adapter and is used as a storage-key suffix. An **empty string is invalid** —
the field is omitted for main transcripts, and an empty `subpath` must be rejected rather than treated
as absent. Before joining, reject any `subpath` containing `..`, a leading `/`, a backslash, or a NUL:
it arrives from the SDK, but it becomes a filesystem path, and a path-traversal check at the boundary
costs nothing.

`session_id` gets the **same path-safety check and no more** — reject `..`, separators, and NUL; do
*not* require a UUID. The conformance suite appends under `"sess"`, `"a"` and `"summ-sess"`, so a store
that insists on UUIDs fails contracts 1 through 14 without ever reaching a real session. UUID
validation belongs at the RPC boundary, where a session ID arrives from a browser, and the SDK's own
`*_from_store` readers already apply it there.

### Engine transcript line format

One JSON object per line, UTF-8, newline-terminated. Entries are **pass-through blobs** — the CLI's
on-disk transcript union. The only required invariant is a `json.dumps`/`json.loads` round-trip:

| Field | Type | Notes |
|---|---|---|
| `type` | string | Required; the discriminator, uninterpreted by us |
| `uuid` | string | Usually present. Treat as an **idempotency key** — upsert or ignore duplicates |
| `timestamp` | string | ISO 8601 |
| … | any | Additional fields are opaque and must be passed through unchanged |

Entries **without** a `uuid` (titles, tags, mode markers) are appended without dedup. Returned entries
must be deep-equal to what was appended; byte-equal serialisation is not required, so key reordering is
acceptable and no hashing or byte comparison may be relied on.

`compact_boundary` entries carry `logicalParentUuid`; the SDK's own resume path intentionally ignores
it, and so must we.

### Summary sidecar format

`<session-uuid>.summary.json`, one JSON object, produced by `fold_session_summary()`:

```pseudo
SessionSummaryEntry:
    session_id: string
    mtime: integer          // Unix epoch MILLISECONDS, storage write time
    data: object            // opaque SDK-owned state — persist verbatim, never interpret
```

`mtime` rules, all load-bearing:

- Stamp it from **the same value `list_sessions` reports for that session** — for our file-backed store,
  `int(st_mtime * 1000)` of the transcript we just appended to. Sharing one filesystem value is stronger
  than sharing a clock: the two sides cannot drift, the sidecar needs no second write to re-stat itself,
  and a crash between the transcript write and the sidecar write leaves a sidecar strictly older than
  the transcript — which is exactly how "stale" is meant to look. The obvious alternative, statting the
  sidecar after writing it, costs a second write per ~100 ms batch and can invert the comparison on a
  filesystem whose timestamp granularity rounds.
- Never derive it from entry timestamps. Batched writes always commit later than the last entry's
  timestamp, which makes every sidecar look stale and defeats the fast-path staleness check in
  `list_sessions_from_store`.
- The comparison that has to hold is the conformance suite's: `summary.mtime >= list_sessions` mtime
  for the same session. Taking both from the transcript makes it hold with equality, by construction.
- `fold_session_summary()` preserves whatever `mtime` it is given via `prev` and returns `mtime=0` for a
  new session; overwriting it is the adapter's job.

Skip the fold entirely for keys that have a `subpath` — subagent transcripts must not contribute to the
main session's summary. Guard with `if key.get("subpath") is None:`.

Sidecar writes are read-fold-write, so they must be serialised per session (an `asyncio.Lock` keyed by
session ID is sufficient for a single-process store). `fold_session_summary()` is pure; concurrency
control is entirely ours.

### `list_sessions` return

```pseudo
SessionStoreListEntry:
    session_id: string
    mtime: integer          // Unix epoch milliseconds
```

Result order is unspecified — the SDK sorts by `mtime` descending, so ours need not.

### Events log (`events.jsonl`) record schema

One record per line, UTF-8, append-only. This file holds **only** what the transcript never contained:
AC⚡DC's own operational events. Messages, tool calls and turn results are not duplicated here — they
are read from the transcript through the SDK's parsers, and their browse rendering is built at read
time.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | ✓ | `{epoch_ms}-{uuid8}` — dash separator, carried over unchanged |
| `session_id` | string | ✓ | SDK session UUID |
| `request_id` | string | — | The turn this event belongs to. Absent for events outside a turn (a commit from the toolbar, a preset switch between turns) |
| `timestamp` | string | ✓ | ISO 8601 UTC, microsecond precision |
| `event` | string | ✓ | Discriminator: `commit`, `reset`, `review_start`, `review_end`, `preset_switch`, `permission_mode`, `session_switch`, `files_written_by_file_tools` |
| `content` | string | ✓ | The rendered line the browser shows. Never null, may be empty |
| `payload` | object | — | Event-specific fields; see below |

`payload` by event:

| Event | Payload |
|---|---|
| `commit` | `{sha, message, files: list[string]}` |
| `reset` | `{to: "HEAD", files: list[string]}` |
| `review_start` / `review_end` | `{base, head, files: list[string]}` |
| `preset_switch` | `{from, to}` |
| `permission_mode` | `{from, to, source: "user" \| "engine"}` |
| `session_switch` | `{action: "resumed" \| "forked", session_id, forked_from?}` — `session_id` is the session now live, so it is **null for a fork** (see *RPC surface*); the record's own `session_id` field is the session switched away from, which for a resume is the same session |
| `files_written_by_file_tools` | `{paths: list[string]}` |

**The `files_written_by_file_tools` name is binding**, per
[CC-18](../../specs5/plan/decisions.md#cc-18). Both available sources — the re-indexer's queue and the
result message's `files_modified` — see only `Write`, `Edit`, `MultiEdit` and `NotebookEdit`, so a file
changed by `Bash` is absent from this record. A field called `files_changed` would be a durable claim
this system cannot make.

There is no `image_refs` field, no `images` count, no `files`, and no `turn_id`: images live in the
transcript entry that carried them, the framing's selected-file list is part of the user message the
engine received, and the request ID replaced `turn_id`.

### Derived index (`index/`) layout

Rebuildable from `sessions/` in full, so its format is **not** an interop boundary and may change
without migration. It exists to answer three questions without reading every transcript:

| Purpose | Key | Value |
|---|---|---|
| Search | term | postings: `(session_id, entry_uuid, kind)` |
| Session list | `session_id` | cached summary fields for sorting and filtering |
| Turn correlation | `request_id` | `(session_id, first_entry_uuid, last_entry_uuid)` |

Tool **results** are never indexed — the transcript holds them verbatim because the protocol requires
it, and the indexer declines to read them. Searching them would return mostly file contents, which is
what `Grep` is for.

A missing or stale index is a performance problem, never a correctness one: search falls back to a
transcript scan, and the index is rebuilt in the background.

### Browse rendering comes from the transcript

What the history browser shows for a message, a tool call, or a turn result is derived at read time
from parsed entries:

| Rendered element | Source |
|---|---|
| User text, assistant text, thinking | `get_session_messages_from_store()` on the session's entries |
| Tool card — name, input summary, status, duration, `agent_id` | The `tool_use` / `tool_result` entries; summaries are built for the card, never stored |
| Turn footer — usage, `num_turns`, `duration_ms` | Reconstructed. **There is no result entry in the transcript** (see below) |
| Turn footer — `total_cost_usd`, `terminal_reason` | Unavailable. Reported as absent, so a browsed turn shows no cost figure and no terminal badge |
| System-event cards | `events.jsonl` |

**No result entry.** The store is a mirror written *after* the CLI's own transcript write — the SDK's
`transcript_mirror_batcher` coalesces `transcript_mirror` frames and calls `store.append()` with exactly
the entries the CLI wrote. The CLI does not write a result entry, verified against real transcripts, so
the store cannot hold one. A reopened turn's footer is therefore rebuilt from what is there:

- **usage** — each assistant message's own `usage`, deduplicated by `message.id` and summed per model.
  The dedup is load-bearing: the CLI writes **one entry per content block** and repeats the whole
  `usage` object on every entry it split a message into, so summing entries multiplies a turn's tokens
  by its block count.
- **`num_turns`** — distinct assistant `message.id`s in the turn.
- **`duration_ms`** — the timestamp span from the prompt entry to the turn's last entry, which is the
  wall clock the user waited through. Per-tool durations are the span from the `tool_use` entry to its
  `tool_result` entry.
- **`total_cost_usd`** — absent. The CLI derives it from a pricing table we do not have, and it is null
  under subscription billing regardless. `$0.00` would be a claim; nothing is the truth.
- **`terminal_reason`** — absent. Nothing in the transcript records why a turn ended, and a "completed"
  badge on no evidence is worse than no badge.

### System-event content templates

The `content` field of an `events.jsonl` record. Templates for commit, reset and mode switch are
carried over from the native engine unchanged, so a user's history reads consistently across the
conversion.

**Compaction boundary** — the one exception: it is the engine's event, arrives in the transcript as
`SystemMessage(subtype="compact_boundary")`, and is rendered from there. It is never written to
`events.jsonl`, because a record we wrote would be a second account of something the transcript
already states.

```
**Context compacted** — {trigger}

{pre_tokens} → {post_tokens} tokens
```

**Permission mode change**

```
Permission mode set to **{mode}**.
```

**Session switch**

```
{Resumed|Forked} session `{session_id}`.
```

The native engine's compaction templates (truncate/summarize cases, `<details>` summary block, the
`[History Summary]` message pair) are deleted along with the compactor.

### Our session summary shape

Returned by `history_list`. Served from the derived index, and recomputable from the transcript when the
index is cold:

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | |
| `timestamp` | string | First entry's timestamp |
| `message_count` | int | User and assistant messages, not entries |
| `preview` | string | First ~100 chars of the first user message |
| `first_role` | string | |
| `resumable` | bool | The store holds loadable entries for this ID and it is a valid UUID. False ⇒ browsable, labelled non-resumable |
| `total_cost_usd` | float \| null | **Always null.** There are no result entries to sum (see *Browse rendering*), and the field is kept only because the session row reads it |

`engine_session` is gone: with one store, a session that exists is an engine session. What used to
distinguish them — a browsable record with no transcript behind it — can now only arise from a deleted
or unreadable transcript, which is what `resumable: false` reports.

## Numeric constants

| Constant | Value | Notes |
|---|---|---|
| Disk-usage warning threshold | 1 GiB over `.ac-dc4/sessions/` | One-shot per server lifetime, dismissible, never blocking. Checked at startup and after each turn. Carried over from the agent-archive warning; only the measured path changed |
| `session_store_flush` | `"eager"` | Batched flushing (the default) can hold a turn's tail until the result message, which makes a crash lose the visible tail of an in-progress turn |
| Mirror append retries | 3 attempts, short backoff | SDK-side. Then dropped and surfaced as `MirrorErrorMessage` |
| `load_timeout_ms` | 60 000 | Per `load()` / `list_subkeys()` during resume materialization |
| Batched-flush ceilings (informational) | 500 entries / 1 MiB | What `"batched"` would have used; relevant when reading SDK logs |
| Search result cap | unchanged from the native engine | Applies to index-backed search and to the fallback transcript scan alike |

## Schemas

### RPC surface — `ClaudeCodeService`

Sessions:

| Method | Arguments | Return |
|---|---|---|
| `new_session` | — | `{session_id: null, status: "new"}` |
| `resume_session` | `session_id: str, fork?: bool` | `{session_id: str \| null, forked_from?: str}` or `{error: str, reason: str}` |

`resume_session` with `fork: true` issues a **new** session ID and leaves the original untouched;
the response carries `forked_from` so the UI can label the fork.

**A new session's ID is not knowable when the call returns.** The CLI mints it and reports it in the
init message of the first turn; `ClaudeSDKClient` exposes no accessor for it before then. So
`new_session` returns a null `session_id` (with `status` to distinguish success from the error shape),
and so does a forked `resume_session`. A plain resume knows its ID, because it passed it in. The
browser learns a minted ID from the `sessionStarted` event the first turn emits — which is also why
`new_session` does not connect the engine: there is nothing to gain by connecting early and a CLI
subprocess to pay for.

Both are **localhost-only**, and both refuse while a turn is running (`reason: "turn_in_progress"`)
rather than interrupting it: pulling the session out from under a live turn loses its tail, and the
user can cancel first. `resume_session` verifies the transcript renders before attaching, so a session
that is browsable but not resumable returns `reason: "not_resumable"` instead of leaving the user in
front of an engine that will not start.

There is no `list_engine_sessions` and no `delete_engine_session`. Under two stores those were the
store-side listing and deletion, while `history_list_sessions` and a browser-side delete covered the
browsable records. With one store each pair is two names for one operation, and two listings that can
disagree about which sessions exist is precisely what
[CC-19](../../specs5/plan/decisions.md#cc-19) removes. `history_list` and `history_delete` are the
single listing and the single deletion.

History — the whole set is renamed off the native engine's names, because none of the shapes survive:

| Method | Arguments | Return |
|---|---|---|
| `history_list` | `limit?: int` | `list[SessionSummary]` (above) |
| `history_load` | `session_id: str` | `list[MessageDict]` — built at read time from parsed entries, interleaved with that session's `events.jsonl` records. Image blocks carry pointers, not data URIs |
| `history_search` | `query: str, role?: str, limit?: int` | `list[{session_id, entry_uuid, role, content_preview, timestamp}]` |
| `history_delete` | `session_id: str` | `{status: str}` — localhost-only. Deletes the transcript, its summary sidecar, its subagent transcripts and its events; the images in those entries go with them |
| `history_image` | `session_id: str, entry_uuid: str, block: int` | `{data_uri: str}` or `{error: str}` — how a thumbnail or lightbox fetches bytes that no broadcast carried |

`history_list`, `history_load` and `history_delete` are the names phase 1 chose and
`test_phase_five_methods_are_absent` has asserted absent ever since; `history_search` keeps its name
because there was nothing wrong with it, and `history_image` is new.

Renaming rather than keeping the native names is the loud option, and the right one here: every payload
changed, so a browser still calling `history_list_sessions` should fail with a method-not-found at the
call site rather than parse a shape it no longer understands.

Two field-level renames run through the shapes above: `message_id` becomes `entry_uuid`, because the
transcript's own `uuid` is already the stable identifier for a line and minting a second one over the
top of it would be a name for the same thing that can disagree with it; and `engine_session` is gone
per the summary shape above.

Subagents:

| Method | Arguments | Return |
|---|---|---|
| `list_subagent_transcripts` | `session_id?: str` | `list[{agent_id, subpath, task_id?, description?}]` |
| `get_subagent_transcript` | `agent_id: str, session_id?: str` | `list[SessionStoreEntry]` or `{error: str}` |

`session_id` defaults to the active session. Deleted RPCs: `get_turn_archive`, `get_agent_history`,
`close_agent_context`, `set_agent_selected_files`, `load_session_into_context`,
`get_history_status`, and the native trio `history_list_sessions` / `history_get_session` /
`history_search`, the first two by rename and the third by shape.

### Subagent keys

Subagent transcripts are addressed by SDK agent ID via `list_subkeys(key)`, whose key argument has
**no** `subpath`:

```pseudo
SessionListSubkeysKey:
    project_key: string
    session_id: string
```

It returns subpath strings (`"subagents/agent-<agent-id>"`). `list_subagents(session_id)` is the
module-level equivalent over local disk; `list_subagents_from_store` is the store-backed variant.

The native engine's `agent_idx` positional namespace, the `agent_blocks` record field, and the
cross-turn reconstruction algorithm have no counterpart here: agent IDs are stable, so the ID is both
identity and storage routing. No positional index appears in any path or record.

### Conformance harness

```python
from claude_agent_sdk.testing import run_session_store_conformance
```

Run against a temp-directory instance of our store as part of the normal test suite, with a fresh
directory per contract. A store that passes locally but violates the protocol produces resume failures
that present as context loss, which is close to undiagnosable from the UI.

Pair it with an explicit assertion that all four optional methods resolve to our overrides and not to
the Protocol's defaults — see § The conformance harness ships with the SDK for why a green run does not
establish that on its own.

## Dependency quirks

### The store is a mirror, not the primary write path

`SessionStore.append()` is called **after** the subprocess's local write succeeds — durability is
already guaranteed on local disk under `CLAUDE_CONFIG_DIR`, and batches arrive at roughly 100 ms
cadence during a turn. Our appends cannot be made synchronous with the turn, and a failed batch is
retried three times and then dropped with a `MirrorErrorMessage`; timeouts are not retried, because the
in-flight call may still land.

Consequences:

- **No pre-acknowledgement durability exists anywhere.** The parent spec's old invariant leaned on our
  own `history.jsonl`, written before the turn was acknowledged; with that store retired
  ([CC-19](../../specs5/plan/decisions.md#cc-19)) the earliest a message is durable in `.ac-dc4/` is
  the first eager flush *during* the turn. The browser's `input-history.js` covers the window.
- `MirrorErrorMessage` (a `SystemMessage` subclass, `subtype: "mirror_error"`, fields `key` and
  `error`) must be surfaced — it means the repo-local copy has a hole. `EngineHealth.mirror_gaps`
  counts them, and `import_session_to_store()` re-imports the local file to repair one.
- Within a process, entries must be persisted in append-call order.

### Resume from the store works, via materialization

When `resume` is paired with `session_store` and the local file is absent, the SDK calls `load()`,
writes the entries to a temporary directory laid out like `~/.claude/`, and points the subprocess at it
via `CLAUDE_CONFIG_DIR` (`_internal/session_resume.py`). Resume from our repo-local copy therefore
works even after the CLI's own `cleanupPeriodDays` sweep has removed the original — which is the
strongest practical reason to implement the store at all.

`load()` is called once, in the parent process, before subprocess spawn. It must return the **whole**
session; there is no streaming variant, and it is bounded by `load_timeout_ms`.

### Adopting sessions that already exist locally

`import_session_to_store(session_id, store, directory=..., include_subagents=True)` streams an existing
`~/.claude/projects/<dir>/<session>.jsonl` into the store, batching by 500 entries / 1 MiB. The
destination `project_key` is the on-disk project directory name, so an imported session is
indistinguishable from a live-mirrored one and resumable from the original `cwd`. Because `uuid` is the
idempotency key, re-import is duplicate-safe — which is what makes it a repair tool as well as a
migration tool.

### Optional methods are probed, not type-checked

Only `append` and `load` are required. The SDK probes for the rest at runtime by attribute presence and
never uses `isinstance`, so the store need not subclass the Protocol. Inheriting the Protocol's default
methods (which raise `NotImplementedError`) marks a method as *absent*, so a store that inherits and
forgets to override `list_sessions` silently loses session listing rather than failing loudly.
Implement all six.

### The conformance harness ships with the SDK

`claude_agent_sdk.testing.run_session_store_conformance(make_store, *, skip_optional=frozenset())`
asserts 14 behavioural contracts. `make_store` is invoked **once per contract** — it may be sync or
async — so each contract gets an isolated store and a test cannot pass by leaking state from the
previous one.

The trap is in the same place as the probe rule above: contracts for the four optional methods
(`list_sessions`, `list_session_summaries`, `delete`, `list_subkeys`) are skipped **either** when named
in `skip_optional` **or** when the store does not override that method. A store missing three of the
six therefore reports a green run over 10 contracts, and nothing in the output distinguishes that from
a complete implementation. The gate is "14 contracts asserted with an empty `skip_optional`", not "the
harness passed".

`InMemorySessionStore` is exported from the top-level package and implements all six methods plus
`clear`, `get_entries`, and `size`. It is the reference to diff our behaviour against when a contract
fails and the fault is ambiguous between our store and our reading of the protocol.

### Deletion is ours to police

The SDK never deletes from the store unless `delete_session_via_store()` is called. Retention is the
adapter's responsibility; local transcripts under `CLAUDE_CONFIG_DIR` are swept independently by the
CLI's `cleanupPeriodDays` setting. AC⚡DC does not expire sessions automatically — it warns at the
threshold above and offers explicit deletion.

Deleting a main-transcript key also removes that session's summary sidecar and its subagent directory:
a subagent transcript whose parent is gone is unreachable through every RPC we expose.
