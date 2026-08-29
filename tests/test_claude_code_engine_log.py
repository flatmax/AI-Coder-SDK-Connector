"""Tests for ``.aic-dc/engine-errors.jsonl``.

The file exists because an engine that will not start used to leave nothing
behind: a log line to a terminal, a broadcast to whoever was listening, and
a health record in memory (``specs5/next.md`` § C9). So these tests care
about durability and about what a record *carries* — a message with no
credential source beside it is the same dead end the file was built to
close.

They also pin the one thing that distinguishes this log from
``events.jsonl``: a record with no session is written here rather than
dropped, because a connect that fails on authentication is precisely a
failure with no session.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aic_dc.claude_code.engine_log import DEFAULT_TAIL, ERROR_KINDS, EngineLog

START = datetime(2026, 8, 29, 7, 15, 0, 500000, tzinfo=timezone.utc)

HEALTH = {
    "connected": False,
    "cli_path": "/opt/aic/claude",
    "cli_version": "2.1.229",
    "cli_source": "bundled",
    "sdk_version": "0.2.137",
    "credential_source": "subscription login in ~/.claude",
    "auth_warning": None,
    "mirror_gaps": 0,
    "last_error": "invalid API key",
    "cli_stderr": ["error: not authenticated", "run `claude login`"],
}


@pytest.fixture
def log(tmp_path):
    """A log on a fixed clock and a fixed token, so ids are predictable."""
    ticks = iter(range(1000))

    def now():
        return START + timedelta(seconds=next(ticks))

    counter = iter(range(1000))
    return EngineLog(
        tmp_path / "engine-errors.jsonl",
        now=now,
        token=lambda: f"tok{next(counter):05d}",
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_a_record_carries_the_state_that_explains_the_failure(log):
    record = await log.append(
        "startup_failed",
        message="Could not start a Claude Code session: invalid API key",
        health=HEALTH,
    )
    assert record == {
        "id": "1787987700500-tok00000",
        "timestamp": "2026-08-29T07:15:00.500000+00:00",
        "kind": "startup_failed",
        "message": "Could not start a Claude Code session: invalid API key",
        "session_id": None,
        "credential_source": "subscription login in ~/.claude",
        "cli_path": "/opt/aic/claude",
        "cli_version": "2.1.229",
        "cli_stderr": ["error: not authenticated", "run `claude login`"],
    }


async def test_the_credential_source_is_kept_because_it_is_the_diagnosis(log):
    """The one fact no live surface holds once the process is gone, and the
    one that identifies an auth failure."""
    await log.append("startup_failed", message="boom", health=HEALTH)
    (record,) = await log.load()
    assert record["credential_source"] == "subscription login in ~/.claude"


async def test_a_failure_with_no_session_is_written_not_dropped(log):
    """The whole difference from ``EventsLog``, which drops one. A connect
    that fails on auth has no session *because* of the failure."""
    record = await log.append("startup_failed", message="boom")
    assert record is not None
    assert record["session_id"] is None
    assert await log.load() == [record]


async def test_a_session_is_carried_when_there_is_one(log):
    record = await log.append("startup_failed", message="boom", session_id="s1")
    assert record["session_id"] == "s1"


async def test_the_session_field_is_present_and_null_never_absent(log):
    """`null` is information here — "this failure had no session" is the
    normal case — where an absent field would read as a schema that varies."""
    record = await log.append("startup_failed", message="boom")
    assert "session_id" in record


async def test_the_stderr_tail_is_copied_not_referenced(log):
    """The health record's ring keeps mutating; a stored reference would
    report a later session's stderr against this failure."""
    health = dict(HEALTH, cli_stderr=["first"])
    await log.append("startup_failed", message="boom", health=health)
    health["cli_stderr"].append("later, from another connect")
    (record,) = await log.load()
    assert record["cli_stderr"] == ["first"]


async def test_absent_health_fields_are_omitted_rather_than_nulled(log):
    record = await log.append("startup_failed", message="boom", health={})
    assert set(record) == {"id", "timestamp", "kind", "message", "session_id"}


async def test_health_that_is_not_a_dict_is_ignored(log):
    record = await log.append("startup_failed", message="boom", health="nonsense")
    assert record["message"] == "boom"
    assert "cli_path" not in record


async def test_an_unknown_kind_is_a_programmer_error(log):
    with pytest.raises(ValueError, match="Unknown error kind"):
        await log.append("mystery", message="boom")
    assert await log.load() == []


async def test_every_kind_in_the_domain_is_writable(log):
    for kind in sorted(ERROR_KINDS):
        assert await log.append(kind, message="boom") is not None


async def test_a_repeated_failure_is_recorded_every_time(log):
    """Not deduplicated, deliberately: forty identical records is a relaunch
    loop, and collapsing them would hide the loop — the same position
    ``note_cli_stderr`` takes on repeated stderr."""
    for _ in range(3):
        await log.append("startup_failed", message="the same failure")
    assert len(await log.load()) == 3


async def test_each_line_is_one_json_object(log, tmp_path):
    await log.append("startup_failed", message="one", health=HEALTH)
    await log.append("startup_failed", message="two")
    lines = (tmp_path / "engine-errors.jsonl").read_text().splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["one", "two"]


async def test_the_file_is_created_on_first_append_not_on_construction(tmp_path):
    path = tmp_path / "nested" / "engine-errors.jsonl"
    log = EngineLog(path)
    assert not path.exists()
    await log.append("startup_failed", message="boom")
    assert path.exists()


async def test_a_write_that_fails_is_counted_and_never_raised(tmp_path):
    """This runs on a path that is already failing. An exception here would
    replace the engine's own error with a disk error."""
    path = tmp_path / "engine-errors.jsonl"
    path.mkdir()  # A directory where the file should be: every write fails.
    log = EngineLog(path)
    assert await log.append("startup_failed", message="boom") is None
    assert log.write_failures == 1


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_a_missing_file_reads_as_no_failures(tmp_path):
    assert await EngineLog(tmp_path / "nothing.jsonl").load() == []


async def test_records_come_back_in_write_order(log):
    for n in range(3):
        await log.append("startup_failed", message=f"failure {n}")
    assert [r["message"] for r in await log.load()] == [
        "failure 0",
        "failure 1",
        "failure 2",
    ]


async def test_the_tail_is_the_most_recent_records(log):
    for n in range(5):
        await log.append("startup_failed", message=f"failure {n}")
    assert [r["message"] for r in await log.load(2)] == ["failure 3", "failure 4"]


async def test_none_asks_for_everything(log):
    for n in range(3):
        await log.append("startup_failed", message=f"failure {n}")
    assert len(await log.load(None)) == 3


async def test_the_default_tail_is_bounded(log):
    for n in range(DEFAULT_TAIL + 5):
        await log.append("startup_failed", message=f"failure {n}")
    records = await log.load()
    assert len(records) == DEFAULT_TAIL
    assert records[-1]["message"] == f"failure {DEFAULT_TAIL + 4}"


async def test_a_malformed_line_is_skipped_and_counted(log, tmp_path):
    """A crash is exactly the company this file keeps, so a partial write
    must not make the rest unreadable."""
    await log.append("startup_failed", message="before")
    with (tmp_path / "engine-errors.jsonl").open("a") as handle:
        handle.write("{not json\n")
    await log.append("startup_failed", message="after")
    assert [r["message"] for r in await log.load()] == ["before", "after"]
    assert log.malformed_lines == 1


async def test_the_tail_counts_readable_records_not_raw_lines(log, tmp_path):
    """Sliced after parsing. A tail over raw lines would silently return
    fewer than asked for whenever one of them was corrupt."""
    await log.append("startup_failed", message="one")
    with (tmp_path / "engine-errors.jsonl").open("a") as handle:
        handle.write("{not json\n")
    await log.append("startup_failed", message="two")
    assert [r["message"] for r in await log.load(2)] == ["one", "two"]


async def test_a_line_that_is_not_an_object_is_skipped(log, tmp_path):
    with (tmp_path / "engine-errors.jsonl").open("a") as handle:
        handle.write("[1, 2, 3]\n")
    await log.append("startup_failed", message="real")
    assert [r["message"] for r in await log.load()] == ["real"]
    assert log.malformed_lines == 1


async def test_blank_lines_are_not_records(log, tmp_path):
    await log.append("startup_failed", message="real")
    with (tmp_path / "engine-errors.jsonl").open("a") as handle:
        handle.write("\n   \n")
    assert len(await log.load()) == 1
    assert log.malformed_lines == 0
