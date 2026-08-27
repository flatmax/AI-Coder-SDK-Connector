"""Symbol index orchestrator — Layer 2.7.

Wires the parser, per-language extractors, cache, import
resolver, reference graph, and formatters into a single
entry point. Consumers call :meth:`SymbolIndex.index_repo`
with a file list and then query via
:meth:`get_symbol_map` / :meth:`get_file_symbol_block` /
:meth:`get_signature_hash`.

Design notes pinned by the test suite and spec:

- **Per-file pipeline** — check cache → parse → extract →
  post-process (import resolution) → store. mtime-based
  caching means unchanged files are a no-op.

- **Multi-file pipeline** — index each file, prune stale
  entries (both in-memory and cache), resolve cross-file
  call-site targets, rebuild the reference graph.

- **Stale-removal ordering** — pruning happens BEFORE the
  reference graph rebuild. Otherwise the ref index would
  briefly contain edges to/from deleted files.

- **Snapshot discipline** — read methods
  (``get_symbol_map``, ``get_file_symbol_block``,
  ``get_signature_hash``) never mutate state.

- **Two formatter variants** — a context formatter (no
  line numbers, for the LLM) and an LSP formatter (with
  line numbers, for editor features).

- **Dispatch by language name** — each extractor declares
  a ``language`` class attribute matching a key in
  LANGUAGE_MAP. The orchestrator resolves a file's
  language via ``language_for_file`` and picks the
  matching extractor.

- **Path normalisation** — incoming paths normalised to
  forward-slash, leading/trailing-slash-stripped form
  before any cache lookup or dict key.

Governing spec: ``specs4/2-indexing/symbol-index.md``.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from aic_dc.symbol_index.cache import SymbolCache
from aic_dc.symbol_index.compact_format import CompactFormatter
from aic_dc.symbol_index.extractors import (
    BaseExtractor,
    CExtractor,
    CppExtractor,
    JavaScriptExtractor,
    MatlabExtractor,
    PythonExtractor,
    TypeScriptExtractor,
)
from aic_dc.symbol_index.import_resolver import ImportResolver
from aic_dc.symbol_index.parser import (
    TreeSitterParser,
    language_for_file,
)
from aic_dc.symbol_index.reference_index import ReferenceIndex

if TYPE_CHECKING:
    from aic_dc.symbol_index.models import FileSymbols, Symbol

logger = logging.getLogger(__name__)


# Per-language extractor classes. MATLAB has no maintained
# tree-sitter grammar — its extractor declares
# tree_optional = True and works from raw source. The
# orchestrator's _parse_and_store passes tree=None for
# tree-optional extractors.
_EXTRACTOR_CLASSES: tuple[type[BaseExtractor], ...] = (
    PythonExtractor,
    JavaScriptExtractor,
    TypeScriptExtractor,
    CExtractor,
    CppExtractor,
    MatlabExtractor,
)


class SymbolIndex:
    """Top-level symbol-index orchestrator.

    Construct once per session. Call :meth:`index_repo`
    with the current file list to (re-)index, then query
    via the read methods. The in-memory ``_all_symbols``
    map is a read-only snapshot between re-index passes.
    """

    def __init__(self, repo_root: Path | str | None = None) -> None:
        """Initialise the orchestrator.

        Parameters
        ----------
        repo_root
            Optional path to the repository root. When
            provided, relative paths passed to
            :meth:`index_file` and :meth:`index_repo` are
            resolved against this directory. When None,
            callers must pass absolute paths.
        """
        self.repo_root: Path | None = (
            Path(repo_root) if repo_root is not None else None
        )

        # Shared tree-sitter parser — caches loaded
        # Language objects internally.
        self._parser = TreeSitterParser.instance()

        # Cache, reference index, resolver. Exposed as
        # underscored attributes for test introspection.
        self._cache = SymbolCache()
        self._ref_index = ReferenceIndex()
        self._resolver = ImportResolver()

        # Extractor registry keyed by language name.
        self._extractors: dict[str, BaseExtractor] = {}
        for cls in _EXTRACTOR_CLASSES:
            instance = cls()
            if instance.language:
                self._extractors[instance.language] = instance

        # In-memory per-file symbol store. Keys are
        # forward-slash relative paths.
        self._all_symbols: dict[str, "FileSymbols"] = {}

        # Two formatter instances — context (LLM-facing)
        # and LSP (editor features, with line numbers).
        self._formatter_context = CompactFormatter(
            include_line_numbers=False
        )
        self._formatter_lsp = CompactFormatter(
            include_line_numbers=True
        )

    # ------------------------------------------------------------------
    # Path normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_rel_path(path: str | Path) -> str:
        """Return the canonical dict-key form for a repo path.

        Forward-slash separators, no leading or trailing
        slash. Matches the conventions used by BaseCache
        and ImportResolver so lookups collide cleanly.
        """
        return str(path).replace("\\", "/").strip("/")

    def _absolute_path(self, rel: str) -> Path:
        """Resolve a normalised relative path to an absolute Path.

        When ``repo_root`` is set, joins against it;
        otherwise treats the input as relative to the
        process cwd. An absolute path is returned as-is.
        """
        candidate = Path(rel)
        if candidate.is_absolute():
            return candidate
        if self.repo_root is not None:
            return self.repo_root / rel
        return candidate

    # ------------------------------------------------------------------
    # Per-file pipeline
    # ------------------------------------------------------------------

    def index_file(
        self, path: str | Path
    ) -> "FileSymbols | None":
        """Index a single file, using the cache when possible.

        Returns the FileSymbols (from cache or freshly
        extracted), or None when the file has no supported
        extractor, is missing, or fails to parse.
        """
        rel = self._normalise_rel_path(path)
        language = language_for_file(rel)
        if language is None:
            return None

        extractor = self._extractors.get(language)
        if extractor is None:
            return None

        absolute = self._absolute_path(rel)
        try:
            mtime = absolute.stat().st_mtime
        except OSError:
            # Missing file / permission error. Invalidate
            # any stale entry and return None.
            self._all_symbols.pop(rel, None)
            self._cache.invalidate(rel)
            return None

        cached = self._cache.get(rel, mtime)
        if cached is not None:
            # Cache hit — identity preserved for callers
            # that hold references. Re-resolve imports
            # every time though: resolved_target is set
            # via setattr (not a real dataclass field) so
            # it may not survive a cache round-trip, and
            # the resolver's file set may have grown since
            # the cache entry was written. Cheap in any
            # case — it's a dict lookup per import.
            self._resolve_imports_for_file(cached)
            self._all_symbols[rel] = cached
            return cached

        return self._parse_and_store(
            rel, absolute, language, extractor, mtime
        )

    def _parse_and_store(
        self,
        rel: str,
        absolute: Path,
        language: str,
        extractor: BaseExtractor,
        mtime: float,
    ) -> "FileSymbols | None":
        """Parse and extract, then store in cache and _all_symbols."""
        try:
            source = absolute.read_bytes()
        except OSError:
            self._all_symbols.pop(rel, None)
            self._cache.invalidate(rel)
            return None

        # Extractors that declare tree_optional=True (e.g.
        # future MATLAB) get tree=None and do their own
        # regex-based extraction. The rest need a real tree.
        if extractor.tree_optional:
            tree = None
        else:
            tree = self._parser.parse(source, language)
            if tree is None:
                # Grammar unavailable — nothing to do.
                return None

        try:
            file_symbols = extractor.extract(tree, source, rel)
        except Exception as exc:
            # Defensive — an extractor bug shouldn't take
            # down the whole index pass.
            logger.warning(
                "Extractor for %s failed on %s: %s",
                language, rel, exc,
            )
            return None

        # Populate Import.resolved_target for the file's
        # own imports using the current resolver state.
        # The resolver's file set may not yet include all
        # repo files during per-file invocations; callers
        # that need cross-file resolution (reference graph)
        # use index_repo which updates the resolver's file
        # set first.
        self._resolve_imports_for_file(file_symbols)

        # Store in both cache and in-memory map.
        self._cache.put(rel, mtime, file_symbols)
        self._all_symbols[rel] = file_symbols
        return file_symbols

    def _resolve_imports_for_file(
        self, file_symbols: "FileSymbols"
    ) -> None:
        """Attach ``resolved_target`` to each Import object.

        The import resolver returns a repo-relative path
        (or None). We stash it on the Import via
        :func:`setattr` — Layer 2.4's reference index
        reads via :func:`getattr` with a None default, so
        pre-resolver Import objects still work. A later
        model change could make this a real field.
        """
        for imp in file_symbols.imports:
            target = self._resolver.resolve(imp, file_symbols.file_path)
            setattr(imp, "resolved_target", target)

    # ------------------------------------------------------------------
    # Multi-file pipeline
    # ------------------------------------------------------------------

    def index_repo(self, file_list: list[str | Path]) -> None:
        """Index a list of files, prune stale entries, rebuild refs.

        The canonical full-repo entry point. Callers pass
        the current repo file list (typically from
        :meth:`aic_dc.repo.Repo.get_flat_file_list`). Order
        of operations:

        1. Normalise every path, filter to those with a
           known extractor. Unknown extensions are skipped
           silently — the walker's file list may contain
           arbitrary content.
        2. Update the resolver's file set so per-file
           import resolution sees every repo file.
        3. Index each file (cache-aware).
        4. Prune entries in ``_all_symbols`` and the cache
           whose paths aren't in the new list. Must run
           BEFORE the reference index rebuild.
        5. Resolve cross-file call-site targets using the
           now-complete import and symbol maps.
        6. Rebuild the reference graph from the current
           ``_all_symbols``.
        """
        # Step 1 — normalise and filter.
        normalised: list[str] = []
        for p in file_list:
            rel = self._normalise_rel_path(p)
            if rel and language_for_file(rel) is not None:
                normalised.append(rel)
        keep = set(normalised)

        # Step 2 — refresh the resolver's file set. We
        # pass the raw normalised list (not the filter),
        # so the resolver can answer queries about files
        # it wouldn't itself index (e.g. plain ``.txt``
        # files referenced by C-style includes in some
        # exotic project).
        self._resolver.set_files([
            self._normalise_rel_path(p) for p in file_list
        ])

        # Step 3 — index each file. Errors inside
        # index_file are swallowed there (returns None);
        # the pass continues.
        for rel in normalised:
            self.index_file(rel)

        # Step 4 — prune stale entries. Done by diffing
        # the in-memory map and cache against the current
        # file set. A file absent from keep is a deleted
        # or moved file; its entries must go.
        self._prune_stale(keep)

        # Step 5 — cross-file call-site resolution.
        self._resolve_call_sites()

        # Step 6 — rebuild the reference graph from
        # scratch. ReferenceIndex.build is idempotent —
        # it clears prior state first.
        self._ref_index.build(list(self._all_symbols.values()))

    def _prune_stale(self, keep: set[str]) -> None:
        """Remove in-memory and cached entries not in ``keep``.

        A union of the two key sets gives us paths that
        might be stale. For each, drop the memory entry
        and invalidate the cache sidecar (the cache's
        no-op for missing keys means extra ``invalidate``
        calls are cheap).

        Must run before the reference graph rebuild — see
        the ordering note in :meth:`index_repo`.
        """
        stale = (
            set(self._all_symbols.keys()) | self._cache.cached_paths
        ) - keep
        for path in stale:
            self._all_symbols.pop(path, None)
            self._cache.invalidate(path)

    def _resolve_call_sites(self) -> None:
        """Populate ``target_file`` on each call site.

        Strategy — for each file, build a map of
        imported-name → resolved target. Then for each
        call site in that file, if the callee's name
        matches an imported name, set ``target_file`` to
        the import's resolved target.

        This is intentionally modest: it catches the
        common case where a function is imported and
        called under the same name. Resolving method
        calls on imported classes, aliased imports, or
        deeply namespaced calls would require symbol
        resolution that's out of scope for Layer 2.7 —
        the reference graph handles those via import
        edges separately.
        """
        for file_symbols in self._all_symbols.values():
            # Per-file imported-name → target map.
            import_map: dict[str, str] = {}
            for imp in file_symbols.imports:
                target = getattr(imp, "resolved_target", None)
                if not target:
                    continue
                # ``from foo import bar`` — each name
                # maps to target. ``import foo`` (no
                # names) maps the module's leaf name to
                # target, but that's more ambiguous and
                # the call-site extractor already strips
                # module prefixes, so we skip it.
                for name in imp.names:
                    if name and name != "*":
                        import_map[name] = target

            # Walk every symbol (top-level + nested) and
            # resolve each call site's target.
            for sym in file_symbols.all_symbols_flat:
                for cs in sym.call_sites:
                    if cs.target_file is not None:
                        continue  # already resolved
                    target = import_map.get(cs.name)
                    if target is not None:
                        cs.target_file = target
                        # target_symbol defaults to the
                        # callee name — good enough for
                        # the reference index's needs.
                        if cs.target_symbol is None:
                            cs.target_symbol = cs.name

    def reindex_files(
        self, paths: Iterable[str | Path]
    ) -> list[str]:
        """Re-index some files after a write, keeping the graph coherent.

        The incremental counterpart to :meth:`index_repo`,
        for the case where a handful of known files changed
        on disk and the rest did not: the post-tool-call
        re-index behind the MCP bridge, one write at a time.

        Returns the normalised paths that ended up in the
        index. A path that vanished from disk is dropped
        instead, and does not appear in the return value —
        the caller learns which of its writes produced
        symbols and which were deletions.

        Three things happen beyond the per-file parse, and
        each closes a way the graph would otherwise lie
        about a *new* file:

        1. The resolver's file set grows to include the
           paths. A file it has never heard of resolves no
           imports, so a brand-new module's own imports —
           and every import *of* it — would come back
           unresolved.
        2. Call sites are re-resolved across the whole
           index, not just these files. A new function
           changes what other files' calls point at, and
           those files were not re-parsed.
        3. The reference graph is rebuilt from scratch,
           because ``ReferenceIndex`` holds no incremental
           update path and a partial rebuild would leave
           edges pointing at symbols that no longer exist.

        Steps 2 and 3 are whole-index passes, which is why
        the caller debounces rather than calling this once
        per keystroke. They are pure in-memory walks over
        already-parsed symbols — seconds only on a repo of
        many thousands of files, where the alternative is a
        reference graph that quietly disagrees with the
        symbol table it was built from.
        """
        normalised = [
            rel for rel in (
                self._normalise_rel_path(p) for p in paths
            ) if rel
        ]
        if not normalised:
            return []

        interesting = [
            rel for rel in normalised
            if language_for_file(rel) is not None
        ]
        if not interesting:
            return []

        # Step 1 — widen the resolver's view before parsing,
        # so a new file's imports resolve on this pass rather
        # than on the next full build.
        self._resolver.set_files(
            self._resolver.files | set(interesting)
        )

        indexed: list[str] = []
        for rel in interesting:
            # index_file already handles the missing-file
            # case by dropping the entry and returning None,
            # so a delete needs no separate branch.
            if self.index_file(rel) is not None:
                indexed.append(rel)

        # Steps 2 and 3 — the same order index_repo uses,
        # and for the same reason: the graph is built from
        # resolved call sites, so resolution comes first.
        self._resolve_call_sites()
        self._ref_index.build(list(self._all_symbols.values()))
        return indexed

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate_file(self, path: str | Path) -> bool:
        """Drop a file from the in-memory map and cache.

        Returns True if anything was removed (either the
        memory entry, the cache entry, or both), False if
        neither existed. Callers use the return value to
        count cleanups; raising on missing would force
        every call site into try/except.
        """
        rel = self._normalise_rel_path(path)
        had_memory = self._all_symbols.pop(rel, None) is not None
        had_cache = self._cache.invalidate(rel)
        return had_memory or had_cache

    def find_stale_files(self) -> list[str]:
        """Indexed paths whose file on disk no longer matches.

        The read half of the mtime cache, which until now
        only ever answered as a side effect of re-indexing
        a file someone already knew had changed. Here it is
        the question itself: *what* changed, when nothing
        told us.

        This exists for changes that arrive through
        ``Bash`` — a ``sed -i``, a ``git checkout``, a
        formatter, an ``mv`` away. ``PostToolUse`` names the
        files for ``Write`` and its neighbours; for a shell
        command it hands over the command line, which is not
        reliably parseable into paths (decisions.md § CC-18).
        Comparing mtimes needs no such attribution: the disk
        is the record.

        Scope, deliberately: paths **already known** to the
        index — the union of the symbol map and the cache,
        matching :meth:`_prune_stale`, so a file that parsed
        to no symbols is still watched. A file a shell
        command *created* is in neither set and is not
        returned; catching those means re-walking the repo,
        which is the cost this approach exists to avoid. That
        residue is recorded in
        ``specs5/2-indexing/symbol-index.md``.

        A file that has vanished or turned unreadable is
        reported stale rather than dropped here: this is a
        query and the caller may not be about to re-index.
        :meth:`reindex_files` is what removes it, and it
        already omits vanished paths from its return value.

        Sorted, and cheap: one ``stat`` per known file and a
        dict lookup, no parsing. Nothing is mutated.
        """
        known = set(self._all_symbols.keys()) | self._cache.cached_paths
        stale: list[str] = []
        for rel in known:
            try:
                mtime = self._absolute_path(rel).stat().st_mtime
            except OSError:
                # Deleted, renamed away, or no longer readable.
                stale.append(rel)
                continue
            if self._cache.get(rel, mtime) is None:
                stale.append(rel)
        return sorted(stale)

    # ------------------------------------------------------------------
    # Read queries — snapshot discipline applies
    # ------------------------------------------------------------------

    def get_symbol_map(
        self,
        exclude_files: set[str] | None = None,
    ) -> str:
        """Render the context-variant symbol map.

        No line numbers — the LLM-facing format. Empty
        when no files are indexed, matching the base
        formatter's contract so callers can concatenate
        the output into a prompt and skip the section
        cleanly when there's nothing to show.
        """
        if not self._all_symbols:
            return ""
        return self._formatter_context.format_files(
            self._all_symbols.values(),
            ref_index=self._ref_index,
            exclude_files=exclude_files,
        )

    def get_lsp_symbol_map(
        self,
        exclude_files: set[str] | None = None,
    ) -> str:
        """Render the LSP-variant symbol map.

        Same content as :meth:`get_symbol_map` but with
        ``:N`` line numbers on every symbol. Consumed by
        editor features (hover, go-to-definition); not
        suitable for the LLM prompt because line numbers
        waste tokens the model doesn't use.
        """
        if not self._all_symbols:
            return ""
        return self._formatter_lsp.format_files(
            self._all_symbols.values(),
            ref_index=self._ref_index,
            exclude_files=exclude_files,
        )

    def get_legend(self) -> str:
        """Return just the legend block (kind codes, aliases).

        Separate from the map body because a caller that
        renders file blocks in pieces — chunked tool output,
        one directory at a time — wants the decoder key once
        rather than per chunk. The base formatter exposes a
        standalone ``get_legend`` method that computes
        path aliases from the supplied file list; we pass
        the current set.
        """
        return self._formatter_context.get_legend(
            self._all_symbols.keys()
        )

    def get_file_symbol_block(
        self, path: str | Path
    ) -> str | None:
        """Render the compact block for a single file.

        Returns None when the file isn't in the index.
        Callers poll per-file, and a file deleted between
        listing and retrieval must read as absent rather
        than raise.

        The block omits the legend and alias block — it is
        meant to be composed with others, which render the
        legend once between them.
        """
        rel = self._normalise_rel_path(path)
        if rel not in self._all_symbols:
            return None
        # Render just this one file without the legend.
        # CompactFormatter.format_files always emits the
        # legend; drop down to the base class's format()
        # method, which accepts include_legend=False, and
        # stash the FileSymbols in _current_by_path so
        # _format_file can look it up by path string.
        fs = self._all_symbols[rel]
        fmt = self._formatter_context
        fmt._current_by_path = {rel: fs}
        try:
            return fmt.format(
                [rel],
                ref_index=self._ref_index,
                include_legend=False,
            )
        finally:
            fmt._current_by_path = {}

    def get_signature_hash(self, path: str | Path) -> str | None:
        """Return the structural hash for a file, or None if unknown.

        Thin wrapper around the cache's hash accessor.
        Lets a caller detect when a file's structure
        genuinely changed, as distinct from a whitespace-only
        edit that changes mtime but not the signature.
        """
        rel = self._normalise_rel_path(path)
        return self._cache.get_signature_hash(rel)

    # ------------------------------------------------------------------
    # Per-directory accessors (D36 dir-blocks)
    # ------------------------------------------------------------------

    @staticmethod
    def _dir_of(rel_path: str) -> str:
        """Return the directory portion of a repo-relative path.

        Top-level files use the empty string as their directory
        key — `_format_dir_label` renders that as `<root>` so
        the rendered block has a non-empty header.
        """
        idx = rel_path.rfind("/")
        if idx == -1:
            return ""
        return rel_path[:idx]

    def get_indexed_directories(self) -> list[str]:
        """Return a sorted list of directories with at least one indexed file.

        The universe of directory keys — what a caller iterates
        to walk the index a folder at a time.
        """
        dirs: set[str] = set()
        for path in self._all_symbols:
            dirs.add(self._dir_of(path))
        return sorted(dirs)

    def get_dir_symbols_block(
        self,
        directory: str,
        exclude_active: set[str] | None = None,
    ) -> str:
        """Render the directory's symbol-table block.

        Concatenates each indexed file's compact block (from
        :meth:`get_file_symbol_block`) for files in the directory,
        excluding those currently in Active full-text. Returns
        empty string when the directory has no eligible files.

        D36 dir-block — one entry per source file in the directory
        minus any currently in Active full-text.
        """
        excluded = exclude_active or set()
        files = sorted(
            path for path in self._all_symbols
            if self._dir_of(path) == directory
            and path not in excluded
        )
        if not files:
            return ""
        blocks: list[str] = []
        for path in files:
            block = self.get_file_symbol_block(path)
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    def get_dir_signature_hash(
        self,
        directory: str,
        exclude_active: set[str] | None = None,
    ) -> str:
        """Return a stable hash over the directory's symbol-table contents.

        Concatenates per-file signature hashes in sorted filename
        order, then SHA-256s the result. Excludes any file in
        Active full-text — content moving in or out of Active
        therefore changes the directory's hash, demoting the
        block to Active to re-ride flux on the next freeze.
        """
        import hashlib

        excluded = exclude_active or set()
        files = sorted(
            path for path in self._all_symbols
            if self._dir_of(path) == directory
            and path not in excluded
        )
        h = hashlib.sha256()
        for path in files:
            sig = self._cache.get_signature_hash(path) or ""
            h.update(path.encode("utf-8"))
            h.update(b"\0")
            h.update(sig.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def get_indexed_files(self) -> list[str]:
        """Return all repo-relative paths currently indexed.

        Sorted for determinism, so a caller that partitions or
        chunks this list gets the same partition every call.
        """
        return sorted(self._all_symbols.keys())

    def resolve_indexed_path(self, path: str | Path) -> str | None:
        """The canonical key for ``path``, or None when it isn't indexed.

        A caller holding a path it was handed — by an agent, by a
        user — needs two answers at once: does the index know this
        file, and what exact string does the map call it? Returning
        the key rather than a bool saves that caller from
        normalising paths itself and then disagreeing with us about
        what ``./src/x.py`` means.

        Deliberately more forgiving than
        :meth:`_normalise_rel_path`, which is the *dict-key*
        convention and is fed paths this codebase produced. This
        one is fed paths a language model typed: an absolute path
        (which is the form the CLI reports writes in), a
        ``./``-prefixed one, a ``src/../src/x.py``. Each of those
        names a file the index holds, and answering "not indexed"
        to a path that is plainly in the index reads as a broken
        index rather than as a spelling quibble.

        A path outside the repo is still None. It is not a
        spelling variant of anything we hold.
        """
        rel = self._normalise_rel_path(path)
        if rel in self._all_symbols:
            return rel

        # Absoluteness has to be judged before normalisation, which
        # strips the leading slash that carried it.
        raw = str(path).replace("\\", "/")
        if raw.startswith("/"):
            if self.repo_root is None:
                return None
            try:
                rel = Path(raw).relative_to(self.repo_root).as_posix()
            except ValueError:
                return None

        tidied = posixpath.normpath(rel).strip("/")
        if not tidied or tidied in (".", "..") or tidied.startswith("../"):
            # Nothing inside the repo. Note the precision: a leading
            # dot is fine — ``.github/workflows/x.py`` is a real file —
            # it is only ``..`` that leaves.
            return None
        return tidied if tidied in self._all_symbols else None

    # ------------------------------------------------------------------
    # Name-based queries
    # ------------------------------------------------------------------

    def find_definitions(self, name: str) -> list[dict[str, object]]:
        """Every definition of ``name``, as ``{file, line, kind, container}``.

        Name-based rather than position-based, because the caller
        behind this is an agent holding a name it read in a diff,
        not a cursor in an editor. :meth:`lsp_get_definition`
        starts from a position and is the browser's path; both
        read the same symbol table.

        Lines are 1-indexed. ``range`` is stored 0-indexed
        (tree-sitter convention) and every consumer of this
        method shows the number to a reader, so the ``+1``
        belongs here rather than in each of them.

        ``container`` is the enclosing symbol's name for a
        method or nested function, None at the top level — the
        one fact that distinguishes six same-named ``build``
        methods from each other.
        """
        matches: list[dict[str, object]] = []
        for rel in sorted(self._all_symbols):
            for sym in self._all_symbols[rel].symbols:
                self._collect_named(sym, name, rel, None, matches)
        return matches

    def _collect_named(
        self,
        sym: "Symbol",
        name: str,
        rel: str,
        container: str | None,
        out: list[dict[str, object]],
    ) -> None:
        """Depth-first walk collecting matches, carrying the parent name.

        ``FileSymbols.all_symbols_flat`` would be shorter but
        drops the parent link, which is the part a caller
        disambiguating a common method name needs most.
        """
        if sym.name == name:
            out.append({
                "file": rel,
                "line": sym.range[0] + 1,
                "kind": sym.kind or "symbol",
                "container": container,
            })
        for child in sym.children:
            self._collect_named(child, name, rel, sym.name, out)

    def find_reference_sites(self, name: str) -> list[tuple[str, int]]:
        """``(file, line)`` for every resolved reference to ``name``.

        Resolved call sites only: the graph records an edge when
        the import resolver could name the target file. That
        restriction is the whole point — a text search for the
        name over-matches on common words and misses aliased
        imports, and this does neither.
        """
        return self._ref_index.references_to_symbol(name)

    def files_importing(self, path: str | Path) -> list[str]:
        """Files with an import or call edge into ``path``, sorted."""
        rel = self._normalise_rel_path(path)
        return sorted(self._ref_index.files_referencing(rel))

    # ------------------------------------------------------------------
    # LSP queries
    # ------------------------------------------------------------------

    def _find_symbol_at(
        self,
        path: str,
        line: int,
        col: int,
    ) -> "tuple[Symbol | None, FileSymbols | None]":
        """Find the deepest symbol containing (line, col).

        Coordinates are 1-indexed (Monaco convention).
        Searches nested children for the deepest match.
        Returns ``(symbol, file_symbols)`` or ``(None, None)``.
        """
        from aic_dc.symbol_index.models import Symbol

        rel = self._normalise_rel_path(path)
        fs = self._all_symbols.get(rel)
        if fs is None:
            return None, None

        # Convert to 0-indexed for range comparison.
        line0 = line - 1
        col0 = col - 1

        best: Symbol | None = None

        def _search(symbols: list["Symbol"]) -> None:
            nonlocal best
            for sym in symbols:
                if sym.range is None:
                    continue
                start_line, start_col, end_line, end_col = sym.range
                # Check containment.
                if (
                    (line0 > start_line or (line0 == start_line and col0 >= start_col))
                    and (line0 < end_line or (line0 == end_line and col0 <= end_col))
                ):
                    best = sym
                    # Search children for a deeper match.
                    if sym.children:
                        _search(sym.children)

        _search(fs.symbols)

        # If no symbol matched, check if the cursor is on a
        # call site within a function/method body.
        if best is not None and best.call_sites:
            for cs in best.call_sites:
                if cs.line == line:  # call_sites use 1-indexed lines
                    # Return the containing symbol — the call site
                    # itself isn't a symbol, but the caller can use
                    # the call site info from the symbol.
                    return best, fs

        # Check imports by line.
        if best is None:
            for imp in fs.imports:
                if imp.line == line:
                    # Synthetic — return None symbol but valid fs
                    # so the caller can use import info.
                    return None, fs

        return best, fs

    def lsp_get_hover(
        self,
        path: str,
        line: int,
        col: int,
    ) -> dict[str, object] | None:
        """Return hover info for the symbol at (path, line, col).

        Coordinates are 1-indexed. Returns
        ``{"contents": [str]}`` on hit, or ``None`` when nothing
        is found. The ``contents`` list holds markdown-formatted
        strings suitable for Monaco's hover widget.
        """
        sym, fs = self._find_symbol_at(path, line, col)
        if sym is None:
            return None

        parts: list[str] = []

        # Build signature string.
        kind = sym.kind or "symbol"
        prefix = f"({kind}) " if kind else ""
        sig = f"{prefix}**{sym.name}**"
        if sym.parameters:
            params = ", ".join(
                (("*" if p.is_vararg else "**" if p.is_kwarg else "")
                 + p.name
                 + (f": {p.type_annotation}" if p.type_annotation else ""))
                for p in sym.parameters
            )
            sig += f"({params})"
        if sym.return_type:
            sig += f" → {sym.return_type}"
        if sym.bases:
            sig += f" extends {', '.join(sym.bases)}"
        parts.append(sig)

        if sym.file_path:
            parts.append(f"*{sym.file_path}*")

        return {"contents": parts}

    def lsp_get_definition(
        self,
        path: str,
        line: int,
        col: int,
    ) -> dict[str, object] | None:
        """Return definition location for the symbol at position.

        Returns ``{"file": str, "range": {"startLineNumber": int,
        "startColumn": int, "endLineNumber": int,
        "endColumn": int}}`` or ``None``.

        Coordinates are 1-indexed (Monaco convention).
        """
        rel = self._normalise_rel_path(path)
        fs = self._all_symbols.get(rel)
        if fs is None:
            return None

        # Check imports FIRST — if the cursor is on an import
        # line, the import is always the intended target
        # regardless of whatever symbol's range happens to
        # contain that line. (Imports nested inside a function
        # body — a common pattern for circular-import avoidance
        # — would otherwise be shadowed by the containing
        # function's range and Go-to-Def would jump to the
        # function's own definition instead.)
        for imp in fs.imports:
            if imp.line == line:
                target = getattr(imp, "resolved_target", None)
                if not target:
                    continue
                # Try to resolve each imported name to its
                # actual definition inside the target file so
                # Monaco scrolls to the class / function / var
                # rather than the top of the file. Falls back
                # to line 1 when names can't be matched (bare
                # `import foo`, wildcard, or the symbol just
                # isn't in the target's extracted symbols).
                target_fs = self._all_symbols.get(target)
                if target_fs is not None and imp.names:
                    for wanted in imp.names:
                        if not wanted or wanted == "*":
                            continue
                        for target_sym in target_fs.all_symbols_flat:
                            if target_sym.name == wanted and target_sym.range:
                                sl, sc, el, ec = target_sym.range
                                return {
                                    "file": target,
                                    "range": {
                                        "startLineNumber": max(1, sl + 1),
                                        "startColumn": max(1, sc + 1),
                                        "endLineNumber": max(1, el + 1),
                                        "endColumn": max(1, ec + 1),
                                    },
                                }
                return {
                    "file": target,
                    "range": {
                        "startLineNumber": 1,
                        "startColumn": 1,
                        "endLineNumber": 1,
                        "endColumn": 1,
                    },
                }

        # Check call sites — if the cursor is on a call
        # that has a resolved target, jump there.
        sym, _ = self._find_symbol_at(path, line, col)
        if sym is not None:
            for cs in sym.call_sites:
                if cs.line == line and cs.target_file:
                    target_fs = self._all_symbols.get(cs.target_file)
                    if target_fs is not None and cs.target_symbol:
                        # Find the target symbol in the target file.
                        for target_sym in target_fs.all_symbols_flat:
                            if target_sym.name == cs.target_symbol and target_sym.range:
                                sl, sc, el, ec = target_sym.range
                                return {
                                    "file": cs.target_file,
                                    "range": {
                                        "startLineNumber": max(1, sl + 1),
                                        "startColumn": max(1, sc + 1),
                                        "endLineNumber": max(1, el + 1),
                                        "endColumn": max(1, ec + 1),
                                    },
                                }
                    # Target file exists but symbol not found — jump
                    # to top of file.
                    if cs.target_file:
                        return {
                            "file": cs.target_file,
                            "range": {
                                "startLineNumber": 1,
                                "startColumn": 1,
                                "endLineNumber": 1,
                                "endColumn": 1,
                            },
                        }

        # Local symbol — return its own definition.
        if sym is not None and sym.range:
            sl, sc, el, ec = sym.range
            return {
                "file": rel,
                "range": {
                    "startLineNumber": max(1, sl + 1),
                    "startColumn": max(1, sc + 1),
                    "endLineNumber": max(1, el + 1),
                    "endColumn": max(1, ec + 1),
                },
            }

        return None

    def lsp_get_references(
        self,
        path: str,
        line: int,
        col: int,
    ) -> list[dict[str, object]]:
        """Return all reference locations for the symbol at position.

        Returns a list of ``{"file": str, "range": {...}}`` dicts.
        Empty list when nothing is found.
        """
        sym, _ = self._find_symbol_at(path, line, col)
        if sym is None:
            return []

        refs = self._ref_index.references_to_symbol(sym.name)
        results: list[dict[str, object]] = []
        for ref_file, ref_line in refs:
            results.append({
                "file": ref_file,
                "range": {
                    "startLineNumber": max(1, ref_line),
                    "startColumn": 1,
                    "endLineNumber": max(1, ref_line),
                    "endColumn": 1,
                },
            })
        return results

    def lsp_get_completions(
        self,
        path: str,
        line: int,
        col: int,
        prefix: str = "",
    ) -> list[dict[str, object]]:
        """Return completion suggestions at the given position.

        Filters by ``prefix`` (case-insensitive substring match
        on the symbol name). Returns up to 50 suggestions, each
        with ``label``, ``kind``, ``detail``, ``insertText``.

        Kind values follow Monaco's CompletionItemKind enum:
        1=Method, 2=Function, 5=Variable, 6=Class, 9=Property,
        17=Keyword.
        """
        rel = self._normalise_rel_path(path)
        fs = self._all_symbols.get(rel)
        if fs is None:
            return []

        kind_map = {
            "class": 6,
            "function": 2,
            "method": 1,
            "variable": 5,
            "property": 9,
        }

        candidates: list[dict[str, object]] = []
        prefix_lower = prefix.lower()

        # File-local symbols.
        for sym in fs.all_symbols_flat:
            if prefix_lower and prefix_lower not in sym.name.lower():
                continue
            candidates.append({
                "label": sym.name,
                "kind": kind_map.get(sym.kind, 0),
                "detail": sym.kind or "",
                "insertText": sym.name,
            })

        # Imported symbols — names from imports that match.
        for imp in fs.imports:
            for name in imp.names:
                if name and name != "*":
                    if prefix_lower and prefix_lower not in name.lower():
                        continue
                    candidates.append({
                        "label": name,
                        "kind": 17,  # Keyword as fallback
                        "detail": f"from {imp.module}" if imp.module else "",
                        "insertText": name,
                    })

        # Deduplicate by label.
        seen: set[str] = set()
        unique: list[dict[str, object]] = []
        for c in candidates:
            label = str(c["label"])
            if label not in seen:
                seen.add(label)
                unique.append(c)

        return unique[:50]