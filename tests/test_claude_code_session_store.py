"""Tests for the repo-local ``SessionStore``.

The first two tests are the ones that matter. ``run_session_store_conformance``
is the SDK's own 14-contract suite, and the gate is not "the harness passed" —
a harness given a store missing the optional four passes six contracts and
reports success. So the presence test asserts what the harness's own
``_has_optional`` probe would see, and the conformance test passes an empty
``skip_optional`` so nothing is waived.

Everything after those covers behaviour the suite does not reach: path
traversal, duplicate suppression across a restart, malformed lines, and the
ordering guarantee under concurrent appends.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from claude_agent_sdk import SessionStore

from aic_dc.claude_code.session_store import (
    RepoSessionStore,
    SessionStoreKeyError,
)

OPTIONAL_METHODS = (
    "list_sessions",
    "list_session_summaries",
    "delete",
    "list_subkeys",
)

KEY = {"project_key": "proj", "session_id": "sess"}


def entry(**fields):
    """An entry shaped like the suite's: ``type`` is the only required field."""
    return {"type": "x", **fields}


@pytest.fixture
def store(tmp_path):
    return RepoSessionStore(tmp_path / "sessions")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_optional_method_is_really_overridden(store):
    """The SDK probes by attribute identity, so a gap degrades silently.

    This is the harness's own ``_has_optional`` check, spelled out: if one
    of these resolved to the Protocol default, the conformance suite would
    skip its contracts and still report success.
    """
    for name in OPTIONAL_METHODS:
        ours = getattr(type(store), name, None)
        default = getattr(SessionStore, name, None)
        assert ours is not None, f"{name} is missing entirely"
        assert ours is not default, f"{name} resolves to the Protocol default"


def test_required_methods_are_present(store):
    for name in ("append", "load"):
        assert callable(getattr(store, name, None)), name


async def test_session_store_conformance(tmp_path):
    """All 14 contracts, nothing waived.

    ``make_store`` is called once per contract for isolation, so each gets
    its own directory — a shared root would let one contract's sessions
    show up in another's ``list_sessions``.
    """
    from claude_agent_sdk.testing import run_session_store_conformance

    counter = iter(range(1000))

    def make_store():
        return RepoSessionStore(tmp_path / f"run-{next(counter)}")

    await run_session_store_conformance(make_store, skip_optional=frozenset())


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


async def test_layout_is_the_documented_one(store, tmp_path):
    await store.append(KEY, [entry(uuid="a")])
    await store.append({**KEY, "subpath": "subagents/agent-1"}, [entry(uuid="b")])

    root = tmp_path / "sessions"
    assert (root / "proj" / "sess.jsonl").is_file()
    assert (root / "proj" / "sess.summary.json").is_file()
    assert (root / "proj" / "sess" / "subagents" / "agent-1.jsonl").is_file()


async def test_construction_touches_no_disk(tmp_path):
    root = tmp_path / "sessions"
    RepoSessionStore(root)
    assert not root.exists()


async def test_empty_append_creates_nothing(store, tmp_path):
    """An empty transcript would list as a session and load as ``[]``."""
    await store.append(KEY, [])
    assert not (tmp_path / "sessions").exists()
    assert await store.load(KEY) is None
    assert await store.list_sessions("proj") == []


async def test_entries_are_stored_verbatim(store, tmp_path):
    """Byte-level check the conformance suite deliberately does not make.

    ``type`` first is load-bearing for the SDK's lite session parser, which
    matches ``{"type":"tag"`` as a line prefix.
    """
    await store.append(KEY, [{"uuid": "a", "type": "user", "nested": {"b": [1, 2]}}])
    line = (tmp_path / "sessions" / "proj" / "sess.jsonl").read_text().splitlines()[0]
    assert line.startswith('{"type":"user"')
    assert json.loads(line) == {"uuid": "a", "type": "user", "nested": {"b": [1, 2]}}


async def test_non_ascii_survives_the_round_trip(store):
    await store.append(KEY, [entry(uuid="a", text="héllo — ✅ 日本語")])
    loaded = await store.load(KEY)
    assert loaded == [entry(uuid="a", text="héllo — ✅ 日本語")]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_duplicate_uuid_is_ignored_not_rewritten(store):
    """A retried mirror batch must not double-write. Append-only, so ignore."""
    await store.append(KEY, [entry(uuid="a", n=1), entry(uuid="b", n=2)])
    await store.append(KEY, [entry(uuid="b", n=99), entry(uuid="c", n=3)])

    assert await store.load(KEY) == [
        entry(uuid="a", n=1),
        entry(uuid="b", n=2),  # the original, not the n=99 retry
        entry(uuid="c", n=3),
    ]


async def test_duplicate_suppression_survives_a_restart(tmp_path):
    """A fresh process re-importing a transcript must not duplicate it.

    ``import_session_to_store`` is the documented repair path, and it
    replays the whole local transcript.
    """
    first = RepoSessionStore(tmp_path / "sessions")
    await first.append(KEY, [entry(uuid="a"), entry(uuid="b")])

    second = RepoSessionStore(tmp_path / "sessions")
    await second.append(KEY, [entry(uuid="a"), entry(uuid="b"), entry(uuid="c")])

    assert await second.load(KEY) == [entry(uuid="a"), entry(uuid="b"), entry(uuid="c")]


async def test_entries_without_uuid_are_not_deduplicated(store):
    """Two identical uuid-less entries are two records, not one."""
    await store.append(KEY, [entry(n=1)])
    await store.append(KEY, [entry(n=1)])
    assert await store.load(KEY) == [entry(n=1), entry(n=1)]


async def test_uuid_scope_is_the_file_not_the_session(store):
    """A subagent re-using the parent's uuid is a different record."""
    sub = {**KEY, "subpath": "subagents/agent-1"}
    await store.append(KEY, [entry(uuid="shared", where="main")])
    await store.append(sub, [entry(uuid="shared", where="sub")])

    assert await store.load(KEY) == [entry(uuid="shared", where="main")]
    assert await store.load(sub) == [entry(uuid="shared", where="sub")]


async def test_all_duplicates_does_not_refold_the_summary(store):
    """Re-folding a replayed batch would double-count the session."""
    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    before = (await store.list_session_summaries("proj"))[0]

    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    after = (await store.list_session_summaries("proj"))[0]

    assert after["data"] == before["data"]


async def test_reused_session_id_after_delete_starts_clean(store):
    """Stale in-memory ids would suppress the new session's first entries."""
    await store.append(KEY, [entry(uuid="a", n=1)])
    await store.delete(KEY)
    await store.append(KEY, [entry(uuid="a", n=2)])
    assert await store.load(KEY) == [entry(uuid="a", n=2)]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


async def test_concurrent_appends_keep_call_order(store):
    """The mirror flushes eagerly, so batches overlap. Order is a contract.

    The per-session lock is taken before any await and asyncio wakes
    waiters FIFO, so the order these coroutines were created in is the
    order they land in.
    """
    await asyncio.gather(*(store.append(KEY, [entry(uuid=str(n), n=n)]) for n in range(25)))
    assert [e["n"] for e in await store.load(KEY)] == list(range(25))


async def test_a_subagent_append_does_not_block_on_another_session(store):
    """Different sessions use different locks; this would deadlock if not."""
    await asyncio.gather(
        store.append(KEY, [entry(uuid="a")]),
        store.append({"project_key": "proj", "session_id": "other"}, [entry(uuid="b")]),
    )
    assert await store.load(KEY) == [entry(uuid="a")]


# ---------------------------------------------------------------------------
# Append observers
# ---------------------------------------------------------------------------
#
# The hook the service builds image pointers on: a pointer needs the entry's
# ``uuid``, which does not exist until the CLI has written the entry. So the
# properties tested here are the ones that decision rests on — post-dedup
# entries, the key verbatim, order, and above all that a subscriber cannot
# damage the mirror.


async def test_an_observer_sees_what_was_written(store):
    seen: list[tuple[dict, list]] = []
    store.add_append_observer(lambda key, written: seen.append((key, written)))

    await store.append(KEY, [entry(uuid="a"), entry(uuid="b")])

    assert len(seen) == 1
    assert seen[0][1] == [entry(uuid="a"), entry(uuid="b")]


async def test_an_observer_sees_the_key_verbatim(store):
    """Including ``subpath`` — telling a subagent's batch from the session's."""
    seen: list[dict] = []
    store.add_append_observer(lambda key, written: seen.append(key))
    sub = {**KEY, "subpath": "subagents/agent-1"}

    await store.append(KEY, [entry(uuid="a")])
    await store.append(sub, [entry(uuid="b")])

    assert seen == [KEY, sub]


async def test_an_observer_is_told_only_about_new_entries(store):
    """Post-dedup: a retried batch re-sends entries nobody should react to twice."""
    seen: list[list] = []
    store.add_append_observer(lambda key, written: seen.append(written))

    await store.append(KEY, [entry(uuid="a")])
    await store.append(KEY, [entry(uuid="a"), entry(uuid="b")])

    assert seen == [[entry(uuid="a")], [entry(uuid="b")]]


async def test_an_all_duplicate_batch_notifies_nobody(store):
    calls = []
    store.add_append_observer(lambda key, written: calls.append(written))

    await store.append(KEY, [entry(uuid="a")])
    await store.append(KEY, [entry(uuid="a")])

    assert len(calls) == 1


async def test_an_empty_append_notifies_nobody(store):
    calls = []
    store.add_append_observer(lambda key, written: calls.append(written))
    await store.append(KEY, [])
    assert calls == []


async def test_an_observer_runs_after_the_bytes_are_on_disk(store, tmp_path):
    """What makes the entry readable by uuid from inside the callback."""
    found: list[list] = []

    def observe(key, written):
        path = tmp_path / "sessions" / "proj" / "sess.jsonl"
        found.append([json.loads(line) for line in path.read_text().splitlines()])

    store.add_append_observer(observe)
    await store.append(KEY, [entry(uuid="a")])

    assert found == [[entry(uuid="a")]]


async def test_a_failing_observer_does_not_fail_the_mirror(store, caplog):
    """The SDK surfaces an ``append`` exception as a hole in the transcript."""
    reached = []

    def explode(key, written):
        raise RuntimeError("subscriber is broken")

    store.add_append_observer(explode)
    store.add_append_observer(lambda key, written: reached.append(written))

    with caplog.at_level(logging.ERROR):
        await store.append(KEY, [entry(uuid="a")])

    assert await store.load(KEY) == [entry(uuid="a")]
    # The one that raised does not stop the one after it.
    assert reached == [[entry(uuid="a")]]
    assert "observer failed" in caplog.text


async def test_observers_see_batches_in_write_order(store):
    """Notified under the same lock the write took, so the two orders agree."""
    seen: list[int] = []
    store.add_append_observer(
        lambda key, written: seen.extend(e["n"] for e in written)
    )

    await asyncio.gather(
        *(store.append(KEY, [entry(uuid=str(n), n=n)]) for n in range(25))
    )

    assert seen == list(range(25))
    assert [e["n"] for e in await store.load(KEY)] == list(range(25))


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


async def test_summary_mtime_matches_list_sessions_exactly(store):
    """Same filesystem value on both sides, so the fast path never looks stale.

    ``list_sessions_from_store`` treats ``summary.mtime < known mtime`` as
    stale and falls back to a full ``load()`` per session.
    """
    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    summary = (await store.list_session_summaries("proj"))[0]
    listed = (await store.list_sessions("proj"))[0]
    assert summary["mtime"] == listed["mtime"]


async def test_summary_survives_a_restart(tmp_path):
    """The sidecar is the fold's ``prev`` on the next process's first append."""
    first = RepoSessionStore(tmp_path / "sessions")
    await first.append(
        KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z", customTitle="kept")]
    )

    second = RepoSessionStore(tmp_path / "sessions")
    await second.append(KEY, [entry(uuid="b", timestamp="2024-01-01T00:00:01.000Z")])

    summary = (await second.list_session_summaries("proj"))[0]
    assert summary["session_id"] == "sess"
    assert "kept" in json.dumps(summary["data"])


async def test_unreadable_summary_is_skipped_not_raised(store, tmp_path):
    """The fold recomputes it; a truncated sidecar must not hide a session."""
    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    (tmp_path / "sessions" / "proj" / "sess.summary.json").write_text("{trunca")

    assert await store.list_session_summaries("proj") == []
    # The transcript is still listed and still loadable.
    assert [s["session_id"] for s in await store.list_sessions("proj")] == ["sess"]
    assert await store.load(KEY) == [
        entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")
    ]


async def test_summary_temp_file_is_never_listed(store, tmp_path):
    """The atomic-write temp name must not match either listing filter."""
    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    (tmp_path / "sessions" / "proj" / "sess.summary.json.tmp").write_text("{}")

    assert len(await store.list_session_summaries("proj")) == 1
    assert len(await store.list_sessions("proj")) == 1


# ---------------------------------------------------------------------------
# Reading damaged transcripts
# ---------------------------------------------------------------------------


async def test_malformed_line_is_skipped_and_counted(store, tmp_path):
    """A crash mid-write must not make the rest of a session unreadable."""
    await store.append(KEY, [entry(uuid="a"), entry(uuid="b")])
    path = tmp_path / "sessions" / "proj" / "sess.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"x","uuid":"c"\n')  # truncated
    await store.append(KEY, [entry(uuid="d")])

    assert await store.load(KEY) == [entry(uuid="a"), entry(uuid="b"), entry(uuid="d")]
    assert store.malformed_lines == 1


async def test_blank_lines_are_ignored(store, tmp_path):
    await store.append(KEY, [entry(uuid="a")])
    path = tmp_path / "sessions" / "proj" / "sess.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert await store.load(KEY) == [entry(uuid="a")]
    assert store.malformed_lines == 0


# ---------------------------------------------------------------------------
# Key safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        {"project_key": "..", "session_id": "sess"},
        {"project_key": "proj", "session_id": ".."},
        {"project_key": "proj", "session_id": "../../etc/passwd"},
        {"project_key": "proj", "session_id": "a/b"},
        {"project_key": "proj", "session_id": "a\\b"},
        {"project_key": "proj", "session_id": "a\0b"},
        {"project_key": "proj", "session_id": ""},
        {"project_key": "", "session_id": "sess"},
        {"project_key": "proj", "session_id": "."},
        {"project_key": "proj", "session_id": "sess", "subpath": ""},
        {"project_key": "proj", "session_id": "sess", "subpath": "../escape"},
        {"project_key": "proj", "session_id": "sess", "subpath": "a/../../b"},
        {"project_key": "proj", "session_id": "sess", "subpath": "/abs"},
        {"project_key": "proj", "session_id": "sess", "subpath": "a//b"},
        {"project_key": "proj", "session_id": 42},
        {"session_id": "sess"},
        {"project_key": "proj"},
    ],
)
async def test_unsafe_keys_are_rejected_loudly(store, key):
    """Raising surfaces as a ``MirrorErrorMessage``; writing elsewhere does not."""
    with pytest.raises(SessionStoreKeyError):
        await store.append(key, [entry(uuid="a")])
    with pytest.raises(SessionStoreKeyError):
        await store.load(key)


async def test_session_id_need_not_be_a_uuid(store):
    """The suite itself appends under ``sess``, ``a`` and ``summ-sess``.

    UUID validation belongs at the RPC boundary, not here.
    """
    await store.append({"project_key": "proj", "session_id": "not-a-uuid"}, [entry()])
    assert await store.load({"project_key": "proj", "session_id": "not-a-uuid"}) == [
        entry()
    ]


async def test_list_subkeys_rejects_a_subpath_key(store):
    """Ignoring the subpath would silently answer a different question."""
    await store.append(KEY, [entry(uuid="a")])
    with pytest.raises(SessionStoreKeyError):
        await store.list_subkeys({**KEY, "subpath": "subagents/agent-1"})


async def test_list_sessions_rejects_an_unsafe_project_key(store):
    with pytest.raises(SessionStoreKeyError):
        await store.list_sessions("../..")
    with pytest.raises(SessionStoreKeyError):
        await store.list_session_summaries("../..")


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def test_deleting_a_subpath_prunes_its_empty_directories(store, tmp_path):
    sub = {**KEY, "subpath": "subagents/agent-1"}
    await store.append(KEY, [entry(uuid="a")])
    await store.append(sub, [entry(uuid="b")])

    await store.delete(sub)

    assert not (tmp_path / "sessions" / "proj" / "sess").exists()
    # The main transcript is untouched.
    assert await store.load(KEY) == [entry(uuid="a")]


async def test_deleting_a_subpath_keeps_a_shared_directory(store, tmp_path):
    await store.append({**KEY, "subpath": "subagents/agent-1"}, [entry(uuid="a")])
    await store.append({**KEY, "subpath": "subagents/agent-2"}, [entry(uuid="b")])

    await store.delete({**KEY, "subpath": "subagents/agent-1"})

    assert await store.list_subkeys(KEY) == ["subagents/agent-2"]


async def test_delete_removes_the_sidecar(store, tmp_path):
    await store.append(KEY, [entry(uuid="a", timestamp="2024-01-01T00:00:00.000Z")])
    await store.delete(KEY)

    assert await store.list_session_summaries("proj") == []
    assert not (tmp_path / "sessions" / "proj" / "sess.summary.json").exists()


async def test_delete_is_idempotent(store):
    await store.append(KEY, [entry(uuid="a")])
    await store.delete(KEY)
    await store.delete(KEY)
    assert await store.load(KEY) is None


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------


async def test_total_bytes_counts_a_missing_root_as_zero(tmp_path):
    assert RepoSessionStore(tmp_path / "absent").total_bytes() == 0


async def test_total_bytes_grows_with_the_transcript(store):
    await store.append(KEY, [entry(uuid="a", text="x" * 1000)])
    assert store.total_bytes() > 1000


# ---------------------------------------------------------------------------
# The SDK's own readers — the browse path the history RPCs are built on
# ---------------------------------------------------------------------------


class TestTheSdkReadersCanReadIt:
    """Conformance proves the protocol; this proves the readers.

    ``history_list`` and ``history_load`` go through these functions rather
    than parsing entries themselves, and they exercise three things the
    14 contracts never touch: the project key has to be the SDK's own, the
    sidecar has to feed the listing fast path, and ``list_subkeys`` has to
    return ``/``-joined subpaths the subagent parser recognises.

    They also validate ``session_id`` as a UUID internally, which is why
    these fixtures use real ones and the store itself does not care.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        path = tmp_path / "repo"
        path.mkdir()
        return path

    @pytest.fixture
    def session_id(self):
        import uuid

        return str(uuid.uuid4())

    @pytest.fixture
    def project_key(self, repo):
        from claude_agent_sdk import project_key_for_directory

        return project_key_for_directory(str(repo))

    @pytest.fixture
    async def populated(self, tmp_path, repo, session_id, project_key):
        """A transcript shaped the way the CLI writes one.

        Entries are linked by ``parentUuid``; the readers walk that chain
        from the leaf back, so an unlinked transcript returns one message
        no matter how many entries it holds.
        """
        store = RepoSessionStore(tmp_path / "sessions")
        key = {"project_key": project_key, "session_id": session_id}
        await store.append(
            key,
            [
                {
                    "type": "user",
                    "uuid": "u1",
                    "parentUuid": None,
                    "timestamp": "2026-08-16T00:00:00.000Z",
                    "sessionId": session_id,
                    "cwd": str(repo),
                    "message": {"role": "user", "content": "hello there"},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "timestamp": "2026-08-16T00:00:01.000Z",
                    "sessionId": session_id,
                    "cwd": str(repo),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hi back"}],
                    },
                },
            ],
        )
        await store.append(
            {**key, "subpath": "subagents/agent-abc"},
            [
                {
                    "type": "user",
                    "uuid": "s1",
                    "parentUuid": None,
                    "timestamp": "2026-08-16T00:00:02.000Z",
                    "sessionId": session_id,
                    "message": {"role": "user", "content": "sub task"},
                }
            ],
        )
        return store

    async def test_the_listing_reads_our_sidecar(self, populated, repo, session_id):
        """Proves the fast path is taken: `summary` and `first_prompt` come
        out of the persisted fold, not out of a per-session load()."""
        from claude_agent_sdk._internal.sessions import list_sessions_from_store

        infos = await list_sessions_from_store(populated, str(repo))
        assert [i.session_id for i in infos] == [session_id]
        assert infos[0].first_prompt == "hello there"
        assert infos[0].cwd == str(repo)

    async def test_messages_come_back_in_order(self, populated, repo, session_id):
        from claude_agent_sdk._internal.sessions import (
            get_session_messages_from_store,
        )

        messages = await get_session_messages_from_store(
            populated, session_id, str(repo)
        )
        assert [m.type for m in messages] == ["user", "assistant"]
        assert messages[0].message["content"] == "hello there"
        assert messages[1].message["content"] == [{"type": "text", "text": "hi back"}]

    async def test_subagents_are_discoverable(self, populated, repo, session_id):
        """The SDK parses `subagents/agent-<id>` out of the raw subpath, so
        the separator our store returns is interface."""
        from claude_agent_sdk._internal.sessions import (
            get_subagent_messages_from_store,
            list_subagents_from_store,
        )

        assert await list_subagents_from_store(populated, session_id, str(repo)) == [
            "abc"
        ]
        messages = await get_subagent_messages_from_store(
            populated, session_id, "abc", str(repo)
        )
        assert [m.message["content"] for m in messages] == ["sub task"]

    async def test_session_info_is_found_by_id(self, populated, repo, session_id):
        from claude_agent_sdk._internal.sessions import get_session_info_from_store

        info = await get_session_info_from_store(populated, session_id, str(repo))
        assert info is not None and info.session_id == session_id

    async def test_the_readers_find_nothing_under_a_different_directory(
        self, populated, tmp_path, session_id
    ):
        """A hand-rolled project key would look here and report an empty repo."""
        from claude_agent_sdk._internal.sessions import list_sessions_from_store

        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        assert await list_sessions_from_store(populated, str(elsewhere)) == []
