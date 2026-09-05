#!/usr/bin/env python3
"""Phase 8's other half: an **approved** write, through the real dialog.

``scripts/probe_agy_gate.py`` proves the gate refuses. That is the half
that fails safe, and it is the half that was easy. This is the other one:
a ``replace_file_content`` call that is *presented for review with a real
diff* and then allowed, after which the bytes on disk have changed.

Why the deny probe does not cover this
--------------------------------------
A gate that denies everything passes the deny tripwire. So does a gate
that denies everything **because it crashed**, or because the tool name
was unrecognised, or because the dialog payload was empty. The refusal
path exercises none of the machinery the product is actually built
around, and
[`sdk-surface.md` § The tool *names* differ](../specs5/plan-ag/sdk-surface.md#the-tool-names-differ-and-only-the-tool-names--measured-2026-09-03)
names the precise failure that would survive it: ``agy`` calls an edit
``replace_file_content`` where the SDK calls it ``edit_file``, and an
unrecognised name classifies as ``exec`` — **still gated**, so the deny
probe stays green, while the dialog calls a file edit a shell command and
renders no diff at all. The gate holds and the product's central feature
is gone.

So this probe asserts on the dialog rather than on the outcome alone:

1. the write **raised a dialog** rather than being waved through;
2. the dialog classified it as a ``write``, not as an ``exec``;
3. the dialog carried a **real diff** — the original text, the proposed
   text, and a non-zero line count on both sides;
4. after allowing it, the file on disk **changed**.

(4) alone would pass against a gate that never ran. (1)–(3) alone would
pass against a gate that showed a beautiful diff and then dropped the
answer. Together they are the exit criterion.

Nothing here is a stand-in except the click
-------------------------------------------
The gate is :class:`~aic_dc.agy.gate_server.AgyGateServer` over the shared
``PermissionBroker``, driven by the shipping
:class:`~aic_dc.agy.session.AgySession`, gated by the hook installed in the
user's own ``~/.gemini/config/hooks.json``. The only thing this file
substitutes for is the human pressing **Allow**: it reads
``broker.pending()`` and calls ``broker.resolve()``, which is exactly what
the browser's RPC does and nothing more.

What it costs: one turn on the paid subscription, and it **writes a file**
— inside a temporary directory that is removed afterwards.

    uv run python scripts/probe_agy_write.py

Exit 0 on PASS. Any other exit is an exit criterion not met.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
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

ORIGINAL = "ORIGINAL_TEXT"
REPLACEMENT = "MODIFIED_TEXT"

#: How long to wait for the whole turn. Generous — it is a real model on a
#: real subscription, and the thing being measured is not latency.
TURN_TIMEOUT_SECONDS = 300.0


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The real gate server, with every decision written down.

    Subclassed rather than monkeypatched so the probe cannot accidentally
    replace the behaviour it is testing: :meth:`decide` still runs, and
    what is recorded is its input and its answer.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = await super().decide(payload)
        tool = (payload.get("toolCall") or {}).get("name", "?")
        self.seen.append((str(tool), answer))
        return answer


async def answer_dialogs(broker: Any, presented: list[dict[str, Any]]) -> None:
    """Stand in for the browser: allow everything, and keep the payloads.

    Polling rather than listening on ``broadcast`` deliberately. The
    dialog the user actually answers is rendered from ``pending()`` — it is
    what a client that connects mid-request is served — so asserting
    against that list checks the payload the human would have seen, not a
    parallel one built for the event.
    """
    while True:
        for payload in broker.pending():
            permission_id = payload.get("permission_id")
            if not permission_id:
                continue
            presented.append(payload)
            log(
                f"dialog: {payload.get('tool_name')} "
                f"[{payload.get('tool_class')}] → allow"
            )
            await broker.resolve(
                permission_id, {"action": "allow"}, resolved_by="probe"
            )
        await asyncio.sleep(0.05)


def check(presented: list[dict[str, Any]], after: str) -> int:
    """The four assertions. Returns a process exit code."""
    writes = [p for p in presented if p.get("diff") is not None]
    edits = [
        p
        for p in presented
        if str(p.get("tool_name")) in ("replace_file_content", "write_to_file")
    ]

    if not presented:
        log("FAIL: no dialog was raised at all — the gate was never consulted")
        return 1
    if not edits:
        # Not a gate failure, but the criterion is untested: the model
        # never proposed an edit, so nothing was approved. Same shape as
        # the deny probe's "no write was ever proposed" guard.
        log("FAIL: no edit was proposed, so the approval was never tested")
        log(f"       tools presented: {[p.get('tool_name') for p in presented]}")
        return 1

    edit = edits[0]
    tool_class = edit.get("tool_class")
    if tool_class != "write":
        # The tool-name trap, caught. An unmapped name classifies as
        # `exec`: still gated, so the deny probe stays green, but the
        # dialog describes a file edit as a shell command.
        log(
            f"FAIL: {edit.get('tool_name')} was classified as {tool_class!r}, "
            f"not 'write' — the per-transport tool-name map has a hole"
        )
        return 1

    diff = edit.get("diff")
    if not isinstance(diff, dict):
        log(f"FAIL: {edit.get('tool_name')} raised a dialog with no diff at all")
        return 1
    original, proposed = diff.get("original"), diff.get("proposed")
    additions, deletions = diff.get("additions"), diff.get("deletions")
    log(
        f"diff: {diff.get('path')}  +{additions} −{deletions}  "
        f"new_file={diff.get('is_new_file')}"
    )
    if not original or ORIGINAL not in original:
        log(f"FAIL: the diff's original text does not contain {ORIGINAL!r}")
        return 1
    if not proposed or REPLACEMENT not in proposed:
        log(f"FAIL: the diff's proposed text does not contain {REPLACEMENT!r}")
        return 1
    if not additions or not deletions:
        log(
            f"FAIL: the diff counted +{additions} −{deletions}; a "
            f"string-for-string edit is neither a pure add nor a pure delete"
        )
        return 1

    # And the one that says the answer was carried back rather than
    # rendered and dropped.
    if REPLACEMENT not in after or ORIGINAL in after:
        log(
            f"FAIL: the edit was approved and the file still reads "
            f"{after.strip()!r} — the allow did not reach agy"
        )
        return 1

    log(
        f"PASS: {edit.get('tool_name')} was presented as a write with a real "
        f"diff (+{additions} −{deletions}), approved, and the edit landed"
    )
    return 0


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    # Trusted, or the approved edit lands in agy's scratch directory and
    # this probe reports the allow as never having reached agy. See
    # `_agy_probe_support`.
    work = probe_root("agy-write-")
    target = work / "target.txt"
    target.write_text(ORIGINAL + "\n", encoding="utf-8")

    # The registry the installed hook reads. Taken from the *installed*
    # command rather than invented, so this probe tests the gate the user
    # is actually running instead of one it stood up for itself.
    install_config_dir = default_config_dir()
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
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = RecordingGateServer(
        work / "gate.sock", gate=gate, config_dir=install_config_dir
    )
    session = AgySession(work, gate=server)

    presented: list[dict[str, Any]] = []
    browser = asyncio.ensure_future(answer_dialogs(gate.broker, presented))
    try:
        conversation_id = await session.start()
        log(f"conversation {conversation_id}")
        translator = AgyTranslator("probe-write")
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for _event in session.stream_turn(
                f"Replace {ORIGINAL} with {REPLACEMENT} in target.txt in the "
                "current directory. Make only that edit.",
                translator=translator,
            ):
                pass
    except TimeoutError:
        log(f"FAIL: the turn did not finish within {TURN_TIMEOUT_SECONDS:.0f}s")
        return 1
    finally:
        browser.cancel()
        await session.close()
        await server.stop()
        if installed_here:
            install.uninstall()

    after = target.read_text(encoding="utf-8")
    log(f"tools the gate decided: {[t for t, _ in server.seen]}")
    log(f"file after the turn: {after.strip()!r}")
    code = check(presented, after)
    shutil.rmtree(work, ignore_errors=True)
    return code


def default_config_dir() -> Path:
    """The config dir the app itself uses, which the hook is installed against."""
    return Path.home() / ".config" / "aic-dc"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
