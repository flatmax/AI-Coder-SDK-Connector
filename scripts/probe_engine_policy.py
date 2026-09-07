#!/usr/bin/env python3
"""AG-17: does a Claude-only ``app.json`` actually produce a Claude-only server?

Phase 11 built ``engines.enabled`` and 20 tests assert every piece of it.
None of them start a server. That is the gap this closes, and the reason
it matters is in the entry's own exit criterion: the mount decisions live
in ``main.py``'s startup block — three ``if`` branches, a router
construction and a session build — and a suite that constructs the pieces
directly never runs the code that assembles them.

**The trap this probe exists to avoid.** On a machine with no ``agy``
binary and no Gemini key, ``mountable == ["claude"]`` is *already true*
with no policy at all. A run that only checks the Claude-only
configuration therefore proves nothing: it cannot distinguish "the policy
removed the second engine" from "there was never a second engine here".
That is the same shape as ``probe_agy_gate.py``'s deny tripwire resting
on write diversion (see :mod:`_agy_probe_support`), and the same shape as
phase 9's note that an absent dialog is also what a turn that never got
that far looks like.

So this runs **two servers** and the first one is the point:

  A. **Positive control.** The same machine, the same environment, an
     ``app.json`` with no ``engines.enabled`` at all. Both Antigravity
     engines must mount and the consultant's MCP server must be present
     in the Claude session. A run where B is clean and A is *also* clean
     has proved nothing except that this machine has one engine.
  B. **The criterion.** ``engines.enabled: ["claude"]``, everything else
     identical. Nothing Antigravity mounts, both switches are refused
     naming the policy, and the consultant's server is gone.

**What "the mounted server list" means here.** Assertion 3 reads
``get_mcp_status()``, which is a control request to the ``claude`` CLI
asking which MCP servers *it* connected. It is the engine's own report,
not ours — the criterion says "asserted against the mounted server list,
never against the log line, because a log line saying it was skipped is
what a build that mounted it anyway would also print". An empty list is
treated as **uninterpretable rather than as a pass**, because a session
with no MCP servers at all also contains no ``aic-dc-antigravity``.

**What it fakes, and why that is honest.** Nothing in the startup path
authenticates either Antigravity transport: the SDK engine mounts on a
credential *resolving* plus the wheel importing, and ``agy`` mounts on
``shutil.which("agy")``. So where this machine lacks one, the probe
supplies the cheapest thing the mount condition actually tests — a
``GEMINI_API_KEY`` in the child's environment, and an executable named
``agy`` on its ``PATH`` — and the report says which were real. Both are
substitutes for a *mount condition*, never for a working transport: a
stub binary would fail the first turn, and this probe never takes one.
Every invocation of the stub is logged and reported, so "something ran it
at startup" cannot pass unnoticed.

Isolation, because a probe that edits the user's ``app.json`` to test a
policy is a probe that changes the running session's behaviour:
``AIC_DC_CONFIG_HOME`` points each server at its own scratch config
directory (``config.py`` § ``_user_config_dir`` — it overrides
everything), and each runs against a throwaway git repository. Nothing
under ``~/.config/aic-dc`` is read or written. The ``claude`` CLI's own
OAuth credentials live in ``~/.claude`` and are untouched by that
override, which is what lets a real session start at all.

**The scratch config directory is seeded as an established install, and
the first run of this probe failed because it was not.** ``app.json`` is
a *managed* file: on any version mismatch — and a directory with no
``.bundled_version`` marker is the largest possible mismatch —
``_run_upgrade`` backs the user's copy up and overwrites it from the
bundle. So the first attempt's ``engines.enabled`` was on disk, was read
by nothing, and both servers mounted everything. The scratch directory
therefore starts as a copy of the bundled config plus a current marker,
with the policy merged into that, which is what an install a user has
edited actually looks like. That the upgrade discards the key at all is
recorded in ``delivery.md`` as a finding about AG-17's premise rather
than worked around silently here.

Ports are read back from the child's own log line rather than trusted:
``main.py`` probes for a free port and the one it binds need not be the
one it was asked for.

Usage:
    .venv/bin/python scripts/probe_engine_policy.py [--keep] [--verbose]

Exit status is 0 when every check passed and the run was interpretable,
1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import websockets.sync.client as ws_client

REPO_ROOT = Path(__file__).resolve().parent.parent
RPC = "ClaudeCodeService"
AG_SERVER = "aic-dc-antigravity"
INDEX_SERVER = "aic-dc"
ENGINES = ("claude", "antigravity", "agy")
ANTIGRAVITY_ENGINES = ("antigravity", "agy")

#: The child logs this once it knows what it bound. Not the flag it was
#: given: ``find_available_port`` walks upwards from it.
PORT_LINE = re.compile(r"WebSocket server port: (\d+)")

STUB_AGY = """#!/bin/sh
# A stand-in for the Antigravity CLI, present so that `shutil.which("agy")`
# — the whole of the agy mount condition — answers the way it would on a
# machine that has one. It is never meant to run: probe_engine_policy.py
# takes no turn. Every invocation is recorded so that "nothing ran it"
# is a measurement rather than an assumption.
printf '%s\\n' "$*" >> "{log}"
exit 0
"""


class Failure(Exception):
    """A check failed, or the run could not be interpreted."""


# ----------------------------------------------------------------------
# RPC
# ----------------------------------------------------------------------


class Rpc:
    """The jrpc-oo wire, spoken directly.

    No browser and no jrpc-oo client: all three assertions are
    server-side facts, and a real client would add a webapp bundle, a
    Chrome profile and a rendering layer to a question about what
    ``main.py`` mounted. Requests the *server* pushes at us (streamed
    events, permission asks) arrive as `method` messages with no `id` we
    know and are dropped — nothing here takes a turn, so there is
    nothing to answer.
    """

    def __init__(self, port: int, timeout: float = 20.0) -> None:
        self._ws = ws_client.connect(
            f"ws://127.0.0.1:{port}",
            max_size=64 * 1024 * 1024,
            open_timeout=timeout,
        )
        self._id = 0

    def call(self, method: str, *args, timeout: float = 90.0):
        self._id += 1
        want = self._id
        self._ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": want,
                    "method": f"{RPC}.{method}",
                    "params": {"args": list(args)},
                }
            )
        )
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Failure(f"{method} did not answer within {timeout:.0f}s")
            raw = self._ws.recv(timeout=left)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != want:
                continue  # a server push, or a stale reply
            if "error" in msg:
                raise Failure(f"{method} returned an error: {msg['error']}")
            return msg.get("result")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# The server under test
# ----------------------------------------------------------------------


class Server:
    """One ``aic-dc`` process, its own config home, its own repo."""

    def __init__(
        self,
        *,
        label: str,
        workspace: Path,
        engines: dict,
        env_extra: dict[str, str],
        base_port: int,
        verbose: bool,
    ) -> None:
        self.label = label
        self.verbose = verbose
        self._dir = workspace / label
        self._config = self._dir / "config"
        self._repo = self._dir / "repo"
        _seed_config_dir(self._config, engines)
        _init_repo(self._repo)
        self._log = self._dir / "server.log"
        self._lines: list[str] = []
        self._base_port = base_port
        self._env_extra = env_extra
        self._proc: subprocess.Popen | None = None
        self.port: int | None = None

    # -- lifecycle ----------------------------------------------------

    def start(self, timeout: float = 90.0) -> None:
        env = dict(os.environ)
        env["AIC_DC_CONFIG_HOME"] = str(self._config)
        env.update(self._env_extra)
        exe = REPO_ROOT / ".venv" / "bin" / "aic-dc"
        cmd = [
            str(exe) if exe.exists() else "aic-dc",
            "--server-port",
            str(self._base_port),
            "--webapp-port",
            str(self._base_port + 1),
            "--repo-path",
            str(self._repo),
            "--no-browser",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(self._repo),
        )
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.port is None:
                for line in list(self._lines):
                    found = PORT_LINE.search(line)
                    if found:
                        self.port = int(found.group(1))
                        break
            if self.port is not None and _port_open(self.port):
                return
            if self._proc.poll() is not None:
                raise Failure(
                    f"[{self.label}] the server exited with "
                    f"{self._proc.returncode} before serving; log at {self._log}"
                )
            time.sleep(0.25)
        raise Failure(
            f"[{self.label}] no WebSocket port within {timeout:.0f}s; "
            f"log at {self._log}"
        )

    def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        with self._log.open("w", encoding="utf-8") as sink:
            for line in self._proc.stdout:
                self._lines.append(line.rstrip("\n"))
                sink.write(line)
                sink.flush()
                if self.verbose:
                    print(f"  [{self.label}] {line.rstrip()}", file=sys.stderr)

    def stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)

    # -- reading ------------------------------------------------------

    def log_mentions(self, needle: str) -> list[str]:
        return [line for line in self._lines if needle in line]


def _seed_config_dir(config_dir: Path, engines: dict) -> None:
    """A config directory that looks edited, not freshly installed.

    Copies the bundled config and writes the version marker, so
    ``_run_upgrade`` takes its fast path and leaves ``app.json`` alone;
    then merges the run's ``engines`` block into the bundled
    ``app.json``. Without the marker the upgrade pass overwrites the file
    from the bundle before anything reads it — see the module docstring.
    """
    from aic_dc.config import (  # noqa: PLC0415 - probe, reaching in on purpose
        _bundled_config_dir,
        _bundled_version,
    )

    config_dir.mkdir(parents=True)
    bundled = _bundled_config_dir()
    for item in bundled.iterdir():
        if item.is_file():
            shutil.copy2(item, config_dir / item.name)
    version = _bundled_version()
    if not version:
        raise Failure(
            "the bundled VERSION is empty, so no marker can make the upgrade "
            "pass take its fast path; app.json would be overwritten"
        )
    (config_dir / ".bundled_version").write_text(version, encoding="utf-8")

    app_path = config_dir / "app.json"
    app = json.loads(app_path.read_text(encoding="utf-8")) if app_path.exists() else {}
    app["engines"] = {**(app.get("engines") or {}), **engines}
    app_path.write_text(json.dumps(app, indent=2), encoding="utf-8")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    ident = [
        "-c",
        "user.name=aic-dc probe",
        "-c",
        "user.email=probe@example.invalid",
    ]
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True
    )
    (path / "README.md").write_text(
        "Throwaway repository for probe_engine_policy.py.\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), *ident, "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    @property
    def failed(self) -> list[tuple[bool, str, str]]:
        return [row for row in self.rows if not row[0]]


def mcp_server_names(rpc: Rpc, report: Report, label: str) -> list[str] | None:
    """The names the CLI reports, or None when the answer is unusable.

    ``connect_engine`` first, because ``get_mcp_status`` is a control
    request and there is no client to send it to until the engine is up.

    **Then it waits for a specific server rather than for a non-empty
    list**, and the first run of this probe is why. The servers this
    process passes over ``--mcp-config`` are dialled asynchronously
    alongside any the user's own ``~/.claude.json`` configures, so the
    first non-empty answer was ``['chrome-devtools']`` — an inherited
    server, arriving before ours, read as "the session mounted nothing of
    ours". Waiting for ``aic-dc`` is waiting for the one server that must
    be present on *both* runs; only once it is there does the presence or
    absence of ``aic-dc-antigravity`` beside it mean anything.
    """
    status = rpc.call("connect_engine", timeout=180.0)
    if isinstance(status, dict) and status.get("error"):
        report.check(
            False,
            f"[{label}] the Claude engine connected",
            str(status.get("error")),
        )
        return None
    deadline = time.monotonic() + 90.0
    names: list[str] = []
    while time.monotonic() < deadline:
        answer = rpc.call("get_mcp_status", timeout=60.0)
        if isinstance(answer, dict) and answer.get("error"):
            report.check(
                False,
                f"[{label}] get_mcp_status answered",
                str(answer.get("error")),
            )
            return None
        servers = (answer or {}).get("mcpServers")
        if isinstance(servers, list):
            names = [
                str(entry.get("name"))
                for entry in servers
                if isinstance(entry, dict) and entry.get("name")
            ]
            if INDEX_SERVER in names:
                break
        time.sleep(2.0)
    if INDEX_SERVER not in names:
        # Not a pass. A session that never mounted our own MCP server is
        # also a session with no `aic-dc-antigravity`, so this cannot be
        # read as the policy having worked.
        report.check(
            False,
            f"[{label}] the CLI reported {INDEX_SERVER} among its servers",
            f"saw {sorted(names)} — assertion 3 would be uninterpretable",
        )
        return None
    report.check(
        True,
        f"[{label}] the CLI reported its mounted MCP servers",
        ", ".join(sorted(names)),
    )
    return names


def run_control(server: Server, report: Report) -> None:
    """A: the configuration where everything mounts. Makes B mean something."""
    rpc = Rpc(server.port)
    try:
        engines = rpc.call("list_engines")
        mountable = set(engines.get("mountable") or ())
        enabled = list(engines.get("enabled") or ())
        report.check(
            set(enabled) == set(ENGINES),
            "[A] every engine is enabled with no policy in app.json",
            f"enabled={enabled}",
        )
        for name in ANTIGRAVITY_ENGINES:
            report.check(
                name in mountable,
                f"[A] {name} mounted",
                f"mountable={sorted(mountable)}",
            )
        names = mcp_server_names(rpc, report, "A")
        if names is not None:
            report.check(
                AG_SERVER in names,
                f"[A] the Claude session mounted {AG_SERVER}",
                f"servers={sorted(names)}",
            )
    finally:
        rpc.close()


def run_criterion(server: Server, report: Report) -> None:
    """B: the exit criterion, three assertions in the order it states them."""
    rpc = Rpc(server.port)
    try:
        engines = rpc.call("list_engines")
        mountable = list(engines.get("mountable") or ())
        available = list(engines.get("available") or ())
        enabled = list(engines.get("enabled") or ())
        report.check(
            mountable == ["claude"],
            "[B] 1. mountable is exactly ['claude']",
            f"mountable={mountable}",
        )
        report.check(
            available == ["claude"] and enabled == ["claude"],
            "[B] 1. the selector is offered claude only",
            f"available={available} enabled={enabled}",
        )

        # Assertion 3 before assertion 2, against the criterion's own
        # numbering, because a switch that *succeeds* when it should not
        # would change which engine the session assertion is about — and
        # then a missing `aic-dc-antigravity` would be explained by the
        # master no longer being Claude rather than by the policy. Reading
        # the session first keeps the two independent.
        names = mcp_server_names(rpc, report, "B")
        if names is not None:
            report.check(
                AG_SERVER not in names,
                f"[B] 3. the Claude session did not mount {AG_SERVER}",
                f"servers={sorted(names)}",
            )

        for name in ANTIGRAVITY_ENGINES:
            answer = rpc.call("switch_engine", name) or {}
            reason = answer.get("reason")
            error = str(answer.get("error") or "")
            report.check(
                reason == "engine_disabled",
                f"[B] 2. switch_engine({name!r}) refused as a policy",
                f"reason={reason!r}",
            )
            report.check(
                "engines.enabled" in error,
                "[B] 2. the refusal names the policy, not a missing binary",
                error[:120] + ("…" if len(error) > 120 else ""),
            )
            report.check(
                answer.get("changed") is not True,
                f"[B] 2. switch_engine({name!r}) changed nothing",
                f"changed={answer.get('changed')!r}",
            )

        active = (rpc.call("list_engines") or {}).get("active")
        report.check(
            active == "claude",
            "[B] 2. claude is still master after both refusals",
            f"active={active!r}",
        )
    finally:
        rpc.close()


def tripwire_report(report: Report) -> None:
    """The static half, run here so one command answers the whole entry.

    ``tests/test_engine_policy.py`` holds the assertion that every
    Antigravity mount point consults the allowlist — the thing that keeps
    a future surface from being added beside them and inheriting the old
    default. It is a unit test rather than a live check, and it is the
    other half of the criterion, so this reports it rather than leaving a
    reader to run it separately.
    """
    proc = subprocess.run(
        [
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "pytest",
            "-q",
            "tests/test_engine_policy.py",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    report.check(
        proc.returncode == 0,
        "[T] the mount-point tripwire still holds",
        tail[-1] if tail else "",
    )


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep", action="store_true", help="keep the scratch workspace and logs"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="echo both servers' logs"
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=18700,
        help="first port to try; each server takes two (default: 18700)",
    )
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="aic-dc-engine-policy-"))
    stub_log = workspace / "agy-stub-invocations.log"
    env_extra: dict[str, str] = {}
    notes: list[str] = []

    real_agy = shutil.which("agy")
    if real_agy:
        notes.append(f"agy: the real binary at {real_agy}")
    else:
        stub_dir = workspace / "stub"
        stub_dir.mkdir()
        stub = stub_dir / "agy"
        stub.write_text(STUB_AGY.format(log=stub_log), encoding="utf-8")
        stub.chmod(0o755)
        env_extra["PATH"] = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        notes.append(
            "agy: a stub on PATH — this machine has no CLI, and the mount "
            "condition is `shutil.which(\"agy\")`. No turn is taken through it"
        )

    key_file = Path.home() / ".config" / "aic-dc" / "gemini-api-key"
    if os.environ.get("GEMINI_API_KEY") or key_file.exists():
        notes.append("Gemini key: this machine's own")
    else:
        env_extra["GEMINI_API_KEY"] = "probe-engine-policy-not-a-real-key"
        notes.append(
            "Gemini key: a placeholder in the child's environment — the SDK "
            "mount condition is that a credential *resolves*, and no "
            "consultation runs"
        )
    try:
        from aic_dc.antigravity.surface import sdk_installed  # noqa: PLC0415

        notes.append(f"google-antigravity wheel installed: {sdk_installed()}")
    except Exception as exc:  # pragma: no cover - diagnostic only
        notes.append(f"google-antigravity wheel: could not tell ({exc})")

    print("\nAG-17 / phase 11 — engine policy, live\n")
    for note in notes:
        print(f"  · {note}")
    print(f"  · scratch workspace: {workspace}")
    print()

    report = Report()
    servers: list[Server] = []
    try:
        print("A. Positive control — no engines.enabled, everything should mount")
        control = Server(
            label="control",
            workspace=workspace,
            engines={},
            env_extra=env_extra,
            base_port=args.base_port,
            verbose=args.verbose,
        )
        servers.append(control)
        control.start()
        print(f"  (server on port {control.port})")
        run_control(control, report)
        control.stop()

        print("\nB. The criterion — engines.enabled: ['claude']")
        claude_only = Server(
            label="claude-only",
            workspace=workspace,
            engines={"enabled": ["claude"]},
            env_extra=env_extra,
            base_port=args.base_port + 20,
            verbose=args.verbose,
        )
        servers.append(claude_only)
        claude_only.start()
        print(f"  (server on port {claude_only.port})")
        run_criterion(claude_only, report)

        print("\nT. The static half")
        tripwire_report(report)

        # Not a criterion, a measurement: the stub is not supposed to be
        # executed by a startup that only asks whether it exists.
        if stub_log.exists():
            invocations = stub_log.read_text(encoding="utf-8").strip().splitlines()
            report.check(
                False,
                "[T] nothing executed the agy stub",
                f"{len(invocations)} invocation(s): {invocations[:3]}",
            )
        elif not real_agy:
            report.check(True, "[T] nothing executed the agy stub", "mount is which() only")
    except Failure as exc:
        print(f"\n  ABORTED — {exc}")
        report.check(False, "the run completed", str(exc))
    finally:
        for server in servers:
            server.stop()
        if args.keep:
            print(f"\nWorkspace kept at {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    print()
    if report.failed:
        print(f"FAIL — {len(report.failed)} of {len(report.rows)} checks failed")
        for _, name, detail in report.failed:
            print(f"  · {name}" + (f" — {detail}" if detail else ""))
        return 1
    print(f"PASS — {len(report.rows)} checks, both servers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
