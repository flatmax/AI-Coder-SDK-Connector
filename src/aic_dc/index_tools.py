"""The ``aic-dc`` tool specs — one description of AIC⚡DC's repo intelligence.

Six read-only tools answer questions an agent's built-ins answer badly or
not at all: the repository's structural map, a file's symbol block, a
name's reference graph, a document's outline, the active review's facts,
and what the user is looking at. This module holds their **names,
descriptions and schemas**; :class:`aic_dc.claude_code.mcp_server.McpBridge`
holds what they do.

Why this is a module of its own
==============================
The text used to live inside ``claude_code/mcp_server.py``'s ``@tool``
calls, which is Claude-SDK packaging with the product's own prose embedded
in it — so the tools reached exactly one engine of three
([AG-34](../../specs5/plan-ag/decisions.md#ag-34)).
[AG-4](../../specs5/plan-ag/decisions.md#ag-4) had already decided that
they should reach all of them, and said why it was cheap: the bridge takes
provider *callables* rather than index objects, so "only the packaging is
per-engine". That was true and stayed unused for eleven days.

Three packagings, then, and one spec:

- Claude Code — ``@tool``-decorated ``SdkMcpTool`` objects folded into an
  in-process SDK MCP server (:meth:`McpBridge.build_tools`).
- Antigravity by API key — ``ToolWithSchema`` callables on
  ``AgentConfig.tools``, which is AG-4's route and takes an **explicit**
  schema rather than deriving one from a signature
  (``tools/tool_runner.py:143``). That branch is what lets the description
  below be the same string on this transport instead of a paraphrase.
- Antigravity by subscription — a FastMCP registration on the authenticated
  loopback listener [AG-22](../../specs5/plan-ag/decisions.md#ag-22) built
  for the opposite direction, because a CLI subprocess cannot be handed a
  Python callable.

A sentence is a feature too
===========================
The descriptions are load-bearing prose, not labels. ``symbol_map``'s
argues its own cost — *"far cheaper than a directory walk plus dozens of
Reads"* — and that sentence is what gets the tool called instead of a Glob
sweep. Three transports rendering it from three sources is
[AG-9](../../specs5/plan-ag/decisions.md#ag-9)'s prohibition against paying
for one feature twice, and the symptom is the one
[AG-33](../../specs5/plan-ag/decisions.md#ag-33) names: a model asked the
same question is told about the same tool in different words depending on
which engine the user picked in Settings.

So :data:`SPECS` is the single source, and ``tests/test_index_tools.py``
pins every description and schema as a **literal** rather than comparing
against this module — a reword has to be deliberate rather than noticed
three times or not at all.

The one description that was wrong when it moved
===============================================
``ui_state`` opened *"What the user is looking at right now: files ticked
in the picker, …"* — and there is no ticking. The checkbox was removed
under ``specs5/plan/decisions.md`` CC-21, which decided that pointing at a
file is something the user does *in the prompt* where the agent already
sees it; ``files-tab/exclusion.js`` says so at the line where the last of
its state outlived it. So the sentence did not describe a snapshot field
that had been forgotten. It described a control the user cannot operate.

Moved **verbatim** on 2026-09-15 all the same, because that change's whole
guarantee was that the prose did not change when it moved, and reworded on
2026-09-16 once it was separable — dropping the clause, and naming the two
keys the answer really carries and the old text never mentioned
(``review_state``, ``permission_mode``). A tool description is product
text read by three engines, so it is a decision rather than a tidy-up,
which is why it took its own commit:
[AG-R-34](../../specs5/plan-ag/risks.md#ag-r-34).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The server name every transport files these tools under, and the one
#: spelling of it.
#:
#: Aliased by ``claude_code.mcp_server.SERVER_NAME`` and by
#: ``claude_code.permissions.AIC_DC_MCP_SERVER``, which is what ungates them
#: on that engine: ``classify_tool`` reads this string to decide that an MCP
#: call is a ``read``, and ``can_use_tool`` then allows it with no dialog. A
#: rename in one place and not the other turns every ``symbol_map`` call
#: into a third-party permission prompt — which is why the name is defined
#: once here rather than spelled in each transport that needs it.
SERVER_NAME = "aic-dc"


@dataclass(frozen=True)
class ToolSpec:
    """One tool as every transport needs to describe it.

    ``parameters`` names the arguments to lift out of the model's argument
    dict and pass to the bridge method of the same name as :attr:`name`.
    Held as data rather than as a closure per tool because each transport
    wraps the call differently — one wants a dict-taking coroutine, one a
    keyword-taking callable, one a FastMCP handler — and the unpacking is
    the only part all three share.
    """

    name: str
    description: str
    schema: dict[str, Any]
    parameters: tuple[str, ...] = ()

    def arguments(self, args: Mapping[str, Any] | None) -> dict[str, Any]:
        """The keyword arguments for the bridge, from the model's dict.

        Absent keys are passed as ``None`` rather than omitted, which
        matches what the ``@tool`` closures did with ``args.get(...)`` and
        keeps the bridge's own defaults from having to agree with a schema
        they cannot see.
        """
        source = args or {}
        return {name: source.get(name) for name in self.parameters}

    async def invoke(self, bridge: Any, args: Mapping[str, Any] | None = None) -> Any:
        """Call the bridge method this spec describes, with the MCP envelope.

        The bridge returns ``{"content": [{"type": "text", …}]}``, which is
        what the Claude SDK path passes straight through.
        """
        return await getattr(bridge, self.name)(**self.arguments(args))

    async def invoke_text(self, bridge: Any, args: Mapping[str, Any] | None = None) -> str:
        """The same call, as plain text.

        For the two transports that are not speaking MCP envelopes: the SDK
        transport's ``ToolResult.result`` is *any JSON-serialisable value*
        (``types.py:718``), so handing it the envelope would show the model
        a nested content array where the Claude engine shows it a sentence.
        """
        return text_of(await self.invoke(bridge, args))


def text_of(result: Any) -> str:
    """The text body of a bridge result, or ``""``.

    Tolerant of a shape it does not recognise rather than raising: the
    caller is a tool handler mid-turn, and an exception here would surface
    to the model as a failed tool call where a bridge that answered
    unusually should at worst produce an empty one.
    """
    if not isinstance(result, Mapping):
        return ""
    blocks = result.get("content")
    if not isinstance(blocks, (list, tuple)):
        return ""
    parts = [
        block["text"]
        for block in blocks
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="symbol_map",
        description=(
            "Structural map of the repository's code: per file, its classes, "
            "functions, methods and imports, in a compact format with a legend. "
            "One call answers 'what is the shape of this codebase?' — far "
            "cheaper than a directory walk plus dozens of Reads. Optionally "
            "scope to a subtree with path_prefix or to one language. Large maps "
            "come back in chunks with a cursor for the next call."
        ),
        schema={
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
        parameters=("path_prefix", "language", "cursor"),
    ),
    ToolSpec(
        name="file_symbols",
        description=(
            "The structural block for specific files: symbols with line "
            "numbers, imports, and incoming reference counts. Use it to orient "
            "in a large file without reading it, and as the follow-up to a "
            "symbol_map call."
        ),
        schema={
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
        parameters=("paths",),
    ),
    ToolSpec(
        name="find_references",
        description=(
            "Where a symbol is used, from the reference graph: definition "
            "sites, resolved call sites, and the files importing them. Unlike "
            "Grep this follows aliased imports and does not match the name in "
            "prose or in an unrelated scope — it answers 'what breaks if I "
            "change this?'."
        ),
        schema={
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
        parameters=("symbol",),
    ),
    ToolSpec(
        name="doc_outline",
        description=(
            "Document structure for markdown and SVG: headings with line "
            "numbers, extracted keywords, content-type markers and "
            "cross-references — and for SVG, the containment hierarchy with box "
            "labels. There is no built-in equivalent: Read on an SVG returns "
            "coordinate soup, this returns the labelled nesting."
        ),
        schema={
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
        parameters=("path_prefix", "cursor"),
    ),
    ToolSpec(
        name="review_state",
        description=(
            "The active code review's facts: reviewed branch, base branch, "
            "merge-base, and changed files with status — plus how AIC-DC has "
            "arranged the repository, which changes what `git status` means. "
            "Returns an explicit not-in-review answer when no review is on."
        ),
        schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="ui_state",
        description=(
            "What the user is looking at right now: the file open in the "
            "viewer pane and the selected line range, whether a review is "
            "in progress, and the permission mode this session runs under. "
            "Browser state, so no built-in tool can answer it. The turn's "
            "opening framing carries a snapshot of this; call the tool to "
            "re-read it after a long turn."
        ),
        schema={"type": "object", "properties": {}, "required": []},
    ),
)

#: The six names, for the permission narrowings that have to recognise them.
TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in SPECS)


def by_name(name: str) -> ToolSpec | None:
    """One spec, or ``None`` for a name this module does not describe."""
    for spec in SPECS:
        if spec.name == name:
            return spec
    return None
