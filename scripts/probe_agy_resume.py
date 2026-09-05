#!/usr/bin/env python3
"""Phase 5's exit criterion, run against the real subscription.

    *Restarting the server resumes the previous Antigravity conversation
    with context intact, and the history browser renders it.*

Both halves, and they fail differently, so both are asserted:

**Context intact** is a claim about the *engine*. It cannot be checked by
looking at our mirror, because the mirror would look perfect for a resume
that silently opened a blank conversation — the transcript is ours and it
is on disk either way. So the first turn tells the model a passphrase it
could not otherwise know, and the second turn, in a **different process**,
asks for it back. Only a real resume can answer.

**The history browser renders it** is a claim about the *reader*. Four
guesses at the stored entry shape parsed to zero messages while looking
entirely reasonable on disk, and an empty list is exactly what the SDK's
parser answers for a session that does not exist. So this asserts on
``get_current_state()["messages"]`` and ``history_list()`` — what the
browser is actually served — rather than on the JSONL.

Why two processes
-----------------
"Restarting the server" is the criterion, so a single process building a
second adapter would be testing a weaker claim. Phase two re-execs this
file with ``--phase 2`` and the work directory, so everything AIC⚡DC knew
— the session object, the conversation id, the mirror's chain — is gone
and has to come back off disk.

Nothing here is a stand-in except the click. The adapter is the shipping
:class:`~aic_dc.agy.service.AgyService`, the gate is the installed hook,
and the probe answers dialogs through ``broker.resolve`` exactly as the
browser's RPC does.

What it costs: two turns on the paid subscription, in a temporary
directory that is removed afterwards. It writes no files of its own —
deliberately, because [AG-R-3](../specs5/plan-ag/risks.md#ag-r-3) can
divert a write and this probe is not about writes. The work directory is
still placed in a trusted root, because a turn that decided to write
anyway should land where it says it did.

    uv run python scripts/probe_agy_resume.py

Exit 0 on PASS. Any other exit is an exit criterion not met.

Governing spec: ``specs5/plan-ag/`` — AG-1, AG-14, README phase 5.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402
from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.service import AgyService  # noqa: E402

#: A token the model cannot have seen and cannot guess. The whole of the
#: "context intact" assertion rests on it being unguessable, so it is a
#: nonsense compound rather than a word.
PASSPHRASE = "KESTREL-9-ORRERY"

TURN_TIMEOUT_SECONDS = 300.0


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def config_dir() -> Path:
    """The config dir the app uses, and the hook is installed against."""
    return Path.home() / ".config" / "aic-dc"


def build(work: Path) -> AgyService:
    """The shipping adapter, on this work directory. No substitutions."""
    return AgyService(
        types.SimpleNamespace(
            repo_root=str(work),
            config_dir=str(config_dir()),
            aic_dc_dir=str(work / ".aic-dc"),
        )
    )


async def answer_dialogs(service: AgyService) -> None:
    """Stand in for the browser: allow everything that is asked.

    Polls ``broker.pending()``, which is the list a client connecting
    mid-request is served — so this answers the dialog a human would have
    seen rather than a parallel one built for the probe.
    """
    while True:
        gate = service._gate
        if gate is not None:
            for payload in gate.broker.pending():
                permission_id = payload.get("permission_id")
                if not permission_id:
                    continue
                log(f"dialog: {payload.get('tool_name')} → allow")
                await gate.broker.resolve(
                    permission_id, {"action": "allow"}, resolved_by="probe"
                )
        await asyncio.sleep(0.05)


async def turn(service: AgyService, request_id: str, prompt: str) -> str:
    """One turn, driven through the adapter, returning the answer's prose.

    Goes through ``chat_streaming`` rather than ``AgySession`` directly,
    because the mirror is wired at ``_dispatch`` and reaching past the
    adapter would be probing a path the product does not use.
    """
    answer: list[str] = []
    original_dispatch = service._dispatch

    async def recording_dispatch(event: Any, rid: str | None) -> None:
        await original_dispatch(event, rid)
        if event.name == "streamComplete":
            answer.append(str(event.payload.get("response_text") or ""))

    service._dispatch = recording_dispatch  # type: ignore[method-assign]
    started = await service.chat_streaming(request_id, prompt)
    if "error" in started:
        raise RuntimeError(f"the turn was refused: {started}")
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while request_id in service._turns:
            await asyncio.sleep(0.1)
    service._dispatch = original_dispatch  # type: ignore[method-assign]
    return answer[-1] if answer else ""


# ----------------------------------------------------------------------
# Phase one — hold a conversation and let the process end
# ----------------------------------------------------------------------


async def phase_one(work: Path) -> int:
    service = build(work)
    browser = asyncio.ensure_future(answer_dialogs(service))
    try:
        outcome = await service.connect_engine()
        if "error" in outcome:
            log(f"FAIL: could not connect: {outcome}")
            return 1
        conversation = service._session.conversation_id
        log(f"conversation {conversation}")

        await turn(
            service,
            "probe-1",
            f"Remember this passphrase for later in our conversation: "
            f"{PASSPHRASE}. Reply with just the word ok. Do not use any "
            f"tools and do not write any files.",
        )
    finally:
        browser.cancel()
        await service.shutdown()

    mirrored = service._mirror.session_id
    if mirrored != conversation:
        log(
            f"FAIL: the mirror filed under {mirrored!r} and the engine's "
            f"conversation is {conversation!r}. Keying on anything but the "
            f"engine's own id is what makes resume impossible."
        )
        return 1
    log(f"phase one done; mirrored under {mirrored}")
    return 0


# ----------------------------------------------------------------------
# Phase two — a fresh process, which is the criterion
# ----------------------------------------------------------------------


async def phase_two(work: Path) -> int:
    service = build(work)

    # (a) The reader. Asserted before anything connects, because this is
    #     what a browser reloading against a just-started server is served.
    state = await service.get_current_state()
    messages = state["messages"]
    roles = [m.get("role") for m in messages]
    log(f"state snapshot: {len(messages)} messages {roles}")
    if roles[:2] != ["user", "assistant"]:
        log(
            "FAIL: the snapshot did not render the previous conversation. "
            "An empty list here is what the parser answers for a session "
            "that does not exist, so this is the failure that looks like "
            "nothing happened."
        )
        return 1
    if PASSPHRASE not in str(messages[0].get("content")):
        log("FAIL: the first message is not the prompt that was sent")
        return 1

    rows = await service.history_list()
    if not isinstance(rows, list) or not rows:
        log(f"FAIL: the history browser lists nothing: {rows}")
        return 1
    log(f"history_list: {[r.get('session_id') for r in rows]}")
    previous = rows[0]["session_id"]

    # (b) The engine. Auto-resume, with no id passed in: a restart is
    #     meant to be invisible, so this is the path a real one takes.
    browser = asyncio.ensure_future(answer_dialogs(service))
    try:
        outcome = await service.connect_engine()
        if "error" in outcome:
            log(f"FAIL: could not reconnect: {outcome}")
            return 1
        resumed = service._session.conversation_id
        log(f"reconnected to {resumed}")
        if resumed != previous:
            log(
                f"FAIL: asked to resume {previous} and got {resumed}. A "
                f"fresh conversation is the wrong kind of success."
            )
            return 1

        reply = await turn(
            service,
            "probe-2",
            "What was the passphrase I asked you to remember? Reply with "
            "just the passphrase. Do not use any tools.",
        )
    finally:
        browser.cancel()
        await service.shutdown()

    log(f"the model answered: {reply.strip()[:200]!r}")
    if PASSPHRASE not in reply:
        log(
            f"FAIL: the resumed conversation does not contain {PASSPHRASE}. "
            f"The transcript survived and the model's context did not, "
            f"which is the failure a mirror alone cannot detect."
        )
        return 1

    final = await service.history_load(previous)
    if isinstance(final, dict):
        log(f"FAIL: the conversation stopped loading after the resume: {final}")
        return 1
    roles = [m.get("role") for m in final]
    log(f"history_load after the resume: {roles}")
    if roles.count("user") < 2:
        log(
            "FAIL: the second turn did not append to the first. An entry "
            "written with no parentUuid starts a second chain, and the "
            "reader walks back from one terminal only — so the older half "
            "silently stops rendering."
        )
        return 1

    log(
        f"PASS: a fresh process resumed {previous}, the model still held "
        f"{PASSPHRASE}, and the browser was served the whole conversation"
    )
    return 0


# ----------------------------------------------------------------------


async def run(phase: int, work: Path | None) -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    if phase == 2:
        assert work is not None
        return await phase_two(work)

    state = install.status(config_dir())
    log(f"installed gate: {state['state']} ({state.get('path')})")
    installed_here = False
    if state["state"] != "current":
        log("installing the gate for the duration of this probe")
        install.install(config_dir())
        installed_here = True

    # Trusted, so that a turn which decides to write anyway lands where it
    # says it did rather than in agy's scratch directory (AG-R-3).
    root = probe_root("agy-resume-")
    try:
        code = await phase_one(root)
        if code:
            return code
        log("--- restarting: phase two runs in a new process ---")
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--phase", "2",
             "--work", str(root)],
            check=False,
        )
        return completed.returncode
    finally:
        if installed_here:
            install.uninstall()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, default=1)
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.phase, args.work)))
