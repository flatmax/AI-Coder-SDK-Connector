#!/usr/bin/env python3
"""Which tools does this `agy` have, and does our table know all of them?

[AG-R-2](../specs5/plan-ag/risks.md#ag-r-2) says an alpha breaks by renaming
rather than by erroring, and the mitigation it names is a probe whose
`unclassified` bucket must stay empty. That probe exists for the **wheel**
(`aic_dc.antigravity.surface`) and there has never been one for the **binary**,
which is the transport that actually reaches the user's subscription.

The gap is not theoretical. `agy` self-updated to 1.2.0 on 2026-09-10 and a
live turn immediately raised a permission dialog for **`schedule`** — a name
absent from `src/aic_dc/agy/tools.py`, so `GATED_BY_DEFAULT.get(None, True)`
gated it and the user was asked to approve a planning step that touches
nothing. That is phase 4's *"every read-only call raises a modal"* arriving
back through the half of its fix that is a table, and a table only knows the
names somebody wrote down.

**The inventory is free to read.** `agy`'s `init` frame carries the whole tool
list, and `init` arrives before any prompt is sent — so this takes no model
turn and costs nothing on the subscription. It spawns `agy` exactly as
`AgySession` does, reads one frame, and kills it.

Three buckets, and only one of them is a failure:

  - **unclassified** — on the binary, and in neither `TOOL_CLASSES` nor
    `SEEN_UNCLASSIFIED`. Every one raises a dialog the user cannot avoid and
    which says nothing useful, because `summarise_request` has no class to
    shape it with. Non-empty is the tripwire, and the bucket is empty **by
    declaration** rather than by neglect — the shape AG-R-2 specifies for the
    SDK probe, brought to the transport that had none.
  - **gone** — in `TOOL_CLASSES`, absent from the binary. Not a failure: a
    name we still gate that no longer exists costs nothing. Reported so the
    table can be pruned deliberately rather than left to rot.
  - **unguarded writes** — anything in `MUTATING_TOOLS` that the binary no
    longer offers, which is the direction that matters most if a *rename*
    rather than a removal is behind it: `write_to_file` becoming
    `write_file` would empty the write seam silently.

Usage:
    .venv/bin/python scripts/probe_agy_tool_inventory.py [--json]

Exit 0 when every advertised tool is classified, 1 when any is not, 2 when the
inventory could not be read.

Governing spec: `specs5/plan-ag/` — AG-2, AG-5, AG-R-2.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aic_dc.agy import tools as agy_tools  # noqa: E402
from aic_dc.antigravity.permissions import TOOL_CLASSES  # noqa: E402

INIT_TIMEOUT_SECONDS = 120.0


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def read_inventory(executable: str = "agy") -> tuple[str, list[str]]:
    """`agy`'s version and the tool list off its `init` frame.

    Spawned with the flags `AgySession._argv` uses, minus the workspace: no
    prompt is sent, so no tool can run and the working directory does not
    matter. Killed as soon as the frame arrives.
    """
    workspace = tempfile.mkdtemp(prefix="agy-inventory-")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=30
    ).stdout.strip()
    proc = subprocess.Popen(
        [
            executable,
            "--print=",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--add-dir", workspace,
        ],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.time() + INIT_TIMEOUT_SECONDS
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            init = frame.get("init") if isinstance(frame, dict) else None
            if not isinstance(init, dict):
                continue
            names = init.get("tools")
            if isinstance(names, list):
                return version, sorted(str(n) for n in names)
        raise SystemExit(
            f"{executable} sent no init frame carrying a tool list within "
            f"{INIT_TIMEOUT_SECONDS:.0f}s"
        )
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw buckets")
    args = parser.parse_args()

    if not shutil.which("agy"):
        log("no agy on PATH; nothing to read")
        return 2

    version, advertised = read_inventory()
    # The **merged** table, which is what `gated_by_default` reads: the SDK's
    # vocabulary is folded in underneath `agy`'s, so a name either product
    # classifies is classified for both. Asking `agy_tools.TOOL_CLASSES`
    # alone reported `finish` as unclassified when it has been `read` all
    # along, which is a false positive in the direction that wastes a
    # reader's time on a tool that never reached a dialog.
    known = set(TOOL_CLASSES)
    declared = set(agy_tools.SEEN_UNCLASSIFIED)
    unclassified = [
        name for name in advertised if name not in known and name not in declared
    ]
    # Pruning is asked of *this transport's* table only. The merged table
    # holds the SDK's names too — `edit_file`, `create_file`,
    # `start_subagent` — and those are not on the binary by design, so
    # reporting them as missing would be a standing false positive.
    gone = sorted(
        (set(agy_tools.TOOL_CLASSES) | declared) - set(advertised)
    )
    write_seam_gone = sorted(agy_tools.MUTATING_TOOLS - set(advertised))

    log(f"agy {version}: {len(advertised)} tools advertised")
    log(
        f"{len(known)} classified, {len(declared)} seen and deliberately not "
        f"classified"
    )

    if args.json:
        print(json.dumps(
            {
                "version": version,
                "advertised": advertised,
                "unclassified": unclassified,
                "gone": gone,
                "write_seam_missing_from_binary": write_seam_gone,
            },
            indent=2,
        ))

    if gone:
        log(f"in the table and not on the binary (prune deliberately): {gone}")
    if write_seam_gone:
        log(
            "WRITE SEAM: these are in MUTATING_TOOLS and the binary does not "
            f"offer them — check for a rename before assuming a removal: "
            f"{write_seam_gone}"
        )
    if unclassified:
        log(
            f"UNCLASSIFIED ({len(unclassified)}): every one of these gates by "
            f"default, so the user meets a dialog with no class to shape it"
        )
        for name in unclassified:
            log(f"  - {name}")
        return 1

    log("every advertised tool is classified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
