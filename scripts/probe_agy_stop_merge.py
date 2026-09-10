#!/usr/bin/env python3
"""When two `Stop` hooks disagree, which one does `agy` obey?

[AG-R-16](../specs5/plan-ag/risks.md#ag-r-16) is the risk that a `Stop` hook this
app does not own can **revive a turn the user stopped**. `agy`'s shipped
`hooks.md` documents `{"decision": "continue"}` as blocking a stop and re-entering
the loop, and says that multiple *named* hooks for one event are *"merged and
executed sequentially"* — and `~/.gemini/config/hooks.json` is a file this app
writes one entry into and does not own.

The mitigation shipped on 2026-09-11 with [AG-19](../specs5/plan-ag/decisions.md#ag-19):
`aic-dc-gate` now registers a `Stop` handler that always returns `{}`, so at least
one voice in the merge is always for stopping. **The risk records the load-bearing
part of that as unverified** — *"whether that wins against a concurrent `continue`
is unverified and worth a probe before relying on it"* — and it became load-bearing
the moment the handler shipped. This is that probe.

Two hooks are registered in one isolated `hooks.json`, both firing on the same
turn:

    aic-dc-stop  → {}                                    (ours: let it stop)
    rival-stop   → {"decision": "continue", "reason": …} (theirs: keep going)

and the question is only which answer the loop obeys. It is asked **both ways
round**, in two runs with the names swapped, because "merged and executed
sequentially" leaves the order unstated and last-write-wins would make the answer
depend on a JSON key order neither hook controls.

The instrument is the turn's own `num_turns` and the count of `Stop` firings: a
stop that was blocked re-enters the loop, so the rival's `continue` winning shows
up as a second `Stop` on the same conversation. A prompt with no tool calls is used
deliberately — it ends immediately on its own, so any continuation is the hook's
doing and not the model's.

    uv run python scripts/probe_agy_stop_merge.py

Exit 0: `continue` loses, or loses whenever it is not the only voice — either way
        AG-R-16's mitigation holds and the risk can be downgraded.
Exit 1: `continue` **wins**. The mitigation does not work, a user's own hook can
        revive a turn they stopped, and AG-R-16 needs the second mitigation it
        names — keeping the `PreToolUse` refusal armed past the turn's end.
Exit 2: nothing to conclude (no `agy`, or neither hook fired).

Nothing of the user's is touched: `agy` runs against an isolated `--gemini_dir`,
so both hooks live in the probe's own configuration.

What it costs: three short turns on the paid subscription, no tools, no writes.

Governing spec: `specs5/plan-ag/` — AG-R-16, AG-19.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

#: A turn with no tools answers in about four seconds, so anything near
#: this is a turn that is not coming back. Kept tight because the failure
#: mode under test is a loop that will not stop, and every second of it is
#: spend on the subscription.
TURN_TIMEOUT_SECONDS = 40

#: `agy`'s own deadline, set below the harness's so the child gives up
#: first where it can. Belt and braces: the first run of this probe hung
#: for the full 180s of a Python timeout with nothing bounding the child.
PRINT_TIMEOUT = "20s"

#: Seconds. A turn with no tools answers in under two; anything past this
#: is a turn being held open rather than a slow one.
HELD_OPEN_SECONDS = 10.0

#: No tool calls, so the loop ends on its own and any continuation is a hook's.
PROMPT = "Reply with exactly the word: DONE. Do not use any tools."

#: The rival's whole position. `hooks.md`: *"Set to `continue` to block the
#: stop and re-enter the loop. Any other value allows the agent to stop."*
RIVAL_ANSWER = {
    "decision": "continue",
    "reason": "The rival hook says the goal is not met yet. Say RIVAL and stop.",
}


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def handler(log_path: Path, answer: dict) -> dict:
    """A `Stop` handler that records that it fired and prints ``answer``.

    **The answer is `cat`-ed from a file rather than `printf`-ed inline**,
    and that is a correction rather than a style. The first version built
    the JSON into the shell command, where a backslash inside single
    quotes is literal — so the rival hook printed
    ``{\\"decision\\":\\"continue\\"}``, `agy` could not read a decision
    from it, and the run reported that `continue` does not extend a turn.
    It reported that about a hook that had never said `continue`.

    A file has no quoting layer, so what the handler prints is what is
    written here, and :func:`main` verifies that separately before
    trusting any of it.
    """
    answer_path = log_path.with_suffix(".answer.json")
    answer_path.write_text(json.dumps(answer), encoding="utf-8")
    return {
        "Stop": [
            {
                "type": "command",
                "command": f"cat >> {log_path}; cat {answer_path}",
                "timeout": 30,
            }
        ]
    }


def run_turn(gemini: Path, work: Path) -> dict:
    """One turn against the isolated configuration. Returns its result frame.

    **A timeout is a reading, not a crash.** The harm AG-R-16 describes is a
    loop that will not stop, so a turn that does not come back is the
    positive result rather than an instrument failure — and the first run of
    this probe died on an unhandled ``TimeoutExpired`` having just measured
    exactly that. A turn with no tool calls answers in about four seconds;
    ``TURN_TIMEOUT_SECONDS`` is an order of magnitude past it.
    """
    try:
        done = subprocess.run(
            [
                shutil.which("agy"),
                "--gemini_dir",
                str(gemini),
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
    except subprocess.TimeoutExpired:
        return {"status": "NEVER_STOPPED", "held_open": True}
    combined = done.stdout + done.stderr
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            # **`status` is not the signal, and reading it as one is what
            # made the first two runs of this probe disagree.** A turn held
            # open by a `continue` still reports `SUCCESS` once
            # `--print-timeout` expires — `agy` returns the partial output
            # it has and calls the turn successful. What separates a held
            # turn from a finished one is how long it took: measured, 1.6s
            # unopposed against 57s held.
            frame["held_open"] = (
                "turn in progress" in combined
                or float(frame.get("duration_seconds") or 0) >= HELD_OPEN_SECONDS
            )
            return frame
    return {
        "status": "UNREADABLE",
        "held_open": "turn in progress" in combined,
        "stdout": done.stdout[:400],
        "stderr": done.stderr[:400],
    }


def firings(path: Path) -> int:
    """How many payloads a handler's log collected. One per `Stop`."""
    if not path.exists():
        return 0
    return path.read_text(encoding="utf-8").count('"terminationReason"')


def one_case(root: Path, label: str, ours_first: bool) -> dict:
    """Register both hooks and take a turn. ``ours_first`` sets the key order."""
    gemini = root / label / "gemini"
    (gemini / "config").mkdir(parents=True, exist_ok=True)
    work = root / label / "work"
    work.mkdir(parents=True, exist_ok=True)

    ours_log = root / label / "ours.log"
    rival_log = root / label / "rival.log"
    ours = ("aic-dc-stop", handler(ours_log, {}))
    rival = ("rival-stop", handler(rival_log, RIVAL_ANSWER))
    entries = [ours, rival] if ours_first else [rival, ours]
    (gemini / "config" / "hooks.json").write_text(
        json.dumps(dict(entries), indent=2), encoding="utf-8"
    )

    result = run_turn(gemini, work)
    return {
        "label": label,
        "ours_first": ours_first,
        "status": result.get("status"),
        "held_open": bool(result.get("held_open")),
        "seconds": round(float(result.get("duration_seconds") or 0), 1),
        "num_turns": result.get("num_turns"),
        "response": (result.get("response") or "").strip()[:60],
        "ours_fired": firings(ours_log),
        "rival_fired": firings(rival_log),
    }


def baseline(root: Path) -> dict:
    """One run with **only** the rival, to prove its `continue` does anything.

    Without this the probe cannot tell "our `{}` won" from "`continue` is
    ignored on this build" — and those have opposite consequences for the
    risk. If the rival alone cannot extend a turn, nothing here is evidence
    about a merge.
    """
    gemini = root / "rival-only" / "gemini"
    (gemini / "config").mkdir(parents=True, exist_ok=True)
    work = root / "rival-only" / "work"
    work.mkdir(parents=True, exist_ok=True)
    rival_log = root / "rival-only" / "rival.log"
    (gemini / "config" / "hooks.json").write_text(
        json.dumps({"rival-stop": handler(rival_log, RIVAL_ANSWER)}, indent=2),
        encoding="utf-8",
    )
    result = run_turn(gemini, work)
    return {
        "label": "rival-only",
        "status": result.get("status"),
        "held_open": bool(result.get("held_open")),
        "seconds": round(float(result.get("duration_seconds") or 0), 1),
        "num_turns": result.get("num_turns"),
        "rival_fired": firings(rival_log),
    }


def main() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    root = Path(tempfile.mkdtemp(prefix="agy-stop-merge-"))
    log(f"isolated root: {root}")

    # Before anything else: prove the rival can say what it is meant to say.
    # The first run of this probe concluded that `continue` does not extend a
    # turn, from a hook whose shell quoting had turned its answer into
    # backslash-littered non-JSON. A probe that cannot show its own stimulus
    # arriving is measuring the stimulus, not the system.
    check = subprocess.run(
        handler(root / "selftest.log", RIVAL_ANSWER)["Stop"][0]["command"],
        shell=True,
        capture_output=True,
        text=True,
        input="{}",
        timeout=30,
    )
    try:
        spoken = json.loads(check.stdout)
    except ValueError:
        spoken = None
    if spoken != RIVAL_ANSWER:
        log(f"the rival hook does not print its own answer: {check.stdout[:200]!r}")
        return 2
    log("self-test: the rival hook prints valid `continue` JSON")

    alone = baseline(root)
    log(
        f"rival alone: held_open={alone['held_open']} ({alone['seconds']}s) "
        f"status={alone['status']} Stop fired {alone['rival_fired']}x"
    )

    cases = [
        one_case(root, "ours-first", ours_first=True),
        one_case(root, "rival-first", ours_first=False),
    ]
    for case in cases:
        log(
            f"{case['label']}: held_open={case['held_open']} "
            f"({case['seconds']}s) ours fired {case['ours_fired']}x, "
            f"rival {case['rival_fired']}x"
        )

    if not alone["rival_fired"] and not any(c["rival_fired"] for c in cases):
        log("no Stop handler fired at all — nothing to conclude")
        return 2

    # A blocked stop does not come back. Measured: an unopposed `continue`
    # turned a four-second turn into one that had not returned in 180
    # seconds, having fired `Stop` exactly once — so the loop is held open
    # rather than re-entered-and-re-asked, and the count of firings is not
    # the signal. Whether the turn *finishes* is.
    rival_alone_held = alone["held_open"]
    merged_held = [c for c in cases if c["held_open"]]
    silenced = [c for c in cases if not c["ours_fired"]]
    if silenced:
        log("")
        log(
            "Note: this app's own Stop handler did not run at all in "
            + ", ".join(c["label"] for c in silenced)
            + " — a `continue` short-circuits the handlers after it, so "
            "being registered is not the same as being asked."
        )

    log("")
    if not rival_alone_held:
        log(
            "INCONCLUSIVE about the merge: the rival's `continue` did not "
            "hold open even a turn it was alone on, so this build does not "
            "honour it and a merge cannot be tested against it. AG-R-16's "
            "mechanism is not reproducible here — which is worth recording, "
            "and is not the same as our handler winning."
        )
        return 2

    if merged_held:
        orders = ", ".join(c["label"] for c in merged_held)
        log(
            f"FAIL — a rival `continue` held the turn open with our `{{}}` in "
            f"the merge ({orders}). AG-R-16's first mitigation does not hold; "
            f"the second one it names — keep the PreToolUse refusal armed past "
            f"the turn's end — is the load-bearing half, and the risk should "
            f"say so."
        )
        return 1

    log(
        "PASS — the rival's `continue` holds a turn open when it is alone and "
        "does not when our `{}` is beside it, in both key orders. One voice "
        "for stopping is enough, which is what AG-R-16's mitigation assumed "
        "and could not show."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
