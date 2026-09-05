#!/usr/bin/env python3
"""The user's own ``agy`` session, running beside ours and never touched.

This is the half of phase 8's exit criterion that had no test at all, and
it is the one that decides whether the gate is shippable rather than
whether it works.

The reason it matters more than it looks
----------------------------------------
Workspace-local ``hooks.json`` is not loaded headlessly on ``agy`` 1.1.25,
so the gate has to live in the user's **global** ``~/.gemini/config/hooks.json``,
which ``agy``'s own documentation says *"fires unconditionally"*. From the
moment it is installed, every tool call in every ``agy`` session on the
machine — including the interactive one the user is running for their own
work, in another repository, with no idea this app exists — is handed to
our hook process.

Two failures are available there and both are unacceptable in a way that
"our gate did not fire" is not:

- **Interception.** A permission dialog from *our* app appears over
  somebody else's conversation, or worse, silently denies it.
- **Stalling.** The hook blocks waiting for a host that is not gating that
  session, and the user's turn hangs for an hour on
  :data:`aic_dc.agy.hook.SOCKET_TIMEOUT_SECONDS`.

``registry.lookup`` returning ``None`` for an unclaimed conversation is
what prevents both, and every test of it so far has been a unit test
calling it directly. This runs the real thing: two ``agy`` processes at
once, one claimed and one not, with the real hook in the real global
configuration in between.

What is asserted
----------------
1. **The stranger's calls never reached our gate** — nothing in the gate
   server's record carries their conversation id.
2. **The stranger's work completed**, and its file is on disk. Not stalled,
   not denied.
3. **Our own call did reach the gate** in the same window. Without this
   the probe passes trivially whenever the hook is misconfigured, which is
   exactly the condition it is meant to detect — the same "passed for the
   wrong reason" shape as the deny probe's first version.
4. **The two overlapped in time.** Sequential sessions would not test
   concurrency, and the registry is keyed per conversation precisely so
   that simultaneous ones can disagree about ownership.

The stranger is driven as a bare subprocess rather than through
:class:`~aic_dc.agy.session.AgySession`, deliberately: it must be a
session this app has nothing to do with, and using our own driver would
claim it.

What it costs: two turns on the paid subscription, one of them ours.

    uv run python scripts/probe_agy_isolation.py

Exit 0 on PASS.

Governing spec: ``specs5/plan-ag/`` — AG-14; ``risks.md`` AG-R-12.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402
from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.agy.steps import AgyTranslator  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

STRANGER_MARKER = "STRANGER_WAS_HERE"
TURN_TIMEOUT_SECONDS = 300.0


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The real gate, recording the **conversation id** of everything it sees.

    The id is the whole assertion here, not the tool name: the question is
    not "what was asked about" but "whose call was it".
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[dict[str, Any]] = []

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = await super().decide(payload)
        self.seen.append(
            {
                "conversation_id": payload.get("conversationId"),
                "tool": (payload.get("toolCall") or {}).get("name"),
                "decision": answer.get("decision"),
            }
        )
        return answer


async def answer_dialogs(broker: Any) -> None:
    """Allow whatever ours raises. The dialog is not what is under test here."""
    while True:
        for payload in broker.pending():
            permission_id = payload.get("permission_id")
            if permission_id:
                await broker.resolve(
                    permission_id, {"action": "allow"}, resolved_by="probe"
                )
        await asyncio.sleep(0.05)


class Stranger:
    """An ``agy`` session belonging to the user, driven by nobody's adapter.

    Spawned exactly as a person's own headless run would be, in its own
    directory, and never claimed in the registry. ``--dangerously-skip-permissions``
    is passed for the same reason the adapter passes it — without it
    ``agy``'s headless layer soft-denies the write and the session would
    finish without ever proposing a tool call, so the probe would prove
    nothing.
    """

    #: Seeded, then edited rather than created. A file `agy` is asked to
    #: *create* by bare name does not land in the session's cwd — measured
    #: three times, see `_agy_probe_support` — so a probe that needs a
    #: write to be observable has to give it an existing file to change.
    SEED = "STRANGER_BEFORE"

    def __init__(self, work: Path) -> None:
        self.work = work
        self.target = work / "stranger.txt"
        self.target.write_text(self.SEED + "\n", encoding="utf-8")
        self.conversation_id: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._proc: Any = None

    async def start(self) -> str:
        self._proc = await asyncio.create_subprocess_exec(
            "agy",
            "--print=",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--print-timeout", "10m",
            cwd=str(self.work),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                raise RuntimeError("the stranger's agy exited before its init frame")
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("event") == "init":
                self.conversation_id = str(frame.get("conversation_id") or "")
                return self.conversation_id

    async def turn(self, prompt: str) -> str:
        """One turn, to completion. Returns the run's status."""
        self.started_at = time.monotonic()
        self._proc.stdin.write(
            json.dumps(
                {"event": "user", "message": {"role": "user", "content": prompt}}
            ).encode("utf-8")
            + b"\n"
        )
        await self._proc.stdin.drain()
        status = ""
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("event") == "result":
                status = str((frame.get("result") or {}).get("status") or "")
                break
        self.finished_at = time.monotonic()
        return status

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001 - teardown must not raise
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    # Inside a workspace agy trusts, or it writes to its scratch directory
    # and reports success — see `_agy_probe_support`. The first run of this
    # probe used plain /tmp and failed on "the stranger's file was never
    # written" while the gate had behaved perfectly.
    ours = probe_root("agy-iso-ours-")
    theirs = probe_root("agy-iso-theirs-")
    (ours / "target.txt").write_text("OURS\n", encoding="utf-8")

    install_config_dir = Path.home() / ".config" / "aic-dc"
    state = install.status(install_config_dir)
    log(f"installed gate: {state['state']} ({state.get('path')})")
    installed_here = False
    if state["state"] != "current":
        log("installing the gate for the duration of this probe")
        install.install(install_config_dir)
        installed_here = True

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        ours, broadcast=broadcast, localhost_available=lambda: True
    )
    server = RecordingGateServer(
        ours / "gate.sock", gate=gate, config_dir=install_config_dir
    )
    session = AgySession(ours, gate=server)
    stranger = Stranger(theirs)

    browser = asyncio.ensure_future(answer_dialogs(gate.broker))
    our_window: tuple[float, float] | None = None
    stranger_status = ""
    try:
        our_id = await session.start()
        their_id = await stranger.start()
        log(f"ours     : {our_id}")
        log(f"stranger : {their_id}")
        if our_id == their_id:
            log("FAIL: both sessions reported the same conversation id")
            return 1

        # Both turns in flight together. The stranger's is the shorter of
        # the two by design — if it finishes while ours is still running,
        # the overlap is certain rather than merely likely.
        async def ours_turn() -> None:
            nonlocal our_window
            start = time.monotonic()
            translator = AgyTranslator("probe-iso")
            async for _event in session.stream_turn(
                "Read target.txt in the current directory, then append a "
                "second line reading OURS_AGAIN to it.",
                translator=translator,
            ):
                pass
            our_window = (start, time.monotonic())

        async def their_turn() -> None:
            nonlocal stranger_status
            stranger_status = await stranger.turn(
                f"In stranger.txt in the current directory, replace "
                f"{Stranger.SEED} with {STRANGER_MARKER}. Make only that one "
                f"edit, and do not search outside the current directory."
            )

        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            await asyncio.gather(ours_turn(), their_turn())
    except TimeoutError:
        # A stalled stranger is one of the two failures this exists to
        # catch, so it is named rather than reported as a flake.
        log(
            f"FAIL: the two turns did not both finish within "
            f"{TURN_TIMEOUT_SECONDS:.0f}s — if the stranger is the one still "
            f"waiting, the hook is stalling a session it does not own"
        )
        return 1
    finally:
        browser.cancel()
        await session.close()
        await stranger.close()
        await server.stop()
        if installed_here:
            install.uninstall()

    code = check(server, stranger, our_window, stranger_status)
    shutil.rmtree(ours, ignore_errors=True)
    shutil.rmtree(theirs, ignore_errors=True)
    return code


def check(
    server: RecordingGateServer,
    stranger: Stranger,
    our_window: tuple[float, float] | None,
    stranger_status: str,
) -> int:
    ids = {entry["conversation_id"] for entry in server.seen}
    intercepted = [
        entry
        for entry in server.seen
        if entry["conversation_id"] == stranger.conversation_id
    ]
    log(f"the gate decided {len(server.seen)} call(s), for conversation(s) {ids}")
    log(f"stranger's run status: {stranger_status or '(none)'}")

    if intercepted:
        # The failure the global install risks. Loud, and worth the detail:
        # this would mean a stranger's session was gated by our dialog.
        log(
            f"FAIL: {len(intercepted)} of the stranger's tool calls reached our "
            f"gate — {[e['tool'] for e in intercepted]}"
        )
        return 1

    if not stranger.target.is_file():
        log(
            "FAIL: the stranger's seeded file is gone — its session was "
            "blocked or abandoned while our gate was installed"
        )
        if stranger_status == "SUCCESS":
            # Distinguishes the two very different causes. agy reporting
            # success with no file is AG-R-3, not a gate that blocked the
            # stranger, and the two would otherwise read identically here.
            log(
                "       (but agy reported SUCCESS — check "
                "~/.gemini/antigravity-cli/scratch/ for the file: that is "
                "AG-R-3 diversion, i.e. this probe ran outside a trusted "
                "workspace, not a gate failure)"
            )
        return 1
    content = stranger.target.read_text(encoding="utf-8")
    if STRANGER_MARKER not in content:
        log(f"FAIL: the stranger's file reads {content.strip()!r}")
        if Stranger.SEED in content:
            # Still the seed: the edit never landed here. Either the
            # session was interfered with, or agy diverted the write —
            # and the first assertion above has already ruled out our
            # gate having seen the call at all.
            log(
                "       (unchanged from the seed, and our gate never saw this "
                "conversation — check ~/.gemini/antigravity-cli/scratch/ for "
                "a diverted copy before suspecting interception)"
            )
        return 1

    if not server.seen:
        # The control. Without a call of our own the first assertion is
        # vacuous: a hook that is not installed intercepts nothing either.
        log(
            "FAIL: our own session raised no tool call, so 'the stranger was "
            "not intercepted' proves nothing — the hook may simply be inert"
        )
        return 1

    if our_window is None:
        log("FAIL: our turn did not complete, so there was no window to overlap")
        return 1
    if stranger.started_at is None or stranger.finished_at is None:
        log("FAIL: the stranger's turn was never timed")
        return 1
    overlap = min(our_window[1], stranger.finished_at) - max(
        our_window[0], stranger.started_at
    )
    if overlap <= 0:
        log(
            f"FAIL: the two turns did not overlap ({overlap:.1f}s), so "
            f"concurrency was not tested"
        )
        return 1

    log(
        f"PASS: {len(server.seen)} call(s) of ours gated, 0 of the stranger's, "
        f"its work completed, {overlap:.1f}s of overlap"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
