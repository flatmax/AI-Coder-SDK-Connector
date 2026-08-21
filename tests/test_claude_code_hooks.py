"""Tests for aic_dc.claude_code.hooks — conversion phase 4.

The hook is bookkeeping, and the properties worth pinning are the ones
that fail silently:

- **It never decides anything.** A ``PostToolUse`` hook that returns a
  ``permissionDecision`` shadows ``can_use_tool``, and the permission
  dialog then never appears again for the rest of the session — with no
  error anywhere. The empty return is the invariant.
- **It never raises.** An exception in a hook is reported to the agent as
  a failure of a tool call that actually succeeded, so it retries a write
  that already landed.
- **It coalesces, and it can be forced to finish.** Eight writes in a turn
  must not mean eight whole-index rebuilds; and an agent that writes a
  file and immediately asks for the symbol map must not race itself.
- **It broadcasts, so the file tree follows the agent.** Without this the
  tree silently drifts from the disk for the rest of the session — the
  CLI's ``Write`` never passes through ``Repo``.

The other registration, ``PreCompact``, is one broadcast and the same two
invariants. It earns a hook because the stream's own ``compact_boundary``
arrives *after* the compaction pause, so it can only explain a stall the
user has already read as a hang.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from aic_dc.claude_code.hooks import (
    DEBOUNCE_SECONDS,
    PATH_KEYS,
    WRITE_TOOL_MATCHER,
    Reindexer,
    build_hook_matchers,
    build_post_tool_use_hook,
    build_pre_compact_hook,
    extract_written_paths,
)
from aic_dc.claude_code.messages import Event


class FakeIndex:
    """Records the batches it is asked to re-index."""

    def __init__(self, error=None, indexed=None):
        self.batches: list[list[str]] = []
        self.error = error
        self._indexed = indexed

    def reindex_files(self, paths):
        self.batches.append(list(paths))
        if self.error is not None:
            raise self.error
        return list(paths) if self._indexed is None else list(self._indexed)


class FakeDocBuilder:
    """``note_file_written``, with the True/False contract the tally reads."""

    def __init__(self, interesting=(".md",)):
        self.calls: list[str] = []
        self.interesting = interesting

    def note_file_written(self, rel_path):
        self.calls.append(rel_path)
        return rel_path.endswith(tuple(self.interesting))


class Broadcasts:
    def __init__(self):
        self.events: list[Event] = []

    async def __call__(self, event):
        self.events.append(event)


def post_tool_use_input(path, tool_name="Write", key="file_path"):
    """The shape the CLI hands a ``PostToolUse`` callback."""
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {key: path, "content": "x = 1\n"},
        "tool_response": {"filePath": path},
        "tool_use_id": "toolu_01",
    }


@pytest.fixture
def index():
    return FakeIndex()


@pytest.fixture
def docs():
    return FakeDocBuilder()


@pytest.fixture
def broadcasts():
    return Broadcasts()


@pytest.fixture
def reindexer(tmp_path, index, docs, broadcasts):
    return Reindexer(
        symbol_index=lambda: index,
        doc_builder=docs,
        broadcast=broadcasts,
        repo_root=tmp_path,
        debounce=0.01,
    )


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


class TestItDecidesNothing:
    async def test_the_hook_returns_an_empty_dict(self, reindexer, tmp_path):
        """Anything else can shadow `can_use_tool` and ungate the session."""
        hook = build_post_tool_use_hook(reindexer)
        answer = await hook(
            post_tool_use_input(str(tmp_path / "a.py")), "toolu_01", {"signal": None}
        )
        assert answer == {}

    async def test_no_control_field_appears_on_any_path(
        self, reindexer, tmp_path, caplog
    ):
        """Including the failure paths, which is where one would sneak in."""
        hook = build_post_tool_use_hook(reindexer)
        cases = [
            post_tool_use_input(str(tmp_path / "a.py")),
            post_tool_use_input("/elsewhere/a.py"),
            {"tool_name": "Write", "tool_input": {}},
            {"tool_name": "Write"},
            {},
            None,
        ]
        for case in cases:
            assert await hook(case, "toolu_01", {"signal": None}) == {}

    def test_only_the_write_tools_are_matched(self):
        """Bash is deliberately absent: `PostToolUse` gives us a command,
        not the files it touched, and guessing would be wrong often."""
        assert WRITE_TOOL_MATCHER == "Write|Edit|MultiEdit|NotebookEdit"
        assert "Bash" not in WRITE_TOOL_MATCHER

    def test_the_matcher_mapping_subscribes_to_two_events(self, reindexer):
        matchers = build_hook_matchers(reindexer)
        assert sorted(matchers) == ["PostToolUse", "PreCompact"]
        assert matchers["PostToolUse"][0].matcher == WRITE_TOOL_MATCHER
        assert len(matchers["PostToolUse"][0].hooks) == 1

    def test_precompact_registers_without_a_tool_matcher(self, reindexer):
        """The field filters on a tool name, and this event has none."""
        matcher = build_hook_matchers(reindexer)["PreCompact"][0]
        assert matcher.matcher is None
        assert len(matcher.hooks) == 1

    def test_pretooluse_is_not_subscribed_at_all(self, reindexer):
        """The shadowing hazard is specifically PreToolUse's."""
        assert "PreToolUse" not in build_hook_matchers(reindexer)


class TestItNeverRaises:
    async def test_a_broken_broadcast_is_swallowed(self, reindexer, tmp_path, caplog):
        async def boom(event):
            raise RuntimeError("no clients")

        hook = build_post_tool_use_hook(reindexer, boom)
        with caplog.at_level(logging.WARNING):
            assert await hook(
                post_tool_use_input(str(tmp_path / "a.py")), None, None
            ) == {}
        assert "hook failed" in caplog.text

    async def test_a_failing_reindex_leaves_the_index_as_it_was(
        self, tmp_path, docs, caplog
    ):
        index = FakeIndex(error=RuntimeError("tree-sitter fell over"))
        reindexer = Reindexer(
            symbol_index=lambda: index, doc_builder=docs, repo_root=tmp_path
        )
        reindexer.note_writes([str(tmp_path / "a.py")])
        with caplog.at_level(logging.WARNING):
            await reindexer.flush()
        assert "re-index failed" in caplog.text
        # The doc half still ran: one subsystem's failure is not the other's.
        assert docs.calls == ["a.py"]


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------


class TestPathExtraction:
    def test_it_reads_both_path_keys(self):
        assert extract_written_paths({"file_path": "a.py"}) == ["a.py"]
        assert extract_written_paths({"notebook_path": "a.ipynb"}) == ["a.ipynb"]
        assert set(PATH_KEYS) == {"file_path", "notebook_path"}

    def test_an_unexpected_shape_yields_nothing(self):
        """A wire format we did not expect means re-index nothing, not raise."""
        assert extract_written_paths(None) == []
        assert extract_written_paths("a.py") == []
        assert extract_written_paths({}) == []
        assert extract_written_paths({"file_path": 7}) == []
        assert extract_written_paths({"file_path": ""}) == []

    def test_duplicates_are_dropped(self):
        both = {"file_path": "a.py", "notebook_path": "a.py"}
        assert extract_written_paths(both) == ["a.py"]

    async def test_an_absolute_path_becomes_repo_relative(
        self, reindexer, tmp_path, index
    ):
        """Every index is keyed on repo-relative paths, and so is the tree."""
        (tmp_path / "pkg").mkdir()
        reindexer.note_writes([str(tmp_path / "pkg" / "a.py")])
        await reindexer.flush()
        assert index.batches == [["pkg/a.py"]]

    async def test_a_write_outside_the_repo_is_dropped_quietly(
        self, reindexer, index, broadcasts, caplog
    ):
        """The agent is allowed to write to /tmp; there is just nothing of
        ours to update when it does."""
        with caplog.at_level(logging.DEBUG):
            assert reindexer.note_writes(["/tmp/scratch.py"]) == []
        await reindexer.flush()
        assert index.batches == []
        assert broadcasts.events == []


# ---------------------------------------------------------------------------
# Coalescing and flushing
# ---------------------------------------------------------------------------


class TestFreshness:
    async def test_a_burst_of_writes_becomes_one_rebuild(
        self, reindexer, tmp_path, index
    ):
        """Each re-index ends in two whole-index passes; eight of them for
        one multi-file edit is the thing the debounce exists to prevent."""
        for name in ("a.py", "b.py", "c.py"):
            reindexer.note_writes([str(tmp_path / name)])
        await reindexer.flush()
        assert index.batches == [["a.py", "b.py", "c.py"]]

    async def test_the_debounce_fires_on_its_own(self, reindexer, tmp_path, index):
        """A browser must see the tree refresh without a tool call."""
        reindexer.note_writes([str(tmp_path / "a.py")])
        for _ in range(200):
            if index.batches:
                break
            await asyncio.sleep(0.01)
        assert index.batches == [["a.py"]]

    async def test_flush_beats_the_debounce(self, tmp_path, index):
        """The tool must not wait out the timer, and must not double up."""
        reindexer = Reindexer(
            symbol_index=lambda: index, repo_root=tmp_path, debounce=30.0
        )
        reindexer.note_writes([str(tmp_path / "a.py")])
        await reindexer.flush()
        assert index.batches == [["a.py"]]
        # And the cancelled timer does not fire a second, empty rebuild.
        await asyncio.sleep(0.05)
        assert index.batches == [["a.py"]]

    async def test_flush_with_nothing_pending_is_a_no_op(self, reindexer, index):
        await reindexer.flush()
        assert index.batches == []

    async def test_flush_waits_for_a_rebuild_already_in_flight(self, tmp_path):
        """Otherwise a tool reads the index mid-rebuild and answers from it."""
        # threading, not asyncio, primitives: the rebuild runs in an
        # executor thread, and `asyncio.Event.set` from another thread does
        # not reliably wake the loop.
        started = threading.Event()
        release = threading.Event()
        finished: list[str] = []

        class SlowIndex:
            def reindex_files(self, paths):
                started.set()
                release.wait(5)
                finished.append("done")
                return list(paths)

        reindexer = Reindexer(
            symbol_index=lambda: SlowIndex(), repo_root=tmp_path, debounce=0.001
        )
        reindexer.note_writes([str(tmp_path / "a.py")])
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        waiter = asyncio.ensure_future(reindexer.flush())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        release.set()
        await waiter
        assert finished == ["done"]

    async def test_a_flush_does_not_abandon_the_batch_a_drain_is_holding(
        self, tmp_path
    ):
        """The drain takes its batch off the queue before it rebuilds, so a
        flush that cancelled it mid-rebuild would lose those files with no
        trace — and then report the index fresh."""
        release = threading.Event()
        seen: list[list[str]] = []

        class SlowIndex:
            def reindex_files(self, paths):
                seen.append(list(paths))
                release.wait(5)
                return list(paths)

        reindexer = Reindexer(
            symbol_index=lambda: SlowIndex(), repo_root=tmp_path, debounce=0.001
        )
        reindexer.note_writes([str(tmp_path / "a.py")])
        for _ in range(500):
            if seen:
                break
            await asyncio.sleep(0.01)

        waiter = asyncio.ensure_future(reindexer.flush())
        await asyncio.sleep(0.05)
        release.set()
        await waiter
        # The one file that was mid-rebuild is accounted for, exactly once.
        assert seen == [["a.py"]]
        assert reindexer.take_reindexed() == ["a.py"]

    async def test_the_debounced_path_survives_a_broken_drain(
        self, tmp_path, caplog
    ):
        """Nobody awaits the debounced drain, so nobody would see it raise:
        without a guard it becomes a stray "never retrieved" much later."""

        class BrokenDocs:
            def note_file_written(self, rel_path):
                raise RuntimeError("doc index fell over")

        reindexer = Reindexer(
            symbol_index=lambda: None,
            doc_builder=BrokenDocs(),
            repo_root=tmp_path,
            debounce=0.001,
        )
        with caplog.at_level(logging.WARNING):
            reindexer.note_writes([str(tmp_path / "a.py")])
            for _ in range(200):
                if "Debounced re-index failed" in caplog.text:
                    break
                await asyncio.sleep(0.01)
        assert "doc index fell over" in caplog.text
        # And the reindexer is still usable afterwards.
        await reindexer.flush()

    async def test_writes_landing_during_a_rebuild_are_not_lost(
        self, tmp_path, index
    ):
        """The second round is what catches them."""
        reindexer = Reindexer(
            symbol_index=lambda: index, repo_root=tmp_path, debounce=30.0
        )
        reindexer.note_writes([str(tmp_path / "a.py")])
        await reindexer.flush()
        reindexer.note_writes([str(tmp_path / "b.py")])
        await reindexer.flush()
        assert index.batches == [["a.py"], ["b.py"]]

    def test_a_queue_with_no_loop_survives_until_the_next_flush(
        self, tmp_path, index
    ):
        """`note_writes` off the loop cannot arm a timer; nothing is lost.

        Synchronous on purpose — the point is that there is no running loop
        when the write is noted, which is the case for a caller of our own
        rather than for the hook.
        """
        reindexer = Reindexer(symbol_index=lambda: index, repo_root=tmp_path)
        assert reindexer.note_writes([str(tmp_path / "a.py")]) == ["a.py"]
        assert index.batches == []
        asyncio.run(reindexer.flush())
        assert index.batches == [["a.py"]]

    def test_the_debounce_default_is_sub_second(self):
        """Long enough to coalesce an edit, short enough not to be felt."""
        assert 0 < DEBOUNCE_SECONDS < 1


# ---------------------------------------------------------------------------
# What it tells the rest of the system
# ---------------------------------------------------------------------------


class TestBroadcast:
    async def test_it_pushes_files_modified_session_wide(
        self, reindexer, tmp_path, broadcasts
    ):
        """The file tree is the same tree for every watching browser,
        including one that did not send this turn."""
        hook = build_post_tool_use_hook(reindexer, broadcasts)
        await hook(post_tool_use_input(str(tmp_path / "pkg.py")), None, None)
        assert [e.name for e in broadcasts.events] == ["filesModified"]
        event = broadcasts.events[0]
        assert event.payload == ["pkg.py"]
        assert event.turn_scoped is False

    async def test_nothing_is_pushed_for_a_write_we_cannot_place(
        self, reindexer, broadcasts
    ):
        hook = build_post_tool_use_hook(reindexer, broadcasts)
        await hook(post_tool_use_input("/elsewhere/a.py"), None, None)
        assert broadcasts.events == []

    async def test_a_notebook_edit_is_picked_up_too(
        self, reindexer, tmp_path, broadcasts
    ):
        hook = build_post_tool_use_hook(reindexer, broadcasts)
        await hook(
            post_tool_use_input(
                str(tmp_path / "nb.ipynb"),
                tool_name="NotebookEdit",
                key="notebook_path",
            ),
            None,
            None,
        )
        assert broadcasts.events[0].payload == ["nb.ipynb"]


# ---------------------------------------------------------------------------
# PreCompact — the pause the stream reports too late
# ---------------------------------------------------------------------------


class TestPreCompact:
    """Why this is a hook at all.

    The stream does announce compaction — ``compact_boundary``, which
    ``messages.py`` turns into ``compactionEvent`` — but it arrives when
    compaction has *finished*. On a long session that is tens of seconds
    after the silence began, so the only thing it can explain is a stall
    the user has already read as a hang. This fires before the pause.
    """

    def input_data(self, trigger="auto", **extra):
        """The shape the CLI hands a ``PreCompact`` callback."""
        return {
            "hook_event_name": "PreCompact",
            "session_id": "sess-1",
            "transcript_path": "/tmp/sess-1.jsonl",
            "trigger": trigger,
            "custom_instructions": None,
            **extra,
        }

    async def test_it_announces_the_compaction(self, broadcasts):
        hook = build_pre_compact_hook(broadcasts)
        assert await hook(self.input_data(), None, None) == {}
        event = broadcasts.events[0]
        assert event.name == "systemEvent"
        assert event.payload["subtype"] == "pre_compact"
        assert event.payload["data"]["trigger"] == "auto"

    async def test_it_stays_turn_scoped(self, broadcasts):
        """Not because it belongs to a turn — because of the arity.

        ``_broadcast`` dispatches a session-wide event as
        ``systemEvent(payload)`` and a turn-scoped one as
        ``systemEvent(request_id, payload)``. The shell's handler takes
        the pair, so a session-wide event here would arrive with the
        payload bound to ``requestId`` and never reach the toast.
        """
        hook = build_pre_compact_hook(broadcasts)
        await hook(self.input_data(), None, None)
        assert broadcasts.events[0].turn_scoped is True

    async def test_a_manual_compaction_says_so(self, broadcasts):
        """`/compact` rather than a full context window."""
        hook = build_pre_compact_hook(broadcasts)
        await hook(self.input_data(trigger="manual"), None, None)
        assert broadcasts.events[0].payload["data"]["trigger"] == "manual"

    async def test_custom_instructions_are_carried(self, broadcasts):
        hook = build_pre_compact_hook(broadcasts)
        await hook(
            self.input_data(custom_instructions="keep the API notes"), None, None
        )
        data = broadcasts.events[0].payload["data"]
        assert data["custom_instructions"] == "keep the API notes"

    async def test_an_unexpected_shape_still_announces_the_pause(self, broadcasts):
        """A missing trigger is no reason to leave the user with silence."""
        hook = build_pre_compact_hook(broadcasts)
        for case in ({}, {"hook_event_name": "PreCompact"}, None, "nonsense"):
            broadcasts.events.clear()
            assert await hook(case, None, None) == {}
            assert broadcasts.events[0].payload["subtype"] == "pre_compact"
            assert broadcasts.events[0].payload["data"]["trigger"] is None

    async def test_it_decides_nothing(self, broadcasts):
        """Observational like its neighbour, and for the same reason."""
        hook = build_pre_compact_hook(broadcasts)
        assert await hook(self.input_data(), None, None) == {}

    async def test_it_never_raises(self, caplog):
        async def boom(event):
            raise RuntimeError("no clients")

        hook = build_pre_compact_hook(boom)
        with caplog.at_level(logging.WARNING):
            assert await hook(self.input_data(), None, None) == {}
        assert "PreCompact" in caplog.text

    async def test_no_broadcast_is_not_a_failure(self):
        """Constructed without one in tests, and in a headless run."""
        hook = build_pre_compact_hook(None)
        assert await hook(self.input_data(), None, None) == {}


class TestReindexedTally:
    async def test_it_reports_what_was_actually_refreshed(
        self, tmp_path, docs
    ):
        """`files_reindexed` in the turn footer is the frontend's only
        evidence that the agent's edits reached the indexes."""
        index = FakeIndex(indexed=["a.py"])
        reindexer = Reindexer(
            symbol_index=lambda: index, doc_builder=docs, repo_root=tmp_path
        )
        reindexer.note_writes([
            str(tmp_path / "a.py"),
            str(tmp_path / "notes.md"),
            str(tmp_path / "data.bin"),
        ])
        await reindexer.flush()
        # a.py from the symbol index, notes.md from the doc builder, and
        # data.bin from neither — which is the point of asking them both.
        assert reindexer.take_reindexed() == ["a.py", "notes.md"]

    async def test_the_tally_is_taken_not_accumulated(
        self, reindexer, tmp_path
    ):
        """A list that grew across a session would re-claim old files."""
        reindexer.note_writes([str(tmp_path / "a.py")])
        await reindexer.flush()
        assert reindexer.take_reindexed() == ["a.py"]
        assert reindexer.take_reindexed() == []

    async def test_a_missing_index_still_lets_the_doc_half_run(
        self, tmp_path, docs
    ):
        """Startup order: writes can land before the symbol index exists."""
        reindexer = Reindexer(
            symbol_index=lambda: None, doc_builder=docs, repo_root=tmp_path
        )
        reindexer.note_writes([str(tmp_path / "notes.md")])
        await reindexer.flush()
        assert docs.calls == ["notes.md"]
        assert reindexer.take_reindexed() == ["notes.md"]
