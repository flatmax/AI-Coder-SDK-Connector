#!/usr/bin/env python3
"""Does ⏹ *end* an `agy` turn, or only ask it to stop?

The measurement [AG-19](../specs5/plan-ag/decisions.md#ag-19) is built on, run
against the shipped code rather than against a hand-rolled hooks file.

Until AG-19 this transport stopped a turn by **starving** it: from the moment
⏹ is pressed the gate refuses every tool call with a reason naming the user's
stop, and the agent reads those refusals and winds down. That is a model's
judgement standing where a mechanism should be, and it has a hole its own
docstring names — a turn producing only prose asks permission for nothing and
cannot be starved at all.

What replaced it is a `PostInvocation` handler on the gate socket answering
`terminationBehavior: "terminate"` while the same latch is set. This probe is
the difference between the two, measured on one prompt.

**The control is the whole instrument.** A stopped turn ending is not evidence
of anything: a cooperative agent ends a stopped turn too, which is precisely
what starvation relies on. So the same prompt is run twice, stopped at the same
point, against the same gate — and the *only* difference is what the host
answers at `PostInvocation`:

    control : `{}`                                 — starvation alone, as before
    armed   : `{"terminationBehavior": "terminate"}` — AG-19

If the armed run ends in fewer invocations than the control, the termination
did it. If both end in one, the agent was cooperative and this prompt has
measured nothing — which is reported as *inconclusive* rather than as a pass,
because a green result that would be green either way is the failure mode this
control exists to prevent.

Three further readings come free, and each closes something the offline tests
structurally cannot:

  * **The footer.** A loop ended this way reports `status: "SUCCESS"` with empty
    prose. The browser must not draw that as a completed answer, so
    `streamComplete` has to carry `cancelled` — asserted on the real frame
    rather than on a translator fed a fixture.
  * **`terminationReason`.** The `Stop` payload should say `TERMINAL_CUSTOM_HOOK`
    on the armed run and something else on the control. That is the field
    [AG-R-16](../specs5/plan-ag/risks.md#ag-r-16) turns on, and it exists nowhere on
    the stream.
  * **The tree is still protected.** Every tool call after the stop must still
    be denied, on both runs. The termination is a second layer, not a
    replacement, and a probe that only counted invocations would not notice
    the gate going quiet.

Nothing of the user's is touched. `agy` runs against an isolated
`--gemini_dir`, so the hooks file this installs into is the probe's own and the
user's `~/.gemini/config/hooks.json` is neither read nor written. The working
directory is a throwaway, and the prompt asks for reads and shell commands
rather than writes — so nothing here depends on `trustedWorkspaces` or on
AG-R-3's diversion.

    uv run python scripts/probe_agy_stop_terminates.py

Exit 0: the armed run ended in fewer invocations, and every other reading held.
Exit 1: it did not — either the termination changed nothing, or the footer, the
        termination reason or the gate's refusals did not hold.
Exit 2: nothing to conclude (no `agy`, the hook never fired, or the prompt was
        cooperative enough that the control ended immediately too).

What it costs: two turns on the paid subscription.

Governing spec: `specs5/plan-ag/` — AG-19, AG-14, AG-5; `risks.md` AG-R-16.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.agy.steps import AgyTranslator  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

TURN_TIMEOUT_SECONDS = 420.0

#: Chosen to keep an agent busy across several invocations, and to need no
#: write: the point is a turn with plenty left to do at the moment it is
#: stopped, so that "it ended" is a fact about the stop rather than about
#: the task running out.
PROMPT = (
    "Work through this list one step at a time, in order, using a separate "
    "tool call for each step, and do not stop until every step is done. "
    "1. List the files in this directory. "
    "2. Read notes/note-01.md. "
    "3. Read notes/note-02.md. "
    "4. Read notes/note-03.md. "
    "5. Run `wc -l notes/note-01.md`. "
    "6. Run `wc -l notes/note-02.md`. "
    "7. Run `date`. "
    "8. Run `uname -a`. "
    "Then reply with a one-line summary of what you found."
)


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The shipped gate, with every answer it gave written down.

    Subclassed rather than monkeypatched so what runs is what ships. The
    ``terminate`` override is the control's only lever, and it is applied
    *after* ``super()`` so the latch is still read, the record is still
    written, and the single thing that differs between the two runs is the
    string that reaches ``agy``.
    """

    def __init__(self, *args: Any, arm: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.arm = arm
        self.tool_calls: list[tuple[str, str]] = []
        self.invocations: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.on_first_call: Any = None

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        call = payload.get("toolCall") or {}
        result = await super().decide(payload)
        self.tool_calls.append(
            (str(call.get("name") or "?"), str(result.get("decision") or "?"))
        )
        if len(self.tool_calls) == 1 and self.on_first_call is not None:
            # The stop is pressed here rather than on a timer: it makes the
            # two runs comparable, because both are stopped with the same
            # amount of work left rather than after the same number of
            # seconds.
            await self.on_first_call()
        return result

    def decide_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The control's one lever, and it does **not** call ``super``.

        Answering the real decision and then discarding it would leave the
        server's own ``was_terminated`` record saying the control had
        terminated a loop it never told ``agy`` to end — which is a lie in
        the instrument, and the first version of this probe failed its own
        control on it. The pre-AG-19 world is the latch existing, the tool
        calls being refused by it, and nothing acting on it here.
        """
        sent = {} if not self.arm else super().decide_invocation(payload)
        self.invocations.append(
            {
                "num": payload.get("invocationNum"),
                "sent": dict(sent),
                "armed": self.arm,
            }
        )
        return sent

    def note_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.stops.append(dict(payload))
        return super().note_stop(payload)


def isolated_agy(root: Path) -> tuple[Path, Path]:
    """An ``agy`` that reads its whole configuration out of ``root``.

    Returns the launcher and the hooks file inside it. The launcher is what
    :class:`AgySession` spawns, so everything the session does — the argv,
    the scope, the handshake — is the shipped path; only the configuration
    root is moved out from under the user's.
    """
    gemini = root / "gemini"
    (gemini / "config").mkdir(parents=True, exist_ok=True)
    launcher = root / "agy"
    launcher.write_text(
        f'#!/bin/sh\nexec {shutil.which("agy")} --gemini_dir {gemini} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, gemini / "config" / "hooks.json"


async def one_run(root: Path, launcher: Path, hooks: Path, *, arm: bool) -> dict:
    """One stopped turn. ``arm`` is the only thing that differs between them."""
    label = "armed" if arm else "control"
    work = root / label
    notes = work / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(1, 4):
        (notes / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n\nThe shelf holds {index} jars.\n", encoding="utf-8"
        )
    subprocess.run(
        ["git", "init", "-q", str(work)], capture_output=True, timeout=30, check=False
    )

    config_dir = root / f"cfg-{label}"
    config_dir.mkdir(parents=True, exist_ok=True)
    report = install.install(config_dir, path=hooks)
    if report.get("state") != "current":
        raise RuntimeError(f"the probe's own gate would not install: {report}")

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = RecordingGateServer(
        work / "gate.sock", gate=gate, config_dir=config_dir, arm=arm
    )
    session = AgySession(work, gate=server, executable=str(launcher))

    async def allow_everything() -> None:
        while True:
            for pending in gate.broker.pending():
                await gate.broker.resolve(
                    pending["permission_id"], {"action": "allow"}, resolved_by="probe"
                )
            await asyncio.sleep(0.05)

    dialogs = asyncio.ensure_future(allow_everything())
    translator = AgyTranslator("probe")
    footer: dict[str, Any] = {}
    try:
        await session.start()
        server.on_first_call = session.cancel
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for event in session.stream_turn(PROMPT, translator=translator):
                if event.name == "streamComplete":
                    footer = dict(event.payload)
    finally:
        dialogs.cancel()
        await session.close()

    return {
        "label": label,
        "invocations": len(server.invocations),
        "terminated": any(i["sent"] for i in server.invocations),
        "tool_calls": server.tool_calls,
        "denied_after_stop": [d for _t, d in server.tool_calls[1:]],
        "stop_reasons": [str(s.get("terminationReason") or "") for s in server.stops],
        "cancelled": footer.get("cancelled"),
        "response": (footer.get("response_text") or "")[:120],
    }


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    root = Path(tempfile.mkdtemp(prefix="agy-stop-terminates-"))
    log(f"isolated root: {root}")
    launcher, hooks = isolated_agy(root)

    results = {}
    for arm in (False, True):
        label = "armed" if arm else "control"
        log(f"--- {label} run: PostInvocation answers "
            f"{'terminate' if arm else '{}'} ---")
        results[label] = await one_run(root, launcher, hooks, arm=arm)
        log(f"{label}: {results[label]['invocations']} invocations, "
            f"{len(results[label]['tool_calls'])} tool calls, "
            f"terminated={results[label]['terminated']}, "
            f"cancelled={results[label]['cancelled']}, "
            f"stop={results[label]['stop_reasons']}")

    control, armed = results["control"], results["armed"]

    if not control["tool_calls"] and not armed["tool_calls"]:
        log("the hook never fired — the gate saw no tool call on either run")
        return 2

    checks: list[tuple[str, bool, str]] = [
        (
            "the armed run answered terminate",
            armed["terminated"],
            f"invocations={armed['invocations']}",
        ),
        (
            "the control never terminated",
            not control["terminated"],
            "the control is only a control if it sent {} throughout",
        ),
        (
            "the armed run ended in fewer invocations",
            armed["invocations"] < control["invocations"],
            f"armed={armed['invocations']} control={control['invocations']}",
        ),
        (
            "the armed footer says cancelled",
            armed["cancelled"] is True,
            f"cancelled={armed['cancelled']!r}",
        ),
        (
            "the control footer says cancelled too",
            control["cancelled"] is True,
            "a starved turn is still a turn the user stopped",
        ),
        (
            "the armed loop ended by a hook",
            "TERMINAL_CUSTOM_HOOK" in armed["stop_reasons"],
            f"stop_reasons={armed['stop_reasons']}",
        ),
        (
            "the control loop did not",
            "TERMINAL_CUSTOM_HOOK" not in control["stop_reasons"],
            f"stop_reasons={control['stop_reasons']}",
        ),
        (
            "every call after the stop was denied, on both runs",
            set(armed["denied_after_stop"]) <= {"deny"}
            and set(control["denied_after_stop"]) <= {"deny"},
            f"armed={armed['denied_after_stop']} control={control['denied_after_stop']}",
        ),
    ]

    if control["invocations"] <= 1:
        log(
            "INCONCLUSIVE: the control ended in one invocation too, so this "
            "prompt cannot tell a termination from a cooperative agent. "
            "Nothing is proved either way."
        )
        return 2

    failed = [name for name, ok, _detail in checks if not ok]
    for name, ok, detail in checks:
        log(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}")
    log(f"control tool calls: {control['tool_calls']}")
    log(f"armed   tool calls: {armed['tool_calls']}")
    if failed:
        log(f"FAIL — {len(failed)} of {len(checks)} checks did not hold")
        return 1
    log(f"PASS — all {len(checks)} checks held")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
