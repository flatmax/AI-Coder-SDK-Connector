"""``.ac-dc4/index/`` — the derived index, and the search that leans on it.

The index answers two questions without reading every transcript: which
sessions could possibly contain a search term, and what the session list
already worked out about a session that has not changed since. It holds no
content of its own, which is the point — it can be stale, and a stale index
is repaired by deleting it, whereas a second copy of the conversation that
disagrees with the first has no repair that does not involve picking a
winner (``specs5/3-engine/history.md`` § The Derived Index).

**A cold or missing index is a performance problem, never a correctness
one.** That is a property of how search is arranged here, not a hope: the
index only narrows the set of sessions and entries to look at, and every
result is then confirmed against the transcript text itself. The same
confirmation runs when there is no index at all, so both paths return the
same rows. Two consequences follow, and both are deliberate:

- A text longer than :data:`_TEXT_CAP` — a ``Write`` of a large file, a
  pasted log — is not tokenised, and its session is flagged as one that
  must always be scanned. A truncated term list would have made a real hit
  unfindable while the index was warm and findable once it was deleted.
- Query tokens are matched against index *terms* by substring, so a search
  for ``arser`` finds ``parser``. A term index that only matched whole
  words would answer differently from the fallback scan.

Tool **results** are never indexed, and are not searched by the fallback
either. The transcript holds them verbatim because the protocol requires
it; searching them would return mostly file contents, which is what
``Grep`` is for, and conversational hits would drown
(``specs-reference/3-engine/history.md`` § Derived index).

Governing spec: ``specs5/3-engine/history.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ac_dc.claude_code.history import strip_framing

if TYPE_CHECKING:
    from claude_agent_sdk import SessionStore

logger = logging.getLogger(__name__)


# The format is not an interop boundary — the index is rebuildable from
# `sessions/` in full — so a bump just discards the file rather than
# migrating it.
INDEX_VERSION = 1

# The native engine's `HistoryStore.search` default, carried over
# unchanged: it applies to index-backed search and the fallback scan alike
# (``specs-reference/3-engine/history.md`` § Numeric constants).
SEARCH_LIMIT = 50

# How much of the matched text a result row carries, and how much of it
# comes before the match — enough context to recognise the hit without
# shipping the message.
_PREVIEW_CHARS = 120
_PREVIEW_LEAD = 30

# Beyond this, a single text is left out of the term index and its session
# is marked always-scan. See the module docstring.
_TEXT_CAP = 20_000

# What a term is: a run of letters, digits or underscores, lowercased.
# Deliberately crude — the term index is a narrowing device and the
# authoritative match is the substring check against the text itself.
_TERM = re.compile(r"[\w]+", re.UNICODE)

# Postings are stored as one string per posting so the file stays flat.
_FIELD = "\t"

# The `kind` of a posting, and the `role` a result reports for it. A tool
# call is neither the user's words nor the model's prose, and labelling a
# `Bash` command "assistant" would make the role column useless for the
# one search where it matters most.
_KINDS = {"u": "user", "a": "assistant", "t": "tool"}
ROLES = frozenset(_KINDS.values())


class HistoryIndex:
    """One repo's derived index, loaded lazily and rewritten atomically.

    Parameters
    ----------
    path:
        The index file, normally ``.ac-dc4/index/<project_key>.json``.
        Constructing this touches no disk.
    store:
        The session store to build from.
    directory:
        The repo root, for the store's ``project_key``.
    """

    def __init__(self, path: Path | str, store: SessionStore, directory: str) -> None:
        self.path = Path(path)
        self._store = store
        self._directory = directory
        self._sessions: dict[str, dict[str, Any]] = {}
        self._postings: dict[str, set[str]] = {}
        # The fingerprints the last listing saw, so a row cached during it
        # is invalidated by the same fact that would invalidate its postings.
        self._fingerprints: dict[str, tuple[int, int]] = {}
        self._loaded = False
        self._dirty = False
        # One writer. Refresh reads the store and rewrites the file, and two
        # concurrent refreshes would each save a half-updated view.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # The summary cache
    # ------------------------------------------------------------------

    async def cached_summaries(self) -> dict[str, dict[str, Any]]:
        """Session rows we already computed, for transcripts that have not moved.

        Called once per listing, and it takes the fingerprint snapshot that
        :meth:`remember_summary` then writes against — the two go together,
        which is why the snapshot lives on the instance rather than being
        recomputed per session. What is cached is the *parser's* answer, not
        a second way of counting messages: a cache that recomputed
        ``message_count`` its own way would be a second number for one fact.
        """
        await self._ensure_loaded()
        try:
            listing = await self._store.list_sessions(self._project_key())
        except OSError:
            logger.warning("Could not check the session list cache; recomputing it")
            self._fingerprints = {}
            return {}
        self._fingerprints = {
            row["session_id"]: _fingerprint(row)
            for row in listing
            if isinstance(row, dict) and row.get("session_id")
        }
        cached: dict[str, dict[str, Any]] = {}
        for session_id, at in self._fingerprints.items():
            state = self._sessions.get(session_id) or {}
            summary = state.get("summary")
            if isinstance(summary, dict) and state.get("summary_at") == list(at):
                cached[session_id] = dict(summary)
        return cached

    async def remember_summary(
        self, session_id: str, summary: dict[str, Any]
    ) -> None:
        """Cache one session row. Written to disk by :meth:`save`.

        Silently declines for a session absent from the last snapshot: with
        no fingerprint there is nothing to invalidate the row against, and a
        cached row that cannot go stale is a row that stays wrong.
        """
        at = self._fingerprints.get(session_id)
        if at is None:
            return
        await self._ensure_loaded()
        state = self._sessions.setdefault(session_id, {})
        state["summary"] = dict(summary)
        state["summary_at"] = list(at)
        self._dirty = True

    # ------------------------------------------------------------------
    # Search support
    # ------------------------------------------------------------------

    async def candidates(
        self, tokens: list[str]
    ) -> dict[str, set[tuple[str, str]] | None] | None:
        """Where every entry containing all of ``tokens`` might be.

        Returns a map from session ID to the ``(entry_uuid, kind)`` pairs
        worth reading, where ``None`` for a session means "read all of it"
        — a session with an over-long text in it, or one the refresh could
        not read. A ``None`` return means the index is unusable and the
        caller should scan everything.

        Narrowing only. The caller confirms each candidate against the
        transcript, which is what lets the fallback return the same rows.
        """
        if not tokens:
            return None
        try:
            await self.refresh()
        except Exception:
            logger.exception("Could not refresh the history index; scanning instead")
            return None

        scope: dict[str, set[tuple[str, str]] | None] = {
            session_id: None
            for session_id, state in self._sessions.items()
            if state.get("scan")
        }
        matched: set[str] | None = None
        for token in tokens:
            found: set[str] = set()
            for term, postings in self._postings.items():
                if token in term:
                    found |= postings
            matched = found if matched is None else (matched & found)
            if not matched:
                break

        for posting in matched or ():
            session_id, entry_uuid, kind = posting.split(_FIELD)
            if session_id in scope and scope[session_id] is None:
                continue
            scope.setdefault(session_id, set()).add((entry_uuid, kind))  # type: ignore[union-attr]
        return scope

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """Bring the index up to date with the store, then save if it moved.

        Incremental by entry count, which is sound because transcripts are
        append-only: a session that grew is indexed from where the last pass
        stopped. A session that *shrank* is one that was deleted and
        recreated under the same ID, so it is purged and rebuilt.
        """
        async with self._lock:
            await self._ensure_loaded()
            listing = await self._store.list_sessions(self._project_key())
            present = {
                row["session_id"]: _fingerprint(row)
                for row in listing
                if isinstance(row, dict) and row.get("session_id")
            }

            for session_id in list(self._sessions):
                if session_id not in present:
                    self._purge(session_id)
                    self._sessions.pop(session_id, None)
                    self._dirty = True

            for session_id, fingerprint in present.items():
                state = self._sessions.get(session_id)
                if state is not None and state.get("at") == list(fingerprint):
                    continue
                await self._index_session(session_id, fingerprint)

            if self._dirty:
                await self._save_locked()

    async def _index_session(
        self, session_id: str, fingerprint: tuple[int, int]
    ) -> None:
        state = self._sessions.setdefault(session_id, {})
        try:
            entries = await self._store.load(
                {"project_key": self._project_key(), "session_id": session_id}
            ) or []
        except OSError:
            # Not fatal, and not silently wrong either: a session we could
            # not read is one search must scan rather than skip.
            logger.warning("Could not index session %s; it will be scanned", session_id)
            state.update({"at": list(fingerprint), "entries": 0, "scan": True})
            self._dirty = True
            return

        indexed = int(state.get("entries") or 0)
        if indexed and len(entries) >= indexed and not state.get("scan"):
            tail = entries[indexed:]
        else:
            self._purge(session_id)
            state.pop("scan", None)
            tail = entries

        oversized = False
        for entry_uuid, kind, text in searchable_texts(tail):
            if len(text) > _TEXT_CAP:
                oversized = True
                continue
            posting = _FIELD.join((session_id, entry_uuid, kind))
            for term in set(_tokens(text)):
                self._postings.setdefault(term, set()).add(posting)

        state["at"] = list(fingerprint)
        state["entries"] = len(entries)
        if oversized:
            state["scan"] = True
        # The summary cache is keyed by its own mtime, so a changed session
        # simply misses next time rather than serving a stale row.
        self._dirty = True

    def _purge(self, session_id: str) -> None:
        """Drop every posting for one session."""
        prefix = session_id + _FIELD
        for term in list(self._postings):
            postings = self._postings[term]
            remaining = {p for p in postings if not p.startswith(prefix)}
            if remaining:
                self._postings[term] = remaining
            else:
                del self._postings[term]

    async def forget(self, session_id: str) -> None:
        """Remove a deleted session from the index, postings and all."""
        async with self._lock:
            await self._ensure_loaded()
            if session_id not in self._sessions and not any(
                p.startswith(session_id + _FIELD)
                for postings in self._postings.values()
                for p in postings
            ):
                return
            self._purge(session_id)
            self._sessions.pop(session_id, None)
            self._dirty = True
            await self._save_locked()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save(self) -> None:
        """Write the index out if anything changed since it was read."""
        async with self._lock:
            await self._save_locked()

    async def _save_locked(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": INDEX_VERSION,
            "sessions": self._sessions,
            "postings": {
                term: sorted(postings) for term, postings in self._postings.items()
            },
        }
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_sync, payload)
        self._dirty = False

    def _save_sync(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        except OSError:
            # Losing the index costs a slower search, so this is a warning
            # and not a raise. The next refresh rebuilds what did not land.
            logger.warning("Could not write the history index at %s", self.path)

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, self._load_sync)
        self._sessions, self._postings = state
        self._loaded = True

    def _load_sync(self) -> tuple[dict[str, Any], dict[str, set[str]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, {}
        except (OSError, json.JSONDecodeError):
            # A corrupt index is discarded rather than repaired: it is
            # derived, and the next refresh rebuilds it from the
            # transcripts it was derived from.
            logger.warning("Discarding an unreadable history index at %s", self.path)
            return {}, {}
        if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
            return {}, {}
        sessions = raw.get("sessions")
        postings = raw.get("postings")
        if not isinstance(sessions, dict) or not isinstance(postings, dict):
            return {}, {}
        return (
            {k: v for k, v in sessions.items() if isinstance(v, dict)},
            {
                term: {p for p in value if isinstance(p, str) and p.count(_FIELD) == 2}
                for term, value in postings.items()
                if isinstance(value, list)
            },
        )

    def _project_key(self) -> str:
        from claude_agent_sdk import project_key_for_directory

        return project_key_for_directory(self._directory)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search(
    store: SessionStore,
    directory: str,
    query: str,
    *,
    index: HistoryIndex | None = None,
    role: str | None = None,
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over stored conversations.

    Newest session first, and newest entry within a session first, stopping
    as soon as ``limit`` rows are found — the same ordering and the same cap
    the native engine's search had, so a user's saved habits still work.

    ``index`` only narrows what gets read. Every row returned here was
    confirmed against the transcript text, which is why passing no index
    changes how long this takes and not what it answers.
    """
    from claude_agent_sdk import project_key_for_directory

    if not query:
        return []
    needle = query.lower()
    project_key = project_key_for_directory(directory)

    scope: dict[str, set[tuple[str, str]] | None] | None = None
    if index is not None:
        scope = await index.candidates(_tokens(query))

    listing = await store.list_sessions(project_key)
    ordered = sorted(
        (row for row in listing if isinstance(row, dict) and row.get("session_id")),
        key=lambda row: row.get("mtime") or 0,
        reverse=True,
    )

    results: list[dict[str, Any]] = []
    for row in ordered:
        session_id = row["session_id"]
        if scope is not None and session_id not in scope:
            continue
        wanted = scope.get(session_id) if scope is not None else None
        try:
            entries = await store.load(
                {"project_key": project_key, "session_id": session_id}
            ) or []
        except OSError:
            logger.warning("Could not search session %s", session_id)
            continue

        by_uuid = {
            entry["uuid"]: entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("uuid"), str)
        }
        for entry_uuid, kind, text in reversed(searchable_texts(entries)):
            if wanted is not None and (entry_uuid, kind) not in wanted:
                continue
            if role and _KINDS[kind] != role:
                continue
            at = text.lower().find(needle)
            if at < 0:
                continue
            results.append(
                {
                    "session_id": session_id,
                    "entry_uuid": entry_uuid,
                    "role": _KINDS[kind],
                    "content_preview": _preview(text, at),
                    "timestamp": by_uuid.get(entry_uuid, {}).get("timestamp") or "",
                }
            )
            if len(results) >= limit:
                return results
    return results


def searchable_texts(entries: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """``(entry_uuid, kind, text)`` for everything search is allowed to see.

    User prose, assistant prose and thinking, and tool *calls* — name and
    input. Tool results are skipped here rather than at the indexer, so the
    index and the fallback scan cannot disagree about what is searchable.

    The CLI writes one entry per content block, so an entry usually yields
    one row; the loop over blocks is what makes a hand-written or older
    multi-block entry work anyway.
    """
    texts: list[tuple[str, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_uuid = entry.get("uuid")
        if not isinstance(entry_uuid, str):
            continue
        kind = {"user": "u", "assistant": "a"}.get(entry.get("type") or "")
        if kind is None:
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append((entry_uuid, kind, strip_framing(content)))
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                texts.append(
                    (entry_uuid, kind, strip_framing(str(block.get("text") or "")))
                )
            elif block_type == "thinking":
                texts.append((entry_uuid, kind, str(block.get("thinking") or "")))
            elif block_type == "tool_use":
                texts.append((entry_uuid, "t", _tool_call_text(block)))
    return [row for row in texts if row[2]]


def _tool_call_text(block: dict[str, Any]) -> str:
    """A tool call as searchable text: its name and its input.

    The input is serialised rather than summarised, because the searches
    this exists for are for a path, a command or a pattern the agent used —
    all of which live in the input and none of which survive the display
    summary.
    """
    name = str(block.get("name") or "")
    payload = block.get("input")
    if payload in (None, {}, ""):
        return name
    try:
        rendered = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(payload)
    return f"{name} {rendered}"


def _fingerprint(row: dict[str, Any]) -> tuple[int, int]:
    """What "this session has not changed" means.

    Mtime **and** byte count. Mtime alone is not enough: it has millisecond
    resolution here and coarser resolution on some filesystems, so two
    appends can share one, and an index that skipped the second would miss
    hits for as long as it stayed warm — the one failure mode this whole
    module is arranged to prevent. Transcripts only ever grow, so the size
    decides what the clock cannot. A store that does not report a size
    degrades to mtime alone.
    """
    return int(row.get("mtime") or 0), int(row.get("size") or 0)


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TERM.finditer(text)]


def _preview(text: str, at: int) -> str:
    """A window around the match, so the row shows why it matched."""
    start = max(0, at - _PREVIEW_LEAD)
    snippet = text[start : start + _PREVIEW_CHARS].strip()
    return f"…{snippet}" if start else snippet
