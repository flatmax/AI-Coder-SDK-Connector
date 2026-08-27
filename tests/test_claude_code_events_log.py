"""Tests for ``.aic-dc/events.jsonl``.

This file is the one thing under ``.aic-dc/`` that is neither the
transcript nor derived from it, so nothing can rebuild it. That shapes
what these tests care about: a record that reaches disk must be readable
and correlatable, a record that does not reach disk must be counted rather
than swallowed, and a delete must take exactly one session's records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aic_dc.claude_code.events_log import (
    EVENT_TYPES,
    EventsLog,
    commit_content,
    permission_mode_content,
    reset_content,
    session_switch_content,
)

START = datetime(2026, 8, 16, 12, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture
def log(tmp_path):
    """A log on a fixed clock and a fixed token, so ids are predictable."""
    ticks = iter(range(1000))

    def now():
        return START + timedelta(seconds=next(ticks))

    counter = iter(range(1000))
    return EventsLog(
        tmp_path / "events.jsonl",
        now=now,
        token=lambda: f"tok{next(counter):05d}",
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_a_record_has_every_required_field(log):
    record = await log.append(
        "commit",
        session_id="s1",
        request_id="r1",
        content=commit_content("abc1234", "fix: a thing"),
        payload={"sha": "abc1234def", "message": "fix: a thing", "files": ["a.py"]},
    )
    assert record == {
        "id": "1786881600123-tok00000",
        "session_id": "s1",
        "timestamp": "2026-08-16T12:00:00.123456+00:00",
        "event": "commit",
        "content": "**Committed** `abc1234`\n\n```\nfix: a thing\n```",
        "request_id": "r1",
        "payload": {"sha": "abc1234def", "message": "fix: a thing", "files": ["a.py"]},
    }


async def test_the_id_and_the_timestamp_come_from_one_clock_read(log):
    """Two reads could straddle a millisecond and disagree."""
    record = await log.append("reset", session_id="s1", content=reset_content())
    stamp = datetime.fromisoformat(record["timestamp"])
    assert record["id"].split("-")[0] == str(int(stamp.timestamp() * 1000))


async def test_request_id_is_omitted_not_nulled_outside_a_turn(log):
    """An event outside a turn has no request to belong to."""
    record = await log.append("reset", session_id="s1", content=reset_content())
    assert "request_id" not in record
    assert "payload" not in record


async def test_records_land_on_disk_as_one_json_line_each(log, tmp_path):
    await log.append("reset", session_id="s1", content=reset_content())
    await log.append("commit", session_id="s1", content="c")

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["event"] for line in lines] == ["reset", "commit"]


async def test_construction_touches_no_disk(tmp_path):
    EventsLog(tmp_path / "events.jsonl")
    assert not (tmp_path / "events.jsonl").exists()


async def test_an_unknown_event_type_raises(log):
    """A discriminator the browser has no renderer for looks like the event
    never happened, which is worse than a loud failure in a test."""
    with pytest.raises(ValueError, match="Unknown event type"):
        await log.append("compact_boundary", session_id="s1", content="x")


async def test_every_event_type_is_accepted(log):
    for event in sorted(EVENT_TYPES):
        assert await log.append(event, session_id="s1", content="x") is not None


async def test_compaction_is_not_one_of_them():
    """It arrives in the transcript and renders from there; a record here
    would be a second account of something the transcript already states."""
    assert "compact_boundary" not in EVENT_TYPES
    assert "compaction" not in EVENT_TYPES


# ---------------------------------------------------------------------------
# The drop that is not silent
# ---------------------------------------------------------------------------


async def test_an_event_with_no_session_is_dropped_and_counted(log, tmp_path):
    """A toolbar commit before the engine ever connected. Every record is
    rendered inside a session, so this one has nowhere to appear — and a
    placeholder ID would file it under somebody else's history."""
    assert await log.append("commit", session_id=None, content="c") is None
    assert log.dropped_without_session == 1
    assert not (tmp_path / "events.jsonl").exists()


async def test_an_empty_session_id_counts_as_no_session(log):
    assert await log.append("commit", session_id="", content="c") is None
    assert log.dropped_without_session == 1


async def test_a_write_failure_is_counted_not_raised(log, tmp_path, monkeypatch):
    """Failing a commit because we could not log it would be the worse
    outcome; hiding that we could not log it would be the worst."""
    target = tmp_path / "events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(target), "open", boom, raising=False)
    assert await log.append("commit", session_id="s1", content="c") is None
    assert log.write_failures == 1


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_load_returns_write_order(log):
    for n in range(5):
        await log.append("reset", session_id="s1", content=str(n))
    assert [r["content"] for r in await log.load()] == ["0", "1", "2", "3", "4"]


async def test_load_filters_by_session(log):
    await log.append("reset", session_id="s1", content="one")
    await log.append("reset", session_id="s2", content="two")
    await log.append("reset", session_id="s1", content="three")

    assert [r["content"] for r in await log.load("s1")] == ["one", "three"]
    assert [r["content"] for r in await log.load("s2")] == ["two"]


async def test_load_of_a_missing_file_is_empty_not_an_error(tmp_path):
    assert await EventsLog(tmp_path / "nope.jsonl").load() == []


async def test_a_malformed_line_is_skipped_and_counted(log, tmp_path):
    await log.append("reset", session_id="s1", content="kept")
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event":"reset"\n')  # truncated
    await log.append("reset", session_id="s1", content="also kept")

    assert [r["content"] for r in await log.load()] == ["kept", "also kept"]
    assert log.malformed_lines == 1


async def test_blank_lines_are_not_malformed(log, tmp_path):
    await log.append("reset", session_id="s1", content="kept")
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert len(await log.load()) == 1
    assert log.malformed_lines == 0


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def test_delete_takes_one_session_and_leaves_the_rest(log):
    await log.append("reset", session_id="s1", content="gone")
    await log.append("reset", session_id="s2", content="stays")
    await log.append("commit", session_id="s1", content="also gone")

    assert await log.delete_session("s1") == 2
    assert [r["content"] for r in await log.load()] == ["stays"]


async def test_deleting_an_unknown_session_rewrites_nothing(log, tmp_path):
    await log.append("reset", session_id="s1", content="stays")
    before = (tmp_path / "events.jsonl").read_bytes()

    assert await log.delete_session("nope") == 0
    assert (tmp_path / "events.jsonl").read_bytes() == before


async def test_deleting_from_a_missing_file_is_not_an_error(tmp_path):
    assert await EventsLog(tmp_path / "nope.jsonl").delete_session("s1") == 0


async def test_a_corrupt_line_survives_a_delete(log, tmp_path):
    """We cannot prove it belongs to the session being deleted, and dropping
    it would turn one bad line into lost history for whoever wrote it."""
    await log.append("reset", session_id="s1", content="gone")
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    await log.append("reset", session_id="s2", content="stays")

    assert await log.delete_session("s1") == 1
    remaining = (tmp_path / "events.jsonl").read_text().splitlines()
    assert "{not json" in remaining
    assert len(remaining) == 2


async def test_no_temp_file_is_left_behind(log, tmp_path):
    await log.append("reset", session_id="s1", content="gone")
    await log.delete_session("s1")
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Content templates
# ---------------------------------------------------------------------------


def test_the_commit_template_matches_what_phase_three_broadcast():
    """The wording carries over verbatim so a user's history reads the same
    either side of the conversion."""
    assert commit_content("abc1234", "fix: a thing") == (
        "**Committed** `abc1234`\n\n```\nfix: a thing\n```"
    )


def test_the_reset_template_matches_what_phase_three_broadcast():
    assert reset_content() == (
        "**Reset to HEAD** — all uncommitted changes have been discarded."
    )


def test_the_permission_mode_template_matches_the_spec():
    assert permission_mode_content("plan") == "Permission mode set to **plan**."


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("resumed", "Resumed session `abc`."),
        ("forked", "Forked session `abc`."),
    ],
)
def test_the_session_switch_template_matches_the_spec(action, expected):
    assert session_switch_content(action, "abc") == expected
