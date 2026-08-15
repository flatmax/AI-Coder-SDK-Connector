"""``.ac-dc4/events.jsonl`` — the events the transcript never held.

A commit, a reset, entering review, a preset switch, a permission-mode
change: AC-DC did these, not the model, and none of them appear in the
engine transcript. Before [CC-19](../../../specs5/plan/decisions.md#cc-19)
they went into our own parallel transcript. They cannot go into the
`SessionStore`, because **the store is never given an entry the CLI did
not write** — `load()` hands its contents to a subprocess that parses its
own union, so a record we invented would surface as a resume failure much
later, in a session someone cares about. So they get their own file.

What that buys, and what it costs:

- Deleting this file loses these events and breaks no session. It is not
  derived from anything, so nothing can rebuild it — which is why it is
  an archive and is never compacted.
- Correlation is by ``session_id`` plus ``request_id``, both carried in
  every record. There is no ``turn_id``: a request ID is already unique
  per turn and already the correlation key everywhere else.
- The browser interleaves these records into a session's rendered
  transcript at read time.

Deliberately absent: compaction boundaries. Those arrive in the
transcript as ``SystemMessage(subtype="compact_boundary")`` and render
from there. A record we wrote would be a second account of something the
transcript already states.

Two of the types below have no writer, for two different reasons.
``preset_switch`` has no producer because the preset selector is deferred
([CC-12](../../../specs5/plan/decisions.md#cc-12)) — the type stays in the
domain so the file format does not move when the selector lands.
``files_written_by_file_tools`` has none because it would be that same
second account: every write is a ``Write``/``Edit``/``MultiEdit``/
``NotebookEdit`` call in the transcript, and the browser's turn footer
already reconstructs the list from those calls at read time
(:mod:`ac_dc.claude_code.history`). Both remain valid ``event`` values, so
a record either one appears in stays renderable.

Governing spec: ``specs5/3-engine/history.md``.
Schema: ``specs-reference/3-engine/history.md`` § Events log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# The `event` discriminator's full domain. Closed on purpose: a typo'd
# discriminator writes a record the browser has no renderer for, which
# looks like the event never happened rather than like a bug.
EVENT_TYPES = frozenset(
    {
        "commit",
        "reset",
        "review_start",
        "review_end",
        "preset_switch",
        "permission_mode",
        # `files_written_by_file_tools` and not `files_changed`: both
        # available sources see only Write/Edit/MultiEdit/NotebookEdit, so
        # a file changed by Bash is absent from this record. The narrow
        # name is binding — CC-18. A wrong live broadcast dies at reload;
        # a wrong field name in `.ac-dc4/` is what the browser shows until
        # someone migrates every transcript users have accumulated.
        "files_written_by_file_tools",
        "session_switch",
    }
)


class EventsLog:
    """Append-only operational events for one repo.

    Parameters
    ----------
    path:
        The log file, normally ``.ac-dc4/events.jsonl``. Created on first
        append; constructing this touches no disk.
    now:
        UTC clock, injectable for tests. ``id`` and ``timestamp`` are both
        derived from one call so they cannot disagree.
    token:
        The random suffix in ``id``, injectable for tests.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        token: Callable[[], str] = lambda: uuid.uuid4().hex[:8],
    ) -> None:
        self.path = Path(path)
        self._now = now
        self._token = token
        # Serialises appends so two concurrent writers cannot interleave
        # a partial line. One process, so a lock is enough.
        self._lock = asyncio.Lock()
        self._malformed_lines = 0
        self._write_failures = 0
        self._dropped_without_session = 0

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def append(
        self,
        event: str,
        *,
        session_id: str | None,
        content: str,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record one event. Returns the record, or ``None`` if dropped.

        ``session_id`` may be ``None`` — a commit from the toolbar before
        the engine has ever connected is a real thing a user did. It is
        **dropped** rather than written, because every record is rendered
        inside a session's transcript and one with no session has nowhere
        to appear; writing it under a placeholder ID would put it in
        somebody else's history. The drop is counted, not silent.

        Raises
        ------
        ValueError
            For an ``event`` outside :data:`EVENT_TYPES`. That is a
            programmer error, and a record the browser cannot render is
            indistinguishable from a missing one.
        """
        if event not in EVENT_TYPES:
            raise ValueError(
                f"Unknown event type {event!r}; expected one of "
                f"{sorted(EVENT_TYPES)}"
            )
        if not session_id:
            self._dropped_without_session += 1
            logger.debug(
                "Dropping a %r event with no session: there is no transcript "
                "for it to be rendered in yet.",
                event,
            )
            return None

        stamp = self._now()
        record: dict[str, Any] = {
            "id": f"{int(stamp.timestamp() * 1000)}-{self._token()}",
            "session_id": session_id,
            "timestamp": stamp.isoformat(),
            "event": event,
            "content": content,
        }
        # Omitted rather than null when absent, matching the schema: an
        # event outside a turn has no request to belong to.
        if request_id is not None:
            record["request_id"] = request_id
        if payload is not None:
            record["payload"] = payload

        async with self._lock:
            loop = asyncio.get_running_loop()
            written = await loop.run_in_executor(None, self._append_sync, record)
        return record if written else None

    def _append_sync(self, record: dict[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
                )
        except OSError:
            # Never raised onward: failing a commit because we could not
            # log that it happened would be a worse outcome than a missing
            # history line. Counted so it is visible rather than silent.
            self._write_failures += 1
            logger.warning(
                "Could not record a %r event in %s", record["event"], self.path
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def load(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Every record, or only one session's, in write order.

        A malformed line is skipped with a warning and counted: a partial
        write from a crash must not make a session's events unreadable.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_sync, session_id)

    def _load_sync(self, session_id: str | None) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Could not read %s", self.path)
            return []

        records: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                self._malformed_lines += 1
                logger.warning("Skipping unparseable line %d of %s", number, self.path)
                continue
            if not isinstance(record, dict):
                self._malformed_lines += 1
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            records.append(record)
        return records

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    async def delete_session(self, session_id: str) -> int:
        """Drop one session's records. Returns how many went.

        The only rewrite this file ever gets, and it is not compaction:
        ``history_delete`` promises that deleting a session takes its
        events with it, and an archive that outlived the session it
        describes would show up in the browser as history for a session
        that no longer exists.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._delete_sync, session_id)

    def _delete_sync(self, session_id: str) -> int:
        surviving: list[str] = []
        removed = 0
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError:
            logger.warning("Could not read %s to delete a session's events", self.path)
            return 0

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                # Kept. A line we cannot parse is a line we cannot prove
                # belongs to the session being deleted, and dropping it
                # would turn one corrupt line into lost history for
                # whichever session actually wrote it.
                surviving.append(stripped)
                continue
            if isinstance(record, dict) and record.get("session_id") == session_id:
                removed += 1
                continue
            surviving.append(stripped)

        if removed:
            self._rewrite(surviving)
        return removed

    def _rewrite(self, lines: list[str]) -> None:
        """Temp file plus rename, so an interrupted delete loses nothing."""
        temporary = self.path.with_name(self.path.name + ".tmp")
        body = "".join(line + "\n" for line in lines)
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def malformed_lines(self) -> int:
        return self._malformed_lines

    @property
    def write_failures(self) -> int:
        """Events we could not record. Non-zero means history has holes."""
        return self._write_failures

    @property
    def dropped_without_session(self) -> int:
        """Events that happened before any session existed to hold them."""
        return self._dropped_without_session


# ---------------------------------------------------------------------------
# Content templates
# ---------------------------------------------------------------------------
#
# The `content` field is the line the browser shows. Commit, reset and the
# mode switches keep the native engine's wording verbatim so a user's
# history reads consistently either side of the conversion — which is why
# these are constants here rather than f-strings at each call site.


def commit_content(short_sha: str, message: str) -> str:
    return f"**Committed** `{short_sha}`\n\n```\n{message}\n```"


def reset_content() -> str:
    return "**Reset to HEAD** — all uncommitted changes have been discarded."


def permission_mode_content(mode: str) -> str:
    return f"Permission mode set to **{mode}**."


def session_switch_content(action: str, session_id: str) -> str:
    """``action`` is ``"resumed"`` or ``"forked"``."""
    return f"{action.capitalize()} session `{session_id}`."


def review_start_content(base: str, head: str) -> str:
    return f"**Review started** — `{base}` → `{head}`."


def review_end_content() -> str:
    return "**Review ended.**"


def preset_switch_content(to_preset: str) -> str:
    return f"Snippet preset switched to **{to_preset}**."
