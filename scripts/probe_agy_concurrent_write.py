#!/usr/bin/env python3
"""Does a *second* concurrent ``agy`` process cause the write diversion?

The last untested candidate. [AG-R-3](../specs5/plan-ag/risks.md#ag-r-3) is
real and its trigger is unknown; two explanations have already been
disproven by measurement:

1. *"The workspace is untrusted."* Writes diverted from inside a trusted
   root.
2. *"Creation diverts, editing lands."* ``probe_agy_write_target.py`` asked
   one session to do both in one turn and **both landed**.

What separates every diverting run from every clean one is that the
diverting ones had **two ``agy`` processes running at the same time** — the
isolation probe's own session and the stranger's. That is a correlation
across a handful of runs, not a mechanism, and this is the probe that
either promotes it or kills it.

The design is the same one that killed hypothesis 2: change exactly one
thing. The **same workspace, the same prompt, the same tool** is run twice
— once alone, once with an unrelated ``agy`` session alive beside it — and
the only difference between the two halves is the second process.

    uv run python scripts/probe_agy_concurrent_write.py

Exit 0 if both landed (concurrency is **not** the trigger, and the register
should say so), 3 if the concurrent one diverted while the solo one landed
(**confirmed**), 1 if the solo run diverted too, which would mean the
workspace is wrong and the probe proves nothing.

What it costs: three turns on the paid subscription.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402

SCRATCH = Path.home() / ".gemini" / "antigravity-cli" / "scratch"
MARKER = "CONCURRENCY_PROBE"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


async def spawn(cwd: Path):
    proc = await asyncio.create_subprocess_exec(
        "agy", "--print=", "--input-format", "stream-json",
        "--output-format", "stream-json", "--dangerously-skip-permissions",
        "--print-timeout", "10m",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise RuntimeError("agy exited before its init frame")
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("event") == "init":
            return proc, str(frame.get("conversation_id") or "")


async def turn(proc, prompt: str) -> str:
    proc.stdin.write(
        json.dumps({"event": "user", "message": {"role": "user", "content": prompt}})
        .encode("utf-8") + b"\n"
    )
    await proc.stdin.drain()
    while True:
        line = await proc.stdout.readline()
        if not line:
            return ""
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("event") == "result":
            return str((frame.get("result") or {}).get("status") or "")


async def close(proc) -> None:
    try:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def one_write(work: Path, name: str, *, alongside: bool) -> bool:
    """Ask for one file and report whether it landed in ``work``."""
    target = work / name
    proc, conversation = await spawn(work)
    noise = None
    try:
        if alongside:
            # An unrelated session in its own workspace, alive for the whole
            # of the write below. It is given a turn of its own so that it is
            # genuinely *working* rather than merely running — an idle
            # process may not be what matters.
            noise_dir = probe_root("agy-conc-noise-")
            noise, _ = await spawn(noise_dir)
            asyncio.ensure_future(turn(noise, "Count from 1 to 20, nothing else."))
            await asyncio.sleep(2)
        status = await turn(
            proc,
            f"Create a file called {name} in the current directory containing "
            f"exactly the line {MARKER}. Do nothing else.",
        )
        log(f"{'concurrent' if alongside else 'solo':10} run status: {status}")
    finally:
        await close(proc)
        if noise is not None:
            await close(noise)
    landed = target.is_file()
    diverted = (SCRATCH / name).is_file()
    log(f"{'concurrent' if alongside else 'solo':10} landed={landed} scratch={diverted}")
    return landed


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    work = probe_root("agy-conc-")
    # **Seeded, and that is the experiment now.** The first run of this
    # probe used an empty workspace and the *solo* write diverted too,
    # which killed the concurrency comparison and pointed somewhere else:
    # every clean run on record had a file in the workspace already, and
    # every diverting one did not. So the workspace gets one file, and if
    # both halves now land, emptiness was the trigger all along and
    # concurrency was never in it.
    (work / "seed.txt").write_text("SEED\n", encoding="utf-8")
    log(f"workspace: {work} (seeded)")
    # Distinct names so the two halves cannot read each other's leftovers,
    # in the workspace or in scratch.
    solo = await one_write(work, "solo_probe.txt", alongside=False)
    concurrent = await one_write(work, "concurrent_probe.txt", alongside=True)
    shutil.rmtree(work, ignore_errors=True)

    if not solo:
        log(
            "INCONCLUSIVE: the solo write diverted too, so this workspace "
            "does not write cleanly and the comparison says nothing."
        )
        return 1
    if solo and not concurrent:
        log(
            "RESULT: concurrency is the trigger — a solo write landed and the "
            "same write beside a second agy session did not. Promote it in "
            "AG-R-3 from candidate to cause."
        )
        return 3
    log(
        "RESULT: both landed. Concurrency is NOT the trigger either — record "
        "it as excluded in AG-R-3 so nobody spends another afternoon on it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
