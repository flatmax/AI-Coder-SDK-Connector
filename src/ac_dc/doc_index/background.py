"""Background doc-index build and keyword enrichment.

The doc index is the one piece of AC⚡DC's own indexing that outlives the
native engine untouched in substance. It backs the ``doc_outline`` MCP tool
and the outline pane, neither of which the CLI provides, and both phases of
its build are expensive enough to belong off the request path.

Two phases, in order (``specs5/2-indexing/document-index.md`` § Two-Phase
Principle):

1. **Structural extraction** — walk the repo's tracked files, keep the ones
   an extractor handles, hand them to :meth:`DocIndex.index_repo` on a
   worker thread. Flips ``ready``.
2. **Keyword enrichment** — per file, read the source and let KeyBERT add
   keywords to the outline. Flips ``enriched``. Optional: when KeyBERT is
   not installed the status becomes ``unavailable`` and phase 1's outlines
   stand on their own.

What the conversion changed here is ownership and coupling, not logic.
This used to be four module-level functions reaching into
``LLMService``'s attributes — its executor, its four readiness flags, its
event callback, its mode. It is now a class that owns those flags and its
executor, and it no longer asks what mode anything is in: the post-write
hook re-extracts whenever the doc index has an extractor for the path,
because there is no longer a mode in which the ``doc_outline`` tool is
switched off.

Failure is never fatal. Every phase logs and leaves its flag False; the
chat session does not know or care.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Source larger than this gets a log line of its own before enrichment
# runs. The OOM mode seen in the field was a kernel SIGKILL inside
# KeyBERT's MMR step on a 1.4MB prose block extracted from an SVG; the
# threshold is well above any ordinary markdown section, so a line here
# means "this file is the suspect".
LARGE_INPUT_KB = 100


class DocIndexBuilder:
    """Owns the doc index's two background passes and their state.

    Parameters
    ----------
    doc_index:
        The :class:`~ac_dc.doc_index.index.DocIndex` to populate.
    enricher:
        The :class:`~ac_dc.doc_index.keyword_enricher.KeywordEnricher`, or
        ``None`` to skip phase 2 entirely.
    repo:
        The ``Repo``, for the tracked-file list and file reads. Without one
        there is nothing to index.
    progress:
        ``async (stage, message, percent) -> None`` — the
        ``startupProgress`` channel the frontend's doc-index overlay reads.
    executor:
        Where the blocking work runs. Defaults to a private single-thread
        pool: extraction and enrichment are GIL-heavy and hold ~1.2GB of
        model once loaded, so they get one thread of their own rather than
        a share of the default pool that everything else also uses.
    """

    def __init__(
        self,
        *,
        doc_index: Any,
        enricher: Any = None,
        repo: Any = None,
        progress: Callable[[str, str, int], Awaitable[None]] | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._doc_index = doc_index
        self._enricher = enricher
        self._repo = repo
        self._progress = progress
        self._executor = executor
        self._owns_executor = executor is None

        # Readiness. `ready` gates the outline tool and the outline pane;
        # `enriched` gates only the cosmetic keyword display, which is why
        # nothing waits on it.
        self.ready = False
        self.building = False
        self.enriched = False
        # Tristate, because a single boolean cannot tell "KeyBERT is not
        # installed" from "still working" — the frontend shows a one-shot
        # install hint for the first and a progress bar for the second.
        self.enrichment_status = "pending"

    # ------------------------------------------------------------------
    # Wiring and teardown
    # ------------------------------------------------------------------

    @property
    def doc_index(self) -> Any:
        return self._doc_index

    def attach_repo(self, repo: Any) -> None:
        """Late-bind the repo, for startup orders that build this first."""
        self._repo = repo

    def executor(self) -> Executor:
        """The worker pool, created on first use."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ac-dc-doc-index"
            )
        return self._executor

    def close(self) -> None:
        """Shut down the private pool, if we made one."""
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    def status(self) -> dict[str, Any]:
        """The four fields the state snapshot carries."""
        return {
            "doc_index_ready": self.ready,
            "doc_index_building": self.building,
            "doc_index_enriched": self.enriched,
            "enrichment_status": self.enrichment_status,
        }

    # ------------------------------------------------------------------
    # Phase 1 — structural extraction
    # ------------------------------------------------------------------

    def schedule(self, *, force: bool = False) -> None:
        """Kick off a build as a fire-and-forget task.

        ``force`` re-runs a build that has already completed — what review
        entry and exit need, since both move the whole working tree under a
        finished index. A build already in flight is never doubled up.
        """
        if self.building:
            return
        if self.ready and not force:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "Doc index: no running loop; build not scheduled"
            )
            return
        loop.create_task(self.build(), name="doc-index-build")

    async def build(self) -> None:
        """Extract structure for every eligible file, then enrich.

        Never raises: a doc index that failed to build costs the outline
        pane and the ``doc_outline`` tool, and nothing else.
        """
        if self.building:
            return
        self.building = True
        try:
            doc_files = self._eligible_files()
            total = len(doc_files)
            if total == 0:
                logger.info(
                    "Doc index: no eligible files; ready with no outlines"
                )
                self.ready = True
                return

            logger.info("Doc index: extracting structure for %d files", total)
            await self._emit(
                "doc_index", f"Indexing documentation ({total} files)", 0
            )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self.executor(), self._doc_index.index_repo, doc_files
            )

            self.ready = True
            logger.info(
                "Doc index: structure complete — %d outlines",
                len(self._doc_index._all_outlines),
            )
            await self._emit("doc_index", "Documentation indexing complete", 100)

            # Chained, not awaited: phase 1 is what everything gates on, so
            # `building` should fall as soon as it lands. Enrichment then
            # runs for however long it runs.
            loop.create_task(self.run_enrichment(), name="doc-index-enrich")
        except Exception as exc:
            logger.exception("Doc index: build failed: %s", exc)
            await self._emit(
                "doc_index_error", f"Documentation indexing failed: {exc}", 0
            )
        finally:
            self.building = False

    def _eligible_files(self) -> list[str]:
        """Tracked files the doc index has an extractor for.

        The list comes from the repo so it honours ``.gitignore`` and the
        user's exclusions; a repo that cannot answer yields nothing rather
        than falling back to a raw filesystem walk that would index
        ``node_modules``.
        """
        if self._repo is None:
            return []
        try:
            flat = self._repo.get_flat_file_list()
        except Exception as exc:
            logger.warning("Doc index: could not list repo files: %s", exc)
            return []
        extractors = self._doc_index._extractors
        return [
            f
            for f in flat.split("\n")
            if f and self._doc_index._extension_of(f) in extractors
        ]

    # ------------------------------------------------------------------
    # Phase 2 — keyword enrichment
    # ------------------------------------------------------------------

    async def run_enrichment(self) -> None:
        """Add keywords to every outline that wants them.

        One file at a time, on the worker thread, with a yield to the loop
        between files so the WebSocket carrying the progress bar keeps
        flowing. A file that fails is logged and skipped — the outline
        simply keeps its structure and no keywords.
        """
        if self._enricher is None:
            self.enrichment_status = "unavailable"
            return
        if not self._enricher.is_available():
            logger.info(
                "Keyword enrichment unavailable — KeyBERT is not installed. "
                "Structural outlines are unaffected."
            )
            self.enrichment_status = "unavailable"
            return

        loop = asyncio.get_running_loop()
        try:
            loaded = await loop.run_in_executor(
                self.executor(), self._enricher.ensure_loaded
            )
        except Exception as exc:
            logger.warning("Keyword enrichment model load raised: %s", exc)
            loaded = False
        if not loaded:
            logger.warning(
                "Keyword enrichment model failed to load. Structural "
                "outlines are unaffected."
            )
            self.enrichment_status = "unavailable"
            return

        queue = self._doc_index.queue_enrichment()
        if not queue:
            self.enriched = True
            self.enrichment_status = "complete"
            await self._emit(
                "doc_enrichment_complete", "Keyword enrichment complete", 100
            )
            return

        total = len(queue)
        logger.info("Keyword enrichment: %d files queued", total)
        self.enrichment_status = "building"
        await self._emit(
            "doc_enrichment_queued", f"Enriching {total} documents", 0
        )

        for idx, rel_path in enumerate(queue, start=1):
            # Logged *before* dispatch, with RSS, because the failure mode
            # this guards against is a kernel SIGKILL mid-file: without a
            # line here that looks like a crash after the previous file
            # succeeded. RSS distinguishes a creeping leak (climbs across
            # files) from a one-shot spike (jumps on one large input).
            # ~1.2GB steady state is normal — that is torch's allocator
            # and loky's workers reaching their working set, not a leak.
            logger.info(
                "Enrichment: starting file %d/%d (rss=%dMB): %s",
                idx,
                total,
                _rss_mb(),
                rel_path,
            )
            try:
                await loop.run_in_executor(
                    self.executor(), self._enrich_one_file_sync, rel_path
                )
            except Exception as exc:
                logger.warning("Enrichment failed for %s: %s", rel_path, exc)

            await self._emit(
                "doc_enrichment_file_done",
                f"Enriched {rel_path}",
                int((idx / total) * 100),
            )
            await asyncio.sleep(0)

        self.enriched = True
        self.enrichment_status = "complete"
        logger.info("Keyword enrichment: complete")
        await self._emit(
            "doc_enrichment_complete", "Keyword enrichment complete", 100
        )

    def _enrich_one_file_sync(self, rel_path: str) -> None:
        """Read the source and enrich one outline. Executor-side.

        The read and the enrich share one worker thread so the disk I/O
        sits next to the GIL-heavy model call instead of ping-ponging
        between threads. The debug brackets are the "last seen alive"
        marker for a segfault inside the native KeyBERT stack.
        """
        if self._repo is None:
            return
        logger.debug(
            "enrich_one_file_sync: enter rel_path=%s pid=%d tid=%d",
            rel_path,
            os.getpid(),
            threading.get_ident(),
        )
        try:
            source_text = self._repo.get_file_content(rel_path)
        except Exception as exc:
            logger.debug(
                "Enrichment source read failed for %s: %s", rel_path, exc
            )
            return
        size_kb = len(source_text) // 1024
        if size_kb > LARGE_INPUT_KB:
            logger.info(
                "enrich_one_file_sync: large input rel_path=%s size=%dKB",
                rel_path,
                size_kb,
            )
        try:
            self._doc_index.enrich_single_file(
                rel_path, source_text=source_text
            )
        finally:
            logger.debug(
                "enrich_one_file_sync: exit rel_path=%s pid=%d tid=%d",
                rel_path,
                os.getpid(),
                threading.get_ident(),
            )

    # ------------------------------------------------------------------
    # Post-write hook
    # ------------------------------------------------------------------

    def note_file_written(self, rel_path: str) -> None:
        """Re-extract one file after a write. Wired to ``Repo``.

        Fires for every successful write, create and rename **through
        Repo** — which means the user's edits in the viewer and the SVG
        editor, not the agent's. The CLI's own ``Write`` and ``Edit``
        tools write straight to disk and never touch this layer; those
        are the post-tool-call re-index's job, which lands with the MCP
        bridge in phase 4. Until then a document the agent edits keeps a
        stale outline until the next full build.

        Interesting paths get their cache entry dropped, a fresh
        structural outline, and a place in the enrichment queue.

        The old gate here — "only when in doc mode or cross-reference is
        on" — is gone with the modes it referred to. What remains is the
        extension check, which is the real question.

        Never raises.
        """
        try:
            extension = self._doc_index._extension_of(rel_path)
            if extension not in self._doc_index._extractors:
                return
            if not self.ready:
                # Mid-build. index_repo will reach this file anyway, and
                # re-extracting underneath it would race.
                return
            self._doc_index.invalidate_file(rel_path)
            keyword_model = (
                self._enricher.model_name if self._enricher is not None else None
            )
            outline = self._doc_index.index_file(
                rel_path, keyword_model=keyword_model
            )
            if outline is None or self._enricher is None:
                return
            if not self._doc_index.needs_enrichment(outline):
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug(
                    "Doc file %s written off-loop; keywords deferred to the "
                    "next full build",
                    rel_path,
                )
                return
            loop.create_task(
                self._enrich_written_file(rel_path), name="doc-index-reenrich"
            )
        except Exception as exc:
            logger.warning(
                "Doc-file post-write hook failed for %s: %s", rel_path, exc
            )

    async def _enrich_written_file(self, rel_path: str) -> None:
        """Enrich one just-written file on the worker thread."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self.executor(), self._enrich_one_file_sync, rel_path
            )
        except Exception as exc:
            logger.warning("Deferred enrichment failed for %s: %s", rel_path, exc)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    async def _emit(self, stage: str, message: str, percent: int) -> None:
        """Send one ``startupProgress`` event; failures are not ours."""
        if self._progress is None:
            return
        try:
            await self._progress(stage, message, percent)
        except Exception as exc:
            logger.debug("Doc index progress event failed for %s: %s", stage, exc)


def _rss_mb() -> int:
    """Best-effort resident set size in MB; 0 where ``/proc`` is absent.

    Parsed by hand rather than through psutil — one file read against one
    line is not worth a dependency, and a 0 still leaves the per-file
    timing in the log.
    """
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except Exception:
        pass
    return 0
