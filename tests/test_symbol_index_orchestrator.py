"""Tests for aic_dc.symbol_index.index — Layer 2.7.

Scope: the SymbolIndex orchestrator that wires parser,
extractors, cache, resolver, reference index, and formatter
into a single entry point.

Strategy:

- Integration-heavy. Tests use real tree-sitter parses via
  the shipped grammars. Small hand-crafted Python files
  under tmp_path exercise the full pipeline end-to-end.
- Mocking reserved for components whose real version would
  drag in git (Repo interactions) — everything else is real.
- One test class per feature area: construction, per-file
  pipeline, multi-file pipeline, stale removal, caching
  behaviour, query methods, snapshot discipline.

Governing spec: specs4/2-indexing/symbol-index.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aic_dc.symbol_index.index import SymbolIndex
from aic_dc.symbol_index.parser import TreeSitterParser


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """A throwaway directory acting as a repo root."""
    return tmp_path


@pytest.fixture
def index(repo_dir: Path) -> SymbolIndex:
    """Fresh SymbolIndex per test.

    Skips the test module if tree-sitter-python isn't
    installed — matches the extractor tests' pattern.
    """
    parser = TreeSitterParser()
    if not parser.is_available("python"):
        pytest.skip("tree_sitter_python not installed")
    return SymbolIndex(repo_root=repo_dir)


def _write(path: Path, content: str) -> None:
    """Write content to path, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    """SymbolIndex wires parser, cache, extractors, resolver, ref index."""

    def test_constructs_with_repo_root(self, repo_dir: Path) -> None:
        """Constructor accepts a repo_root and initialises components."""
        parser = TreeSitterParser()
        if not parser.is_available("python"):
            pytest.skip("tree_sitter_python not installed")
        idx = SymbolIndex(repo_root=repo_dir)
        assert idx.repo_root == repo_dir

    def test_constructs_without_repo_root(self) -> None:
        """Repo root is optional — used for file walking but not required
        for single-file operations."""
        parser = TreeSitterParser()
        if not parser.is_available("python"):
            pytest.skip("tree_sitter_python not installed")
        idx = SymbolIndex()
        assert idx.repo_root is None

    def test_exposes_core_components(self, index: SymbolIndex) -> None:
        """The cache, reference index, and resolver are reachable.

        Tests downstream (especially orchestration tests) need
        access to these — pin the attribute names so a refactor
        can't silently break the public surface.
        """
        assert index._cache is not None
        assert index._ref_index is not None
        assert index._resolver is not None

    def test_all_symbols_empty_initially(self, index: SymbolIndex) -> None:
        """Fresh index has no indexed files."""
        assert index._all_symbols == {}


# ---------------------------------------------------------------------------
# Per-file pipeline — index_file
# ---------------------------------------------------------------------------


class TestIndexFile:
    """Single-file extraction, caching, and storage."""

    def test_indexes_python_file(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A .py file is parsed, extracted, and stored."""
        _write(
            repo_dir / "foo.py",
            "def hello():\n    return 42\n",
        )
        result = index.index_file("foo.py")
        assert result is not None
        assert result.file_path == "foo.py"
        names = [s.name for s in result.symbols]
        assert "hello" in names

    def test_stores_in_all_symbols(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Indexed files are accessible via _all_symbols."""
        _write(repo_dir / "foo.py", "x = 1\n")
        index.index_file("foo.py")
        assert "foo.py" in index._all_symbols
        assert index._all_symbols["foo.py"].file_path == "foo.py"

    def test_unknown_extension_returns_none(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Files without a recognised extension skip extraction.

        The orchestrator walks the repo and calls index_file per
        file; unrecognised extensions must return None cleanly
        so the caller just skips them rather than crashing.
        """
        _write(repo_dir / "readme.md", "# hello\n")
        assert index.index_file("readme.md") is None

    def test_missing_file_returns_none(
        self, index: SymbolIndex
    ) -> None:
        """Missing files return None rather than raising.

        The walker might race with a file deletion; graceful
        None makes the orchestrator resilient to that case.
        """
        assert index.index_file("does-not-exist.py") is None

    def test_cache_hit_on_unchanged_mtime(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A second index_file with unchanged mtime uses the cache.

        We verify this by confirming the returned object is the
        same instance — the cache stores references, not copies,
        so identity is preserved across cache hits.
        """
        _write(repo_dir / "foo.py", "def hello():\n    pass\n")
        first = index.index_file("foo.py")
        second = index.index_file("foo.py")
        assert first is second

    def test_cache_miss_on_mtime_change(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Modifying a file triggers a re-parse.

        We bump the mtime to a known-distinct value rather than
        relying on filesystem timing — some filesystems have 1s
        mtime granularity and back-to-back writes can produce
        the same timestamp.
        """
        import os

        path = repo_dir / "foo.py"
        _write(path, "def old(): pass\n")
        first = index.index_file("foo.py")
        assert first is not None
        assert first.symbols[0].name == "old"

        _write(path, "def new(): pass\n")
        # Force a distinct mtime to avoid filesystem granularity
        # masking the change.
        mtime = path.stat().st_mtime + 1
        os.utime(path, (mtime, mtime))

        second = index.index_file("foo.py")
        assert second is not None
        assert second.symbols[0].name == "new"

    def test_dispatches_by_language(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Different languages route to their own extractor.

        We check that a .py and a .js file both extract
        successfully — proves the dispatch is wired correctly,
        not just that one extractor path works.
        """
        parser = TreeSitterParser()
        if not parser.is_available("javascript"):
            pytest.skip("tree_sitter_javascript not installed")
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.js", "function bar() {}\n")

        py_result = index.index_file("a.py")
        js_result = index.index_file("b.js")

        assert py_result is not None
        assert py_result.symbols[0].name == "foo"
        assert js_result is not None
        assert js_result.symbols[0].name == "bar"


# ---------------------------------------------------------------------------
# Multi-file pipeline — index_repo
# ---------------------------------------------------------------------------


class TestIndexRepo:
    """Full-repo indexing: discovery, dispatch, reference graph."""

    def test_indexes_all_supported_files(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """index_repo walks the given file list and extracts each."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])
        assert "a.py" in index._all_symbols
        assert "b.py" in index._all_symbols

    def test_skips_unsupported_extensions(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Files with no matching extractor are silently skipped.

        The caller often passes the full repo file list; the
        orchestrator must filter rather than crash.
        """
        _write(repo_dir / "a.py", "x = 1\n")
        _write(repo_dir / "readme.md", "# hi\n")
        _write(repo_dir / "data.txt", "plain text\n")
        index.index_repo(["a.py", "readme.md", "data.txt"])
        assert "a.py" in index._all_symbols
        assert "readme.md" not in index._all_symbols
        assert "data.txt" not in index._all_symbols

    def test_builds_reference_index(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """After indexing, the reference index reflects cross-file calls.

        File a.py imports from b.py and calls its function.
        The resolver populates target_file on the call site,
        which feeds into the reference index — files_referencing
        should show the edge.
        """
        _write(
            repo_dir / "b.py",
            "def helper():\n    return 42\n",
        )
        _write(
            repo_dir / "a.py",
            "from b import helper\n"
            "\n"
            "def caller():\n"
            "    return helper()\n",
        )
        index.index_repo(["a.py", "b.py"])
        # a.py depends on b.py (via import + call).
        assert "b.py" in index._ref_index.file_dependencies("a.py")
        assert "a.py" in index._ref_index.files_referencing("b.py")

    def test_resolves_call_site_targets(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Call sites get target_file populated via cross-file resolution.

        Without this post-pass, the reference index would see
        bare call names with no file attribution and no edges
        would form. This is the contract Layer 2.4 depends on.
        """
        _write(
            repo_dir / "helpers.py",
            "def util():\n    return 1\n",
        )
        _write(
            repo_dir / "main.py",
            "from helpers import util\n"
            "\n"
            "def run():\n"
            "    return util()\n",
        )
        index.index_repo(["helpers.py", "main.py"])
        main_symbols = index._all_symbols["main.py"]
        run_fn = next(s for s in main_symbols.symbols if s.name == "run")
        util_call = next(
            cs for cs in run_fn.call_sites if cs.name == "util"
        )
        assert util_call.target_file == "helpers.py"

    def test_resolves_imports(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Import.resolved_target is populated after indexing.

        The reference graph reads resolved_target to build import
        edges — see Layer 2.4 for the contract.
        """
        _write(repo_dir / "b.py", "x = 1\n")
        _write(
            repo_dir / "a.py",
            "from b import x\n",
        )
        index.index_repo(["a.py", "b.py"])
        a_imports = index._all_symbols["a.py"].imports
        assert len(a_imports) == 1
        assert getattr(a_imports[0], "resolved_target", None) == "b.py"

    def test_empty_file_list_clears_nothing(
        self, index: SymbolIndex
    ) -> None:
        """index_repo with an empty list is safe — no crash, no error.

        Edge case for fresh repos or tests setting up state
        incrementally. Orchestrator must not raise on empty
        input.
        """
        index.index_repo([])
        assert index._all_symbols == {}


# ---------------------------------------------------------------------------
# Stale removal
# ---------------------------------------------------------------------------


class TestStaleRemoval:
    """Files dropped from the repo are removed from memory + cache."""

    def test_removes_file_absent_from_list(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A previously-indexed file not in the new list is pruned."""
        _write(repo_dir / "a.py", "x = 1\n")
        _write(repo_dir / "b.py", "y = 2\n")
        index.index_repo(["a.py", "b.py"])
        assert "a.py" in index._all_symbols
        assert "b.py" in index._all_symbols

        # Re-index with only b.py — a.py should be pruned.
        index.index_repo(["b.py"])
        assert "a.py" not in index._all_symbols
        assert "b.py" in index._all_symbols

    def test_invalidates_cache_for_removed_files(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Removed files are also dropped from the signature cache.

        Without this, a file that reappears later would hit a
        stale cache entry with the wrong mtime semantics — the
        cache would happily return the pre-removal content.
        """
        _write(repo_dir / "a.py", "x = 1\n")
        _write(repo_dir / "b.py", "y = 2\n")
        index.index_repo(["a.py", "b.py"])
        assert index._cache.has("a.py")

        index.index_repo(["b.py"])
        assert not index._cache.has("a.py")

    def test_stale_removal_before_reference_build(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Pruning runs before the reference index is rebuilt.

        If stale removal ran after, the reference index would
        briefly contain edges to/from the deleted file. Pin the
        ordering by checking that the rebuilt ref index has no
        trace of the removed file.
        """
        _write(
            repo_dir / "b.py",
            "def helper(): return 1\n",
        )
        _write(
            repo_dir / "a.py",
            "from b import helper\n"
            "def caller(): return helper()\n",
        )
        index.index_repo(["a.py", "b.py"])
        assert "b.py" in index._ref_index.file_dependencies("a.py")

        # Remove b.py and a.py together; ref index should be clean.
        index.index_repo([])
        assert index._ref_index.file_dependencies("a.py") == set()
        assert index._ref_index.files_referencing("b.py") == set()


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestInvalidateFile:
    """Explicit invalidation clears cache + memory for a single file."""

    def test_invalidate_removes_entry(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """invalidate_file drops the file from _all_symbols and cache."""
        _write(repo_dir / "foo.py", "x = 1\n")
        index.index_file("foo.py")
        assert "foo.py" in index._all_symbols
        assert index._cache.has("foo.py")

        result = index.invalidate_file("foo.py")
        assert result is True
        assert "foo.py" not in index._all_symbols
        assert not index._cache.has("foo.py")

    def test_invalidate_absent_file_returns_false(
        self, index: SymbolIndex
    ) -> None:
        """Invalidating an unknown file is a no-op returning False.

        Callers use the return value to log "we cleaned up N
        entries"; raising on missing would force try/except
        around every call.
        """
        assert index.invalidate_file("nope.py") is False


# ---------------------------------------------------------------------------
# Symbol map formatting
# ---------------------------------------------------------------------------


class TestSymbolMap:
    """Formatter integration — context and LSP variants."""

    def test_get_symbol_map_returns_text(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """get_symbol_map produces non-empty output for a non-empty index."""
        _write(
            repo_dir / "foo.py",
            "def hello(): pass\n",
        )
        index.index_repo(["foo.py"])
        result = index.get_symbol_map()
        assert "foo.py" in result
        assert "hello" in result

    def test_get_symbol_map_empty_index_is_empty(
        self, index: SymbolIndex
    ) -> None:
        """No indexed files → empty string.

        Matches the formatter contract — callers concatenate
        this into the prompt and an empty string lets them
        skip the section cleanly.
        """
        assert index.get_symbol_map() == ""

    def test_get_symbol_map_respects_exclude_files(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Excluded files don't appear in the map output.

        Used by the streaming handler when a file's full content
        is already in a cached tier (uniqueness invariant).
        """
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])
        result = index.get_symbol_map(exclude_files={"a.py"})
        assert "a.py" not in result
        assert "b.py" in result
        assert "bar" in result

    def test_lsp_map_includes_line_numbers(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """LSP variant annotates symbols with :N line numbers.

        Per specs3 — the LSP variant is what editor features
        consume, so line numbers are required. The context
        variant (for the LLM) omits them for token efficiency.
        """
        _write(
            repo_dir / "foo.py",
            "def hello(): pass\n",
        )
        index.index_repo(["foo.py"])
        lsp = index.get_lsp_symbol_map()
        # Function is on line 1 (1-indexed).
        assert ":1" in lsp

    def test_context_map_has_no_symbol_line_numbers(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Context variant (LLM-facing) has no :N annotations on symbols.

        Callers grep for ``:N`` patterns on symbol lines to
        detect LSP-variant leaks into the context prompt. The
        file header line uses a colon but we're checking the
        symbol line specifically.
        """
        _write(
            repo_dir / "foo.py",
            "def hello(): pass\n",
        )
        index.index_repo(["foo.py"])
        ctx = index.get_symbol_map()
        # Find the symbol line containing `hello`; file header
        # (``foo.py:``) is a separate line.
        hello_line = next(
            line for line in ctx.splitlines()
            if "hello" in line and "foo.py" not in line
        )
        # No trailing :N on the symbol line itself.
        assert ":" not in hello_line

    def test_get_legend_returns_legend_text(
        self, index: SymbolIndex
    ) -> None:
        """get_legend returns the abbreviation key block.

        Layer 3's prompt assembly splits the legend from the
        map so the legend can live in a cached L0 block while
        the map cascades through tiers. Pinning this method
        keeps that separation possible.
        """
        legend = index.get_legend()
        assert legend
        # Kind codes are documented in the legend.
        assert "c=class" in legend or "c = class" in legend

    def test_get_file_symbol_block_returns_single_file(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """get_file_symbol_block returns just one file's entry.

        Used by the stability tracker when assembling cached
        tier content — it needs to render one file's block at
        a time, not the whole map.
        """
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])
        block = index.get_file_symbol_block("a.py")
        assert block is not None
        assert "a.py" in block
        assert "foo" in block
        # Other file's symbol must not appear.
        assert "bar" not in block

    def test_get_file_symbol_block_unknown_file(
        self, index: SymbolIndex
    ) -> None:
        """Unknown paths return None rather than raising.

        The stability tracker polls per-file; a deleted file
        between request assembly and block retrieval would
        otherwise crash the tier build.
        """
        assert index.get_file_symbol_block("nope.py") is None


# ---------------------------------------------------------------------------
# Signature hash
# ---------------------------------------------------------------------------


class TestSignatureHash:
    """get_signature_hash exposes the cache's structural hash."""

    def test_returns_hash_for_indexed_file(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Hash is non-empty for an indexed file."""
        _write(repo_dir / "foo.py", "def hello(): pass\n")
        index.index_file("foo.py")
        h = index.get_signature_hash("foo.py")
        assert h is not None
        assert len(h) == 64  # SHA-256 hex digest

    def test_returns_none_for_unknown_file(
        self, index: SymbolIndex
    ) -> None:
        """Unknown paths return None, not empty string.

        The stability tracker uses None as a distinct signal
        from "empty hash" — see base_cache.py's contract.
        """
        assert index.get_signature_hash("nope.py") is None

    def test_hash_stable_across_unchanged_content(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Re-indexing identical content yields the same hash.

        Critical for the stability tracker — a spurious hash
        change would demote files unnecessarily. Identity of
        content must produce identity of hash.
        """
        path = repo_dir / "foo.py"
        _write(path, "def hello(): pass\n")
        index.index_file("foo.py")
        first_hash = index.get_signature_hash("foo.py")

        # Invalidate and re-index with the same content.
        index.invalidate_file("foo.py")
        index.index_file("foo.py")
        second_hash = index.get_signature_hash("foo.py")

        assert first_hash == second_hash


# ---------------------------------------------------------------------------
# Snapshot discipline
# ---------------------------------------------------------------------------


class TestSnapshotDiscipline:
    """Read queries don't mutate indexed state.

    Layer 3's streaming pipeline treats the index as a
    read-only snapshot within a request's execution window.
    These tests pin that contract — multiple reads return
    identical results without any re-indexing.
    """

    def test_repeated_map_calls_are_stable(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Two get_symbol_map calls return identical output."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        first = index.get_symbol_map()
        second = index.get_symbol_map()
        assert first == second

    def test_file_block_stable_across_calls(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """get_file_symbol_block is deterministic across calls."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        first = index.get_file_symbol_block("a.py")
        second = index.get_file_symbol_block("a.py")
        assert first == second

    def test_reads_do_not_mutate_all_symbols(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Query methods don't insert/remove entries in _all_symbols.

        If a query path accidentally triggered re-indexing,
        _all_symbols might grow or shrink between two reads.
        Pin the snapshot contract by asserting the dict is
        byte-identical before and after query calls.
        """
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])
        before = set(index._all_symbols.keys())

        # Exercise every query method.
        index.get_symbol_map()
        index.get_lsp_symbol_map()
        index.get_legend()
        index.get_file_symbol_block("a.py")
        index.get_signature_hash("a.py")

        after = set(index._all_symbols.keys())
        assert before == after


# ---------------------------------------------------------------------------
# Incremental re-index — reindex_files
# ---------------------------------------------------------------------------


class TestReindexFiles:
    """The post-write path behind the MCP bridge.

    The interesting cases are all about the graph *around* the
    file that changed: a re-parse that leaves the resolver, the
    call sites, or the reference index describing the previous
    version is worse than no index at all, because it answers
    confidently.
    """

    def test_it_picks_up_a_new_symbol(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The plain case: the file's own symbols are current after."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        _write(repo_dir / "a.py", "def foo(): pass\ndef added(): pass\n")

        assert index.reindex_files(["a.py"]) == ["a.py"]
        assert "added" in index.get_symbol_map()

    def test_a_brand_new_file_joins_the_index(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The agent's Write creates files; index_repo never saw them."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        _write(repo_dir / "b.py", "def bar(): pass\n")

        assert index.reindex_files(["b.py"]) == ["b.py"]
        assert index.get_indexed_files() == ["a.py", "b.py"]

    def test_a_new_file_resolves_its_own_imports(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The resolver only resolves within the file set it knows, so a
        file it has never heard of would resolve nothing until the next
        full build — this is why reindex_files widens that set first."""
        _write(repo_dir / "lib.py", "def helper(): pass\n")
        index.index_repo(["lib.py"])
        _write(
            repo_dir / "app.py",
            "from lib import helper\n\ndef run():\n    helper()\n",
        )

        index.reindex_files(["app.py"])
        assert index.files_importing("lib.py") == ["app.py"]

    def test_an_edit_updates_what_other_files_point_at(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The caller of a renamed function was not re-parsed, so its call
        sites have to be re-resolved across the whole index."""
        _write(repo_dir / "lib.py", "def helper(): pass\n")
        _write(
            repo_dir / "app.py",
            "from lib import helper\n\ndef run():\n    helper()\n",
        )
        index.index_repo(["lib.py", "app.py"])
        assert index.find_reference_sites("helper")

        _write(repo_dir / "lib.py", "def renamed(): pass\n")
        index.reindex_files(["lib.py"])
        # The edge is gone rather than left pointing at a symbol that no
        # longer exists, which is what a partial rebuild would leave.
        assert index.find_definitions("helper") == []

    def test_a_deleted_file_drops_out(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A write can be a delete; the caller learns from the return
        value which of its paths produced symbols."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])
        (repo_dir / "b.py").unlink()

        assert index.reindex_files(["b.py"]) == []
        assert index.get_indexed_files() == ["a.py"]

    def test_files_of_no_interest_are_skipped(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A README write must not cost two whole-index passes."""
        _write(repo_dir / "README.md", "# hi\n")
        assert index.reindex_files(["README.md"]) == []
        assert index.reindex_files([]) == []

    def test_it_is_idempotent(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Twice in a row is the debounce racing itself; the second call
        must not duplicate symbols or reference edges."""
        _write(repo_dir / "lib.py", "def helper(): pass\n")
        _write(
            repo_dir / "app.py",
            "from lib import helper\n\ndef run():\n    helper()\n",
        )
        index.index_repo(["lib.py", "app.py"])

        index.reindex_files(["app.py"])
        once = (index.get_symbol_map(), index.find_reference_sites("helper"))
        index.reindex_files(["app.py"])
        assert (index.get_symbol_map(), index.find_reference_sites("helper")) == once


# ---------------------------------------------------------------------------
# Path and name queries the bridge asks
# ---------------------------------------------------------------------------


class TestResolveIndexedPath:
    """Does the index know this file, and what does it call it?"""

    def test_it_returns_the_canonical_key(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A caller holding a path handed to it by an agent must not have
        to normalise paths itself and then disagree with us."""
        _write(repo_dir / "pkg" / "a.py", "def foo(): pass\n")
        index.index_repo(["pkg/a.py"])

        assert index.resolve_indexed_path("pkg/a.py") == "pkg/a.py"
        assert index.resolve_indexed_path("./pkg/a.py") == "pkg/a.py"
        assert index.resolve_indexed_path(repo_dir / "pkg" / "a.py") == "pkg/a.py"

    def test_it_tidies_a_path_a_model_typed(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Answering "not indexed" for a path plainly in the index reads
        as a broken index, not as a spelling quibble."""
        _write(repo_dir / "pkg" / "a.py", "def foo(): pass\n")
        index.index_repo(["pkg/a.py"])
        assert index.resolve_indexed_path("pkg/../pkg/a.py") == "pkg/a.py"

    def test_a_dotted_directory_is_not_mistaken_for_a_relative_path(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """A leading dot is a real directory name; only `..` leaves."""
        _write(repo_dir / ".tools" / "a.py", "def foo(): pass\n")
        index.index_repo([".tools/a.py"])
        assert index.resolve_indexed_path(".tools/a.py") == ".tools/a.py"
        assert index.resolve_indexed_path("./.tools/a.py") == ".tools/a.py"

    def test_an_unindexed_path_is_none_not_an_error(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The caller reports it as unknown, one path among several."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        assert index.resolve_indexed_path("nope.py") is None
        assert index.resolve_indexed_path("") is None
        assert index.resolve_indexed_path("..") is None

    def test_a_path_outside_the_repo_is_none(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Not a spelling variant of anything we hold."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        assert index.resolve_indexed_path("/etc/passwd") is None
        assert index.resolve_indexed_path("../outside/a.py") is None


class TestNameQueries:
    """find_definitions / find_reference_sites / files_importing.

    Name-based rather than position-based on purpose: the caller
    is an agent holding a name it read in a diff, not a cursor in
    an editor.
    """

    @pytest.fixture
    def populated(self, index: SymbolIndex, repo_dir: Path) -> SymbolIndex:
        _write(
            repo_dir / "lib.py",
            "class Thing:\n"
            "    def build(self): pass\n"
            "\n"
            "def build(): pass\n",
        )
        _write(
            repo_dir / "app.py",
            "from lib import build\n\ndef run():\n    build()\n",
        )
        index.index_repo(["lib.py", "app.py"])
        return index

    def test_definitions_carry_their_container(
        self, populated: SymbolIndex
    ) -> None:
        """The one fact that tells six same-named `build` methods apart."""
        found = populated.find_definitions("build")
        containers = {(d["file"], d["container"]) for d in found}
        assert ("lib.py", "Thing") in containers
        assert ("lib.py", None) in containers

    def test_lines_are_one_indexed(self, populated: SymbolIndex) -> None:
        """Ranges are stored 0-indexed; every consumer shows the number to
        a reader, so the +1 belongs in the index rather than in each."""
        method = [
            d for d in populated.find_definitions("build")
            if d["container"] == "Thing"
        ][0]
        assert method["line"] == 2

    def test_an_unknown_name_finds_nothing(self, populated: SymbolIndex) -> None:
        assert populated.find_definitions("no_such_name") == []
        assert populated.find_reference_sites("no_such_name") == []

    def test_reference_sites_are_file_and_line(
        self, populated: SymbolIndex
    ) -> None:
        sites = populated.find_reference_sites("build")
        assert sites
        assert all(
            isinstance(f, str) and isinstance(line, int) for f, line in sites
        )

    def test_files_importing_names_the_importers(
        self, populated: SymbolIndex
    ) -> None:
        assert populated.files_importing("lib.py") == ["app.py"]
        assert populated.files_importing("app.py") == []

# ---------------------------------------------------------------------------
# Staleness detection — find_stale_files (CC-18)
# ---------------------------------------------------------------------------


def _touch_newer(path: Path, seconds: float = 10.0) -> None:
    """Push a file's mtime forward, without depending on clock
    resolution.

    Writing the same file twice in a fast test can land inside one
    filesystem timestamp tick, which would make a genuine change look
    unchanged and the test pass for the wrong reason.
    """
    stamp = path.stat().st_mtime + seconds
    os.utime(path, (stamp, stamp))


class TestFindStaleFiles:
    """What changed when nothing told us — the `Bash` blind spot.

    The mtime cache has always been able to answer this; until CC-18
    nothing asked it. These tests are about the question, not the
    caching, so they change files behind the index's back rather than
    through `reindex_files`.
    """

    def test_an_untouched_index_is_not_stale(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        assert index.find_stale_files() == []

    def test_an_empty_index_reports_nothing(
        self, index: SymbolIndex
    ) -> None:
        assert index.find_stale_files() == []

    def test_a_file_changed_behind_our_back_is_found(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The `sed -i` case, which is the whole reason this exists."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])

        _write(repo_dir / "a.py", "def foo(): pass\ndef added(): pass\n")
        _touch_newer(repo_dir / "a.py")

        assert index.find_stale_files() == ["a.py"]

    def test_a_deleted_file_is_stale_not_silent(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """An `mv` away or an `rm`. Reported rather than dropped: this
        is a query, and the caller may not be about to re-index."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        _write(repo_dir / "b.py", "def bar(): pass\n")
        index.index_repo(["a.py", "b.py"])

        (repo_dir / "a.py").unlink()

        assert index.find_stale_files() == ["a.py"]

    def test_only_the_changed_file_is_named(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The saving that makes the sweep affordable: `reindex_files`
        ends in two whole-index passes, so a sweep that over-reported
        would cost as much as re-indexing everything."""
        for name in ("a.py", "b.py", "c.py"):
            _write(repo_dir / name, f"def {name[0]}(): pass\n")
        index.index_repo(["a.py", "b.py", "c.py"])

        _write(repo_dir / "b.py", "def b(): pass\ndef more(): pass\n")
        _touch_newer(repo_dir / "b.py")

        assert index.find_stale_files() == ["b.py"]

    def test_a_file_the_index_never_knew_is_not_reported(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """The documented gap. A file a shell command *created* holds no
        cached mtime to disagree with, so the sweep cannot see it —
        catching it would mean re-walking the repo. Recorded in
        specs5/2-indexing/symbol-index.md; asserted here so the
        limitation is a decision rather than a surprise.
        """
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])

        _write(repo_dir / "created_by_a_shell.py", "def new(): pass\n")

        assert index.find_stale_files() == []

    def test_it_is_a_query_and_mutates_nothing(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """Asking twice gives the same answer, and the index still
        serves the old content until someone re-indexes."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        _write(repo_dir / "a.py", "def renamed(): pass\n")
        _touch_newer(repo_dir / "a.py")

        assert index.find_stale_files() == ["a.py"]
        assert index.find_stale_files() == ["a.py"]
        assert index.get_indexed_files() == ["a.py"]
        assert "foo" in index.get_symbol_map()

    def test_the_sweep_feeds_reindex_and_closes_the_gap(
        self, index: SymbolIndex, repo_dir: Path
    ) -> None:
        """End to end: the pair is what phase 8 actually ships."""
        _write(repo_dir / "a.py", "def foo(): pass\n")
        index.index_repo(["a.py"])
        _write(repo_dir / "a.py", "def foo(): pass\ndef added(): pass\n")
        _touch_newer(repo_dir / "a.py")

        index.reindex_files(index.find_stale_files())

        assert "added" in index.get_symbol_map()
        assert index.find_stale_files() == []
