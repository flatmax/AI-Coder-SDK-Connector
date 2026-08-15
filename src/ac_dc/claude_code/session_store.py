"""The repo-local session store — one transcript, mirrored into ``.ac-dc4/``.

AC-DC implements the SDK's ``SessionStore`` protocol so a copy of the
engine transcript lands inside the repository instead of only under
``~/.claude/projects/``. That copy is what makes a session survive: the
CLI expires its own transcripts on a retention timer that knows nothing
about this repo, and when a resumed session's local file is gone the SDK
loads ours and materialises it for the subprocess.

Four properties here are contracts, not choices
(``specs5/3-engine/history.md`` § ``SessionStore``, and
[CC-19](../../../specs5/plan/decisions.md#cc-19)):

- **Entries are stored verbatim.** ``SessionStoreEntry`` is documented as
  a pass-through blob whose concrete shape is the CLI's own internal
  transcript union. Round-tripping ``json.dumps``/``json.loads`` is the
  only invariant the protocol requires and the only one we rely on. We
  never inspect an entry beyond its ``uuid``, and we never write one the
  CLI did not give us — ``load()`` hands this file's contents back to a
  subprocess that parses its own union, so a record we invented would
  surface as a resume failure, which presents as context loss much later
  in a session the user cares about.
- **All six methods are implemented.** The SDK probes for the optional
  four (``list_sessions``, ``list_session_summaries``, ``delete``,
  ``list_subkeys``) by attribute presence, never by ``isinstance``. A
  missing one degrades a feature *silently* — a history browser that
  lists nothing and reports no error.
- **Append and delete only.** Nothing here rewrites an existing record.
- **``uuid`` is an idempotency key.** A failed mirror batch is retried by
  the SDK, and ``import_session_to_store()`` re-imports a whole local
  transcript as a repair tool. Both must be duplicate-safe.

This class deliberately does **not** subclass the SDK's ``SessionStore``
Protocol. Its default methods raise ``NotImplementedError``, and the
SDK's presence probe reads an inherited default as *absent* — so
subclassing turns "forgot to override one method" into a silently
missing feature rather than a loud error. Duck typing is what the SDK
documents, and it removes that failure mode by construction.

Governing spec: ``specs5/3-engine/history.md``.
Reference: ``specs-reference/3-engine/history.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk import SessionKey, SessionStoreEntry, SessionSummaryEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numeric constants (specs-reference/3-engine/history.md § Numeric constants)
# ---------------------------------------------------------------------------

# Warn once per server lifetime when the mirrored transcripts pass this.
# Carried over from the native engine's agent-archive warning; only the
# measured path changed. Never blocks work — a transcript is the one thing
# under .ac-dc4/ that does not rebuild.
DISK_WARNING_BYTES = 1024 * 1024 * 1024

_JSONL_SUFFIX = ".jsonl"
_SUMMARY_SUFFIX = ".summary.json"

# Rejected in any path component we build a filesystem path from. The
# subpath arrives from the SDK rather than from a user, but it *becomes* a
# path, and a traversal check at the boundary costs nothing.
_UNSAFE_COMPONENTS = frozenset({"", ".", ".."})
_UNSAFE_CHARS = ("/", "\\", "\0")


class SessionStoreKeyError(ValueError):
    """A key could not be turned into a path safely.

    Raised rather than swallowed: the SDK logs an exception from
    ``append`` and surfaces it as a ``MirrorErrorMessage``, which is a
    visible hole in the mirror. Silently writing somewhere else, or
    silently writing nothing, is worse.
    """


# ---------------------------------------------------------------------------
# Key validation and path mapping
# ---------------------------------------------------------------------------


def _safe_component(value: Any, what: str) -> str:
    """Validate one path component. Path-safety only — not UUID-ness.

    The conformance suite appends under ``"sess"``, ``"a"`` and
    ``"summ-sess"``, so a store that insists on UUIDs fails every
    contract without ever reaching a real session. UUID validation
    belongs at the RPC boundary, where a session ID arrives from a
    browser, and the SDK's own ``*_from_store`` readers already apply it
    there.
    """
    if not isinstance(value, str):
        raise SessionStoreKeyError(f"{what} must be a string, got {type(value).__name__}")
    if value in _UNSAFE_COMPONENTS or any(ch in value for ch in _UNSAFE_CHARS):
        raise SessionStoreKeyError(f"{what} is not a usable path component: {value!r}")
    if os.sep in value or (os.altsep and os.altsep in value):
        raise SessionStoreKeyError(f"{what} contains a path separator: {value!r}")
    return value


def _safe_subpath(subpath: Any) -> str:
    """Validate a ``/``-joined subpath.

    An **empty string is invalid** — the protocol says to omit the field
    for main transcripts, so an empty ``subpath`` is a bug in the caller,
    not a main transcript.
    """
    if not isinstance(subpath, str) or not subpath:
        raise SessionStoreKeyError(f"subpath must be a non-empty string, got {subpath!r}")
    parts = subpath.split("/")
    for part in parts:
        _safe_component(part, "subpath component")
    return "/".join(parts)


class _FileState:
    """Per-file idempotency state, mutated only under the session lock."""

    __slots__ = ("seen_uuids", "seeded")

    def __init__(self) -> None:
        self.seen_uuids: set[str] = set()
        # Whether the existing file has been scanned for ids yet. Kept
        # separate from `seen_uuids` being empty, which is also the state
        # of a scanned-but-empty file.
        self.seeded = False


class _SummaryState:
    """Per-session summary cache, mutated only under the session lock.

    A mutable holder rather than a dict entry so the worker thread can
    publish the folded summary without reaching back into the store's
    dictionaries from off the event loop.
    """

    __slots__ = ("value", "loaded")

    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.loaded = False


class _Key:
    """A validated key, split into the three things paths are built from."""

    __slots__ = ("project_key", "session_id", "subpath")

    def __init__(self, key: Any) -> None:
        if not isinstance(key, dict):
            raise SessionStoreKeyError(f"key must be a mapping, got {type(key).__name__}")
        self.project_key = _safe_component(key.get("project_key"), "project_key")
        self.session_id = _safe_component(key.get("session_id"), "session_id")
        raw_subpath = key.get("subpath")
        self.subpath = None if raw_subpath is None else _safe_subpath(raw_subpath)

    @property
    def session_scope(self) -> tuple[str, str]:
        """What the per-session lock and the summary cache are keyed on."""
        return (self.project_key, self.session_id)

    @property
    def storage_scope(self) -> tuple[str, str, str | None]:
        """What the seen-``uuid`` set is keyed on — one file, one set."""
        return (self.project_key, self.session_id, self.subpath)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class RepoSessionStore:
    """``SessionStore`` over ``<root>/<project_key>/`` on local disk.

    Parameters
    ----------
    root:
        The ``sessions/`` directory, normally ``.ac-dc4/sessions``.
        Created on first write rather than at construction, so building a
        store does not touch the disk.

    Layout — ``specs-reference/3-engine/history.md`` § Working-directory
    layout::

        <root>/<project_key>/<session_id>.jsonl          main transcript
        <root>/<project_key>/<session_id>.summary.json   summary sidecar
        <root>/<project_key>/<session_id>/<subpath>.jsonl subagent transcript

    The ``<project_key>`` level exists even though AC-DC is single-repo:
    the SDK's key includes it, and two worktrees of the same repo produce
    different keys. Flattening it would make them collide.

    Keys become path components, so this store inherits the filesystem's
    case sensitivity. On Linux that is exact. On a case-insensitive volume
    the project keys ``A`` and ``B`` still differ but ``a`` and ``A`` would
    not — which is a conformance failure (contract 6), not a silent bug.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        # Per-session, so a read-fold-write of the sidecar cannot
        # interleave with another append to the same session. Keyed on
        # the session rather than the file because the sidecar is shared
        # by a session's main transcript and its subagents.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Seeded from disk on first append to a file, then maintained in
        # memory. Per *file*: a subagent transcript re-using a parent's
        # uuid is a different record, not a duplicate.
        self._files: dict[tuple[str, str, str | None], _FileState] = {}
        self._summaries: dict[tuple[str, str], _SummaryState] = {}
        self._malformed_lines = 0

    # ------------------------------------------------------------------
    # Protocol — required
    # ------------------------------------------------------------------

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """Mirror a batch of transcript entries.

        Called *after* the subprocess's local write has already succeeded,
        so this is a copy and not a durability barrier. Entries are
        persisted in append-call order: the per-session lock is acquired
        before any await, and asyncio wakes lock waiters FIFO.
        """
        if not entries:
            # A no-op that must not create a file. An empty transcript
            # would otherwise appear in list_sessions() and load() would
            # answer [] where it should answer None.
            return
        k = _Key(key)
        async with self._lock_for(k):
            # Both holders are created here, on the event loop thread, so
            # the worker only ever mutates objects it was handed.
            file_state = self._file_state(k)
            summary = None if k.subpath is not None else self._summary_state(k)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._append_sync, k, list(entries), file_state, summary
            )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Read every entry for ``key``, or ``None`` if there is no file.

        Returns the whole session — the SDK calls this once, in the
        parent process, before spawning the subprocess, and there is no
        streaming variant. It is bounded by the SDK's ``load_timeout_ms``.
        """
        k = _Key(key)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_sync, self._path_for(k))

    # ------------------------------------------------------------------
    # Protocol — optional, and all four implemented
    # ------------------------------------------------------------------

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        """Main transcripts under ``project_key``, with storage mtimes.

        Subagent transcripts are excluded: they live one directory deeper,
        so the shape of the layout does the excluding.

        Each row carries a ``size`` alongside the two fields the protocol
        asks for. The SDK reads ``session_id`` and ``mtime`` and ignores the
        rest; what needs the extra field is the derived index, whose
        staleness check must not miss an append that landed in the same
        millisecond as the last one it saw. Transcripts only grow, so the
        byte count settles that where a timestamp cannot.
        """
        _safe_component(project_key, "project_key")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._list_sessions_sync, project_key)

    async def list_session_summaries(
        self, project_key: str
    ) -> list[SessionSummaryEntry]:
        """Every persisted sidecar under ``project_key``, verbatim.

        ``data`` is opaque SDK-owned state. We persist and return it
        without interpreting it — the protocol says so, and interpreting
        it is how a store starts disagreeing with the fold that produced
        it.
        """
        _safe_component(project_key, "project_key")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._list_summaries_sync, project_key)

    async def delete(self, key: SessionKey) -> None:
        """Delete one transcript. Missing is not an error.

        Deleting a main transcript cascades to its sidecar and its
        subagent directory: a subagent transcript whose parent is gone is
        unreachable through every RPC we expose, so leaving it behind
        would only leak disk. A delete with an explicit ``subpath``
        removes that one file.
        """
        k = _Key(key)
        async with self._lock_for(k):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._delete_sync, k)
            self._forget(k)

    async def list_subkeys(self, key: SessionKey) -> list[str]:
        """Subpaths under a session — how subagent transcripts are found.

        The key argument has no ``subpath``, and the main transcript is
        not a subkey of itself.
        """
        k = _Key(key)
        if k.subpath is not None:
            # A subkey of a subkey is not a thing this layout expresses,
            # and answering as if the subpath were absent would quietly
            # return a superset.
            raise SessionStoreKeyError(
                f"list_subkeys takes a session key, not a subpath: {k.subpath!r}"
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._list_subkeys_sync, k)

    # ------------------------------------------------------------------
    # Local surface (not part of the protocol)
    # ------------------------------------------------------------------

    @property
    def malformed_lines(self) -> int:
        """How many unparseable lines have been skipped since startup.

        A partial write from a crash must not make a session unreadable,
        so those lines are skipped — but skipping silently would hide a
        corrupt mirror, so the count is readable and reported in engine
        health.
        """
        return self._malformed_lines

    def total_bytes(self) -> int:
        """Bytes under ``root``, for the disk-usage warning."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
        return total

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _project_dir(self, project_key: str) -> Path:
        return self.root / project_key

    def _path_for(self, k: _Key) -> Path:
        project_dir = self._project_dir(k.project_key)
        if k.subpath is None:
            return project_dir / f"{k.session_id}{_JSONL_SUFFIX}"
        return project_dir / k.session_id / f"{k.subpath}{_JSONL_SUFFIX}"

    def _summary_path(self, k: _Key) -> Path:
        return self._project_dir(k.project_key) / f"{k.session_id}{_SUMMARY_SUFFIX}"

    def _session_dir(self, k: _Key) -> Path:
        return self._project_dir(k.project_key) / k.session_id

    # ------------------------------------------------------------------
    # Synchronous bodies — every one of these runs in a worker thread
    # ------------------------------------------------------------------

    def _append_sync(
        self,
        k: _Key,
        entries: list[Any],
        file_state: _FileState,
        summary: _SummaryState | None,
    ) -> None:
        path = self._path_for(k)
        if not file_state.seeded:
            # First touch of this file in this process. Seeding from disk
            # is what makes a retried batch and a re-import idempotent
            # across a restart, not just within one.
            file_state.seen_uuids.update(self._existing_uuids(path))
            file_state.seeded = True

        seen = file_state.seen_uuids
        written: list[Any] = []
        lines: list[str] = []
        for entry in entries:
            uuid = entry.get("uuid") if isinstance(entry, dict) else None
            if isinstance(uuid, str) and uuid:
                if uuid in seen:
                    continue
                seen.add(uuid)
            # Entries without a uuid (titles, tags, mode markers) are
            # appended without dedup — the protocol says so, and they have
            # nothing to be idempotent on.
            written.append(entry)
            lines.append(_encode(entry))

        if not lines:
            # Every entry was a duplicate. Not an error, and deliberately
            # not a fold either: folding them again would double-count the
            # session's own summary.
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))

        if summary is not None:
            # Subagent transcripts get no summary of their own and must not
            # contribute to the main session's, which is why `append` only
            # hands a holder over for a main transcript.
            self._fold_and_persist(k, path, written, summary)

    def _fold_and_persist(
        self, k: _Key, transcript: Path, entries: list[Any], summary: _SummaryState
    ) -> None:
        """Update the sidecar via the SDK's fold, then stamp its mtime."""
        from claude_agent_sdk import fold_session_summary

        if not summary.loaded:
            summary.value = self._read_summary(self._summary_path(k))
            summary.loaded = True

        folded = dict(
            fold_session_summary(
                summary.value,
                {"project_key": k.project_key, "session_id": k.session_id},
                entries,
            )
        )
        # `mtime` is contractually the *storage* write time and must share
        # a clock with list_sessions()'s mtime for this session, or the
        # fast-path staleness check in list_sessions_from_store() treats
        # every sidecar as stale and falls back to a load() per session.
        #
        # We take it from the transcript we just wrote, which is the exact
        # number list_sessions() reports. Sharing one filesystem value is
        # stronger than sharing a clock: it cannot drift, it needs no
        # second write of the sidecar to re-stat it, and a crash between
        # the two writes leaves a sidecar strictly older than the
        # transcript — which is precisely how "stale" is meant to look.
        folded["mtime"] = _mtime_ms(transcript)
        _write_json_atomic(self._summary_path(k), folded)
        summary.value = folded

    def _load_sync(self, path: Path) -> list[Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            # Not "no session" — an unreadable session. Returning None
            # here would let a resume start with an empty context and
            # look like it worked.
            logger.exception("Could not read transcript %s", path)
            raise
        return self._parse_lines(text, path)

    def _parse_lines(self, text: str, path: Path) -> list[Any]:
        entries: list[Any] = []
        for number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                self._malformed_lines += 1
                logger.warning("Skipping unparseable line %d of %s", number, path)
        return entries

    def _existing_uuids(self, path: Path) -> set[str]:
        """Every ``uuid`` already in ``path``. Absent file means none."""
        found: set[str] = set()
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    uuid = entry.get("uuid") if isinstance(entry, dict) else None
                    if isinstance(uuid, str) and uuid:
                        found.add(uuid)
        except FileNotFoundError:
            return found
        except OSError:
            logger.warning("Could not scan %s for existing entry ids", path)
        return found

    def _list_sessions_sync(self, project_key: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for entry in _scandir(self._project_dir(project_key)):
            if not entry.name.endswith(_JSONL_SUFFIX):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
                mtime = int(stat.st_mtime * 1000)
            except OSError:
                continue
            results.append(
                {
                    "session_id": entry.name[: -len(_JSONL_SUFFIX)],
                    "mtime": mtime,
                    "size": stat.st_size,
                }
            )
        # Order is unspecified by the protocol — the SDK sorts by mtime
        # descending itself — so this is for humans reading a log, not for
        # a caller relying on it.
        results.sort(key=lambda item: item["mtime"], reverse=True)
        return results

    def _list_summaries_sync(self, project_key: str) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for entry in _scandir(self._project_dir(project_key)):
            if not entry.name.endswith(_SUMMARY_SUFFIX):
                continue
            summary = self._read_summary(Path(entry.path))
            if summary is not None:
                summaries.append(summary)
        return summaries

    def _read_summary(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # A truncated sidecar is recoverable: the fold recomputes it
            # from the transcript, and list_sessions_from_store() gap-fills
            # a session with no usable summary.
            logger.warning("Ignoring unreadable session summary %s", path)
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            logger.warning("Ignoring malformed session summary %s", path)
            return None
        if not isinstance(raw.get("session_id"), str) or not isinstance(
            raw.get("mtime"), int
        ):
            logger.warning("Ignoring malformed session summary %s", path)
            return None
        return raw

    def _delete_sync(self, k: _Key) -> None:
        if k.subpath is not None:
            _unlink(self._path_for(k))
            _prune_empty_dirs(self._path_for(k).parent, stop=self._session_dir(k).parent)
            return
        _unlink(self._path_for(k))
        _unlink(self._summary_path(k))
        shutil.rmtree(self._session_dir(k), ignore_errors=True)

    def _list_subkeys_sync(self, k: _Key) -> list[str]:
        session_dir = self._session_dir(k)
        subkeys: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(session_dir):
            for name in filenames:
                if not name.endswith(_JSONL_SUFFIX):
                    continue
                relative = Path(dirpath, name).relative_to(session_dir)
                # Subpaths are always /-joined regardless of os.sep, so a
                # key written on Windows still reads on Linux.
                subkeys.append(relative.as_posix()[: -len(_JSONL_SUFFIX)])
        return subkeys

    # ------------------------------------------------------------------
    # In-process state
    # ------------------------------------------------------------------

    def _lock_for(self, k: _Key) -> asyncio.Lock:
        lock = self._locks.get(k.session_scope)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[k.session_scope] = lock
        return lock

    def _file_state(self, k: _Key) -> _FileState:
        state = self._files.get(k.storage_scope)
        if state is None:
            state = _FileState()
            self._files[k.storage_scope] = state
        return state

    def _summary_state(self, k: _Key) -> _SummaryState:
        state = self._summaries.get(k.session_scope)
        if state is None:
            state = _SummaryState()
            self._summaries[k.session_scope] = state
        return state

    def _forget(self, k: _Key) -> None:
        """Drop cached state for a deleted key, and its subkeys.

        Without this, appending to a session id that was deleted and then
        reused would dedup against ids that are no longer on disk, and
        would fold onto a summary describing the deleted session.
        """
        if k.subpath is not None:
            self._files.pop(k.storage_scope, None)
            return
        self._summaries.pop(k.session_scope, None)
        for scope in [s for s in self._files if s[:2] == k.session_scope]:
            self._files.pop(scope, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode(entry: Any) -> str:
    """One transcript line.

    ``type`` is hoisted to the front to match the byte shape the CLI
    writes and the SDK's own ``_entries_to_jsonl`` produces — its lite
    session parser scans for ``{"type":"tag"`` as a *line prefix*, so key
    order is load-bearing for that one reader even though ``load()``
    itself only needs a JSON round-trip.
    """
    if isinstance(entry, dict) and "type" in entry:
        entry = {"type": entry["type"], **entry}
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n"


def _mtime_ms(path: Path) -> int:
    return int(path.stat().st_mtime * 1000)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write via a temp file and rename, so a crash cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _scandir(directory: Path) -> list[os.DirEntry[str]]:
    """``os.scandir`` that answers "nothing here" for a missing directory.

    A project with no sessions yet and a project that does not exist are
    the same answer to every caller of this store.
    """
    try:
        with os.scandir(directory) as it:
            return list(it)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        logger.warning("Could not list %s", directory)
        return []


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not delete %s", path)


def _prune_empty_dirs(directory: Path, *, stop: Path) -> None:
    """Remove now-empty directories left by a targeted subpath delete."""
    current = directory
    while current != stop and current.is_dir():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
