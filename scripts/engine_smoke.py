"""Claude Code engine smoke test — send a prompt, print the taxonomy.

The conversion plan's phase-1 exit criterion: *a CLI-side smoke test can
send a prompt and print the streamed message taxonomy*
(``specs5/plan/README.md``). It runs the real engine against a real
``claude`` CLI with the user's real credentials, so it lives in
``scripts/`` rather than the test suite — it costs tokens and needs a
login.

It exercises exactly what phase 1 built and nothing else: CLI resolution,
options assembly, connect, ``query()``, the message pump, and the
translation layer. Permissions, MCP tools, and transcript mirroring are
later phases and are absent by construction.

Usage::

    python scripts/engine_smoke.py
    python scripts/engine_smoke.py "list the files in src/ac_dc/claude_code"
    python scripts/engine_smoke.py --repo /path/to/repo --raw
    python scripts/engine_smoke.py --cancel-after 3 "explain this repo"

Options:

``--raw``
    Print each payload in full, including the ``raw`` key holding the
    untouched CLI dict. That is what to look at when a field is arriving
    under a name the translator does not read.
``--cancel-after N``
    Interrupt the turn N seconds in. Verifies the drain-to-``ResultMessage``
    path: the run should still end with a ``streamComplete`` whose
    ``terminal_reason`` is ``aborted_streaming`` or ``aborted_tools``, not
    with a hang or a traceback.
``--permission-mode MODE``
    Overrides ``engine.json``. Defaults to ``plan``, which lets the smoke
    test run without granting write access — phase 1 has no permission
    gate, so a mode that can edit files would be an unprompted one.
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
    sdk_cli_pin,
    sdk_version,
)

DEFAULT_PROMPT = (
    "In one sentence, what kind of project is this? Read at most one file."
)

# Events whose payloads are long and uninteresting when they are merely
# working. Summarised to one line instead of dumped.
_SUMMARISED = {"streamChunk", "thinkingChunk"}


def _short(value: object, limit: int = 96) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_event(event: Event, index: int, raw: bool) -> None:
    scope = "turn" if event.turn_scoped else "session"
    header = f"[{index:>3}] {event.name:<22} ({scope})"
    payload = event.payload
    if event.name in _SUMMARISED and isinstance(payload, dict):
        print(
            f"{header}  block={payload.get('block_id')} "
            f"seq={payload.get('seq')} done={payload.get('done')} "
            f"content={_short(payload.get('content'))}"
        )
        return
    if isinstance(payload, dict):
        if raw:
            print(f"{header}\n{json.dumps(payload, indent=2, default=str)}")
            return
        # `raw` holds the untouched CLI dict: useful in the debug view,
        # noise here, where the point is to see which channel fired.
        trimmed = {k: v for k, v in payload.items() if k != "raw"}
        print(f"{header}  {_short(json.dumps(trimmed, default=str), 300)}")
    else:
        print(f"{header}  {_short(payload)}")


async def _run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"Not a directory: {repo_root}", file=sys.stderr)
        return 2

    print(f"claude-agent-sdk {sdk_version()} (bundled CLI pin {sdk_cli_pin()})")
    try:
        resolution = resolve_cli(None)
    except EngineStartupError as exc:
        print(f"\nCLI resolution failed:\n  {exc}", file=sys.stderr)
        return 1
    print(f"CLI            {resolution.path}")
    print(f"               {resolution.source}, version {resolution.version}")
    if resolution.version_warning:
        print(f"  warning:     {resolution.version_warning}")

    config = dataclasses.replace(
        EngineConfig.load(args.config_dir), permission_mode=args.permission_mode
    )
    session = EngineSession(repo_root, config)
    print(f"credentials    {session.health.credential_source or 'resolving…'}")

    print(f"\nConnecting (cwd={repo_root}, permission_mode={config.permission_mode})…")
    try:
        await session.connect()
    except EngineStartupError as exc:
        print(f"\nConnect failed:\n  {exc}", file=sys.stderr)
        return 1
    print(f"credentials    {session.health.credential_source}")
    if session.health.auth_warning:
        print(f"  warning:     {session.health.auth_warning}")

    counter = 0
    seen: dict[str, int] = {}

    async def emit(event: Event) -> None:
        nonlocal counter
        counter += 1
        seen[event.name] = seen.get(event.name, 0) + 1
        if event.name not in _SUMMARISED or args.verbose:
            _print_event(event, counter, args.raw)

    turn = Turn(request_id="smoke-0000000000000-000000", message=args.prompt)
    print(f"\nPrompt: {args.prompt}\n" + "-" * 72)

    cancel_task = None
    if args.cancel_after is not None:

        async def _cancel() -> None:
            await asyncio.sleep(args.cancel_after)
            print(f"\n--- interrupting after {args.cancel_after}s ---")
            print(await session.interrupt(turn.request_id))

        cancel_task = asyncio.create_task(_cancel())

    try:
        result = await session.run_turn(turn, emit)
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        await session.disconnect()

    print("-" * 72)
    print("Event channels seen:")
    for name in sorted(seen):
        print(f"  {name:<22} {seen[name]}")

    print("\nResult:")
    for key in (
        "subtype",
        "terminal_reason",
        "is_error",
        "cancelled",
        "num_turns",
        "duration_ms",
        "tool_calls",
        "files_modified",
        "total_cost_usd",
        "mirror_gap",
        "user_message_id",
    ):
        print(f"  {key:<18} {result.get(key)!r}")
    usage = result.get("usage")
    if usage:
        print(f"  usage              {_short(json.dumps(usage, default=str), 200)}")
    if result.get("errors"):
        print(f"  errors             {result['errors']}")

    print(f"\nResponse:\n{result.get('response') or '(none)'}")

    # A cancelled turn that reached a result message is the drain path
    # working; a cancelled turn with no result is the bug this flag exists
    # to catch.
    if args.cancel_after is not None and not result.get("cancelled"):
        print(
            "\nNote: the turn finished before the interrupt landed. "
            "Try a smaller --cancel-after or a longer prompt.",
        )
    return 1 if result.get("is_error") and not result.get("cancelled") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--repo", default=".", help="Repository root; becomes the session cwd."
    )
    parser.add_argument(
        "--permission-mode",
        default="plan",
        help="Overrides engine.json. Defaults to plan so nothing is written.",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help=(
            "Directory holding engine.json (normally AC-DC's user config dir). "
            "Omitted means every option falls through to the CLI's own default."
        ),
    )
    parser.add_argument(
        "--cancel-after",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Interrupt the turn to exercise the drain-to-result path.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print each payload in full, including the untouched CLI `raw` dict.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Print every stream chunk."
    )
    parser.add_argument(
        "--log", default="WARNING", help="Root log level (DEBUG shows SDK internals)."
    )
    args = parser.parse_args(argv)

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
