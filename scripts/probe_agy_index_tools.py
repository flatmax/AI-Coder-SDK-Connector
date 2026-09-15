#!/usr/bin/env python3
"""AG-34: do the six `aic-dc` tools exist to the model on the `agy` transport?

`bridge_smoke.py` is this probe's Claude counterpart, and its opening
argument applies here word for word: unit tests pin the bridge's answers,
and only a live run can tell us the *server* registered, the names are
spelled the way the model sees them, and the schemas are ones it will
actually fill in. What makes a second probe necessary rather than
duplicative is that this transport shares none of that plumbing. The Claude
engine hands its CLI an in-process SDK server; the SDK engine hands its
harness Python callables. `agy` is a **subprocess reached over a pipe**, so
it can be handed neither — the six specs reach it over the consultation
listener's second MCP server, discovered from a `mcp_config.json` this app
writes into a root only this app owns.

Which means three things can be true at once and the tools still not exist
to the model: the bridge answers, the listener serves `/index`, and `agy`
never read the config. That gap is exactly the shape of the defect AG-34
closed — six tools that existed in Python and did not exist to a model —
so it is the one a green test suite cannot rule out.

Two questions, and the first is free
------------------------------------
**Does the binary reach both servers?** It connects to them during startup,
before any prompt is sent, so this costs no model turn and nothing on the
subscription. Both servers are asserted, not just ours: one MCP server
working while the other does not is the failure mode of putting two on one
socket, and AG-22's `second_opinion` was there first. The consultation
server is expected only when this install has a Claude CLI to run one — an
install without one is supported, and AG-34's own change is what stopped
that install losing the index server too.

**The `init` frame is not where that answer lives, and the first run of this
probe is what settled it.** `probe_agy_tool_inventory.py` reads the tool
list off `init` for free, so this was written to assert our six names in it
and *failed on correct code*: `init` advertised 57 tools on 2026-09-15,
every one of them a builtin, with neither our six nor AG-22's
``second_opinion`` — which has worked since it was written — among them. MCP
tools are not enumerated there at all. That is consistent with the rest of
this transport rather than surprising: every MCP call arrives as the
multiplexer ``call_mcp_tool`` carrying ``ServerName``/``ToolName`` in its
*arguments*, which is the whole reason
:func:`aic_dc.antigravity.permissions.is_index_read` has to read them
(`agy_tools.MCP_TOOL`). So the free signal is taken from **our** side of the
socket: the listener's own session table, which holds one live transport per
server once `agy` has handshaked with it. ``call_mcp_tool`` itself *is*
asserted in `init`, because it is the one builtin our six ride on.

**Can it call one and get a real answer?** One turn, asking for
``file_symbols`` on a file whose contents this script knows. Two things are
asserted about it that the answer alone would not show:

  - **No dialog for any of the six.** They are ungated on purpose, because
    all six read — "displayed, not gated", the same rule
    `bridge_smoke.py` holds the Claude engine to. On this transport the
    ungating is *two* narrowings, not one, and a dialog here means one of
    them is missing: :func:`aic_dc.antigravity.permissions.is_index_read`
    (the `own_read_tools` set, which reads the qualified name) or the
    listener having advertised ``readOnlyHint``.
  - **The bridge was really the source.** Wrapped in a recorder, so a
    model that answered from having read the file itself — or from a guess
    — fails rather than passes. `find_references` was chosen for the
    second half of the prompt for the same reason: nothing in the tree
    tells you the answer without an index.

What it costs: one turn on the paid subscription, and it builds this
repository's symbol index first (a minute or so). It writes nothing to the
tree — all six tools read, which is the property that makes them ungated.

    uv run python scripts/probe_agy_index_tools.py
    uv run python scripts/probe_agy_index_tools.py --no-turn

Exit 0 on PASS, 2 when there is nothing to probe, 1 on a criterion not met.

Governing spec: `specs5/plan-ag/` — AG-34, AG-22, AG-4, AG-R-33.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aic_dc import index_tools  # noqa: E402
from aic_dc.agy import roots  # noqa: E402
from aic_dc.agy import tools as agy_tools  # noqa: E402
from aic_dc.agy.service import AgyService  # noqa: E402
from aic_dc.antigravity import permissions  # noqa: E402
from aic_dc.claude_code.consult_listener import (  # noqa: E402
    INDEX_SERVER_NAME,
    SERVER_NAME,
)

#: The tools this repository's own tree can be asked about, and a symbol the
#: probe can check the answer against. ``ToolSpec`` is defined in the file
#: named here, so ``file_symbols`` must report it and ``find_references``
#: must find the module that consumes it.
SUBJECT = "src/aic_dc/index_tools.py"
SUBJECT_SYMBOL = "ToolSpec"

PROMPT = (
    "Use your aic-dc tools and nothing else — do not read, cat, grep or "
    f"open any file. First call file_symbols on {SUBJECT} and tell me every "
    "class it reports. Then call find_references on "
    f"{SUBJECT_SYMBOL} and tell me which files use it. Answer only from what "
    "those two tools return."
)

TURN_TIMEOUT_SECONDS = 420.0

#: The six, in declaration order — ``TOOL_NAMES`` is a frozenset, and a
#: failure that lists them in a different order each run is one nobody can
#: diff against the last.
OURS = tuple(spec.name for spec in index_tools.SPECS)


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def config_dir() -> Path:
    """The config dir the app uses, and the gate is installed against."""
    return Path.home() / ".config" / "aic-dc"


class RecordingBridge:
    """The real bridge, with every tool call written down.

    Delegating rather than subclassing, because
    :meth:`aic_dc.index_tools.ToolSpec.invoke_text` reaches the bridge by
    ``getattr(bridge, spec.name)`` — so one ``__getattr__`` sees every one
    of the six without naming any of them, and the object that answers is
    the one the app built. Nothing here changes an answer; it only proves
    which answers came from here.
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge
        #: ``(tool, arguments)`` for every call the listener dispatched.
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._bridge, name)
        if name not in index_tools.TOOL_NAMES:
            return attribute

        async def recorded(**kwargs: Any) -> Any:
            self.calls.append((name, dict(kwargs)))
            answer = await attribute(**kwargs)
            log(f"bridge: {name}({json.dumps(kwargs, default=str)}) answered")
            return answer

        return recorded


async def build_indexes(repo: Any, claude: Any, config: Any) -> None:
    """The app's own deferred init, so the index is the running app's index.

    ``main._heavy_init`` and not a second walk of the tree: it seeds the
    import resolver before per-file indexing, resolves call sites after, and
    builds the reference graph — three steps in that order, without which
    ``find_references`` answers from name matches. A probe with its own
    index-building would be asserting against a differently built index,
    which is what `bridge_smoke.py`'s first criterion warns about.
    """
    from aic_dc import main as main_module

    async def progress(*_args: Any, **_kwargs: Any) -> None:
        return None

    log("building the symbol index the tools read (this is the slow part)")
    await main_module._heavy_init(claude, repo, config, progress)
    log(
        f"symbol index ready={claude.index_sources['symbol_index_ready']()} "
        f"doc index ready={claude.index_sources['doc_index_ready']()}"
    )


async def answer_dialogs(service: AgyService, raised: list[dict[str, Any]]) -> None:
    """Stand in for the browser: allow everything, and keep what was asked.

    Every dialog is allowed so that a mis-gated index tool shows up as a
    *recorded payload* rather than as a turn that hung — a probe that let it
    block would report a timeout, which is the diagnostic that sends a
    reader to the transport instead of to the gate.

    The whole payload is kept rather than the name, because on this
    transport the name is ``call_mcp_tool`` for every MCP call: which server
    and which tool are in the *arguments*, which is why
    :func:`aic_dc.antigravity.permissions.is_index_read` takes both.
    """
    while True:
        gate = service._gate
        if gate is not None:
            for payload in gate.broker.pending():
                permission_id = payload.get("permission_id")
                if not permission_id:
                    continue
                raised.append(dict(payload))
                log(f"dialog: {payload.get('tool_name')} → allow")
                await gate.broker.resolve(
                    permission_id, {"action": "allow"}, resolved_by="probe"
                )
        await asyncio.sleep(0.05)


def dialog_name(payload: dict[str, Any]) -> str:
    """What a reader should call the tool this dialog was about."""
    tool_name = str(payload.get("tool_name") or "?")
    target = permissions.mcp_rule_target(tool_name, payload.get("input") or {})
    return target or tool_name


def check_config(service: AgyService) -> int:
    """The document `agy` was given: both servers, one socket, one bearer."""
    path = roots.mcp_config_file(roots.master_root(config_dir()))
    if not path.is_file():
        log(f"FAIL: {path} does not exist, so agy was offered no MCP server")
        return 1
    document = json.loads(path.read_text())
    servers = document.get("mcpServers") or {}
    mode = path.stat().st_mode & 0o777
    log(f"config: {path} ({oct(mode)}) names {sorted(servers)}")
    if INDEX_SERVER_NAME not in servers:
        log(f"FAIL: no {INDEX_SERVER_NAME!r} entry, so the six tools are unreachable")
        return 1
    urls = {name: entry.get("serverUrl") or entry.get("url") for name, entry in servers.items()}
    tokens = {
        name: (entry.get("headers") or {}).get("Authorization")
        for name, entry in servers.items()
    }
    if len(set(urls.values())) != len(urls):
        log(f"FAIL: two servers share one URL, so one of them is unreachable: {urls}")
        return 1
    ports = {str(url).split(":")[2].split("/")[0] for url in urls.values() if url}
    if len(ports) != 1:
        log(f"FAIL: {ports} — one listener should mean one port: {urls}")
        return 1
    if len(set(tokens.values())) != 1:
        log("FAIL: the bearer differs per server; one spawn holds one token")
        return 1
    if mode != 0o600:
        log(f"FAIL: {path} is {oct(mode)}; the bearer file must be 0600")
        return 1
    log(f"config: one port {ports.pop()}, one bearer, {len(servers)} servers")
    return 0


def check_reach(service: AgyService, claude_available: bool) -> int:
    """Did `agy` handshake with each server it was offered?

    Read off the listener rather than off `agy`: the SDK's session table
    holds one transport per MCP server a client has connected to, and this
    listener already reaches into it — :meth:`ConsultationListener._forget`
    reclaims from the same two private names, and warns at startup if a
    later SDK renames them. So this asserts on the table the shipping code
    depends on, which is the honest place for a probe to look when the far
    side does not report what it connected to.

    A server with no transport is the specific failure two servers on one
    socket can produce: Starlette does not run a *mounted* sub-app's
    lifespan, so a session manager that was never entered serves a route
    that fails every request — and `agy` would carry on with the other one
    and say nothing.
    """
    listener = service._listener
    if listener is None:
        log("FAIL: no listener survived the spawn, so nothing was offered")
        return 1
    served = sorted(listener.managers)
    log(f"listener: serving {served}")
    if INDEX_SERVER_NAME not in served:
        log(f"FAIL: {INDEX_SERVER_NAME!r} is not served, so the six do not exist")
        return 1
    if claude_available and SERVER_NAME not in served:
        log(
            f"FAIL: this install has a Claude CLI, so {SERVER_NAME!r} should "
            "be on the same socket. Two servers on one listener is what the "
            "combined lifespan exists for"
        )
        return 1
    if not claude_available:
        log(
            "note: no Claude CLI here, so no consultation server was offered "
            "— and AG-34's point is that the index server is offered anyway"
        )

    connected = {}
    for name, manager in listener.managers.items():
        instances = getattr(manager, "_server_instances", None)
        if instances is None:  # pragma: no cover - a future SDK, not a state
            log(f"FAIL: the MCP SDK no longer exposes {name}'s session table")
            return 1
        connected[name] = len(instances)
    log(f"listener: live transports per server {connected}")
    silent = sorted(name for name, count in connected.items() if count == 0)
    if silent:
        log(
            f"FAIL: agy opened no session on {silent}. It was handed the "
            "config above and either did not read it or could not reach the "
            "route — INDEX_PATH in consult_listener.py is the one it wants"
        )
        return 1

    # The multiplexer, not our six: see this module's docstring for why the
    # init frame does not carry an MCP tool name on this transport.
    advertised = set(getattr(service._session, "advertised_tools", frozenset()))
    log(f"init: {len(advertised)} builtin tools advertised")
    if agy_tools.MCP_TOOL not in advertised:
        log(
            f"FAIL: agy advertised no {agy_tools.MCP_TOOL!r}, "
            "which is the builtin every MCP call arrives as. Without it the "
            "servers are reachable and nothing can call them"
        )
        return 1
    leaked = sorted(n for n in advertised if n.startswith("mcp__"))
    if leaked:
        # Not a failure, and worth saying loudly: it would mean this
        # transport had started enumerating MCP tools by qualified name,
        # which is what `is_index_read` reads the arguments instead of.
        log(f"note: init now names MCP tools directly ({leaked}) — see is_index_read")
    return 0


async def one_turn(service: AgyService, bridge: RecordingBridge) -> int:
    """One real turn: the model calls two of the six and answers from them."""
    log("asking for file_symbols and find_references (one subscription turn)")
    raised: list[dict[str, Any]] = []
    dialogs = asyncio.ensure_future(answer_dialogs(service, raised))
    # **Every fragment as well as the footer**, which is
    # `probe_agy_pre_invocation.py`'s remedy for a trap this probe walked
    # straight into on its first live run. The footer's key is `response`;
    # `response_text` was renamed when it was found that both Antigravity
    # transports had been settling every turn with empty content, and reading
    # the old name reported an empty answer for a turn that had really called
    # two of our tools and really answered from them — an instrument fault
    # shaped exactly like the finding. No single key can do that again.
    prose: list[str] = []
    original_dispatch = service._dispatch

    async def recording_dispatch(event: Any, request_id: str | None) -> None:
        await original_dispatch(event, request_id)
        for key in ("text", "delta", "content", "response"):
            value = event.payload.get(key)
            if isinstance(value, str) and value:
                prose.append(value)

    service._dispatch = recording_dispatch  # type: ignore[method-assign]
    try:
        started = await service.chat_streaming("probe-index", PROMPT)
        if "error" in started:
            log(f"FAIL: the turn was refused: {started}")
            return 1
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            while "probe-index" in service._turns:
                await asyncio.sleep(0.1)
    except TimeoutError:
        log(f"FAIL: the turn did not finish within {TURN_TIMEOUT_SECONDS:.0f}s")
        return 1
    finally:
        service._dispatch = original_dispatch  # type: ignore[method-assign]
        dialogs.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dialogs

    # Asserted against the join, printed as the longest fragment. This
    # transport's text events carry the whole answer *so far* rather than a
    # delta, so the join is every prefix of the reply concatenated — right
    # for a substring check and unreadable for the human reading a failure.
    text = "\n".join(prose)
    settled = max(prose, key=len) if prose else ""
    log(f"answer ({len(settled)} chars): {settled[:400]!r}")

    called = [name for name, _ in bridge.calls]
    if not called:
        log(
            "FAIL: the model answered without calling one of our tools, so "
            "whatever it said came from somewhere else. The tools were "
            "advertised, so this is the model choosing not to use them — "
            "re-run before reading it as a defect"
        )
        return 1
    log(f"bridge: {len(bridge.calls)} calls — {called}")
    if "file_symbols" not in called:
        log(f"FAIL: file_symbols was asked for by name and never called: {called}")
        return 1

    gated = [
        dialog_name(payload)
        for payload in raised
        if permissions.is_index_read(
            str(payload.get("tool_name") or ""), payload.get("input") or {}
        )
    ]
    if gated:
        log(
            f"FAIL: {gated} raised a permission dialog. All six read, and "
            "both narrowings should have let them through — check "
            "`permissions.is_index_read` and the listener's readOnlyHint"
        )
        return 1
    if raised:
        others = [dialog_name(payload) for payload in raised]
        log(f"note: dialogs were raised for {others}, none of them ours")

    if SUBJECT_SYMBOL not in text:
        log(
            f"FAIL: the answer never names {SUBJECT_SYMBOL}, which "
            f"file_symbols reports for {SUBJECT}. The tool answered — the "
            "model did not use what came back"
        )
        return 1
    log(f"PASS: agy called {sorted(set(called))} and answered from them")
    return 0


async def run(args: argparse.Namespace) -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    from aic_dc.claude_code.consultant import ClaudeConsultant
    from aic_dc.claude_code.service import ClaudeCodeService
    from aic_dc.repo import Repo

    config = types.SimpleNamespace(
        repo_root=str(repo_root),
        config_dir=str(config_dir()),
        aic_dc_dir=str(repo_root / ".aic-dc"),
    )
    repo = Repo(str(repo_root))
    claude = ClaudeCodeService(config)
    await build_indexes(repo, claude, config)

    # `main.py`'s own arguments, verbatim. An adapter built any other way
    # would be answering for a wiring the product does not have.
    service = AgyService(
        config,
        repo=repo,
        reindexer=claude.reindexer,
        index_sources=claude.index_sources,
    )
    if service.mcp_bridge is None:
        log("FAIL: the adapter built no bridge, so it was given no tree sources")
        return 1
    recording = RecordingBridge(service.mcp_bridge)
    # Set before the spawn, because `_offer_consultant` reads this attribute
    # when it builds the listener — after that the listener holds it and a
    # substitution would be invisible.
    service.mcp_bridge = recording

    claude_available = ClaudeConsultant(config_dir()).available()
    log(f"the six: {', '.join(OURS)}")
    log(f"claude second opinion available: {claude_available}")

    connected = await service.connect_engine()
    if connected.get("error"):
        log(f"FAIL: the session did not start: {connected}")
        return 1
    log(f"session: {connected}")

    try:
        code = check_config(service)
        if code == 0:
            code = check_reach(service, claude_available)
        if code == 0 and not args.no_turn:
            code = await one_turn(service, recording)
    finally:
        # Both, and in this order: the adapter's shutdown is what retires the
        # listener and reclaims the bearer, and the Claude service holds the
        # doc builder's background task.
        with contextlib.suppress(Exception):
            await service.shutdown()
        with contextlib.suppress(Exception):
            await claude.shutdown()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-turn",
        action="store_true",
        help="check what agy connected to and stop: no prompt, no cost",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
