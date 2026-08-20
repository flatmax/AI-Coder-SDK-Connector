"""The ``ac-dc`` in-process MCP server — AC-DC's repo intelligence as tools.

Six tools, each answering a question Claude Code's built-ins answer badly
or not at all. The server runs *in* this process (an SDK MCP server, not a
subprocess), so it reads the same index instances the browser queries:
a symbol resolvable in Monaco is resolvable by the agent, and vice versa.

Three properties are load-bearing, and each is a spec invariant rather
than an implementation preference:

- **Read-only, all six.** Nothing here mutates the repository, the engine
  or the UI. That is what lets ``classify_tool`` treat ``ac-dc`` calls as
  ``read`` and leave them ungated; a tool that wrote would need the
  permission dialog and would be competing with ``Edit`` for the job.
- **Never stale.** An index-reading tool flushes any pending re-index
  before it answers (:mod:`ac_dc.claude_code.hooks`). An agent misled by
  our own map about code it just wrote is worse off than an agent with no
  map, because it will reason confidently from the wrong shape.
- **Never empty when it means "not ready".** An empty symbol map reads as
  "this repo has no symbols", which sends the agent down a path it will
  not revisit. While an index is building, the tools say so and say to
  retry.

The tool budget is the reason there are six and not sixteen: every
definition costs context on every turn, and shows up in the Context tab's
tool inventory with its price. The bar for a new one is not "is this
useful?" but "is this cheaper or better than the agent doing it with
Glob, Grep, Read and Bash?".

Governing spec: ``specs5/3-engine/mcp-bridge.md``.
Decision: ``specs5/plan/decisions.md`` § CC-6.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The server name the CLI prefixes onto every tool: `mcp__ac-dc__symbol_map`.
# `permissions.AC_DC_MCP_SERVER` must match. That string is what ungates
# these tools: `can_use_tool` allows `mcp__ac-dc__*` without a dialog, and a
# rename here without one there turns every `symbol_map` call into a
# third-party MCP permission prompt.
SERVER_NAME = "ac-dc"

# Per-response character budget for the two map tools. Chosen as a size a
# turn can absorb without the CLI spilling the result to disk: a 900-file
# map is not a single response, and chunking it ourselves keeps the legend
# with the files it decodes.
MAX_RESPONSE_CHARS = 24_000

# `file_symbols` is the follow-up to a `symbol_map` call, not a way to read
# the repo one call at a time. The cap is a guard against a 200-path
# argument that would return the whole map without the map's chunking.
MAX_FILE_SYMBOL_PATHS = 40

# `find_references` on a name like `get` or `run` can match hundreds of
# sites. Past this the list stops being an answer and starts being a
# haystack, so it truncates and says by how much.
MAX_REFERENCE_SITES = 200


# ---------------------------------------------------------------------------
# Result text
# ---------------------------------------------------------------------------

# Said instead of an empty map. "Retry shortly" is the actionable half:
# the build finishes on its own, and the agent has other work meanwhile.
NOT_READY = (
    "The {index} index is still building. Retry this call shortly — it "
    "finishes on its own. Until then, Glob/Grep/Read answer the same "
    "questions more expensively."
)

# Said when the index will never be ready in this session. Distinct from
# NOT_READY on purpose: an agent that retries a permanent failure wastes
# turns, so this one names the fallback and does not invite a retry.
UNAVAILABLE = (
    "The {index} index is not available in this session (it failed to "
    "build). Use Glob/Grep/Read instead; this tool will not start working "
    "on a retry."
)


def _text(body: str) -> dict[str, Any]:
    """One text block, the shape every handler returns."""
    return {"content": [{"type": "text", "text": body}]}


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class McpBridge:
    """State the six tools read, and the server that exposes them.

    Every source arrives as a callable rather than an object, for two
    reasons that both bite otherwise: the symbol index does not exist yet
    when the service is constructed (it is built in the background after
    the RPC server is already serving), and the review state changes under
    the tools between calls. A bound attribute would capture the wrong
    thing on the first call and keep it.

    Parameters
    ----------
    symbol_index:
        ``() -> SymbolIndex | None``. None means construction failed —
        reported as unavailable, not as "still building".
    symbol_index_ready:
        ``() -> bool``. False while the repo walk is in flight. The index
        object exists and answers throughout that window, which is exactly
        why the flag is separate: a half-built index answers with a
        half-built map and nothing in the answer says so.
    doc_index / doc_index_ready:
        The same pair for the document index.
    review_state:
        ``() -> dict`` — ``ReviewMode.state()``, active or not.
    ui_state:
        ``() -> dict`` — what the user is pointing at.
    flush:
        ``() -> Awaitable`` draining the pending re-index queue. Awaited
        by every index-reading tool before it answers.
    """

    def __init__(
        self,
        *,
        symbol_index: Callable[[], Any] | None = None,
        symbol_index_ready: Callable[[], bool] | None = None,
        doc_index: Callable[[], Any] | None = None,
        doc_index_ready: Callable[[], bool] | None = None,
        review_state: Callable[[], dict[str, Any]] | None = None,
        ui_state: Callable[[], dict[str, Any]] | None = None,
        flush: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._symbol_index = symbol_index or (lambda: None)
        self._symbol_index_ready = symbol_index_ready or (lambda: False)
        self._doc_index = doc_index or (lambda: None)
        self._doc_index_ready = doc_index_ready or (lambda: False)
        self._review_state = review_state or (lambda: {"active": False})
        self._ui_state = ui_state or dict
        self._flush = flush

    # ------------------------------------------------------------------
    # Index access
    # ------------------------------------------------------------------

    async def _symbols(self) -> tuple[Any | None, str | None]:
        """The symbol index, or the text explaining why there isn't one.

        The flush happens before the readiness check rather than after:
        a pending re-index is the thing that would make the answer stale,
        and a caller that bailed early on readiness would skip it.
        """
        await self.flush()
        index = self._symbol_index()
        if index is None:
            return None, UNAVAILABLE.format(index="symbol")
        if not self._symbol_index_ready():
            return None, NOT_READY.format(index="symbol")
        return index, None

    async def _docs(self) -> tuple[Any | None, str | None]:
        """The doc index, or the text explaining why there isn't one."""
        await self.flush()
        index = self._doc_index()
        if index is None:
            return None, UNAVAILABLE.format(index="document")
        if not self._doc_index_ready():
            return None, NOT_READY.format(index="document")
        return index, None

    async def flush(self) -> None:
        """Drain pending re-indexing so the next read includes the writes.

        Failures are logged and swallowed: a re-index that raised leaves
        the index as it was, which is stale rather than wrong, and
        refusing to answer at all would be a worse trade.
        """
        if self._flush is None:
            return
        try:
            await self._flush()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Pending re-index failed before an ac-dc tool read: %s", exc)

    # ------------------------------------------------------------------
    # Tools — symbols
    # ------------------------------------------------------------------

    async def symbol_map(
        self,
        path_prefix: str | None = None,
        language: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Whole-repo or subtree structural map, chunked."""
        index, refusal = await self._symbols()
        if index is None:
            return _text(refusal or "")

        indexed = index.get_indexed_files()
        files = _scope(indexed, path_prefix, language)
        if not files:
            return _text(_no_files_note(indexed, path_prefix, language))

        chunk, remaining = _pack(files, cursor, lambda p: index.get_file_symbol_block(p))
        if not chunk:
            return _text(
                f"No indexed files sort at or after cursor {cursor!r}. The map is "
                "complete; drop the cursor to start again."
            )
        # Excluded against the whole index, not against the scoped list:
        # the formatter renders everything it holds minus the exclusions,
        # so a set built from the scope alone would quietly re-admit every
        # file the scope was meant to leave out.
        body = index.get_symbol_map(exclude_files=set(indexed) - set(chunk))
        return _text(_with_continuation(body, chunk, remaining, "symbol_map"))

    async def file_symbols(self, paths: list[str] | None = None) -> dict[str, Any]:
        """The structural block for specific files, with line numbers."""
        index, refusal = await self._symbols()
        if index is None:
            return _text(refusal or "")

        wanted = [p for p in (paths or []) if isinstance(p, str) and p]
        if not wanted:
            return _text("No paths given. Pass `paths` as a list of repo-relative files.")
        if len(wanted) > MAX_FILE_SYMBOL_PATHS:
            return _text(
                f"{len(wanted)} paths is more than this tool answers in one call "
                f"(limit {MAX_FILE_SYMBOL_PATHS}). For a whole subtree use "
                "symbol_map with a path_prefix, which chunks and aliases paths."
            )

        resolved = {p: index.resolve_indexed_path(p) for p in wanted}
        hits = {rel for rel in resolved.values() if rel is not None}
        misses = [p for p, rel in resolved.items() if rel is None]

        parts: list[str] = []
        if hits:
            # The LSP variant, not the context one: `:N` line numbers are
            # what make this cheaper than Read — the agent can jump to a
            # range instead of reading the file to find one.
            parts.append(
                index.get_lsp_symbol_map(
                    exclude_files=set(index.get_indexed_files()) - hits
                )
            )
        if misses:
            parts.append(
                "Not in the symbol index (unsupported language, ignored by git, "
                "or no such file): " + ", ".join(misses)
            )
        return _text("\n\n".join(part for part in parts if part))

    async def find_references(self, symbol: str | None = None) -> dict[str, Any]:
        """Definition sites, reference sites, and importing files for a name."""
        index, refusal = await self._symbols()
        if index is None:
            return _text(refusal or "")

        name = (symbol or "").strip()
        if not name:
            return _text("No symbol given. Pass `symbol` as the name to look up.")

        definitions = index.find_definitions(name)
        sites = index.find_reference_sites(name)
        importers: list[str] = []
        for definition in definitions:
            for path in index.files_importing(str(definition["file"])):
                if path not in importers:
                    importers.append(path)

        if not definitions and not sites:
            return _text(
                f"No definition or resolved reference to {name!r} in the symbol "
                "index. It may be defined in a language the index does not parse, "
                "or it may be an attribute rather than a named symbol — Grep is "
                "the fallback for both."
            )

        lines = [f"References to {name!r}", ""]
        lines.append("Defined:")
        if definitions:
            for definition in definitions:
                where = f"  {definition['file']}:{definition['line']} ({definition['kind']}"
                container = definition.get("container")
                where += f" in {container})" if container else ")"
                lines.append(where)
        else:
            lines.append("  (no definition in the index — referenced but not defined here)")

        lines.append("")
        shown = sites[:MAX_REFERENCE_SITES]
        lines.append(f"Referenced from {len(sites)} resolved call site(s):")
        for ref_file, ref_line in shown:
            lines.append(f"  {ref_file}:{ref_line}")
        if len(sites) > len(shown):
            lines.append(f"  … {len(sites) - len(shown)} more, not shown")

        if importers:
            lines.append("")
            lines.append("Files importing a file that defines it:")
            lines.extend(f"  {path}" for path in importers)

        lines.append("")
        lines.append(
            "Resolved edges only: aliased imports are followed, and a name that "
            "merely appears as text is not counted. Grep is the wider, noisier net."
        )
        return _text("\n".join(lines))

    # ------------------------------------------------------------------
    # Tools — documents
    # ------------------------------------------------------------------

    async def doc_outline(
        self,
        path_prefix: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Document structure for markdown and SVG, chunked."""
        index, refusal = await self._docs()
        if index is None:
            return _text(refusal or "")

        indexed = index.get_indexed_files()
        files = _scope(indexed, path_prefix, None)
        if not files:
            return _text(_no_files_note(indexed, path_prefix, None))

        chunk, remaining = _pack(files, cursor, lambda p: index.get_file_doc_block(p))
        if not chunk:
            return _text(
                f"No indexed documents sort at or after cursor {cursor!r}. The "
                "outline is complete; drop the cursor to start again."
            )
        # Against the whole index, for the reason in `symbol_map`.
        body = index.get_doc_map(exclude_files=set(indexed) - set(chunk))
        return _text(_with_continuation(body, chunk, remaining, "doc_outline"))

    # ------------------------------------------------------------------
    # Tools — session facts
    # ------------------------------------------------------------------

    async def review_state(self) -> dict[str, Any]:
        """What is being reviewed, or an explicit not-in-review answer."""
        state = self._review_state() or {}
        if not state.get("active"):
            return _text(
                "No code review is active. The working tree means what `git "
                "status` says it means."
            )

        lines = ["A code review is active in AC-DC.", ""]
        for key in ("branch", "base_branch", "merge_base"):
            value = state.get(key)
            if value:
                lines.append(f"{key.replace('_', ' ')}: {value}")

        changed = state.get("changed_files") or []
        lines.append("")
        lines.append(f"{len(changed)} changed file(s):")
        for entry in changed:
            if isinstance(entry, dict):
                lines.append(f"  {entry.get('status', '?')} {entry.get('path', '?')}")
            else:
                lines.append(f"  {entry}")

        lines.append("")
        # The one fact no `git` command reports, and the one that changes
        # what every `git` command means while a review is on.
        lines.append(
            "How the repository is arranged: the working tree holds the branch "
            "tip, HEAD is at the merge-base, and every change is staged. So "
            "`git status` shows the branch's whole change set as staged work, "
            "not as uncommitted edits. `git diff --cached` is the review diff; "
            "`git show HEAD` is the pre-change state."
        )
        return _text("\n".join(lines))

    async def ui_state(self) -> dict[str, Any]:
        """What the user is looking at in the browser."""
        state = self._ui_state() or {}
        lines: list[str] = []

        viewer = state.get("viewer")
        if isinstance(viewer, dict) and viewer.get("path"):
            where = f"  {viewer['path']}"
            start = viewer.get("start_line")
            end = viewer.get("end_line")
            if start and end and end != start:
                where += f" (lines {start}-{end} selected)"
            elif start:
                where += f" (line {start})"
            lines.append("Open in the user's viewer pane:")
            lines.append(where)
        else:
            lines.append("Nothing is open in the user's viewer pane, or no browser has")
            lines.append("reported one this session.")

        review = state.get("review_state") or {}
        if review.get("active"):
            lines.append("")
            lines.append(
                "A code review is active — call review_state for the arrangement."
            )

        mode = state.get("permission_mode")
        if mode:
            lines.append("")
            lines.append(f"Permission mode: {mode}")

        return _text("\n".join(lines))

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    def build_tools(self) -> list[Any]:
        """The six ``SdkMcpTool`` definitions, before they are wrapped.

        Separate from :meth:`build_server` because ``create_sdk_mcp_server``
        folds them into an MCP ``Server`` object that does not hand them
        back: after wrapping, the descriptions, schemas and annotations are
        no longer reachable, and those are exactly what wants checking.

        The handlers are closures over ``self`` rather than the methods
        themselves because the SDK's ``@tool`` decorator hands the handler
        one positional dict, and unpacking the arguments here keeps each
        method's signature readable and directly testable.

        Every tool carries ``readOnlyHint``, which is the invariant "every
        bridge tool is read-only" stated to the CLI rather than only to us.
        """
        from claude_agent_sdk import tool
        from mcp.types import ToolAnnotations

        read_only = ToolAnnotations(readOnlyHint=True)

        @tool(
            "symbol_map",
            "Structural map of the repository's code: per file, its classes, "
            "functions, methods and imports, in a compact format with a legend. "
            "One call answers 'what is the shape of this codebase?' — far "
            "cheaper than a directory walk plus dozens of Reads. Optionally "
            "scope to a subtree with path_prefix or to one language. Large maps "
            "come back in chunks with a cursor for the next call.",
            {
                "type": "object",
                "properties": {
                    "path_prefix": {
                        "type": "string",
                        "description": "Repo-relative directory or path prefix to "
                        "scope the map to, e.g. 'src/ac_dc/claude_code'.",
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
            read_only,
        )
        async def symbol_map(args: dict[str, Any]) -> dict[str, Any]:
            return await self.symbol_map(
                path_prefix=args.get("path_prefix"),
                language=args.get("language"),
                cursor=args.get("cursor"),
            )

        @tool(
            "file_symbols",
            "The structural block for specific files: symbols with line "
            "numbers, imports, and incoming reference counts. Use it to orient "
            "in a large file without reading it, and as the follow-up to a "
            "symbol_map call.",
            {
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
            read_only,
        )
        async def file_symbols(args: dict[str, Any]) -> dict[str, Any]:
            return await self.file_symbols(paths=args.get("paths"))

        @tool(
            "find_references",
            "Where a symbol is used, from the reference graph: definition "
            "sites, resolved call sites, and the files importing them. Unlike "
            "Grep this follows aliased imports and does not match the name in "
            "prose or in an unrelated scope — it answers 'what breaks if I "
            "change this?'.",
            {
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
            read_only,
        )
        async def find_references(args: dict[str, Any]) -> dict[str, Any]:
            return await self.find_references(symbol=args.get("symbol"))

        @tool(
            "doc_outline",
            "Document structure for markdown and SVG: headings with line "
            "numbers, extracted keywords, content-type markers and "
            "cross-references — and for SVG, the containment hierarchy with box "
            "labels. There is no built-in equivalent: Read on an SVG returns "
            "coordinate soup, this returns the labelled nesting.",
            {
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
            read_only,
        )
        async def doc_outline(args: dict[str, Any]) -> dict[str, Any]:
            return await self.doc_outline(
                path_prefix=args.get("path_prefix"),
                cursor=args.get("cursor"),
            )

        @tool(
            "review_state",
            "The active code review's facts: reviewed branch, base branch, "
            "merge-base, and changed files with status — plus how AC-DC has "
            "arranged the repository, which changes what `git status` means. "
            "Returns an explicit not-in-review answer when no review is on.",
            {"type": "object", "properties": {}, "required": []},
            read_only,
        )
        async def review_state(args: dict[str, Any]) -> dict[str, Any]:
            return await self.review_state()

        @tool(
            "ui_state",
            "What the user is looking at right now: files ticked in the picker, "
            "the file open in the viewer pane and the selected line range. "
            "Browser state, so no built-in tool can answer it. The turn's "
            "opening framing carries a snapshot of this; call the tool to "
            "re-read it after a long turn.",
            {"type": "object", "properties": {}, "required": []},
            read_only,
        )
        async def ui_state(args: dict[str, Any]) -> dict[str, Any]:
            return await self.ui_state()

        return [
            symbol_map,
            file_symbols,
            find_references,
            doc_outline,
            review_state,
            ui_state,
        ]

    def build_server(self) -> Any:
        """The ``McpSdkServerConfig`` for ``ClaudeAgentOptions.mcp_servers``.

        In-process: no subprocess, no second copy of the index. The tools
        close over the same objects the browser's LSP calls read, which is
        what makes "resolvable in Monaco" and "resolvable by the agent"
        the same statement.
        """
        from claude_agent_sdk import create_sdk_mcp_server

        return create_sdk_mcp_server(name=SERVER_NAME, tools=self.build_tools())


# ---------------------------------------------------------------------------
# Scoping, packing, continuation
# ---------------------------------------------------------------------------


def _scope(
    files: Iterable[str],
    path_prefix: str | None,
    language: str | None,
) -> list[str]:
    """Filter an indexed-file list by path prefix and language.

    The prefix matches a directory or a path fragment from the left, so
    both ``src/ac_dc`` and ``src/ac_dc/claude_code/mcp`` do what a reader
    expects. Sorted, because the cursor is a path and a chunk boundary
    only means something over a stable order.
    """
    scoped = sorted(files)
    if path_prefix:
        prefix = path_prefix.strip("/")
        if prefix:
            scoped = [p for p in scoped if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix)]
    if language:
        from ac_dc.symbol_index.parser import language_for_file

        wanted = language.strip().lower()
        scoped = [p for p in scoped if (language_for_file(p) or "") == wanted]
    return scoped


def _no_files_note(
    all_files: Iterable[str],
    path_prefix: str | None,
    language: str | None,
) -> str:
    """Explain an empty scope without implying the index is empty.

    The distinction the agent needs: "your filter matched nothing" and
    "there is nothing indexed" lead to different next moves.
    """
    total = len(list(all_files))
    if total == 0:
        return (
            "The index holds no files. Either the repository has nothing this "
            "index understands, or every file is ignored — Glob will say which."
        )
    scope = []
    if path_prefix:
        scope.append(f"path_prefix={path_prefix!r}")
    if language:
        scope.append(f"language={language!r}")
    filters = " and ".join(scope) if scope else "the given filters"
    return (
        f"No indexed files match {filters}, though {total} file(s) are indexed "
        "overall. Check the prefix against the repo layout, or call again "
        "without it."
    )


def _pack(
    files: list[str],
    cursor: str | None,
    block_for: Callable[[str], str | None],
) -> tuple[list[str], list[str]]:
    """Choose the files for this chunk, and report what is left.

    Per-file block lengths are the size estimate. Rendering each block
    once to measure it and then rendering the chosen set together is one
    extra pass over already-parsed symbols — cheap next to the
    alternative, which is rendering the whole map repeatedly until it
    fits.

    The cursor is a **path**, not an offset: files come and go between
    calls, and an offset into a list that shifted underneath silently
    skips or repeats a file. A path resumes at the first entry that sorts
    at or after it, which degrades to "close enough" rather than "wrong".

    At least one file always goes into a chunk, even when its own block
    exceeds the budget. A tool that returns nothing because the first
    file is too big has no way to make progress.
    """
    start = 0
    if cursor:
        start = next((i for i, path in enumerate(files) if path >= cursor), len(files))

    chunk: list[str] = []
    budget = MAX_RESPONSE_CHARS
    for path in files[start:]:
        block = block_for(path) or ""
        if chunk and len(block) > budget:
            break
        budget -= len(block)
        chunk.append(path)
    return chunk, files[start + len(chunk):]


def _with_continuation(
    body: str,
    chunk: list[str],
    remaining: list[str],
    tool_name: str,
) -> str:
    """Append the chunk's own accounting to the rendered map.

    Stated in the response rather than left to the agent to infer from a
    truncated-looking map: an agent that does not know a map was chunked
    will conclude the missing files do not exist.
    """
    if not remaining:
        return body
    return (
        f"{body}\n"
        f"[chunk: {len(chunk)} file(s); {len(remaining)} remaining. "
        f'Call {tool_name} again with cursor="{remaining[0]}" for the next '
        "chunk. Each chunk carries its own legend and path aliases.]\n"
    )
