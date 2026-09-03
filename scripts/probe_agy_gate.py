#!/usr/bin/env python3
"""Phase 8's gate, proved against the real ``agy`` binary.

**This is AG-R-12's tripwire, and it asserts on the file.** A probe that
checked the hook had *fired* is what produced the wrong answer on
2026-09-03: it fired, and the write landed anyway, because the matcher
named one tool and the model reached for another. So the only assertion
that counts here is that the bytes on disk did not change.

It is also the first working piece of the adapter, because proving the
gate requires the ownership handshake the adapter will use:

    spawn agy → read `init` → claim the conversation → send the prompt

The order is forced. A conversation id is not known until ``init``
arrives, and a tool call cannot happen until a prompt is sent, so there is
exactly one window in which to claim ownership and it is between those two
events. Claiming late would let the first tool call through as somebody
else's session; claiming early is impossible.

What it costs: two or three turns on the paid subscription.

Run it from anywhere:

    uv run python scripts/probe_agy_gate.py

Exit 0 on PASS. Any other exit is a gate that does not hold, and phase 8
does not ship on it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aic_dc.agy import registry  # noqa: E402

GLOBAL_HOOKS = Path.home() / ".gemini" / "config" / "hooks.json"
ORIGINAL = "ORIGINAL_TEXT"
REPLACEMENT = "MODIFIED_TEXT"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


#: Tools this probe's gate allows, so the model can actually *reach* an
#: edit. A gate that denied reads would stop the turn before the write was
#: ever proposed, and the tripwire would pass without testing anything —
#: which is the shape of the mistake this whole file exists to avoid.
#: The shipping gate allows reads for the same reason, via the dialog's
#: own classification.
READ_CLASS = frozenset(
    {
        "view_file",
        "find_by_name",
        "list_directory",
        "search_directory",
        "grep_search",
        "read_url_content",
        "search_web",
        "codebase_search",
    }
)


class Gate:
    """Allows reads, refuses everything that could write.

    The refusal is what is under test, so it has to be reachable: the
    model must get far enough to *propose* the edit. Denying the reads it
    needs first would end the turn early and leave "the file is unchanged"
    true for a reason that has nothing to do with the gate.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    call = json.loads(data.decode("utf-8"))
                except ValueError:
                    call = {"unparseable": True}
                self.calls.append(call)
                tool = (call.get("toolCall") or {}).get("name", "")
                if tool in READ_CLASS:
                    answer = {"decision": "allow"}
                else:
                    answer = {
                        "decision": "deny",
                        "reason": "the user declined this edit in AIC-DC",
                    }
                conn.sendall(json.dumps(answer).encode("utf-8") + b"\n")

    def close(self) -> None:
        self._stop = True
        self._server.close()


def install_hook(config_dir: Path) -> Path | None:
    """Install the gate globally, because workspace-local hooks do not load.

    Returns the backup path of any pre-existing file, so the user's own
    configuration is restored exactly. The hook passes through every
    conversation this host has not claimed, so an unrelated ``agy`` session
    running during the probe is unaffected.
    """
    backup = None
    GLOBAL_HOOKS.parent.mkdir(parents=True, exist_ok=True)
    if GLOBAL_HOOKS.exists():
        backup = Path(tempfile.mkdtemp()) / "hooks.json.bak"
        shutil.copy2(GLOBAL_HOOKS, backup)
    command = (
        f"{sys.executable} -m aic_dc.agy.hook {config_dir}"
    )
    GLOBAL_HOOKS.write_text(
        json.dumps(
            {
                "aic-dc-gate": {
                    "PreToolUse": [
                        {
                            # AG-R-12: every tool, never a list. A blocked
                            # tool is an error the model can see, and it
                            # will reach for whatever the matcher missed.
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": command,
                                    "timeout": 3600,
                                }
                            ],
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup


def restore_hook(backup: Path | None) -> None:
    GLOBAL_HOOKS.unlink(missing_ok=True)
    if backup and backup.exists():
        shutil.copy2(backup, GLOBAL_HOOKS)


def main() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    work = Path(tempfile.mkdtemp(prefix="agy-gate-"))
    config_dir = work / "cfg"
    target = work / "target.txt"
    target.write_text(ORIGINAL + "\n", encoding="utf-8")

    gate = Gate(str(work / "gate.sock"))
    backup = install_hook(config_dir)
    conversation_id = None
    proc = None
    try:
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent / "src"))
        proc = subprocess.Popen(
            [
                "agy",
                "--print=",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                # The gate is the only gate on this transport: agy's own
                # headless layer auto-denies rather than asking, and would
                # refuse the turn before our hook ever ran.
                "--dangerously-skip-permissions",
                # Well past any dialog. The hook may block for an hour;
                # this must not be what ends the turn.
                "--print-timeout", "30m",
            ],
            cwd=work,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        log("waiting for the init frame")
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "init":
                conversation_id = event.get("conversation_id")
                break
        if not conversation_id:
            log("FAIL: no init frame, so ownership could never be claimed")
            return 1

        # The one window: after init, before the prompt.
        registry.claim(conversation_id, gate.path, config_dir=config_dir)
        log(f"claimed {conversation_id}")

        proc.stdin.write(
            json.dumps(
                {
                    "event": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f"Replace {ORIGINAL} with {REPLACEMENT} in "
                            "target.txt in the current directory. Make only "
                            "that edit."
                        ),
                    },
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        proc.stdin.close()

        log("turn sent; draining")
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "result":
                break
        proc.wait(timeout=120)
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        if conversation_id:
            registry.release(conversation_id, config_dir=config_dir)
        gate.close()
        restore_hook(backup)

    after = target.read_text(encoding="utf-8")
    tools = [c.get("toolCall", {}).get("name") for c in gate.calls]

    denied = [t for t in tools if t not in READ_CLASS]

    log(f"tools the gate was asked about: {tools}")
    log(f"of those, refused: {denied}")
    log(f"file after the turn: {after.strip()!r}")

    # The assertion that counts, and the only one. Not "the hook fired".
    if ORIGINAL not in after or REPLACEMENT in after:
        log("FAIL: the file changed despite every mutating call being refused")
        return 1
    if not gate.calls:
        log("FAIL: the gate was never consulted — it is not installed or not ours")
        return 1
    if not denied:
        # Without this the tripwire passes for the wrong reason: the model
        # never proposed a write, so nothing was ever refused and "the file
        # is unchanged" says nothing about the gate.
        log("FAIL: no write was ever proposed, so the refusal was never tested")
        return 1
    log(
        f"PASS: {len(denied)} write attempt(s) refused across {len(denied)} "
        f"distinct route(s), file unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
