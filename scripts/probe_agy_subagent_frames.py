#!/usr/bin/env python3
"""Can ``agy``'s stream feed a subagent tab? Measured, not assumed.

``subagent_tabs`` is ``UNBUILT`` for Antigravity, and the two transports
are unbuilt for reasons that are recorded as *different*. On the SDK
transport the data is there — ``Step`` is flat with ``trajectory_id`` and
``depth``, and ``antigravity/steps.py``'s ``_scope`` already maps a nested
trajectory onto the Claude pump's ``agent_id`` — so what is missing is a
renderer. On this one, ``agy/steps.py`` says something stronger:

    There is no per-step scope to derive here, unlike the SDK
    translator's ``_scope``: ``agy``'s stream carries no trajectory or
    depth field at all, so a nested trajectory is invisible to this pump
    and the whole turn belongs to one agent. That is also why the
    ``subagent_tabs`` surface is unbuilt on this transport.

**That is a claim about the wire, measured once**, against a bidirectional
capture taken 2026-09-03 at ``agy`` 1.1.22 (``sdk-surface.md`` § *The
stream, measured in bidirectional mode*). A CLI that releases weekly is
the kind of thing a fortnight-old measurement stops describing, and the
suite cannot notice: every offline test of that pump is written against
the recorded shape, so a field that *appeared* is invisible to all of
them. ``next.md`` § *Keeping this file true* — date a claim about the
tree, or check it.

What it does
------------
One turn, asked to delegate, with the **raw frames** recorded —
:meth:`AgySession.stream_frames` rather than ``stream_turn``, so nothing
is filtered by the translator whose blind spot is the thing under test.
Then the capture is analysed: every key in every frame, walked
recursively.

Two questions, because the first one alone gave a wrong answer
---------------------------------------------------------------
The first version asked only *"is there a trajectory or depth field"*,
excluded ``dict`` and ``list`` leaves as noise, and reported **MEASURED
ABSENT** on a capture that contained ``subagent_info`` — a dict, and the
whole answer. The literal question was answered correctly and the verdict
drawn from it was wrong, because scope is not the only way a stream can
name a subagent. So this now asks:

1. is there a per-step scope (the SDK's mechanism), and
2. is there **any** identity a tab could be keyed on (the contract's
   actual requirement, read off ``subagent-tabs.js`` in AG-13)?

Container keys are scanned, since a dict named for what it holds is
itself the signal.

Three outcomes, and the third is why this is not a boolean
----------------------------------------------------------
"Nothing in the stream" means nothing unless a subagent actually ran. A
turn the model answered itself would produce an identical stream and look
like a confirmation — the same shape as ``probe_agy_gate.py``'s deny
tripwire passing under write diversion, where the assertion held for a
reason unrelated to what it claimed. So a run that provokes no delegation
reports **INCONCLUSIVE** rather than confirming the comment it was
written to test.

    uv run python scripts/probe_agy_subagent_frames.py
    uv run python scripts/probe_agy_subagent_frames.py --capture FILE

The second form re-runs the analysis over a saved capture and spends
nothing. It exists because the first run's verdict was wrong while its
*capture* was complete: re-reading beat re-measuring, and an analysis
that can only be corrected by buying another turn will be corrected less
often than it is wrong.

Exit 0: an identity exists and ``subagent_tabs`` is feedable here.
Exit 1: measured absent, with a delegation confirmed to have run.
Exit 2: nothing to conclude (no ``agy``, no delegation, or a dead turn).

What a live run costs: one turn on the paid subscription. It asks for a
delegated *read*, so no write is attempted and AG-R-3 is not in play. The
capture is kept rather than deleted — the frames are the evidence, and a
capture nobody can re-read is a claim again.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-13, AG-9;
``capabilities.py`` ``subagent_tabs``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402
from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.agy.steps import KNOWN_STEP_TYPES  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

#: Key names that would carry the SDK's per-step scope if this stream had
#: one. Matched on the key, substring and case-insensitively, because the
#: point is to find a field nobody has named yet: a probe looking only for
#: ``trajectory_id`` would answer "absent" for a stream carrying
#: ``sub_agent_index``.
SCOPE_HINTS = re.compile(r"trajector|depth|parent", re.IGNORECASE)

#: Key names that would carry a subagent's *identity* — which is what a
#: tab is actually keyed on. Separate from :data:`SCOPE_HINTS` because
#: conflating them is what produced this file's first wrong verdict.
IDENTITY_HINTS = re.compile(r"subagent|sub_agent|delegat", re.IGNORECASE)

#: Tool names that mean a delegation happened. ``sdk-surface.md`` records
#: two spellings among the 57; neither is assumed to be the live one.
DELEGATION_TOOLS = ("invoke_subagent", "start_subagent", "run_subagent")

#: A real model on a real subscription, and a delegated turn is two turns
#: of work. Latency is not what is being measured.
TURN_TIMEOUT_SECONDS = 420.0

PROMPT = (
    "Delegate this to a subagent rather than doing it yourself: have the "
    "subagent read notes.txt in the current directory and report the "
    "single word on its second line. Use your subagent tool. When the "
    "subagent answers, tell me the word it found."
)

NOTES = "first line here\nPELICAN\nthird line here\n"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


async def allow_everything(broker: Any) -> None:
    """Stand in for the browser, allowing whatever is asked.

    A delegation is gated — ``start_subagent`` is in the still-asks column
    of AG-5's table, because a subagent inherits the tool set — so a probe
    that answered nothing would measure a *denied* delegation and conclude
    the stream carries nothing. That is the inconclusive case wearing a
    result's clothes.
    """
    while True:
        for payload in broker.pending():
            permission_id = payload.get("permission_id")
            if not permission_id:
                continue
            log(f"dialog: {payload.get('tool_name')} → allow")
            await broker.resolve(
                permission_id, {"action": "allow"}, resolved_by="probe"
            )
        await asyncio.sleep(0.05)


def walk_keys(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every ``(key path, value)`` in a frame, depth first.

    Frames nest under their own event name and tool payloads nest again,
    so a flat ``.keys()`` reads the envelope and misses everything the
    question is about — the same shape as the ``unwrap`` trap in
    ``agy/steps.py``. Containers are yielded *as well as* descended into,
    which the first version did not do; ``subagent_info`` is a dict, and
    excluding it is what hid the answer.
    """
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.append((path, inner))
            found.extend(walk_keys(inner, path))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(walk_keys(inner, f"{prefix}[{index}]"))
    return found


def steps_of(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The step payload of every ``step_update`` frame."""
    steps = []
    for frame in frames:
        if frame.get("event") != "step_update":
            continue
        inner = frame.get("step_update")
        if isinstance(inner, dict):
            steps.append(inner.get("step") if isinstance(inner.get("step"), dict) else inner)
    return [s for s in steps if isinstance(s, dict)]


def matches(frames: list[dict[str, Any]], pattern: re.Pattern[str]) -> list[str]:
    """Key paths whose last component matches, containers included."""
    return sorted(
        {
            path
            for frame in frames
            for path, _ in walk_keys(frame)
            if pattern.search(path.rsplit(".", 1)[-1].split("[")[0])
        }
    )


def report(frames: list[dict[str, Any]]) -> int:
    steps = steps_of(frames)
    events = sorted({str(f.get("event")) for f in frames})
    types = sorted({str(s.get("step_type")) for s in steps})
    called = [
        str(s.get("tool_name")) for s in steps if isinstance(s.get("tool_name"), str)
    ]
    log(f"{len(frames)} frames, events: {events}")
    log(f"step types: {types}")
    log(f"tools named in steps: {sorted(set(called)) or '(none)'}")

    unknown = [t for t in types if t and t not in KNOWN_STEP_TYPES]
    if unknown:
        log(
            f"step types this pump does not dispatch: {unknown} — each one "
            f"renders as a systemEvent of subtype 'unknown_step'"
        )

    scope = matches(frames, SCOPE_HINTS)
    identity = matches(frames, IDENTITY_HINTS)
    delegated = [n for n in called if n in DELEGATION_TOOLS]

    log(f"per-step scope fields (the SDK's mechanism): {scope or 'none'}")
    log(f"subagent identity fields: {identity or 'none'}")

    if not delegated and not identity:
        log(
            f"INCONCLUSIVE: no delegation tool was called (looked for "
            f"{list(DELEGATION_TOOLS)}), so an empty stream says nothing. "
            f"The model answered without a subagent, or the tool is named "
            f"something else — read the capture and re-run."
        )
        return 2

    if identity:
        log(
            "PASS: agy announces a subagent with an identity of its own, so "
            "subagent_tabs is feedable on this transport — by announcement, "
            "not by the SDK's per-step scope"
        )
        return 0

    log(
        f"MEASURED ABSENT: {delegated[0]} ran and no frame named the subagent "
        f"at all. subagent_tabs stays unbuilt on this transport."
    )
    return 1


def load(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    work = probe_root("agy-subagent-")
    (work / "notes.txt").write_text(NOTES, encoding="utf-8")
    capture = work / "frames.jsonl"

    config_dir = Path.home() / ".config" / "aic-dc"
    state = install.status(config_dir)
    log(f"installed gate: {state['state']} ({state.get('path')})")
    # Restore what was there, rather than removing what this probe added:
    # the first run found the installed gate `stale`, installed a current
    # one, and uninstalled it at the end — leaving the user's app with no
    # gate at all, which is a worse state than the one it borrowed.
    restore = state["state"]
    if restore != "current":
        log("installing the gate for the duration of this probe")
        install.install(config_dir)

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = AgyGateServer(work / "gate.sock", gate=gate, config_dir=config_dir)
    session = AgySession(work, gate=server)

    frames: list[dict[str, Any]] = []
    browser = asyncio.ensure_future(allow_everything(gate.broker))
    try:
        conversation_id = await session.start()
        log(f"conversation {conversation_id}")
        with capture.open("w", encoding="utf-8") as sink:
            async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                async for frame in session.stream_frames(PROMPT):
                    frames.append(frame)
                    sink.write(json.dumps(frame) + "\n")
                    sink.flush()
    except TimeoutError:
        log(f"the turn did not finish within {TURN_TIMEOUT_SECONDS:.0f}s")
        return 2
    finally:
        browser.cancel()
        await session.close()
        await server.stop()
        if restore == "absent":
            install.uninstall()
        elif restore != "current":
            log(f"NOTE: the gate was {restore} before this run and is now current")

    log(f"capture: {capture}")
    if not frames:
        log("INCONCLUSIVE: the turn produced no frames at all")
        return 2
    return report(frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        type=Path,
        help="re-analyse a saved frames.jsonl instead of taking a live turn",
    )
    args = parser.parse_args()
    if args.capture:
        log(f"offline: {args.capture}")
        return report(load(args.capture))
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
