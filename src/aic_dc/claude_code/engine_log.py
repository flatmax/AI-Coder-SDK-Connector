""".aic-dc/engine-errors.jsonl`` — the failures that happen before a session.

An engine that will not start leaves nothing behind. The failure path sets
``health.last_error``, writes a line to the server's log, broadcasts an
``engineHealth`` event and returns an error to its one caller — and all four
of those die with the process. The log line goes to whatever terminal
launched AIC-DC, which for a desktop launch is nowhere; the broadcast reaches
whichever browsers happened to be listening at that instant; and the health
record is in memory. So a user who saw an authentication error on a cold
start, closed the terminal, and came back to ask what it said has no way to
find out, and neither does anyone reading the repo afterwards
(``specs5/next.md`` § C9).

This file is the durable half. It exists because
:class:`~aic_dc.claude_code.events_log.EventsLog` **structurally cannot hold
these**: every record there is rendered inside a session's transcript, so one
with no session is dropped rather than written, and a connect that fails on
authentication is exactly a failure with no session. That is not an oversight
in the events log — writing a session-less record under a placeholder ID
would put it in somebody else's history. The two files answer different
questions, so they are two files:

===================== ==========================================
``events.jsonl``      What the user did in a session.
``engine-errors.jsonl`` Why the engine would not run.
===================== ==========================================

What a record carries is chosen for one job: diagnosing the failure later,
without the terminal. So each one holds not only the message but the state
that explains it — which binary was resolved, which credential source it
would have used, and the tail of what the CLI itself said on stderr. For an
auth failure the credential source *is* the diagnosis, and it is the one fact
a user cannot recover after the fact by any other route.

Deliberately absent: any attempt to be a general error log. Turn-time
failures are not here. A turn that fails has a session, reaches the browser
as a ``streamComplete`` carrying ``terminal_reason``, and is visible in the
transcript it belongs to — recording it a second time would be the second
account of one event that ``events.jsonl`` refuses to keep for the same
reason.

**Nothing is deduplicated and nothing is capped.** A relaunch loop that fails
forty times writes forty records, because forty is the fact worth seeing and
collapsing it would hide the loop — the same position
:meth:`EngineHealth.note_cli_stderr` takes on repeated stderr. Growth is
bounded in practice by engine failures being rare, and a file that is not
small is itself the finding. Readers ask for a tail rather than the whole
file, which is what keeps an unbounded archive cheap to consume.

Governing spec: ``specs5/3-engine/session.md``.
Schema: ``specs-reference/3-engine/history.md`` § Engine error log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# The `kind` discriminator's full domain, closed for `EVENT_TYPES`' reason:
# a typo'd discriminator writes a record no reader has a rendering for,
# which looks like the failure never happened rather than like a bug.
#
# One member today. It stays a set rather than becoming a bare string
# because the distinction it will need is already visible — a CLI that
# cannot be resolved and a CLI that resolves and then refuses to
# authenticate are the same `EngineStartupError` to the caller and very
# different things to a reader.
ERROR_KINDS = frozenset({"startup_failed"})

#: How many records :meth:`EngineLog.load` returns when asked for a tail.
#: A diagnosis reads the last few failures, not the year's.
DEFAULT_TAIL = 20


class EngineLog:
    """Append-only engine failures for one repo.

    Parameters
    ----------
    path:
        The log file, normally ``.aic-dc/engine-errors.jsonl``. Created on
        first append; constructing this touches no disk.
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
        # Serialises appends so two concurrent writers cannot interleave a
        # partial line. One process, so a lock is enough.
        self._lock = asyncio.Lock()
        self._malformed_lines = 0
        self._write_failures = 0

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def append(
        self,
        kind: str,
        *,
        message: str,
        health: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record one failure. Returns the record, or ``None`` if unwritten.

        ``session_id`` is optional and **not** a reason to drop the record,
        which is the whole difference from
        :meth:`~aic_dc.claude_code.events_log.EventsLog.append`. A failure
        that precedes every session is the case this file exists for; the
        field is carried when there is one so a failure *during* a session
        can be correlated with it.

        ``health`` is an :meth:`EngineHealth.to_dict` snapshot. The fields
        worth keeping are copied out rather than the whole dict being
        stored: most of it is unchanging (the SDK version, the mirror-gap
        tolerance) and would make every record mostly boilerplate, while the
        four kept here are the ones that differ between a working start and
        a failed one.

        Raises
        ------
        ValueError
            For a ``kind`` outside :data:`ERROR_KINDS`. That is a programmer
            error, and a record no reader can render is indistinguishable
            from a missing one.
        """
        if kind not in ERROR_KINDS:
            raise ValueError(
                f"Unknown error kind {kind!r}; expected one of {sorted(ERROR_KINDS)}"
            )

        stamp = self._now()
        record: dict[str, Any] = {
            "id": f"{int(stamp.timestamp() * 1000)}-{self._token()}",
            "timestamp": stamp.isoformat(),
            "kind": kind,
            "message": str(message),
        }
        # Present and null rather than omitted: "this failure had no
        # session" is the normal case here and is information, where in
        # `events.jsonl` an absent `request_id` means "outside a turn" and
        # is omitted. A reader must be able to tell the two apart without
        # knowing which file it is reading.
        record["session_id"] = session_id or None

        if isinstance(health, dict):
            # `credential_source` first among these because for the failure
            # this file was built after — an auth error on a cold start — it
            # is the diagnosis, and the one fact no other surface keeps.
            for key in ("credential_source", "cli_path", "cli_version"):
                value = health.get(key)
                if value:
                    record[key] = value
            tail = health.get("cli_stderr")
            if isinstance(tail, list) and tail:
                # The CLI's own last words. Copied, not referenced: the
                # health record's ring keeps mutating and a stored
                # reference would report a later session's stderr against
                # this failure.
                record["cli_stderr"] = [str(line) for line in tail]

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
            # Never raised onward, for `EventsLog`'s reason turned up a
            # notch: this runs on a path that is *already* failing, and an
            # exception from the recorder would replace the engine's own
            # error with a disk error in the report the user reads.
            self._write_failures += 1
            logger.warning("Could not record an engine failure in %s", self.path)
            return False
        return True

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def load(self, limit: int | None = DEFAULT_TAIL) -> list[dict[str, Any]]:
        """The most recent records, newest last, in write order.

        ``limit`` counts from the end because this is a diagnostic archive
        rather than a history: the failure being investigated is almost
        always the last one. ``None`` returns everything.

        A malformed line is skipped with a warning and counted, for the
        reason ``events.jsonl`` does it: a partial write from a crash must
        not make the rest of the file unreadable — and a crash is exactly
        the company this file keeps.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_sync, limit)

    def _load_sync(self, limit: int | None) -> list[dict[str, Any]]:
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
            records.append(record)

        if limit is not None and limit >= 0:
            # Sliced after parsing rather than before, so `limit` counts
            # readable records: a tail taken over raw lines would silently
            # return fewer than asked for whenever one of them was corrupt.
            records = records[-limit:] if limit else []
        return records

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def malformed_lines(self) -> int:
        return self._malformed_lines

    @property
    def write_failures(self) -> int:
        """Failures we could not record. Non-zero means this file has holes."""
        return self._write_failures
