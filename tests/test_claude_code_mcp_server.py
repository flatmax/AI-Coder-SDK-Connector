"""Tests for aic_dc.claude_code.mcp_server — conversion phase 4.

The bridge's job is to answer the agent's questions about the repo from
AIC-DC's own indexes. What is worth pinning is not the map format — the
formatters have their own tests — but the properties an agent's behaviour
depends on:

- **Read-only, all six.** Pinned as a statement of intent, not as the
  mechanism: a live ``acceptEdits`` run showed the CLI raising a permission
  request for an ``mcp__aic-dc__*`` tool that carried ``readOnlyHint=True``,
  so the annotation is advisory metadata for the model and the UI and buys
  nothing at the gate. What keeps these calls out of the dialog is the
  explicit allow in ``PermissionBroker.can_use_tool``
  (``TestOurOwnToolsAreUngated``). The assertion here still earns its
  place: a tool that lost the annotation, or gained a mutation, would be
  advertising itself wrongly to the model, and the ungating above would be
  covering for a tool that is no longer read-only.
- **Never an empty answer for a missing index.** An empty symbol map reads
  as "this repo has no symbols", and an agent does not go back to check.
  Not-ready and never-going-to-work are also different answers, because
  one of them is worth retrying.
- **Never stale.** Every index-reading tool flushes the pending re-index
  before it answers, so the agent can write a file and immediately ask
  about it.
- **Scoping actually scopes.** The formatter renders everything it holds
  minus the exclusions, so a scope expressed as "the files I want" instead
  of "the files I do not" silently returns the whole repo.
- **Chunked output says it was chunked.** A truncated-looking map with no
  continuation note is read as a complete one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aic_dc.claude_code.mcp_server import (
    MAX_FILE_SYMBOL_PATHS,
    MAX_RESPONSE_CHARS,
    SERVER_NAME,
    McpBridge,
)
from aic_dc.claude_code.permissions import AIC_DC_MCP_SERVER, classify_tool


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSymbolIndex:
    """Answers the four calls the bridge makes, and records exclusions."""

    def __init__(self, files=None):
        self._files = dict(files or {})
        self.exclusions: list[set[str] | None] = []
        self.definitions: list[dict[str, object]] = []
        self.sites: list[tuple[str, int]] = []
        self.importers: list[str] = []

    def get_indexed_files(self) -> list[str]:
        return sorted(self._files)

    def resolve_indexed_path(self, path):
        rel = str(path).strip("/")
        return rel if rel in self._files else None

    def get_file_symbol_block(self, path):
        return self._files.get(str(path))

    def _render(self, exclude_files):
        self.exclusions.append(exclude_files)
        included = [
            path for path in sorted(self._files)
            if path not in (exclude_files or set())
        ]
        return "\n".join(f"{path}: {self._files[path]}" for path in included)

    def get_symbol_map(self, exclude_files=None):
        return self._render(exclude_files)

    def get_lsp_symbol_map(self, exclude_files=None):
        return self._render(exclude_files)

    def find_definitions(self, name):
        return list(self.definitions)

    def find_reference_sites(self, name):
        return list(self.sites)

    def files_importing(self, path):
        return list(self.importers)


class FakeDocIndex:
    def __init__(self, files=None):
        self._files = dict(files or {})
        self.exclusions: list[set[str] | None] = []

    def get_indexed_files(self):
        return sorted(self._files)

    def get_file_doc_block(self, path):
        return self._files.get(str(path))

    def get_doc_map(self, exclude_files=None):
        self.exclusions.append(exclude_files)
        included = [
            path for path in sorted(self._files)
            if path not in (exclude_files or set())
        ]
        return "\n".join(f"{path}: {self._files[path]}" for path in included)


def text_of(result) -> str:
    """The one text block every handler returns."""
    assert list(result) == ["content"]
    blocks = result["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    return blocks[0]["text"]


@pytest.fixture
def symbols():
    return FakeSymbolIndex({
        "src/a.py": "c A",
        "src/b.py": "f b",
        "webapp/c.js": "f c",
    })


@pytest.fixture
def docs():
    return FakeDocIndex({"README.md": "h1 Title", "docs/guide.md": "h1 Guide"})


@pytest.fixture
def bridge(symbols, docs):
    return McpBridge(
        symbol_index=lambda: symbols,
        symbol_index_ready=lambda: True,
        doc_index=lambda: docs,
        doc_index_ready=lambda: True,
    )


# ---------------------------------------------------------------------------
# The server and its tool inventory
# ---------------------------------------------------------------------------


class TestServerShape:
    def test_the_server_name_matches_the_permission_classifier(self):
        """`mcp__aic-dc__*` is classified `read` by name.

        A rename here without one there turns every bridge call into a
        third-party MCP prompt — gated by default, so the agent would stop
        being able to ask for a symbol map without a click.
        """
        assert SERVER_NAME == AIC_DC_MCP_SERVER
        assert classify_tool(f"mcp__{SERVER_NAME}__symbol_map") == "read"

    def test_it_builds_an_in_process_server(self, bridge):
        config = bridge.build_server()
        assert config["type"] == "sdk"
        assert config["name"] == SERVER_NAME

    def test_the_six_tools_are_all_there_and_all_read_only(self, bridge):
        """The inventory is a budget: every tool costs context every turn."""
        tools = _tools_of(bridge)
        assert set(tools) == {
            "symbol_map",
            "file_symbols",
            "find_references",
            "doc_outline",
            "review_state",
            "ui_state",
        }
        for name, tool in tools.items():
            assert tool.annotations is not None, name
            assert tool.annotations.readOnlyHint is True, name

    def test_every_optional_argument_stays_optional(self, bridge):
        """Dict-style schemas mark every key required; these must not.

        A `symbol_map` whose `path_prefix` is required cannot be called for
        the whole repo, which is its main use.
        """
        tools = _tools_of(bridge)
        assert tools["symbol_map"].input_schema["required"] == []
        assert tools["doc_outline"].input_schema["required"] == []
        assert tools["review_state"].input_schema["required"] == []
        assert tools["ui_state"].input_schema["required"] == []
        assert tools["file_symbols"].input_schema["required"] == ["paths"]
        assert tools["find_references"].input_schema["required"] == ["symbol"]

    async def test_the_handlers_unpack_their_arguments(self, bridge, symbols):
        """The SDK hands the handler one dict; the tools take named args."""
        tools = _tools_of(bridge)
        answer = text_of(await tools["symbol_map"].handler({"path_prefix": "webapp"}))
        assert "webapp/c.js" in answer
        assert "src/a.py" not in answer

    async def test_a_handler_called_with_no_arguments_still_works(self, bridge):
        """`ui_state` and `review_state` take an empty dict."""
        tools = _tools_of(bridge)
        assert text_of(await tools["ui_state"].handler({}))
        assert text_of(await tools["review_state"].handler({}))


def _tools_of(bridge):
    """Name → ``SdkMcpTool``, read before the SDK swallows them.

    ``create_sdk_mcp_server`` folds the definitions into an MCP ``Server``
    that does not hand them back, which is why ``build_tools`` exists as a
    separate step.
    """
    return {tool.name: tool for tool in bridge.build_tools()}


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    async def test_a_building_index_says_so_and_says_to_retry(self):
        """Not an empty map: an empty map is read as "no symbols here"."""
        bridge = McpBridge(
            symbol_index=lambda: FakeSymbolIndex({"a.py": "c A"}),
            symbol_index_ready=lambda: False,
        )
        answer = text_of(await bridge.symbol_map())
        assert "still building" in answer
        assert "Retry" in answer
        assert "a.py" not in answer

    async def test_a_missing_index_does_not_invite_a_retry(self):
        """A permanent failure retried every turn is a turn wasted."""
        bridge = McpBridge(symbol_index=lambda: None)
        answer = text_of(await bridge.symbol_map())
        assert "not available" in answer
        assert "will not start working on a retry" in answer

    async def test_the_document_index_degrades_the_same_way(self):
        building = McpBridge(
            doc_index=lambda: FakeDocIndex({"a.md": "h1"}),
            doc_index_ready=lambda: False,
        )
        assert "still building" in text_of(await building.doc_outline())
        absent = McpBridge(doc_index=lambda: None)
        assert "not available" in text_of(await absent.doc_outline())

    async def test_an_empty_scope_is_distinguished_from_an_empty_index(
        self, bridge, symbols
    ):
        """Two different next moves: fix the filter, or stop asking."""
        narrowed = text_of(await bridge.symbol_map(path_prefix="nope/"))
        assert "3 file(s) are indexed" in narrowed
        empty = McpBridge(
            symbol_index=lambda: FakeSymbolIndex({}),
            symbol_index_ready=lambda: True,
        )
        assert "no files" in text_of(await empty.symbol_map())

    async def test_a_failing_flush_still_answers(self, symbols):
        """Stale beats silent: the index kept its previous contents."""

        async def boom():
            raise RuntimeError("re-index exploded")

        bridge = McpBridge(
            symbol_index=lambda: symbols,
            symbol_index_ready=lambda: True,
            flush=boom,
        )
        assert "src/a.py" in text_of(await bridge.symbol_map())


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    async def test_every_index_reading_tool_flushes_first(self, symbols, docs):
        """The agent writes a file, then asks about it, in the same turn."""
        order: list[str] = []

        async def flush():
            order.append("flush")

        bridge = McpBridge(
            symbol_index=lambda: (order.append("read") or symbols),
            symbol_index_ready=lambda: True,
            doc_index=lambda: (order.append("read") or docs),
            doc_index_ready=lambda: True,
            flush=flush,
        )
        for call in (
            bridge.symbol_map(),
            bridge.file_symbols(paths=["src/a.py"]),
            bridge.find_references(symbol="A"),
            bridge.doc_outline(),
        ):
            order.clear()
            await call
            assert order[0] == "flush", order

    async def test_the_session_fact_tools_do_not_wait_on_the_index(self, symbols):
        """`ui_state` and `review_state` read no index, so nothing to flush.

        Not just an optimisation: these two are the tools the agent reaches
        for while a long re-index is in flight.
        """
        flushes: list[int] = []

        async def flush():
            flushes.append(1)

        bridge = McpBridge(
            review_state=lambda: {"active": False},
            ui_state=lambda: {},
            flush=flush,
        )
        await bridge.review_state()
        await bridge.ui_state()
        assert flushes == []


# ---------------------------------------------------------------------------
# symbol_map
# ---------------------------------------------------------------------------


class TestSymbolMap:
    async def test_it_excludes_against_the_whole_index_not_the_scope(
        self, bridge, symbols
    ):
        """The bug this test exists for.

        ``get_symbol_map`` renders everything it holds minus the
        exclusions. An exclusion set computed from the scoped list alone
        re-admits every file the scope was meant to drop, so a
        ``path_prefix`` of one file returned the entire repo.
        """
        answer = text_of(await bridge.symbol_map(path_prefix="src/a.py"))
        assert "src/a.py" in answer
        assert "src/b.py" not in answer
        assert "webapp/c.js" not in answer
        assert symbols.exclusions[-1] == {"src/b.py", "webapp/c.js"}

    async def test_a_directory_prefix_takes_the_subtree(self, bridge):
        answer = text_of(await bridge.symbol_map(path_prefix="src"))
        assert "src/a.py" in answer and "src/b.py" in answer
        assert "webapp/c.js" not in answer

    async def test_a_language_filter_uses_the_real_extension_table(self, bridge):
        """Not a substring guess: `language_for_file` is the one authority."""
        answer = text_of(await bridge.symbol_map(language="javascript"))
        assert "webapp/c.js" in answer
        assert "src/a.py" not in answer

    async def test_a_big_map_is_chunked_and_says_it_was(self):
        """A map that looks truncated and says nothing reads as complete."""
        big = FakeSymbolIndex({
            f"src/f{i:03d}.py": "x" * (MAX_RESPONSE_CHARS // 4)
            for i in range(12)
        })
        bridge = McpBridge(
            symbol_index=lambda: big, symbol_index_ready=lambda: True
        )
        first = text_of(await bridge.symbol_map())
        assert "remaining" in first
        assert 'cursor="src/f' in first

    async def test_the_cursor_walks_the_whole_index_exactly_once(self):
        big = FakeSymbolIndex({
            f"src/f{i:03d}.py": "x" * (MAX_RESPONSE_CHARS // 4)
            for i in range(12)
        })
        bridge = McpBridge(
            symbol_index=lambda: big, symbol_index_ready=lambda: True
        )
        seen: list[str] = []
        cursor = None
        for _ in range(20):
            answer = text_of(await bridge.symbol_map(cursor=cursor))
            seen.extend(
                line.split(":")[0]
                for line in answer.splitlines()
                if line.startswith("src/f")
            )
            if "remaining" not in answer:
                break
            cursor = answer.split('cursor="')[1].split('"')[0]
        assert seen == big.get_indexed_files()

    async def test_one_oversized_file_still_makes_progress(self):
        """A chunk of nothing is a cursor that never advances."""
        huge = FakeSymbolIndex({"src/huge.py": "x" * (MAX_RESPONSE_CHARS * 3)})
        bridge = McpBridge(
            symbol_index=lambda: huge, symbol_index_ready=lambda: True
        )
        assert "src/huge.py" in text_of(await bridge.symbol_map())

    async def test_a_cursor_past_the_end_says_the_map_is_complete(self, bridge):
        answer = text_of(await bridge.symbol_map(cursor="zzz"))
        assert "complete" in answer


# ---------------------------------------------------------------------------
# file_symbols
# ---------------------------------------------------------------------------


class TestFileSymbols:
    async def test_it_returns_the_line_numbered_variant(self, bridge, symbols):
        """`:N` numbers are what make this cheaper than Read."""
        await bridge.file_symbols(paths=["src/a.py"])
        assert symbols.exclusions[-1] == {"src/b.py", "webapp/c.js"}

    async def test_an_unindexed_path_is_named_rather_than_dropped(self, bridge):
        """Silence would read as "this file has no symbols"."""
        answer = text_of(await bridge.file_symbols(paths=["src/a.py", "nope.txt"]))
        assert "src/a.py" in answer
        assert "nope.txt" in answer
        assert "Not in the symbol index" in answer

    async def test_no_paths_asks_for_paths(self, bridge):
        assert "No paths given" in text_of(await bridge.file_symbols(paths=[]))
        assert "No paths given" in text_of(await bridge.file_symbols())

    async def test_too_many_paths_points_at_the_map_instead(self, bridge):
        many = [f"src/f{i}.py" for i in range(MAX_FILE_SYMBOL_PATHS + 1)]
        answer = text_of(await bridge.file_symbols(paths=many))
        assert "symbol_map" in answer
        assert "path_prefix" in answer

    async def test_junk_in_the_path_list_is_ignored(self, bridge):
        answer = text_of(await bridge.file_symbols(paths=["", None, 7, "src/a.py"]))
        assert "src/a.py" in answer


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


class TestFindReferences:
    async def test_it_reports_definitions_sites_and_importers(
        self, bridge, symbols
    ):
        symbols.definitions = [
            {"file": "src/a.py", "line": 12, "kind": "method", "container": "A"}
        ]
        symbols.sites = [("src/b.py", 40), ("webapp/c.js", 3)]
        symbols.importers = ["src/b.py"]
        answer = text_of(await bridge.find_references(symbol="run"))
        assert "src/a.py:12" in answer
        assert "in A" in answer
        assert "src/b.py:40" in answer
        assert "2 resolved call site(s)" in answer
        assert "Files importing" in answer

    async def test_a_name_with_nothing_behind_it_says_to_use_grep(
        self, bridge, symbols
    ):
        """Because the index parses some languages and not others."""
        answer = text_of(await bridge.find_references(symbol="ghost"))
        assert "Grep" in answer

    async def test_a_referenced_but_undefined_name_is_not_silent(
        self, bridge, symbols
    ):
        """An imported third-party name has sites and no definition."""
        symbols.sites = [("src/b.py", 4)]
        answer = text_of(await bridge.find_references(symbol="requests"))
        assert "no definition in the index" in answer
        assert "src/b.py:4" in answer

    async def test_no_symbol_asks_for_one(self, bridge):
        assert "No symbol given" in text_of(await bridge.find_references())
        assert "No symbol given" in text_of(await bridge.find_references(symbol="  "))

    async def test_a_flood_of_sites_is_truncated_and_says_how_many(
        self, bridge, symbols
    ):
        """A list of 900 sites is a haystack, not an answer."""
        symbols.definitions = [
            {"file": "src/a.py", "line": 1, "kind": "function", "container": None}
        ]
        symbols.sites = [(f"src/f{i}.py", i) for i in range(400)]
        answer = text_of(await bridge.find_references(symbol="get"))
        assert "400 resolved call site(s)" in answer
        assert "more, not shown" in answer


# ---------------------------------------------------------------------------
# doc_outline
# ---------------------------------------------------------------------------


class TestDocOutline:
    async def test_it_renders_the_outline_map(self, bridge):
        answer = text_of(await bridge.doc_outline())
        assert "README.md" in answer and "docs/guide.md" in answer

    async def test_it_scopes_by_prefix_against_the_whole_index(self, bridge, docs):
        answer = text_of(await bridge.doc_outline(path_prefix="docs"))
        assert "docs/guide.md" in answer
        assert "README.md" not in answer
        assert docs.exclusions[-1] == {"README.md"}


# ---------------------------------------------------------------------------
# review_state
# ---------------------------------------------------------------------------


class TestReviewState:
    async def test_no_review_is_an_explicit_answer(self, bridge):
        answer = text_of(await bridge.review_state())
        assert "No code review is active" in answer
        assert "git status" in answer

    async def test_an_active_review_explains_the_arrangement(self):
        """The fact no `git` command reports, and the one that changes what
        every `git` command means."""
        bridge = McpBridge(review_state=lambda: {
            "active": True,
            "branch": "feature",
            "base_branch": "main",
            "merge_base": "abc1234",
            "changed_files": [{"path": "src/a.py", "status": "M"}],
        })
        answer = text_of(await bridge.review_state())
        assert "feature" in answer and "abc1234" in answer
        assert "M src/a.py" in answer
        assert "git diff --cached" in answer
        assert "merge-base" in answer

    async def test_a_bare_string_file_list_still_renders(self):
        bridge = McpBridge(review_state=lambda: {
            "active": True, "changed_files": ["src/a.py"]
        })
        assert "src/a.py" in text_of(await bridge.review_state())


# ---------------------------------------------------------------------------
# ui_state
# ---------------------------------------------------------------------------


class TestUiState:
    async def test_it_reports_the_viewer_and_the_mode(self):
        bridge = McpBridge(ui_state=lambda: {
            "viewer": {"path": "src/b.py", "start_line": 10, "end_line": 20},
            "permission_mode": "acceptEdits",
        })
        answer = text_of(await bridge.ui_state())
        assert "src/b.py" in answer
        assert "lines 10-20" in answer
        assert "acceptEdits" in answer

    async def test_it_does_not_report_a_picker_selection(self):
        """There is no selection to report — pointing at a file happens in
        the prompt now (``specs5/plan/decisions.md`` CC-21). A stray
        ``selected_files`` key from an older caller is ignored rather than
        rendered."""
        bridge = McpBridge(ui_state=lambda: {
            "selected_files": ["src/a.py"],
            "viewer": {"path": "src/b.py"},
        })
        answer = text_of(await bridge.ui_state())
        assert "src/a.py" not in answer
        assert "ticked" not in answer

    async def test_a_single_line_cursor_is_not_reported_as_a_range(self):
        bridge = McpBridge(ui_state=lambda: {
            "viewer": {"path": "src/b.py", "start_line": 7, "end_line": 7},
        })
        answer = text_of(await bridge.ui_state())
        assert "line 7" in answer
        assert "lines 7-7" not in answer

    async def test_an_empty_ui_says_nothing_is_open(self):
        """Rather than an empty answer, which reads as a broken tool."""
        answer = text_of(await McpBridge().ui_state())
        assert "Nothing is open" in answer

    async def test_it_points_at_review_state_rather_than_duplicating_it(self):
        bridge = McpBridge(ui_state=lambda: {"review_state": {"active": True}})
        assert "call review_state" in text_of(await bridge.ui_state())


# ---------------------------------------------------------------------------
# Against a real index
# ---------------------------------------------------------------------------


class TestAgainstARealIndex:
    """One end-to-end pass, because the fakes cannot catch a formatter
    contract change — and the formatters are where the map's legend, path
    aliases and line numbers actually come from."""

    @pytest.fixture
    def real(self, tmp_path):
        from aic_dc.symbol_index.index import SymbolIndex

        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "engine.py").write_text(
            "class Engine:\n"
            "    def start(self):\n"
            "        return 1\n"
        )
        (tmp_path / "pkg" / "caller.py").write_text(
            "from pkg.engine import Engine\n"
            "\n"
            "def go():\n"
            "    return Engine().start()\n"
        )
        index = SymbolIndex(tmp_path)
        index.index_repo(["pkg/engine.py", "pkg/caller.py"])
        return index

    async def test_the_map_carries_a_legend_and_the_symbols(self, real):
        bridge = McpBridge(
            symbol_index=lambda: real, symbol_index_ready=lambda: True
        )
        answer = text_of(await bridge.symbol_map())
        assert "c=class" in answer
        assert "Engine" in answer and "start" in answer

    async def test_file_symbols_carries_line_numbers(self, real):
        bridge = McpBridge(
            symbol_index=lambda: real, symbol_index_ready=lambda: True
        )
        answer = text_of(await bridge.file_symbols(paths=["pkg/engine.py"]))
        assert ":N=line(s)" in answer
        assert "Engine:1" in answer
        assert "caller.py" not in answer

    async def test_find_references_follows_a_real_import_edge(self, real):
        bridge = McpBridge(
            symbol_index=lambda: real, symbol_index_ready=lambda: True
        )
        answer = text_of(await bridge.find_references(symbol="Engine"))
        assert "pkg/engine.py:1" in answer
        assert "pkg/caller.py" in answer
