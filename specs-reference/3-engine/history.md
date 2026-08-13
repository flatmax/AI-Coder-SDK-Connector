# Reference: History and Sessions

**Supplements:** `specs5/3-engine/history.md`

On-disk layout for both stores, the `SessionStore` adapter's key-to-path mapping, the mirrored record
schema, and the history RPC surface. The behavioural contracts — two stores, resume-never-replays,
append-only — are in the parent spec.

Verified against `claude-agent-sdk` **0.2.136**.

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
  history.jsonl                         — AC⚡DC mirrored store (browsable record)
  images/                               — unchanged
```

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

`session_id` is validated as a UUID before it becomes a path component.

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

- Stamp it **after** persisting the sidecar, from the same clock source as `list_sessions`' `mtime`
  (for a file-backed store: `int(st.st_mtime * 1000)`).
- Never derive it from entry timestamps. Batched writes always commit later than the last entry's
  timestamp, which makes every sidecar look stale and defeats the fast-path staleness check in
  `list_sessions_from_store`.
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

### Mirrored store (`history.jsonl`) record schema

One record per line, UTF-8. Superset of the native-engine schema, so the existing loader, history
browser, and search keep working.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | ✓ | `{epoch_ms}-{uuid8}` — unchanged, dash separator |
| `session_id` | string | ✓ | SDK session UUID for post-conversion records; `sess_…` for older ones |
| `timestamp` | string | ✓ | ISO 8601 UTC, microsecond precision |
| `role` | `"user"` \| `"assistant"` | ✓ | Retained for the existing browser and search paths |
| `content` | string | ✓ | Rendered text; never null, may be empty |
| `kind` | string | — | New discriminator: `user`, `assistant`, `thinking`, `tool`, `result`, `system`. **Absent on pre-conversion records** — readers derive it from `role` and `system_event` |
| `request_id` | string | — | The turn. Absent on pre-conversion records |
| `block_id` | string | — | Correlates with the streamed block |
| `system_event` | bool | — | Unchanged; omitted when false, absence means false |
| `image_refs` | list[string] | — | Filenames in `.ac-dc4/images/`; omitted when empty |
| `images` | int | — | Legacy count. Tolerated, never written |
| `files` | list[string] | — | Framing's selected-file list (user records) |
| `files_modified` | list[string] | — | Paths changed by tool calls (assistant and tool records) |
| `turn_id` | string | — | Pre-conversion only. Read, never written |

`kind: "tool"` records add:

| Field | Type | Notes |
|---|---|---|
| `tool_name` | string | |
| `tool_use_id` | string | |
| `tool_input_summary` | string | ≤ 200 chars |
| `tool_result_summary` | string | Truncated per the session twin's limits — the mirror is for browsing, not for replaying tool output |
| `tool_status` | `"ok"` \| `"error"` | |
| `duration_ms` | int | |
| `agent_id` | string \| null | Non-null when a subagent made the call |

`kind: "result"` records add `usage`, `model_usage`, `total_cost_usd`, `num_turns`, `duration_ms`,
`terminal_reason`, `is_error`, `permission_prompts` — the same values as
`StreamCompleteResult`, so a reopened session shows the same footer it showed live.

`kind: "system"` records use `role: "user"` with `system_event: true`, as before. Content templates for
commit, reset, and mode switch are unchanged. New templates:

**Compaction boundary**

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

Returned by `history_list_sessions`. Computed on demand, not persisted:

| Field | Type | Notes |
|---|---|---|
| `session_id` | string | |
| `timestamp` | string | First message's timestamp |
| `message_count` | int | |
| `preview` | string | First ~100 chars of the first message |
| `first_role` | string | |
| `engine_session` | bool | An engine transcript exists for this ID |
| `resumable` | bool | `engine_session` and the ID is a valid UUID. False ⇒ browsable, labelled non-resumable |
| `total_cost_usd` | float \| null | Sum over the session's result records; null when every record has a null cost |

## Numeric constants

| Constant | Value | Notes |
|---|---|---|
| Disk-usage warning threshold | 1 GiB over `.ac-dc4/sessions/` | One-shot per server lifetime, dismissible, never blocking. Checked at startup and after each turn. Carried over from the agent-archive warning; only the measured path changed |
| `session_store_flush` | `"eager"` | Batched flushing (the default) can hold a turn's tail until the result message, which makes a crash lose the visible tail of an in-progress turn |
| Mirror append retries | 3 attempts, short backoff | SDK-side. Then dropped and surfaced as `MirrorErrorMessage` |
| `load_timeout_ms` | 60 000 | Per `load()` / `list_subkeys()` during resume materialization |
| Batched-flush ceilings (informational) | 500 entries / 1 MiB | What `"batched"` would have used; relevant when reading SDK logs |
| Search result cap | unchanged from the native engine | Mirrored store only |

## Schemas

### RPC surface — `ClaudeCodeService`

Sessions:

| Method | Arguments | Return |
|---|---|---|
| `new_session` | — | `{session_id: str}` |
| `resume_session` | `session_id: str, fork?: bool` | `{session_id: str, forked_from?: str}` or `{error: str, reason: str}` |
| `list_engine_sessions` | `limit?: int` | `list[{session_id, mtime, summary?}]` — from the store |
| `delete_engine_session` | `session_id: str` | `{status: str}` — localhost-only |

`resume_session` with `fork: true` issues a **new** session ID and leaves the original untouched;
the response carries both so the UI can label the fork.

History (mirrored store, shapes unchanged from the native engine):

| Method | Arguments | Return |
|---|---|---|
| `history_list_sessions` | `limit?: int` | `list[SessionSummary]` (above) |
| `history_get_session` | `session_id: str` | `list[MessageDict]` — full metadata, reconstructed image data URIs |
| `history_search` | `query: str, role?: str, limit?: int` | `list[{session_id, message_id, role, content_preview, timestamp}]` |

Subagents:

| Method | Arguments | Return |
|---|---|---|
| `list_subagent_transcripts` | `session_id?: str` | `list[{agent_id, subpath, task_id?, description?}]` |
| `get_subagent_transcript` | `agent_id: str, session_id?: str` | `list[SessionStoreEntry]` or `{error: str}` |

`session_id` defaults to the active session. Deleted RPCs: `get_turn_archive`, `get_agent_history`,
`close_agent_context`, `set_agent_selected_files`, `load_session_into_context`,
`get_history_status`.

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
from claude_agent_sdk.testing import session_store_conformance
```

Run against a temp-directory instance of our store as part of the normal test suite. A store that
passes locally but violates the protocol produces resume failures that present as context loss, which
is close to undiagnosable from the UI.

## Dependency quirks

### The store is a mirror, not the primary write path

`SessionStore.append()` is called **after** the subprocess's local write succeeds — durability is
already guaranteed on local disk under `CLAUDE_CONFIG_DIR`, and batches arrive at roughly 100 ms
cadence during a turn. Our appends cannot be made synchronous with the turn, and a failed batch is
retried three times and then dropped with a `MirrorErrorMessage`; timeouts are not retried, because the
in-flight call may still land.

Consequences:

- The parent spec's durability invariant applies to **our** `history.jsonl` (which we write ourselves,
  before acknowledging the turn), not to the engine transcript.
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

### Deletion is ours to police

The SDK never deletes from the store unless `delete_session_via_store()` is called. Retention is the
adapter's responsibility; local transcripts under `CLAUDE_CONFIG_DIR` are swept independently by the
CLI's `cleanupPeriodDays` setting. AC⚡DC does not expire sessions automatically — it warns at the
threshold above and offers explicit deletion.

Deleting a main-transcript key also removes that session's summary sidecar and its subagent directory:
a subagent transcript whose parent is gone is unreachable through every RPC we expose.
