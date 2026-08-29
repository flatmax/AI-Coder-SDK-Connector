"""The doc index's background build — ``aic_dc.doc_index.background``.

The extraction and the enrichment themselves belong to ``DocIndex`` and
``KeywordEnricher`` and are tested against those. What is under test here
is the builder that drives them off the request path: which files it picks,
what it tells the frontend, what its flags say while it works, and what it
does when a phase fails.

The conversion moved this out of ``LLMService`` and changed two things
worth pinning down:

- **The mode gate is gone.** The post-write hook used to re-extract only in
  doc mode or with cross-referencing on. There is no such mode now, so the
  extension is the only question asked.
- **The flags and the executor are the builder's own.** Nothing reaches
  into a service's attributes, and ``close()`` shuts down only a pool the
  builder made itself.

Everything else is preserved behaviour, tested because it is load-bearing:
phase 1 flips ``ready`` and phase 2 is chained rather than awaited (the
outline pane must not wait on keywords); a missing KeyBERT is
``unavailable`` rather than a failure; and no phase is ever allowed to
raise into the caller.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest

from aic_dc.doc_index import background as background_mod
from aic_dc.doc_index.background import DocIndexBuilder


class FakeDocIndex:
    """The ``DocIndex`` surface the builder uses, and only that."""

    def __init__(self, *, extensions=(".md", ".rst")):
        self._extractors = {ext: object() for ext in extensions}
        self._all_outlines: dict[str, object] = {}
        self.index_repo_calls: list[list[str]] = []
        self.index_repo_error: BaseException | None = None
        self.enrich_calls: list[tuple[str, str]] = []
        self.enrich_errors: dict[str, BaseException] = {}
        self.invalidated: list[str] = []
        self.indexed_files: list[tuple[str, str | None]] = []
        self.queue: list[str] = []
        self.needs = True
        self.file_outline: object | None = object()

    @staticmethod
    def _extension_of(path: str) -> str:
        from pathlib import Path

        return Path(path).suffix.lower()

    def index_repo(self, files):
        self.index_repo_calls.append(list(files))
        if self.index_repo_error is not None:
            raise self.index_repo_error
        for path in files:
            self._all_outlines[path] = object()

    def queue_enrichment(self):
        return list(self.queue)

    def enrich_single_file(self, path, source_text):
        self.enrich_calls.append((path, source_text))
        error = self.enrich_errors.get(path)
        if error is not None:
            raise error

    def invalidate_file(self, path):
        self.invalidated.append(path)
        return True

    def index_file(self, path, keyword_model=None):
        self.indexed_files.append((path, keyword_model))
        return self.file_outline

    def needs_enrichment(self, outline):
        return self.needs


class FakeEnricher:
    def __init__(self, *, available=True, loads=True, model_name="bge-small"):
        self._available = available
        self._loads = loads
        self.model_name = model_name
        self.load_calls = 0

    def is_available(self):
        return self._available

    def ensure_loaded(self):
        self.load_calls += 1
        if isinstance(self._loads, BaseException):
            raise self._loads
        return self._loads


class FakeRepo:
    """Tracked-file list and file reads, the builder's only two needs."""

    def __init__(self, files=("README.md", "notes.rst", "main.py"), content="text"):
        self.files = list(files)
        self.content = content
        self.list_error: BaseException | None = None
        self.read_errors: dict[str, BaseException] = {}
        self.reads: list[str] = []

    def get_flat_file_list(self):
        if self.list_error is not None:
            raise self.list_error
        return "\n".join(self.files) + "\n"

    def get_file_content(self, rel_path):
        self.reads.append(rel_path)
        error = self.read_errors.get(rel_path)
        if error is not None:
            raise error
        return self.content


class ProgressRecorder:
    """The ``startupProgress`` channel, recorded."""

    def __init__(self, *, error: BaseException | None = None):
        self.events: list[tuple[str, str, int]] = []
        self.error = error

    async def __call__(self, stage, message, percent):
        self.events.append((stage, message, percent))
        if self.error is not None:
            raise self.error

    def stages(self):
        return [stage for stage, _, _ in self.events]

    def percent_of(self, stage):
        return next(p for s, _, p in self.events if s == stage)


class InlineExecutor:
    """Runs submitted work on the calling thread.

    The builder's real pool is a thread; using one here would make every
    assertion a race. What the executor *is* gets its own tests below.
    """

    def submit(self, fn, *args, **kwargs):
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirrors Executor
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True):
        pass


@pytest.fixture
def doc_index() -> FakeDocIndex:
    return FakeDocIndex()


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def progress() -> ProgressRecorder:
    return ProgressRecorder()


@pytest.fixture
def builder(doc_index, repo, progress) -> DocIndexBuilder:
    """A builder whose blocking work runs inline, with no enricher."""
    return DocIndexBuilder(
        doc_index=doc_index,
        repo=repo,
        progress=progress,
        executor=InlineExecutor(),
    )


def with_enricher(builder: DocIndexBuilder, enricher: FakeEnricher) -> FakeEnricher:
    """Attach an enricher to a built fixture, as construction would."""
    builder._enricher = enricher
    return enricher


async def drain():
    """Let chained fire-and-forget tasks run to completion."""
    for _ in range(10):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# Which files get indexed
# ---------------------------------------------------------------------------


class TestEligibleFiles:
    async def test_only_files_an_extractor_handles(self, builder, doc_index):
        await builder.build()
        assert doc_index.index_repo_calls == [["README.md", "notes.rst"]]

    async def test_the_list_comes_from_the_repo_not_the_filesystem(
        self, builder, repo, doc_index
    ):
        """So ``.gitignore`` and the user's exclusions are honoured."""
        repo.files = ["docs/a.md"]
        await builder.build()
        assert doc_index.index_repo_calls == [["docs/a.md"]]

    async def test_no_repo_indexes_nothing(self, doc_index, progress):
        builder = DocIndexBuilder(
            doc_index=doc_index, progress=progress, executor=InlineExecutor()
        )
        await builder.build()
        assert doc_index.index_repo_calls == []
        assert builder.ready is True, "an empty index is still a finished one"

    async def test_a_repo_that_cannot_list_files_indexes_nothing(
        self, builder, repo, doc_index, caplog
    ):
        repo.list_error = RuntimeError("git ls-files exploded")
        await builder.build()
        assert doc_index.index_repo_calls == []
        assert "git ls-files exploded" in caplog.text

    async def test_no_eligible_files_is_ready_with_no_progress_noise(
        self, builder, repo, progress
    ):
        repo.files = ["main.py", "Makefile"]
        await builder.build()
        assert builder.ready is True
        assert progress.events == []


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------


class TestStructuralPhase:
    async def test_it_reports_start_and_finish(self, builder, progress):
        await builder.build()
        assert progress.stages() == ["doc_index", "doc_index"]
        assert "2 files" in progress.events[0][1]
        assert progress.events[0][2] == 0
        assert progress.events[1][2] == 100

    async def test_ready_flips_and_building_falls(self, builder):
        assert builder.ready is False
        await builder.build()
        assert builder.ready is True
        assert builder.building is False

    async def test_a_failure_is_reported_and_not_raised(
        self, builder, progress, caplog
    ):
        builder._doc_index.index_repo_error = RuntimeError("extractor fell over")
        await builder.build()
        assert builder.ready is False
        assert builder.building is False
        assert progress.stages()[-1] == "doc_index_error"
        assert "extractor fell over" in progress.events[-1][1]

    async def test_a_broken_progress_channel_does_not_break_the_build(
        self, doc_index, repo
    ):
        builder = DocIndexBuilder(
            doc_index=doc_index,
            repo=repo,
            progress=ProgressRecorder(error=RuntimeError("socket closed")),
            executor=InlineExecutor(),
        )
        await builder.build()
        assert builder.ready is True

    async def test_the_status_dict_is_what_the_snapshot_carries(self, builder):
        assert builder.status() == {
            "doc_index_ready": False,
            "doc_index_building": False,
            "doc_index_enriched": False,
            "enrichment_status": "pending",
        }
        await builder.build()
        assert builder.status()["doc_index_ready"] is True

    async def test_enrichment_is_chained_not_awaited(self, builder, doc_index):
        """Phase 1 is what everything gates on; keywords come later."""
        enricher = with_enricher(builder, FakeEnricher())
        doc_index.queue = ["README.md"]
        await builder.build()
        # Phase 1 is done and reported before phase 2 has even started.
        assert builder.ready is True
        assert enricher.load_calls == 0
        await drain()
        assert builder.enriched is True


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestSchedule:
    async def test_it_builds(self, builder, doc_index):
        builder.schedule()
        await drain()
        assert doc_index.index_repo_calls

    async def test_a_finished_index_is_not_rebuilt(self, builder, doc_index):
        builder.ready = True
        builder.schedule()
        await drain()
        assert doc_index.index_repo_calls == []

    async def test_force_rebuilds_a_finished_index(self, builder, doc_index):
        """What review entry and exit need: the whole tree moved."""
        builder.ready = True
        builder.schedule(force=True)
        await drain()
        assert len(doc_index.index_repo_calls) == 1

    async def test_a_build_in_flight_is_never_doubled_up(self, builder, doc_index):
        builder.building = True
        builder.schedule(force=True)
        await drain()
        assert doc_index.index_repo_calls == []

    async def test_build_itself_refuses_to_overlap(self, builder, doc_index):
        builder.building = True
        await builder.build()
        assert doc_index.index_repo_calls == []
        assert builder.building is True, "the running build's flag is not cleared"

    def test_off_loop_scheduling_is_a_log_line_not_a_crash(self, builder, caplog):
        builder.schedule()
        assert builder.ready is False


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------


class TestEnrichmentPreference:
    """``app.json``'s ``doc_index.keywords_enabled``, finally wired.

    The key was parsed by ``ConfigManager.doc_index_config`` and read by
    nothing, so enrichment ran whatever the file said. These pin the three
    things the gate has to get right: that it stops the pass, that it is
    read per pass rather than captured at construction, and that its
    status word is not the one the frontend turns into an install hint.
    """

    async def test_the_preference_stops_the_pass(self, builder):
        with_enricher(builder, FakeEnricher())
        builder._enrichment_enabled = lambda: False
        await builder.run_enrichment()
        assert builder.enrichment_status == "disabled"
        assert builder.enriched is False

    async def test_disabled_is_not_unavailable(self, builder):
        """The words drive different browser behaviour.

        ``unavailable`` raises a one-shot "install ``aic-dc[docs]``" toast.
        Telling somebody how to install what they just switched off is the
        one wrong answer, so the gate runs *before* the installed-ness
        probe and answers with a word the frontend ignores.
        """
        with_enricher(builder, FakeEnricher(available=False))
        builder._enrichment_enabled = lambda: False
        await builder.run_enrichment()
        assert builder.enrichment_status == "disabled"

    async def test_the_preference_is_read_per_pass(self, builder, doc_index):
        """A reload of ``app.json`` has to reach a builder built at startup.

        The builder is constructed once per process, so a boolean captured
        at construction would make the Settings tab's switch an
        app-restart field — and that tab has no disposition between "now"
        and "next session" to describe it with.
        """
        with_enricher(builder, FakeEnricher())
        doc_index.queue = []
        wanted = [False]
        builder._enrichment_enabled = lambda: wanted[0]

        await builder.run_enrichment()
        assert builder.enrichment_status == "disabled"

        wanted[0] = True
        await builder.run_enrichment()
        assert builder.enrichment_status == "complete"

    async def test_an_unreadable_preference_leaves_enrichment_on(
        self, builder, doc_index, caplog
    ):
        """Fails open, and says so.

        The alternative is that an unreadable ``app.json`` silently turns
        off a feature nobody asked to turn off — a config error presenting
        as a missing feature, which is the hardest kind to attribute.
        """
        with_enricher(builder, FakeEnricher())
        doc_index.queue = []

        def raises():
            raise OSError("no config")

        builder._enrichment_enabled = raises
        await builder.run_enrichment()
        assert builder.enrichment_status == "complete"
        assert "leaving enrichment on" in caplog.text

    async def test_no_callable_means_on(self, builder, doc_index):
        """Every caller that does not care about the preference is unaffected."""
        with_enricher(builder, FakeEnricher())
        doc_index.queue = []
        assert builder._enrichment_enabled is None
        await builder.run_enrichment()
        assert builder.enrichment_status == "complete"


class TestEnrichment:
    async def test_no_enricher_is_unavailable_not_a_failure(self, builder):
        await builder.run_enrichment()
        assert builder.enrichment_status == "unavailable"
        assert builder.enriched is False

    async def test_keybert_absent_is_unavailable(self, builder, caplog):
        import logging

        caplog.set_level(logging.INFO)
        with_enricher(builder, FakeEnricher(available=False))
        await builder.run_enrichment()
        assert builder.enrichment_status == "unavailable"
        assert "KeyBERT is not installed" in caplog.text

    async def test_a_model_that_will_not_load_is_unavailable(self, builder):
        with_enricher(builder, FakeEnricher(loads=False))
        await builder.run_enrichment()
        assert builder.enrichment_status == "unavailable"

    async def test_a_model_load_that_raises_is_unavailable(self, builder, caplog):
        with_enricher(builder, FakeEnricher(loads=MemoryError("no room")))
        await builder.run_enrichment()
        assert builder.enrichment_status == "unavailable"
        assert "model load raised" in caplog.text

    async def test_an_empty_queue_is_complete_immediately(
        self, builder, doc_index, progress
    ):
        with_enricher(builder, FakeEnricher())
        doc_index.queue = []
        await builder.run_enrichment()
        assert builder.enriched is True
        assert builder.enrichment_status == "complete"
        assert progress.stages() == ["doc_enrichment_complete"]

    async def test_each_file_is_enriched_with_its_own_source(
        self, builder, doc_index, repo
    ):
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["a.md", "b.md"]
        repo.content = "# Heading\n\nProse."
        await builder.run_enrichment()
        assert doc_index.enrich_calls == [
            ("a.md", "# Heading\n\nProse."),
            ("b.md", "# Heading\n\nProse."),
        ]

    async def test_progress_walks_from_queued_to_complete(
        self, builder, doc_index, progress
    ):
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["a.md", "b.md", "c.md", "d.md"]
        await builder.run_enrichment()
        assert progress.stages() == [
            "doc_enrichment_queued",
            "doc_enrichment_file_done",
            "doc_enrichment_file_done",
            "doc_enrichment_file_done",
            "doc_enrichment_file_done",
            "doc_enrichment_complete",
        ]
        percents = [p for s, _, p in progress.events if s == "doc_enrichment_file_done"]
        assert percents == [25, 50, 75, 100]
        assert "4 documents" in progress.events[0][1]

    async def test_the_status_is_building_while_it_runs(self, builder, doc_index):
        seen: list[str] = []
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["a.md", "b.md"]
        original = doc_index.enrich_single_file

        def spy(path, source_text):
            seen.append(builder.enrichment_status)
            return original(path, source_text)

        doc_index.enrich_single_file = spy
        await builder.run_enrichment()
        assert seen == ["building", "building"]
        assert builder.enrichment_status == "complete"

    async def test_one_bad_file_does_not_stop_the_rest(
        self, builder, doc_index, caplog
    ):
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["a.md", "bad.md", "c.md"]
        doc_index.enrich_errors["bad.md"] = RuntimeError("keybert blew up")
        await builder.run_enrichment()
        assert [path for path, _ in doc_index.enrich_calls] == [
            "a.md",
            "bad.md",
            "c.md",
        ]
        assert builder.enriched is True
        assert "keybert blew up" in caplog.text

    async def test_an_unreadable_file_is_skipped_without_enriching(
        self, builder, doc_index, repo
    ):
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["gone.md"]
        repo.read_errors["gone.md"] = FileNotFoundError("gone.md")
        await builder.run_enrichment()
        assert doc_index.enrich_calls == []
        assert builder.enriched is True

    async def test_a_large_input_is_named_in_the_log(
        self, builder, doc_index, repo, caplog
    ):
        """The OOM forensics: which file was the suspect."""
        import logging

        caplog.set_level(logging.INFO)
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["huge.md"]
        repo.content = "x" * (background_mod.LARGE_INPUT_KB * 1024 * 2)
        await builder.run_enrichment()
        assert "large input" in caplog.text
        assert "huge.md" in caplog.text

    async def test_every_file_gets_an_rss_line_before_dispatch(
        self, builder, doc_index, caplog
    ):
        """Without it, a kernel SIGKILL looks like a clean stop."""
        import logging

        caplog.set_level(logging.INFO)
        with_enricher(builder, FakeEnricher())
        doc_index.queue = ["a.md"]
        await builder.run_enrichment()
        assert "starting file 1/1" in caplog.text
        assert "rss=" in caplog.text


# ---------------------------------------------------------------------------
# The post-write hook
# ---------------------------------------------------------------------------


class TestNoteFileWritten:
    async def test_an_interesting_file_is_re_extracted(self, builder, doc_index):
        with_enricher(builder, FakeEnricher(model_name="bge-small"))
        builder.ready = True
        builder.note_file_written("README.md")
        assert doc_index.invalidated == ["README.md"]
        assert doc_index.indexed_files == [("README.md", "bge-small")]

    async def test_no_mode_gates_it_any_more(self, builder, doc_index):
        """The old gate was "doc mode or cross-reference on"; both are gone."""
        builder.ready = True
        builder.note_file_written("notes.rst")
        assert doc_index.indexed_files == [("notes.rst", None)]

    async def test_a_file_no_extractor_handles_is_ignored(self, builder, doc_index):
        builder.ready = True
        builder.note_file_written("main.py")
        assert doc_index.invalidated == []
        assert doc_index.indexed_files == []

    async def test_a_write_mid_build_is_left_to_the_build(self, builder, doc_index):
        """Re-extracting underneath ``index_repo`` would race it."""
        builder.ready = False
        builder.note_file_written("README.md")
        assert doc_index.indexed_files == []

    async def test_keywords_follow_when_the_outline_wants_them(
        self, builder, doc_index
    ):
        with_enricher(builder, FakeEnricher())
        builder.ready = True
        builder.note_file_written("README.md")
        await drain()
        assert [path for path, _ in doc_index.enrich_calls] == ["README.md"]

    async def test_an_outline_that_needs_nothing_is_not_enriched(
        self, builder, doc_index
    ):
        with_enricher(builder, FakeEnricher())
        builder.ready = True
        doc_index.needs = False
        builder.note_file_written("README.md")
        await drain()
        assert doc_index.enrich_calls == []

    async def test_a_file_that_would_not_parse_is_not_enriched(
        self, builder, doc_index
    ):
        with_enricher(builder, FakeEnricher())
        builder.ready = True
        doc_index.file_outline = None
        builder.note_file_written("README.md")
        await drain()
        assert doc_index.enrich_calls == []

    async def test_the_hook_never_raises(self, builder, doc_index, caplog):
        builder.ready = True
        doc_index.index_file = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("parser segfault surrogate")
        )
        builder.note_file_written("README.md")
        assert "post-write hook failed" in caplog.text

    def test_an_off_loop_write_defers_instead_of_failing(self, builder, doc_index):
        """``Repo`` can be driven from a worker thread."""
        with_enricher(builder, FakeEnricher())
        builder.ready = True
        builder.note_file_written("README.md")
        assert doc_index.indexed_files == [("README.md", "bge-small")]
        assert doc_index.enrich_calls == []


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class TestExecutorOwnership:
    def test_a_private_pool_is_made_on_first_use(self, doc_index):
        builder = DocIndexBuilder(doc_index=doc_index)
        pool = builder.executor()
        assert pool is builder.executor(), "a second pool would double the model"
        builder.close()
        assert builder._executor is None

    def test_a_supplied_pool_is_not_shut_down(self, doc_index):
        class Pool(InlineExecutor):
            shutdowns = 0

            def shutdown(self, wait=True):
                Pool.shutdowns += 1

        pool = Pool()
        builder = DocIndexBuilder(doc_index=doc_index, executor=pool)
        builder.close()
        assert Pool.shutdowns == 0
        assert builder.executor() is pool

    def test_close_is_safe_before_any_work(self, doc_index):
        DocIndexBuilder(doc_index=doc_index).close()

    def test_the_repo_can_arrive_late(self, doc_index, repo):
        builder = DocIndexBuilder(doc_index=doc_index)
        builder.attach_repo(repo)
        assert builder._eligible_files() == ["README.md", "notes.rst"]

    def test_the_index_is_reachable_for_the_tool_layer(self, doc_index):
        assert DocIndexBuilder(doc_index=doc_index).doc_index is doc_index
