#!/usr/bin/env python3
"""Where does ``agy`` actually put a file — created versus edited?

This settles a question three phase-8 probe runs raised and none of them
isolated. [`risks.md` AG-R-3](../specs5/plan-ag/risks.md#ag-r-3) attributes
``agy``'s silent write-diversion into ``~/.gemini/antigravity-cli/scratch/``
to a workspace missing from ``trustedWorkspaces``. On 2026-09-05 writes
diverted from **inside** a trusted root, so that explanation is at best
incomplete, and the pattern across every diverted file on record is that it
was **newly created** rather than edited.

That was a reading of accumulated evidence, never a measurement — the two
cases differed in more than one way each time. This probe removes the other
variables: **one session, one workspace, one turn**, asked to do both
things. Whatever differs between the two files is caused by
create-versus-edit and by nothing else.

It deliberately runs **without the gate**, because the gate is not the
subject and involving it would add a variable back.

    uv run python scripts/probe_agy_write_target.py

Exit 0 if both landed where asked, 3 if the created file was diverted while
the edit landed (which confirms the reading), 1 on anything else.

What it costs: one turn on the paid subscription.
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
SEED = "EDIT_ME_BEFORE"
EDITED = "EDIT_ME_AFTER"
CREATED_NAME = "created_by_agy.txt"
CREATED_BODY = "CREATED_CONTENT"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    work = probe_root("agy-target-")
    existing = work / "existing.txt"
    existing.write_text(SEED + "\n", encoding="utf-8")
    created = work / CREATED_NAME

    # Anything already in scratch under our names would make the result
    # ambiguous, and scratch is not cleaned by anyone.
    stale = SCRATCH / CREATED_NAME
    stale_before = stale.exists()

    log(f"workspace: {work}")
    proc = await asyncio.create_subprocess_exec(
        "agy",
        "--print=",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout", "10m",
        cwd=str(work),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    status = ""
    try:
        # Drain to init so the session is up before the prompt.
        while True:
            line = await proc.stdout.readline()
            if not line:
                log("FAIL: agy exited before its init frame")
                return 1
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("event") == "init":
                log(f"conversation {frame.get('conversation_id')}")
                break

        proc.stdin.write(
            json.dumps(
                {
                    "event": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f"Do exactly two things in the current directory, "
                            f"and nothing else. First, in existing.txt replace "
                            f"{SEED} with {EDITED}. Second, create a new file "
                            f"called {CREATED_NAME} containing exactly the line "
                            f"{CREATED_BODY}. Do not search outside the current "
                            f"directory."
                        ),
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        await proc.stdin.drain()
        proc.stdin.close()

        async with asyncio.timeout(300):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if frame.get("event") == "result":
                    status = str((frame.get("result") or {}).get("status") or "")
                    break
    except TimeoutError:
        log("FAIL: the turn did not finish in 300s")
        return 1
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    edit_landed = existing.is_file() and EDITED in existing.read_text("utf-8")
    create_landed = created.is_file()
    diverted = stale.exists() and not stale_before

    log(f"run status               : {status}")
    log(f"edit landed in workspace : {edit_landed}")
    log(f"create landed in workspace: {create_landed}")
    log(f"created file in scratch/  : {diverted}")

    shutil.rmtree(work, ignore_errors=True)

    if edit_landed and create_landed:
        log(
            "RESULT: both landed. The create/edit reading is WRONG — update "
            "AG-R-3 and the delivery note, and look for another variable."
        )
        return 0
    if edit_landed and not create_landed:
        log(
            "RESULT: the edit landed and the create did not — the "
            "create-versus-edit reading is CONFIRMED inside a trusted "
            "workspace. AG-R-3's stated cause is wrong and its mitigation "
            "must test creation specifically."
        )
        return 3
    log("RESULT: inconclusive — the edit itself did not land, so the turn is not a clean control")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
