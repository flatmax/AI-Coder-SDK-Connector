"""Tests for aic_dc.index_tools — the one description of the six tools.

The module exists because the names, descriptions and schemas lived inside
the Claude adapter's ``@tool`` calls, where the other two engines could not
reach them: `McpBridge` was built transport-neutral by
[AG-4](../specs5/plan-ag/decisions.md#ag-4) and only ever had one caller
(AG-34). So the assertions here are about **sameness**, not about the
sentences.

Two are load-bearing.

**The words did not change when they moved.**
``TestTheTextIsWhatClaudeAlreadySent`` pins every description and every
schema as a **literal** rather than comparing against
:data:`aic_dc.index_tools.SPECS`, so a refactor that quietly reworded a
tool description fails here. Written from the shipped ``@tool`` calls,
measured 2026-09-15. These strings are what makes a model reach for
``symbol_map`` instead of a directory walk, so they are product text and a
reword is a decision.

**Claude's own packaging still says exactly this.**
``test_the_claude_tools_are_built_from_these_specs`` reads the assembled
``SdkMcpTool`` objects back and compares them to the specs. It is the
assertion that the extraction was behaviour-preserving on the engine that
already had the feature — and it was run against the *unrewired*
``build_tools`` first, where it checks the transcription rather than the
wiring.
"""

from __future__ import annotations

import pytest

from aic_dc import index_tools

# ---------------------------------------------------------------------------
# The literals
# ---------------------------------------------------------------------------

SYMBOL_MAP_DESCRIPTION = (
    "Structural map of the repository's code: per file, its classes, "
    "functions, methods and imports, in a compact format with a legend. "
    "One call answers 'what is the shape of this codebase?' — far "
    "cheaper than a directory walk plus dozens of Reads. Optionally "
    "scope to a subtree with path_prefix or to one language. Large maps "
    "come back in chunks with a cursor for the next call."
)

FILE_SYMBOLS_DESCRIPTION = (
    "The structural block for specific files: symbols with line "
    "numbers, imports, and incoming reference counts. Use it to orient "
    "in a large file without reading it, and as the follow-up to a "
    "symbol_map call."
)

FIND_REFERENCES_DESCRIPTION = (
    "Where a symbol is used, from the reference graph: definition "
    "sites, resolved call sites, and the files importing them. Unlike "
    "Grep this follows aliased imports and does not match the name in "
    "prose or in an unrelated scope — it answers 'what breaks if I "
    "change this?'."
)

DOC_OUTLINE_DESCRIPTION = (
    "Document structure for markdown and SVG: headings with line "
    "numbers, extracted keywords, content-type markers and "
    "cross-references — and for SVG, the containment hierarchy with box "
    "labels. There is no built-in equivalent: Read on an SVG returns "
    "coordinate soup, this returns the labelled nesting."
)

REVIEW_STATE_DESCRIPTION = (
    "The active code review's facts: reviewed branch, base branch, "
    "merge-base, and changed files with status — plus how AIC-DC has "
    "arranged the repository, which changes what `git status` means. "
    "Returns an explicit not-in-review answer when no review is on."
)

UI_STATE_DESCRIPTION = (
    "What the user is looking at right now: files ticked in the picker, "
    "the file open in the viewer pane and the selected line range. "
    "Browser state, so no built-in tool can answer it. The turn's "
    "opening framing carries a snapshot of this; call the tool to "
    "re-read it after a long turn."
)

DESCRIPTIONS = {
    "symbol_map": SYMBOL_MAP_DESCRIPTION,
    "file_symbols": FILE_SYMBOLS_DESCRIPTION,
    "find_references": FIND_REFERENCES_DESCRIPTION,
    "doc_outline": DOC_OUTLINE_DESCRIPTION,
    "review_state": REVIEW_STATE_DESCRIPTION,
    "ui_state": UI_STATE_DESCRIPTION,
}

SCHEMAS = {
    "symbol_map": {
        "type": "object",
        "properties": {
            "path_prefix": {
                "type": "string",
                "description": "Repo-relative directory or path prefix to "
                "scope the map to, e.g. 'src/aic_dc/claude_code'.",
            },
            "language": {
                "type": "string",
                "description": "Restrict to one language: python, "
                "javascript, typescript, c, cpp, matlab.",
            },
            "cursor": {
                "type": "string",
                "description": "Continuation token from a previous "
                "chunked response.",
            },
        },
        "required": [],
    },
    "file_symbols": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Repo-relative file paths.",
            },
        },
        "required": ["paths"],
    },
    "find_references": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "The symbol name: a class, function, "
                "method or variable.",
            },
        },
        "required": ["symbol"],
    },
    "doc_outline": {
        "type": "object",
        "properties": {
            "path_prefix": {
                "type": "string",
                "description": "Repo-relative directory or path prefix "
                "to scope the outline to.",
            },
            "cursor": {
                "type": "string",
                "description": "Continuation token from a previous "
                "chunked response.",
            },
        },
        "required": [],
    },
    "review_state": {"type": "object", "properties": {}, "required": []},
    "ui_state": {"type": "object", "properties": {}, "required": []},
}


class TestTheTextIsWhatClaudeAlreadySent:
    """The refactor's own guard. Literals, so it survives the move."""

    def test_the_six_are_these_six_in_this_order(self):
        """Order is part of it: it is the order the model sees them in."""
        assert [spec.name for spec in index_tools.SPECS] == [
            "symbol_map",
            "file_symbols",
            "find_references",
            "doc_outline",
            "review_state",
            "ui_state",
        ]

    @pytest.mark.parametrize("name", sorted(DESCRIPTIONS))
    def test_the_description_is_byte_for_byte_what_claude_sent(self, name):
        """Pinned as a literal, on purpose.

        Comparing against ``index_tools.SPECS`` would pass for free. What
        has to keep working is the *prose the model reads* — the sentence
        arguing that ``symbol_map`` is cheaper than a directory walk is
        why the tool gets called at all, and a shared renderer makes a
        reword three times as expensive to notice as it used to be.
        """
        assert index_tools.by_name(name).description == DESCRIPTIONS[name]

    @pytest.mark.parametrize("name", sorted(SCHEMAS))
    def test_the_schema_is_byte_for_byte_what_claude_sent(self, name):
        assert index_tools.by_name(name).schema == SCHEMAS[name]

    def test_the_claude_tools_are_built_from_these_specs(self):
        """The engine that already had the feature still says exactly this.

        Run against the *unrewired* ``build_tools`` before the extraction
        landed, where it asserts the transcription rather than the wiring;
        it keeps working afterwards, where it asserts both.
        """
        from aic_dc.claude_code.mcp_server import McpBridge

        built = {tool.name: tool for tool in McpBridge().build_tools()}
        assert set(built) == index_tools.TOOL_NAMES
        for spec in index_tools.SPECS:
            assert built[spec.name].description == spec.description, spec.name
            assert built[spec.name].input_schema == spec.schema, spec.name

    def test_every_claude_tool_is_still_declared_read_only(self):
        """``readOnlyHint`` is the "all six are read-only" invariant stated
        to the CLI rather than only to us, and it is what lets the
        permission narrowing be a narrowing rather than a hole."""
        from aic_dc.claude_code.mcp_server import McpBridge

        for tool in McpBridge().build_tools():
            assert tool.annotations.readOnlyHint is True, tool.name


class TestTheServerNameIsSpelledOnce:
    """``aic-dc`` is what ungates these tools, so it has one definition."""

    def test_the_claude_adapter_aliases_it(self):
        from aic_dc.claude_code import mcp_server, permissions

        assert mcp_server.SERVER_NAME == index_tools.SERVER_NAME
        assert permissions.AIC_DC_MCP_SERVER == index_tools.SERVER_NAME

    def test_the_name_ungates_a_call_on_the_claude_engine(self):
        """The pair that has to agree, asserted as behaviour.

        ``classify_tool`` reading this string is the whole reason a
        ``symbol_map`` call is not a third-party MCP permission prompt.
        """
        from aic_dc.claude_code.permissions import classify_tool

        assert classify_tool(f"mcp__{index_tools.SERVER_NAME}__symbol_map") == "read"
        assert classify_tool("mcp__somebody-else__symbol_map") == "mcp"


class TestTheArgumentLift:
    """``parameters`` — what each transport unpacks out of the model's dict."""

    def test_absent_keys_arrive_as_none_not_omitted(self):
        """What the ``@tool`` closures did with ``args.get(...)``.

        Omitting them instead would hand the bridge its own signature
        defaults, which happen to agree today and are a second place for
        the same fact to live.
        """
        spec = index_tools.by_name("symbol_map")
        assert spec.arguments({}) == {
            "path_prefix": None,
            "language": None,
            "cursor": None,
        }
        assert spec.arguments(None) == spec.arguments({})

    def test_a_stated_argument_is_passed_through(self):
        spec = index_tools.by_name("symbol_map")
        assert spec.arguments({"language": "python"})["language"] == "python"

    def test_an_argument_nobody_declared_is_dropped(self):
        """The schema names what the model may send; anything else would
        reach the bridge as an unexpected keyword and raise."""
        spec = index_tools.by_name("review_state")
        assert spec.arguments({"surprise": 1}) == {}

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("symbol_map", ("path_prefix", "language", "cursor")),
            ("file_symbols", ("paths",)),
            ("find_references", ("symbol",)),
            ("doc_outline", ("path_prefix", "cursor")),
            ("review_state", ()),
            ("ui_state", ()),
        ],
    )
    def test_the_parameters_match_the_schema(self, name, expected):
        """Two records of one fact, so they are checked against each other.

        A parameter the schema does not declare can never arrive; a schema
        property no parameter lifts is silently ignored. Both are the kind
        of drift that shows up as a tool that "does not work" on one
        engine.
        """
        spec = index_tools.by_name(name)
        assert spec.parameters == expected
        assert set(spec.parameters) == set(spec.schema["properties"])

    def test_every_parameter_is_a_bridge_method_argument(self):
        """The lift has to match the method it feeds, on every spec."""
        import inspect

        from aic_dc.claude_code.mcp_server import McpBridge

        for spec in index_tools.SPECS:
            signature = inspect.signature(getattr(McpBridge(), spec.name))
            assert set(spec.parameters) == set(signature.parameters), spec.name


class TestTheTwoResultShapes:
    """``invoke`` keeps the MCP envelope; ``invoke_text`` unwraps it."""

    @pytest.fixture
    def bridge(self):
        class FakeBridge:
            async def review_state(self):
                return {"content": [{"type": "text", "text": "not in review"}]}

            async def symbol_map(self, path_prefix=None, language=None, cursor=None):
                return {
                    "content": [{"type": "text", "text": f"map of {path_prefix}"}]
                }

        return FakeBridge()

    @pytest.mark.asyncio
    async def test_invoke_returns_what_the_bridge_returned(self, bridge):
        spec = index_tools.by_name("review_state")
        assert await spec.invoke(bridge) == {
            "content": [{"type": "text", "text": "not in review"}]
        }

    @pytest.mark.asyncio
    async def test_invoke_text_is_the_sentence(self, bridge):
        """The two transports that are not speaking MCP need this.

        ``ToolResult.result`` is any JSON-serialisable value, so handing it
        the envelope shows the model a nested content array where the
        Claude engine shows it a sentence.
        """
        spec = index_tools.by_name("review_state")
        assert await spec.invoke_text(bridge) == "not in review"

    @pytest.mark.asyncio
    async def test_the_arguments_reach_the_bridge(self, bridge):
        spec = index_tools.by_name("symbol_map")
        assert await spec.invoke_text(bridge, {"path_prefix": "src"}) == "map of src"

    def test_several_blocks_join_with_newlines(self):
        assert (
            index_tools.text_of(
                {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
            )
            == "a\nb"
        )

    @pytest.mark.parametrize(
        "result",
        [None, {}, "text", [], {"content": None}, {"content": "text"}, {"content": [{}]}],
    )
    def test_a_shape_it_does_not_recognise_is_empty_not_a_raise(self, result):
        """The caller is a tool handler mid-turn.

        An exception here reaches the model as a failed tool call, where a
        bridge that answered unusually should at worst produce an empty
        one.
        """
        assert index_tools.text_of(result) == ""
