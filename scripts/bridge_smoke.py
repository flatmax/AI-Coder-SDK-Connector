"""MCP bridge smoke test — can Claude Code actually call our tools?

The conversion plan's phase-4 exit criterion: *Claude Code can call
``symbol_map`` / ``doc_outline``* (``specs5/plan/README.md``). Unit tests
pin the bridge's answers against fake and real indexes; only a live CLI
can tell us the *server* registered, the tool names are spelled the way
the model sees them, and the schemas are ones it will actually fill in.
So this lives in ``scripts/`` beside ``engine_smoke.py``, for the same
reason: it costs tokens and needs a login.

What it exercises, end to end:

1. A real :class:`~ac_dc.symbol_index.index.SymbolIndex` over the repo,
   built the way ``main.py`` builds it (resolver seeding, call-site
   resolution, reference graph) — because a bridge over a differently
   built index would prove nothing about the running app.
2. :class:`~ac_dc.claude_code.mcp_server.McpBridge`, registered as an
   in-process SDK server, so the tools share those index objects rather
   than a second copy.
3. The real :class:`~ac_dc.claude_code.permissions.PermissionBroker`, so
   the run is gated the way the app gates it. Our own tools must reach
   the model *without* a dialog — "displayed, not gated"
   (``specs5/3-engine/permissions.md``) — and a dialog for one is
   reported as a failure below.
4. The ``PostToolUse`` re-index hook, if ``--write`` is passed.
5. The turn itself: which ``mcp__ac-dc__*`` tools the model chose, what
   they returned, and whether it answered from them.

Usage::

    python scripts/bridge_smoke.py
    python scripts/bridge_smoke.py --tool doc_outline
    python scripts/bridge_smoke.py --write            # exercises the hook
    python scripts/bridge_smoke.py "which files import Reindexer?"

Options:

``--tool NAME``
    Ask for one specific tool by name instead of the default prompt,
    which asks for the symbol map. Use this to check a tool the model
    would not reach for on its own.
``--write``
    Let the agent write a scratch file, so the ``PostToolUse`` hook runs.
    Implies ``--permission-mode acceptEdits``; the file is created under
    the repo and reported at the end, not deleted, so you can look at it.
    Without this flag the run is read-only: ``plan`` mode, like
    ``engine_smoke.py``.
``--no-docs``
    Skip the doc-index build. It is the slow half of startup on a large
    repo and irrelevant unless you are asking about ``doc_outline``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path

# Allow running straight from a checkout without installing.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ac_dc.claude_code import (  # noqa: E402
    EngineConfig,
    EngineSession,
    EngineStartupError,
    Event,
    Turn,
    resolve_cli,
)
from ac_dc.claude_code.hooks import Reindexer, build_hook_matchers  # noqa: E402
from ac_dc.claude_code.mcp_server import (  # noqa: E402
    SERVER_NAME,
    McpBridge,
)
from ac_dc.claude_code.permissions import PermissionBroker  # noqa: E402

DEFAULT_PROMPT = (
    "Call the ac-dc symbol_map tool for the path prefix "
    "src/ac_dc/claude_code and tell me, from its output alone, which "
    "module holds the permission gate. Do not read any files."
)

TOOL_PROMPTS = {
    "symbol_map": DEFAULT_PROMPT,
    "file_symbols": (
        "Call the ac-dc file_symbols tool for "
        "src/ac_dc/claude_code/hooks.py and list the functions it reports, "
        "with their line numbers. Do not read the file."
    ),
    "find_references": (
        "Call the ac-dc find_references tool for the symbol Reindexer and "
        "tell me where it is defined and what refers to it. Do not grep."
    ),
    "doc_outline": (
        "Call the ac-dc doc_outline tool for the path prefix specs5/plan "
        "and summarise what those documents cover, from the outline alone."
    ),
    "review_state": "Call the ac-dc review_state tool and report what it says.",
    "ui_state": "Call the ac-dc ui_state tool and report what it says.",
}

WRITE_PROMPT = (
    "Create a file at scratch_bridge_smoke.py containing a single "
    "function `smoke_marker()` that returns the string 'ok'. Then call the "
    "ac-dc file_symbols tool for that path and tell me what it reports. "
    "The tool must see the function you just wrote — say so plainly if it "
    "does not."
)

# The prefix the CLI puts on an SDK server's tools. Worth asserting on
# rather than assuming: it is the whole reason `classify_tool` can tell
# our read-only tools from a `Bash`.
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"


def _short(value: object, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_symbol_index(repo_root: Path) -> object | None:
    """The repo's symbol index, built the way ``main.py`` builds it."""
    from ac_dc.repo import Repo
    from ac_dc.symbol_index.index import SymbolIndex

    index = SymbolIndex(repo_root)
    repo = Repo(str(repo_root))
    flat = repo.get_flat_file_list()
    file_list = [f for f in flat.split("\n") if f]
    # Seed the resolver before per-file indexing, or every import
    # resolves to None — the same ordering trap main.py documents.
    index._resolver.set_files(file_list)
    for rel in file_list:
        index.index_file(rel)
    index._resolve_call_sites()
    index._ref_index.build(list(index._all_symbols.values()))
    print(
        f"symbol index    {len(index.get_indexed_files())} files of "
        f"{len(file_list)} in the tree"
    )
    return index


def _build_doc_index(repo_root: Path) -> object | None:
    """The doc index, structural pass only — no keyword enrichment."""
    from ac_dc.doc_index.index import DocIndex
    from ac_dc.repo import Repo

    repo = Repo(str(repo_root))
    doc_index = DocIndex(repo_root=repo.root)
    flat = repo.get_flat_file_list()
    docs = [f for f in flat.split("\n") if f.endswith((".md", ".svg"))]
    for rel in docs:
        try:
            doc_index.index_file(rel)
        except Exception as exc:  # noqa: BLE001 - a smoke script
            print(f"  doc skip     {rel}: {exc}")
    print(f"doc index       {len(doc_index.get_indexed_files())} documents")
    return doc_index


async def _run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"Not a directory: {repo_root}", file=sys.stderr)
        return 2

    try:
        resolution = resolve_cli(None)
    except EngineStartupError as exc:
        print(f"\nCLI resolution failed:\n  {exc}", file=sys.stderr)
        return 1
    print(f"CLI             {resolution.path} ({resolution.version})")

    print("\nBuilding the indexes the tools read…")
    symbol_index = _build_symbol_index(repo_root)
    doc_index = None if args.no_docs else _build_doc_index(repo_root)

    reindexer = Reindexer(
        symbol_index=lambda: symbol_index,
        repo_root=repo_root,
    )
    bridge = McpBridge(
        symbol_index=lambda: symbol_index,
        symbol_index_ready=lambda: True,
        doc_index=lambda: doc_index,
        doc_index_ready=lambda: doc_index is not None,
        review_state=lambda: {"active": False},
        ui_state=lambda: {
            "selected_files": [],
            "viewer": None,
            "review_state": {"active": False},
            "permission_mode": args.permission_mode,
        },
        flush=reindexer.flush,
    )
    tools = bridge.build_tools()
    print(
        f"\nbridge          {SERVER_NAME}: "
        + ", ".join(TOOL_PREFIX + t.name for t in tools)
    )

    # The app's own gate. Not left as ``None``: with no callback the *CLI*
    # answers for itself, and in `acceptEdits` it answered a call to our own
    # `file_symbols` with "you haven't granted it yet" — a denial this script
    # would have reported as the model's choice. With the real broker wired,
    # a dialog for one of our tools is visible, and it is a failure.
    dialogs: list[str] = []

    async def dialog(event: Event) -> None:
        if event.name != "permissionRequest":
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        name = str(payload.get("tool_name") or "?")
        dialogs.append(name)
        print(f"  dialog       {name} → allow (no browser here to click)")
        # Spawned, not awaited: `can_use_tool` is *inside* this broadcast,
        # and it is what waits on the future `resolve` completes.
        asyncio.get_running_loop().create_task(
            broker.resolve(str(payload.get("permission_id")), {"action": "allow"})
        )

    broker = PermissionBroker(repo_root, broadcast=dialog, decision_timeout=120.0)

    config = dataclasses.replace(
        EngineConfig.load(args.config_dir), permission_mode=args.permission_mode
    )
    session = EngineSession(
        repo_root,
        config,
        can_use_tool=broker.can_use_tool,
        hooks=build_hook_matchers(reindexer),
        mcp_servers={SERVER_NAME: bridge.build_server()},
    )

    print(f"\nConnecting (permission_mode={config.permission_mode})…")
    try:
        await session.connect()
    except EngineStartupError as exc:
        print(f"\nConnect failed:\n  {exc}", file=sys.stderr)
        return 1

    # Printed for context only. It does *not* tell you whether our server
    # loaded: `get_mcp_status` lists the configured stdio/http servers, and
    # an in-process SDK server is not one of those — a live run showed only
    # `chrome-devtools` from the user's settings while our tools were being
    # called successfully in the same turn. What proves registration is the
    # model calling a `mcp__ac-dc__*` tool at all, which is the check at the
    # bottom of this function.
    try:
        status = await session.get_mcp_status()
        print(f"mcp status      {_short(json.dumps(status, default=str))}")
    except Exception as exc:  # noqa: BLE001 - a smoke script
        print(f"mcp status      unavailable: {exc}")

    calls: list[tuple[str, object]] = []
    # `toolResult` carries only the id the call was made under, so the
    # name has to come from the card we saw go out.
    names_by_id: dict[str, str] = {}

    async def emit(event: Event) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.name == "toolUse":
            name = str(payload.get("name") or "")
            names_by_id[str(payload.get("tool_use_id"))] = name
            calls.append((name, payload.get("input")))
            marker = "→ ours" if name.startswith(TOOL_PREFIX) else "      "
            print(f"  tool  {marker} {name}  {_short(payload.get('input'), 160)}")
        elif event.name == "toolResult":
            name = names_by_id.get(str(payload.get("tool_use_id")), "?")
            status = payload.get("status")
            print(
                f"  result       {name} [{status}] "
                f"{_short(payload.get('preview'), 400)}"
            )
        elif event.name == "filesModified":
            print(f"  hook         filesModified {event.payload}")

    prompt = args.prompt
    turn = Turn(request_id="bridge-smoke-000000000-000000", message=prompt)
    print(f"\nPrompt: {prompt}\n" + "-" * 72)
    try:
        result = await session.run_turn(turn, emit)
    finally:
        await session.disconnect()

    print("-" * 72)
    ours = [name for name, _ in calls if name.startswith(TOOL_PREFIX)]
    print(f"\nOur tools called: {ours or 'NONE'}")
    print(f"Every tool called: {[name for name, _ in calls]}")
    print(f"\nResponse:\n{result.get('response') or '(none)'}")

    if args.write:
        # The hook's own bookkeeping, which the browser reads as
        # `files_reindexed` in the turn footer.
        await reindexer.flush()
        print(f"\nRe-indexed by the hook: {reindexer.take_reindexed()}")
        print(f"CLI-reported writes:    {result.get('files_modified')}")

    if not ours:
        print(
            "\nFAIL: the model called no ac-dc tool. Either the prompt let it "
            "answer another way, or the server never registered — `mcp status` "
            "above cannot tell you which, since an in-process SDK server does "
            "not appear there. Re-run with --log DEBUG to see the tool list "
            "the CLI was given.",
            file=sys.stderr,
        )
        return 1

    gated = [name for name in dialogs if name.startswith(TOOL_PREFIX)]
    if gated:
        print(
            f"\nFAIL: our own tools opened a permission dialog: {gated}. "
            "specs5/3-engine/permissions.md puts them in the read-only row — "
            "displayed, not gated — so `PermissionBroker.can_use_tool` should "
            "have allowed them without asking anyone.",
            file=sys.stderr,
        )
        return 1
    return 1 if result.get("is_error") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("prompt", nargs="?", default=None)
    parser.add_argument(
        "--repo", default=".", help="Repository root; becomes the session cwd."
    )
    parser.add_argument(
        "--tool",
        choices=sorted(TOOL_PROMPTS),
        default=None,
        help="Ask for one specific tool instead of the default prompt.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Let the agent write a scratch file, exercising the re-index hook.",
    )
    parser.add_argument(
        "--no-docs",
        action="store_true",
        help="Skip the doc-index build (slow, and only doc_outline needs it).",
    )
    parser.add_argument("--config-dir", default=None)
    parser.add_argument(
        "--permission-mode",
        default=None,
        help="Overrides engine.json. Defaults to plan, or acceptEdits with --write.",
    )
    parser.add_argument(
        "--log", default="WARNING", help="Root log level (DEBUG shows SDK internals)."
    )
    args = parser.parse_args(argv)

    if args.permission_mode is None:
        # `plan` cannot write, which is the honest default for a script
        # someone runs to see whether a *read* tool works.
        args.permission_mode = "acceptEdits" if args.write else "plan"
    if args.prompt is None:
        args.prompt = (
            WRITE_PROMPT if args.write else TOOL_PROMPTS[args.tool or "symbol_map"]
        )

    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.WARNING),
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
