#!/usr/bin/env python3
"""Does a real `agy` outlive its host being SIGKILLed, and for how long?

`specs5/plan-ag/delivery.md` § *What was not measured, and why* states this as
a citation rather than a measurement: `main.py` watched a Claude CLI reparent
to init and keep running 38 seconds after its host died, and **the same
argument was made for `agy` without being checked.** The two-pid rule in
`registry.entry_is_live` rests on it — an entry is a corpse only when *both*
its host and its `agy` are gone, and the whole point of asking about the second
pid is that our own child survives a killed parent.

The rule does not depend on the answer, which is why this is a residue rather
than a blocker: if `agy` exits on EOF the entry becomes a corpse and the next
sweep collects it, and if it lingers the entry stands and its calls are denied.
Both are the intended behaviour. **What is missing is the distribution** — how
long an orphan lives — and that is what decides whether a user who kills a
server meets a stale gate for a moment or for the rest of the day.

Three things this establishes, and one it was not written for and found:

  1. **Which process the registry's `agy_pid` actually names.** `AgySession`
     records `self._proc.pid`, and under AG-18 the process it spawned is
     `systemd-run`, not `agy`. Whether those are the same pid is a property of
     `systemd-run --scope` that this file measures rather than assumes, because
     the two-pid rule is only about `agy` if that pid is `agy`'s.
  2. **Whether `agy` survives a SIGKILLed host**, by killing one — the host is
     a child of this probe, signalled with SIGKILL so it gets no chance to
     clean up, which is precisely the case `stop()` cannot cover.
  3. **How long it takes to go**, polled at 100 ms until it does, so the answer
     is a duration rather than a yes.

And the separable half the residue names: **does `agy` exit promptly when its
stdin pipe closes?** Scenario B closes stdin with the host *alive*, which
distinguishes "it exits on EOF" from "it died with its host for some other
reason". A SIGKILL closes the pipe as a side effect, so the first scenario
alone cannot tell those apart.

The registry's own verdict is read at each step — `entry_is_live`, and whether
`reap_stale` collects the entry — so the decision table `f598638e` shipped is
exercised against a pid that is really dead rather than a monkeypatched one, in
the arrangement it was written for.

Usage:
    .venv/bin/python scripts/probe_agy_orphan.py [--ceiling 120] [--runs 1]

Exit 0 when every measurement was taken, 2 when it could not run.

What it costs: it starts `agy` sessions and never prompts them, so it reaches
the `init` frame and stops. No model turn is taken and nothing is written in
the repository. Every process it starts, it kills.

Governing spec: `specs5/plan-ag/` — AG-14, AG-18, AG-R-14.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _agy_probe_support import probe_root  # noqa: E402

from aic_dc.agy import registry, scope  # noqa: E402

CONFIG_DIR = Path.home() / ".config" / "aic-dc"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


# ---------------------------------------------------------------------------
# Reading /proc, because the question is about processes
# ---------------------------------------------------------------------------

def proc_info(pid: int | None) -> dict | None:
    """`comm`, `ppid` and the command line for a live pid, or `None`.

    Read off `/proc` rather than through `os.kill(pid, 0)` alone: the pid is
    the thing under test, and "alive" without "and it is still the program we
    think it is" is the recycled-pid hole `registry.py` states as a known
    limit. A reader that prints `comm` makes that visible when it happens.
    """
    if not pid:
        return None
    entry = Path("/proc") / str(pid)
    try:
        stat = (entry / "stat").read_text()
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        rest = stat[stat.rindex(")") + 2 :].split()
        cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
    except (OSError, ValueError):
        return None
    return {
        "pid": pid,
        "comm": comm,
        "state": rest[0],
        "ppid": int(rest[1]),
        "cmdline": cmdline.replace("\x00", " ").strip()[:160],
    }


def tree(root: int) -> list[dict]:
    """Every live descendant of `root`, by ppid walk."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        info = proc_info(int(entry.name))
        if info:
            parents[info["pid"]] = info["ppid"]
    out = []
    for pid in parents:
        seen = pid
        for _ in range(24):
            parent = parents.get(seen)
            if parent is None or parent <= 1:
                break
            if parent == root:
                info = proc_info(pid)
                if info:
                    out.append(info)
                break
            seen = parent
    return sorted(out, key=lambda i: i["pid"])


def read_cgroup(pid: int | None) -> str | None:
    """A process's cgroup path, which is what says the scope really applied.

    `scope.available()` answering True means `systemd-run --user --scope` can
    run, not that this process went into one. The kernel's own answer is the
    only one worth recording, and it is the same file `agy/hook.py` reads to
    recognise its own calls.
    """
    if not pid:
        return None
    try:
        raw = (Path("/proc") / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2].strip()
    return raw.strip()[:200] or None


def agy_processes() -> list[dict]:
    """Every live process whose command line is the `agy` binary itself."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        info = proc_info(int(entry.name))
        if info and info["comm"] == "agy":
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# The host, which is this file run again with --host
# ---------------------------------------------------------------------------

async def _be_the_host(repo: Path, handshake: Path, close_stdin: bool) -> int:
    """Start one `agy` session, report what it spawned, and wait to be killed.

    A separate process rather than a thread, because the thing being killed
    has to be a whole host: SIGKILL is not deliverable to a thread, and the
    file descriptors that hold `agy`'s stdin open belong to the process.
    """
    from aic_dc.agy.gate_server import AgyGateServer
    from aic_dc.agy.session import AgySession
    from aic_dc.antigravity.permissions import AntigravityPermissionGate

    async def broadcast(_event: object) -> None:
        return None

    gate = AntigravityPermissionGate(
        repo, broadcast=broadcast, localhost_available=lambda: True
    )
    server = AgyGateServer(repo / "gate.sock", gate=gate, config_dir=CONFIG_DIR)
    session = AgySession(repo, gate=server)
    conversation = await session.start()

    spawned = session._proc.pid  # noqa: SLF001 - the pid is the measurement
    handshake.write_text(
        json.dumps(
            {
                "host_pid": os.getpid(),
                "spawned_pid": spawned,
                "conversation_id": conversation,
                "scope_available": scope.available(),
            }
        ),
        encoding="utf-8",
    )
    if close_stdin:
        # Scenario B: the pipe goes, the host stays. Nothing else changes.
        session._proc.stdin.close()  # noqa: SLF001 - the point of the scenario
        (handshake.parent / "stdin-closed").write_text("1", encoding="utf-8")
    while True:  # Killed from outside. There is no clean exit from here.
        await asyncio.sleep(3600)


def start_host(repo: Path, handshake: Path, *, close_stdin: bool) -> dict:
    """Spawn the host and wait for it to say what it started."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host",
        "--repo",
        str(repo),
        "--handshake",
        str(handshake),
    ]
    if close_stdin:
        cmd.append("--close-stdin")
    proc = subprocess.Popen(cmd, start_new_session=True)
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the host exited before starting agy ({proc.returncode})")
        if handshake.is_file() and handshake.stat().st_size:
            data = json.loads(handshake.read_text(encoding="utf-8"))
            data["popen"] = proc
            return data
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("the host never reported an agy session within 180s")


# ---------------------------------------------------------------------------
# The measurements
# ---------------------------------------------------------------------------

def watch_exit(pid: int, ceiling: float, poll: float = 0.1) -> float | None:
    """Seconds until `pid` is gone, or `None` if it outlived the ceiling."""
    start = time.time()
    while time.time() - start < ceiling:
        if proc_info(pid) is None:
            return time.time() - start
        time.sleep(poll)
    return None


def registry_verdict(conversation: str) -> dict:
    """What the gate would decide about this conversation right now."""
    path = registry.registry_dir(CONFIG_DIR) / f"{conversation}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entry": None, "live": None, "owns_anything": registry.owns_anything(CONFIG_DIR)}
    return {
        "entry": {k: entry.get(k) for k in ("pid", "agy_pid", "socket")},
        "live": registry.entry_is_live(entry),
        "owns_anything": registry.owns_anything(CONFIG_DIR),
    }


def scenario_kill(repo: Path, ceiling: float, index: int = 0) -> dict:
    """SIGKILL the host and watch what it leaves behind.

    `index` names this run's handshake file. A shared name would have run 2
    reading run 1's answer the moment it looked, and reporting on a pid that
    died a minute earlier — the whole measurement, silently taken from the
    wrong process.
    """
    handshake = repo / f"handshake-kill-{index}.json"
    started = start_host(repo, handshake, close_stdin=False)
    conversation = started["conversation_id"]
    spawned = started["spawned_pid"]
    host_pid = started["host_pid"]

    spawned_info = proc_info(spawned)
    spawned_cgroup = read_cgroup(spawned)
    before = tree(host_pid)
    log(f"host {host_pid} spawned pid {spawned}: {spawned_info}")
    log(f"scope available: {started['scope_available']}; cgroup: {spawned_cgroup}")
    log(f"agy processes on this machine: {[i['pid'] for i in agy_processes()]}")
    for row in before:
        log(f"  under the host: {row['pid']} {row['comm']} ppid={row['ppid']}")

    # The `agy` this session is really talking to, found by descent from the
    # host rather than by scanning the machine: the user's own sessions are
    # running too, and a probe that killed one of those would be reporting on
    # something it broke.
    real_agy = [i for i in before if i["comm"] == "agy"]

    verdict_before = registry_verdict(conversation)
    log(f"registry before the kill: {verdict_before}")

    os.kill(host_pid, signal.SIGKILL)
    killed_at = time.time()
    # Reaped, or it lingers as a zombie: `/proc/<pid>` survives an unwaited
    # child and `os.kill(pid, 0)` succeeds on one, so `process_alive` would
    # answer True over a process that is dead in every sense that matters.
    # The first run of this probe measured exactly that and read the registry
    # as holding a live claim on two dead pids.
    try:
        started["popen"].wait(timeout=15)
    except subprocess.TimeoutExpired:
        log("the host did not reap within 15s")
    host_gone = watch_exit(host_pid, 30.0)
    log(
        "host gone after "
        + (f"{host_gone:.2f}s" if host_gone is not None else "never (still in /proc)")
    )

    time.sleep(0.2)
    survived = proc_info(spawned)
    survivors = [i for i in (real_agy or []) if proc_info(i["pid"])]
    log(f"recorded pid {spawned} 200 ms after the kill: {survived}")
    for row in survivors:
        info = proc_info(row["pid"])
        log(f"  agy {row['pid']} still alive, reparented to ppid={info['ppid']}")

    verdict_after = registry_verdict(conversation)
    log(f"registry 200 ms after the kill: {verdict_after}")

    lifetime = watch_exit(spawned, ceiling) if survived else 0.0
    agy_lifetimes = {
        row["pid"]: watch_exit(row["pid"], max(ceiling - (time.time() - killed_at), 1.0))
        for row in survivors
    }
    verdict_settled = registry_verdict(conversation)
    reaped = registry.reap_stale(CONFIG_DIR)
    log(f"registry once agy is gone: {verdict_settled}; reap_stale -> {reaped}")

    for row in survivors:
        if proc_info(row["pid"]):
            log(f"  killing the orphan {row['pid']} the probe created")
            try:
                os.kill(row["pid"], signal.SIGKILL)
            except OSError:
                pass
    registry.release(conversation, config_dir=CONFIG_DIR)

    return {
        "conversation": conversation,
        "host_pid": host_pid,
        "recorded_agy_pid": spawned,
        "recorded_pid_is": (spawned_info or {}).get("comm"),
        "recorded_pid_cgroup": spawned_cgroup,
        "scope_available": started["scope_available"],
        "host_gone_after_s": host_gone,
        "recorded_pid_survived_the_kill": survived is not None,
        "recorded_pid_lifetime_s": lifetime,
        "agy_pids_under_host": [i["pid"] for i in real_agy],
        "agy_lifetimes_s": agy_lifetimes,
        "registry_before": verdict_before,
        "registry_after_kill": verdict_after,
        "registry_settled": verdict_settled,
        "reaped": reaped,
    }


def scenario_stdin(repo: Path, ceiling: float) -> dict:
    """Close `agy`'s stdin with the host alive. Does it exit on EOF?"""
    handshake = repo / "handshake-stdin.json"
    started = start_host(repo, handshake, close_stdin=True)
    conversation = started["conversation_id"]
    spawned = started["spawned_pid"]
    host_pid = started["host_pid"]
    log(f"host {host_pid} closed the stdin pipe of {spawned}")

    lifetime = watch_exit(spawned, ceiling)
    log(
        f"recorded pid {spawned} after stdin EOF: "
        + (f"exited after {lifetime:.2f}s" if lifetime else f"still alive at {ceiling}s")
    )
    verdict = registry_verdict(conversation)

    try:
        os.kill(host_pid, signal.SIGKILL)
    except OSError:
        pass
    watch_exit(host_pid, 30.0)
    if proc_info(spawned):
        try:
            os.kill(spawned, signal.SIGKILL)
        except OSError:
            pass
    registry.release(conversation, config_dir=CONFIG_DIR)

    return {
        "conversation": conversation,
        "recorded_agy_pid": spawned,
        "exited_on_stdin_eof_after_s": lifetime,
        "registry_after_eof": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--handshake", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--close-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--ceiling",
        type=float,
        default=120.0,
        help="how long to wait for an orphan to exit before calling it long-lived",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="how many times to repeat the kill scenario — one run is a fact, "
        "not a distribution",
    )
    parser.add_argument("--skip-stdin", action="store_true")
    args = parser.parse_args()

    if args.host:
        return asyncio.run(
            _be_the_host(
                Path(args.repo), Path(args.handshake), args.close_stdin
            )
        )

    if not shutil.which("agy"):
        log("no agy on PATH; nothing to measure")
        return 2

    repo = probe_root("agy-orphan-")
    results: dict[str, object] = {"runs": []}
    try:
        for index in range(args.runs):
            log(f"--- kill scenario, run {index + 1} of {args.runs} ---")
            results["runs"].append(scenario_kill(repo, args.ceiling, index))
        if not args.skip_stdin:
            log("--- stdin-EOF scenario ---")
            results["stdin"] = scenario_stdin(repo, args.ceiling)
    finally:
        shutil.rmtree(repo, ignore_errors=True)

    print("\n" + "=" * 70)
    print(json.dumps(results, indent=2, default=str))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
