#!/usr/bin/env python3
"""⏹ says stop and a stranger's hook says keep going. Who wins?

The question [AG-R-16](../specs5/plan-ag/risks.md#ag-r-16) was left holding on
2026-09-12, and the reason it is not academic:

`AgySession` passes `--print-timeout 12h` and **nothing else in this app bounds a
turn's wall clock** — `INIT_TIMEOUT_SECONDS` covers only the handshake. The same
day's `probe_agy_stop_merge.py` showed that a `Stop` hook this app does not own,
answering `{"decision": "continue"}`, holds a turn open and beats this app's own
`Stop` handler in either order. So a user with any third-party `Stop` hook — their
own, or one arriving inside a plugin — can have an AIC-DC turn held open for up to
twelve hours.

Whether ⏹ can still end it comes down to one untested interaction.
[AG-19](../specs5/plan-ag/decisions.md#ag-19)'s stop is a **`PostInvocation`**
handler answering `terminationBehavior: "terminate"`, which is a different event
from the `Stop` the rival answers. The two could plausibly ping-pong: terminate
ends the loop, the rival's `continue` blocks the stop and re-enters it, terminate
ends it again. This measures which one the loop obeys.

Four runs, one prompt, hooks written straight into an isolated `hooks.json` — no
host and no socket, because the question is about `agy`'s precedence and not about
this app's plumbing:

    terminate only   → the AG-19 stop, unopposed. The control for "terminate works"
    continue only    → the rival, unopposed. The control for "continue works"
    both, ours first → the question
    both, rival first → the question again, because merge order is unstated

**Both controls are required.** A run where the turn ends proves nothing unless
`continue` is known to hold it open in this rig, and a run where it hangs proves
nothing unless `terminate` is known to end it.

    uv run python scripts/probe_agy_terminate_vs_continue.py

Exit 0: `terminate` wins — ⏹ survives a hostile `Stop` hook, and AG-R-16 can be
        downgraded to spend-and-prose.
Exit 1: `continue` wins — ⏹ can be defeated by a config the user may not know they
        have, for as long as `--print-timeout` allows. A wall-clock ceiling on a
        turn stops being a design question and becomes a defect fix.
Exit 2: nothing to conclude (no `agy`, or a control did not behave).

Nothing of the user's is touched: an isolated `--gemini_dir` holds both hooks.

What it costs: four short turns on the paid subscription, two of which run to a
20-second print timeout by construction.

Governing spec: `specs5/plan-ag/` — AG-R-16, AG-19.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

TURN_TIMEOUT_SECONDS = 60
PRINT_TIMEOUT = "20s"

#: Seconds. A turn on this prompt lands in a few; anything past this is a
#: turn being held open rather than a slow one.
HELD_OPEN_SECONDS = 10.0

#: Enough steps that a loop has somewhere to go after the first invocation,
#: so `terminate` has something to cut short and its effect is visible as a
#: count rather than inferred.
PROMPT = (
    "Do these one at a time, each with its own separate tool call, and do not "
    "stop early. 1. List the files here. 2. Run `date`. 3. Run `uname -a`. "
    "4. Run `echo one`. 5. Run `echo two`. Then reply with: FINISHED."
)

TERMINATE = {"terminationBehavior": "terminate"}
CONTINUE = {
    "decision": "continue",
    "reason": "The rival hook says the goal is not met. Keep working.",
}


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def command_for(log_path: Path, answer: dict) -> str:
    """A hook command that records its payload and prints ``answer``.

    ``cat`` from a file rather than ``printf`` inline: a backslash inside
    single quotes is literal in ``sh``, and building JSON into the command
    is what made ``probe_agy_stop_merge.py`` report a result about a hook
    that had never said what it was meant to say.
    """
    answer_path = log_path.with_suffix(".answer.json")
    answer_path.write_text(json.dumps(answer), encoding="utf-8")
    return f"cat >> {log_path}; cat {answer_path}"


def flat(log_path: Path, answer: dict) -> list:
    """A handler list for a flat event — ``PostInvocation`` or ``Stop``."""
    return [{"type": "command", "command": command_for(log_path, answer), "timeout": 30}]


def self_test(root: Path) -> bool:
    """Prove each stimulus prints the JSON it is meant to, before trusting it."""
    for name, answer in (("terminate", TERMINATE), ("continue", CONTINUE)):
        done = subprocess.run(
            command_for(root / f"selftest-{name}.log", answer),
            shell=True,
            capture_output=True,
            text=True,
            input="{}",
            timeout=30,
        )
        try:
            spoken = json.loads(done.stdout)
        except ValueError:
            spoken = None
        if spoken != answer:
            log(f"the {name} hook does not print its own answer: {done.stdout[:200]!r}")
            return False
    log("self-test: both hooks print the JSON they are meant to")
    return True


def run_case(root: Path, label: str, *, ours: bool, rival: bool, ours_first: bool = True) -> dict:
    """One turn with the named hooks registered. ``ours`` is AG-19's stop."""
    home = root / label
    (home / "gemini" / "config").mkdir(parents=True, exist_ok=True)
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)

    ours_log, rival_log = home / "ours.log", home / "rival.log"
    entries = []
    if ours:
        # This app's two handlers, as `install.hook_entry` writes them: the
        # AG-19 stop, and the Stop handler that always permits the stop.
        entries.append(
            (
                "aic-dc-gate",
                {
                    "PostInvocation": flat(ours_log, TERMINATE),
                    "Stop": flat(home / "ours-stop.log", {}),
                },
            )
        )
    if rival:
        entries.append(("rival-stop", {"Stop": flat(rival_log, CONTINUE)}))
    if not ours_first:
        entries.reverse()
    (home / "gemini" / "config" / "hooks.json").write_text(
        json.dumps(dict(entries), indent=2), encoding="utf-8"
    )

    try:
        done = subprocess.run(
            [
                shutil.which("agy"),
                "--gemini_dir",
                str(home / "gemini"),
                "--add-dir",
                str(work),
                "--dangerously-skip-permissions",
                "--print-timeout",
                PRINT_TIMEOUT,
                "-p",
                PROMPT,
                "--output-format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=TURN_TIMEOUT_SECONDS,
            cwd=str(work),
        )
        combined = done.stdout + done.stderr
        frame = {}
        for line in done.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    frame = json.loads(line)
                    break
                except ValueError:
                    continue
        seconds = float(frame.get("duration_seconds") or 0)
        held = "turn in progress" in combined or seconds >= HELD_OPEN_SECONDS
    except subprocess.TimeoutExpired:
        frame, seconds, held = {"status": "NEVER_RETURNED"}, TURN_TIMEOUT_SECONDS, True

    return {
        "label": label,
        "held_open": held,
        "seconds": round(seconds, 1),
        "status": frame.get("status"),
        # One `PostInvocation` per invocation, so this counts the loop.
        "invocations": _count(ours_log),
        "rival_stops": _count(rival_log),
    }


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    return path.read_text(encoding="utf-8").count('"conversationId"')


def main() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    root = Path(tempfile.mkdtemp(prefix="agy-terminate-vs-continue-"))
    log(f"isolated root: {root}")
    if not self_test(root):
        return 2

    runs = {
        "terminate-only": run_case(root, "terminate-only", ours=True, rival=False),
        "continue-only": run_case(root, "continue-only", ours=False, rival=True),
        "both-ours-first": run_case(
            root, "both-ours-first", ours=True, rival=True, ours_first=True
        ),
        "both-rival-first": run_case(
            root, "both-rival-first", ours=True, rival=True, ours_first=False
        ),
    }
    for name, run in runs.items():
        log(
            f"{name:18} held_open={run['held_open']!s:5} {run['seconds']:>5}s  "
            f"invocations={run['invocations']}  rival_stops={run['rival_stops']}  "
            f"status={run['status']}"
        )

    log("")
    terminate_works = not runs["terminate-only"]["held_open"]
    continue_works = runs["continue-only"]["held_open"]
    if not terminate_works:
        log("CONTROL FAILED: `terminate` did not end an unopposed loop. Nothing follows.")
        return 2
    if not continue_works:
        log("CONTROL FAILED: `continue` did not hold an unopposed loop open. Nothing follows.")
        return 2

    contested = [runs["both-ours-first"], runs["both-rival-first"]]
    lost = [run for run in contested if run["held_open"]]
    if lost:
        log(
            "FAIL — `continue` wins: "
            + ", ".join(f"{r['label']} held open at {r['seconds']}s" for r in lost)
            + ". ⏹ can be defeated by a third-party Stop hook for as long as "
            "--print-timeout allows, which is 12h in AgySession. A wall-clock "
            "ceiling on a turn is now a defect fix rather than a design question."
        )
        return 1

    log(
        "PASS — `terminate` wins in both merge orders. ⏹ survives a hostile "
        "`Stop` hook: the loop ends even while another hook is blocking the "
        "stop, so AG-R-16's harm stays bounded to spend and prose."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
