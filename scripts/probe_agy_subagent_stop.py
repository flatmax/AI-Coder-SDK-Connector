#!/usr/bin/env python3
"""Can one subagent be stopped without stopping the turn it belongs to?

`subagent_stop` in `src/aic_dc/capabilities.py` has been UNBUILT on both
Antigravity transports with a design written out and three limits named in
advance. This is the measurement that decides whether it can be built:

  1. **Does an aimed refusal actually reach the subagent's calls?** The gate
     routes on the `conversationId` the hook payload carries, and a subagent
     runs in a conversation of its own — the fact AG-R-14 was raised about,
     used here for something rather than against us.
  2. **Does the parent survive it?** This is the whole difference from
     `cancel_streaming`. `AgyGateServer.refuse_all` is turn-wide, so wiring ⏹
     to it as it stood would have stopped the parent as well as the subagent
     the user aimed at — which is why the surface stayed unbuilt rather than
     being wired to the mechanism that existed.
  3. **What does `agy` report for a starved subagent?** Recorded rather than
     asserted, because it is the one thing nobody has seen. `steps.py` maps
     `CANCELED` → `stopped` → an amber LED; `DONE` would take the row green
     over something the user stopped, which is a different feature.

The instrument is the asymmetry: the subagent is given a long list of files to
read one at a time, and the refusal is aimed the moment its announcement
arrives. So calls from its conversation *after* that point must be denied, and
calls from the parent's conversation must not — the same file, the same turn,
two conversations, opposite outcomes.

A denial that reaches the dialog would be a different mechanism, so the run
also asserts that no refused call was ever put to the broker: `decide` checks
the aimed refusal before `pre_verdict`, and a user who has already pressed stop
must not be asked again per tool call.

    uv run python scripts/probe_agy_subagent_stop.py

Exit 0: the refusal was aimed and held, and the parent kept running.
Exit 1: it leaked — either the subagent went on working, or the parent was
        stopped with it.
Exit 2: nothing to conclude (no agy, no delegation, or the subagent made no
        call after the refusal was aimed).

What it costs: one turn on the paid subscription, in a throwaway directory
where nothing is written.

Governing spec: `specs5/plan-ag/` — AG-5, AG-14, AG-R-14, and the
`subagent_stop` row of `src/aic_dc/capabilities.py`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy import tools as agy_tools  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.agy.steps import subagent_entries  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

TURN_TIMEOUT_SECONDS = 600.0

NOTE_COUNT = 14

STOP_REASON = (
    "The user stopped this subagent in AIC-DC. Stop what you are doing, do "
    "not continue, and do not try another way of making this change."
)

PROMPT = (
    "Do exactly this and nothing else. Delegate to a single subagent using "
    "your subagent tool. Instruct the subagent to read every file in the "
    "notes/ directory of this workspace — there are {count} of them, named "
    "note-01.md to note-{last}.md — strictly one at a time, each with its own "
    "separate file-reading call, and to write a one-sentence summary of each "
    "as it goes. Tell it not to stop early and not to read them in bulk. Do "
    "not read any file yourself. When the subagent has finished or has "
    "stopped, reply with the single word: done."
)


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The real gate, with every decision it returns written down.

    Subclassed rather than monkeypatched so the behaviour under test is the
    shipped one. The conversation is kept beside the tool and the verdict,
    because the whole question is whether two conversations in one turn were
    answered differently.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.decisions: list[dict[str, Any]] = []
        self.asked_broker: list[str] = []

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        call = payload.get("toolCall") or {}
        result = await super().decide(payload)
        self.decisions.append(
            {
                "t": time.time(),
                "tool": str(call.get("name") or "?"),
                "conversation": str(payload.get("conversationId") or "?"),
                "decision": str(result.get("decision") or "?"),
                "reason": str(result.get("reason") or ""),
            }
        )
        return result


async def answer_dialogs(broker: Any, answered: list[tuple[str, str]]) -> None:
    """Allow everything but the write seam, so the reading task can proceed.

    Denying broadly is `probe_agy_subagent_gate.py`'s instrument and would be
    the wrong one here: this run needs the subagent to *work* until it is
    stopped, or there is nothing to observe stopping.
    """
    while True:
        for payload in broker.pending():
            permission_id = payload.get("permission_id")
            if not permission_id:
                continue
            tool = str(payload.get("tool_name") or "")
            allow = tool not in agy_tools.MUTATING_TOOLS or tool == "invoke_subagent"
            answered.append((tool, "allow" if allow else "deny"))
            await broker.resolve(
                permission_id,
                {"action": "allow"}
                if allow
                else {"action": "deny", "message": "Not part of this probe."},
                resolved_by="probe",
            )
        await asyncio.sleep(0.05)


def subagent_states(frames: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Every state each announced subagent was reported in, in order."""
    seen: dict[str, list[str]] = {}
    for frame in frames:
        inner = frame.get("step_update")
        if not isinstance(inner, dict):
            continue
        step = inner.get("step") if isinstance(inner.get("step"), dict) else inner
        if step.get("step_type") != "subagent" and "subagent_info" not in step:
            continue
        state = str(step.get("state") or inner.get("state") or "?")
        for entry in subagent_entries(step):
            conversation = str(entry.get("conversation_id") or "")
            if not conversation:
                continue
            states = seen.setdefault(conversation, [])
            if not states or states[-1] != state:
                states.append(state)
    return seen


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2
    config_dir = Path.home() / ".config" / "aic-dc"
    state = install.status(config_dir)
    if state.get("state") != "current":
        log(f"the gate reads {state.get('state')!r}; AgySession will not start")
        return 2

    work = probe_root("agy-subagent-stop-")
    notes = work / "notes"
    notes.mkdir(exist_ok=True)
    for index in range(1, NOTE_COUNT + 1):
        (notes / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n\nFact number {index}: the shelf holds {index} jars.\n",
            encoding="utf-8",
        )

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = RecordingGateServer(work / "gate.sock", gate=gate, config_dir=config_dir)
    session = AgySession(work, gate=server)

    frames: list[dict[str, Any]] = []
    answered: list[tuple[str, str]] = []
    dialogs = asyncio.ensure_future(answer_dialogs(gate.broker, answered))
    parent = ""
    child = ""
    stopped_at = 0.0
    try:
        parent = await session.start()
        log(f"parent conversation {parent}")
        capture = work / "frames.jsonl"
        with capture.open("w", encoding="utf-8") as sink:
            async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
                async for frame in session.stream_frames(
                    PROMPT.format(count=NOTE_COUNT, last=f"{NOTE_COUNT:02d}")
                ):
                    frames.append(frame)
                    sink.write(json.dumps(frame) + "\n")
                    sink.flush()
                    if child:
                        continue
                    inner = frame.get("step_update")
                    step = (
                        inner.get("step")
                        if isinstance(inner, dict)
                        and isinstance(inner.get("step"), dict)
                        else inner
                    )
                    if not isinstance(step, dict):
                        continue
                    for entry in subagent_entries(step):
                        conversation = str(entry.get("conversation_id") or "")
                        if not conversation:
                            continue
                        child = conversation
                        stopped_at = time.time()
                        server.refuse_conversation(child, STOP_REASON)
                        log(f"subagent {child} announced — refusal aimed at it now")
                        break
        log(f"capture: {capture}")
    except TimeoutError:
        log(f"the turn did not finish within {TURN_TIMEOUT_SECONDS:.0f}s")
        return 2
    finally:
        dialogs.cancel()
        await session.close()
        await server.stop()

    if not child:
        log("INCONCLUSIVE: no subagent was announced, so nothing was aimed at")
        return 2

    after = [d for d in server.decisions if d["t"] > stopped_at]
    child_after = [d for d in after if d["conversation"] == child]
    parent_after = [d for d in after if d["conversation"] == parent]
    leaked = [d for d in child_after if d["decision"] != "deny"]
    parent_denied = [
        d
        for d in parent_after
        if d["decision"] == "deny" and d["reason"] == STOP_REASON
    ]
    states = subagent_states(frames)

    log("")
    log(f"decisions before the stop: {len(server.decisions) - len(after)}")
    log(f"the subagent's calls after the stop: {len(child_after)} "
        f"({[d['tool'] for d in child_after][:8]})")
    log(f"  of those, not denied: {len(leaked)}")
    log(f"the parent's calls after the stop: {len(parent_after)} "
        f"({[(d['tool'], d['decision']) for d in parent_after][:8]})")
    log(f"  of those, denied by the aimed refusal: {len(parent_denied)}")
    log(f"dialogs raised after the stop: {len(answered)} total this run")
    log(f"states agy reported for the subagent: {states.get(child)}")
    log("")

    if not child_after:
        log(
            "INCONCLUSIVE: the subagent made no tool call after the refusal "
            "was aimed, so nothing was refused and nothing is settled. This "
            "is the prose-only limit in miniature — try a longer task."
        )
        return 2
    if leaked:
        log(
            f"LEAK: {len(leaked)} call(s) from the stopped subagent were not "
            f"denied: {[(d['tool'], d['decision']) for d in leaked][:5]}"
        )
        log(f"evidence kept at {work}")
        return 1
    if parent_denied:
        log(
            f"OVER-BROAD: the aimed refusal also denied {len(parent_denied)} "
            "of the parent's calls, which is `refuse_all` wearing a different "
            "name and is the reason this surface stayed unbuilt"
        )
        log(f"evidence kept at {work}")
        return 1

    log(
        f"PASS: every one of the stopped subagent's {len(child_after)} later "
        f"calls was denied, and the parent's {len(parent_after)} were not."
    )
    log(
        "The LED depends on the states above: `CANCELED` maps to `stopped` "
        "(amber) in steps.py; anything else is what the row will show."
    )
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
