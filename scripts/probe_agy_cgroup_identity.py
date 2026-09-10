#!/usr/bin/env python3
"""Can the gate identify its own sessions by **cgroup** instead of by claim?

[AG-R-14](../specs5/plan-ag/risks.md#ag-r-14) has two residues that share one
cause: identity is routed by a ``conversationId`` somebody has to write into a
registry *in advance*. A grandchild is never announced, so nobody writes it
down; and a child starts before its announcement arrives, so the write can lose
a race. Both are properties of the mechanism, not of the code around it.

**The candidate is a kernel-enforced group.** Launch ``agy`` inside a cgroup of
our own and have the hook read ``/proc/self/cgroup``. cgroup v2 binds every
descendant irrevocably — an unprivileged process cannot move itself out, and
``fork``, ``env -i``, a subshell and a double-fork all stay inside it. If that
holds, the hook can answer "is this mine?" at the instant it runs, with nothing
written down beforehand, which closes both residues and lets a conversation be
adopted on first contact.

Two userspace schemes were considered first and both are unsound, which is why
this measures the kernel one: an inherited environment marker is mutable data
belonging to the process it identifies (and ``env -i`` strips it in perfectly
benign tooling), and a private config root has to travel by environment — which
reduces it to the first — or by argv, which a shell-spawned process does not
inherit.

What this measures, and the control that makes it mean something
---------------------------------------------------------------
A single scoped run proves nothing on its own: if the matcher is wrong in the
permissive direction, everything looks like ours. So this runs **two** turns —
one inside a scope of ours, one outside it, the way a user's own session runs —
and requires them to disagree. ``probe_engine_policy.py`` is the precedent, and
it exists because a Claude-only assertion was already true on a machine with no
``agy`` installed.

The four questions, in the order a failure would stop the design:

1. Is ``systemd-run --user --scope`` available, and does ``agy`` run normally
   inside one? A transport that will not start is the end of the idea.
2. Does the **hook subprocess** land in our scope? This is the decisive one:
   the hook is spawned by ``agy``, not by us, and it is the process that has to
   answer the question.
3. Does a **subagent's** hook call land in our scope? Residue 1 is about
   delegation, and a mechanism that covers the parent only is the mechanism we
   already have.
4. Does a session started **outside** the scope look different? This is
   [AG-R-12](../specs5/plan-ag/risks.md#ag-r-12), which is non-negotiable: a
   user's own ``agy`` must never reach our dialog.

How the hook is instrumented
----------------------------
``install(config_dir, python=<wrapper>)`` writes the ordinary command with a
substitute interpreter, so the wrapper below is what ``agy`` executes. It
records its own cgroup and the payload, then execs the real interpreter with
the same arguments — so the gate still runs and the turn behaves normally.
Nothing hand-edits ``hooks.json``, and the record path is baked into the
wrapper as a literal rather than passed in the environment, because the
environment is the thing this probe exists to stop relying on.

    uv run python scripts/probe_agy_cgroup_identity.py

Exit 0: cgroup identity holds — scoped calls match, unscoped calls do not.
Exit 1: it does not hold, and the reason is named.
Exit 2: nothing to conclude (no agy, no systemd, or no turn completed).

What it costs: two turns on the paid subscription, in a temporary directory
that is removed afterwards. It takes no writes — every tool call is allowed and
the prompts only read.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-R-12, AG-R-14.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

TURN_TIMEOUT_SECONDS = 420.0

#: A turn that makes the agent use a tool without changing anything. The
#: question is which cgroup the hook runs in, not what the model says.
READ_PROMPT = (
    "List the files in {root} using your file listing tool, then say DONE. "
    "Do not create, edit or delete anything."
)

#: Best-effort: residue 1 is about delegation, so a subagent's hook call is
#: the interesting one. Reported as inconclusive rather than failed when no
#: subagent is announced — the model chooses whether to delegate.
DELEGATE_PROMPT = (
    "Delegate to a subagent. Instruct the subagent to list the files in "
    "{root} using its file listing tool and report what it found. Do not "
    "create, edit or delete anything."
)


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def own_cgroup() -> str:
    """This process's cgroup path, or ``""`` where the file does not exist."""
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_wrapper(path: Path, record: Path) -> Path:
    """A hook interpreter that records its cgroup, then runs the real one.

    ``exec``s rather than returning, so the real hook owns the exit status —
    the installed command's ``|| printf allow`` fallback keys on that, and a
    wrapper that swallowed a non-zero exit would silently turn a fail-closed
    hook into a fail-open one for the duration of this probe.

    The payload is read and re-fed rather than passed through, because the
    conversation id is in it and this probe's whole question is which
    conversations arrive in which cgroup.
    """
    path.write_text(
        "#!/bin/sh\n"
        'payload=$(cat)\n'
        "cg=$(cat /proc/self/cgroup 2>/dev/null | tr '\\n' ' ')\n"
        'printf \'{"cgroup": "%s", "payload": %s}\\n\' "$cg" "$payload"'
        f" >> {record}\n"
        f'printf \'%s\' "$payload" | exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def records(path: Path) -> list[dict[str, Any]]:
    """Every hook invocation the wrapper recorded, best effort.

    A malformed line is skipped rather than fatal: the wrapper appends from
    several short-lived processes at once, and one interleaved write should
    cost one observation rather than the run.
    """
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def tool_of(entry: dict[str, Any]) -> str:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return "?"
    return str((payload.get("toolCall") or {}).get("name", "?"))


def conversation_of(entry: dict[str, Any]) -> str:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return "?"
    return str(payload.get("conversationId") or "?")


def systemd_available() -> str:
    """``""`` if a user scope can be created, else why it cannot."""
    if not shutil.which("systemd-run"):
        return "systemd-run is not on PATH"
    if not Path("/proc/self/cgroup").exists():
        return "/proc/self/cgroup does not exist (not Linux, or no procfs)"
    probe = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit=aic-dc-cgroup-probe-{uuid.uuid4().hex[:8]}",
            "true",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return f"systemd-run --user --scope failed: {probe.stderr.strip()[:200]}"
    return ""


async def take_turn(work: Path, prompt: str, config_dir: Path) -> tuple[str, list]:
    """One `agy` turn with the gate wired up and every call allowed.

    Allowing everything is deliberate. This probe is about *which cgroup the
    hook ran in*, and a deny would end the turn before later calls — including
    a subagent's — ever reached it.
    """

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = AgyGateServer(
        work / f"gate-{uuid.uuid4().hex[:8]}.sock", gate=gate, config_dir=config_dir
    )
    session = AgySession(work, gate=server)

    async def allow_everything() -> None:
        while True:
            for payload in gate.broker.pending():
                permission_id = payload.get("permission_id")
                if permission_id:
                    await gate.broker.resolve(
                        permission_id, {"action": "allow"}, resolved_by="probe"
                    )
            await asyncio.sleep(0.05)

    browser = asyncio.ensure_future(allow_everything())
    frames: list = []
    conversation = ""
    try:
        conversation = await session.start()
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for frame in session.stream_frames(prompt.format(root=work)):
                frames.append(frame)
    except TimeoutError:
        log(f"turn did not finish within {TURN_TIMEOUT_SECONDS:.0f}s")
    finally:
        browser.cancel()
        await session.close()
        await server.stop()
    return conversation, frames


def subagent_ids(frames: list[dict[str, Any]]) -> list[str]:
    """Every subagent conversation id a turn announced.

    Lifted from ``probe_agy_subagent_gate.py``; the frame shape is measured
    there and in ``probe_agy_subagent_frames.py``.
    """
    ids = []
    for frame in frames:
        inner = frame.get("step_update")
        if not isinstance(inner, dict):
            continue
        step = inner.get("step") if isinstance(inner.get("step"), dict) else inner
        info = step.get("subagent_info")
        if not isinstance(info, dict):
            continue
        for entry in info.get("subagents") or []:
            if isinstance(entry, dict) and entry.get("conversation_id"):
                ids.append(str(entry["conversation_id"]))
    return ids


def check(unit: str, scoped: list, unscoped: list, children: list[str]) -> int:
    """The four questions, answered against what the hook actually recorded."""
    log("")
    log(f"scope unit: {unit}")
    log(f"hook invocations inside the scope:  {len(scoped)}")
    log(f"hook invocations outside the scope: {len(unscoped)}")

    if not scoped:
        log(
            "INCONCLUSIVE: the hook never ran during the scoped turn, so the "
            "agent took no tool call and there is nothing to read a cgroup from"
        )
        return 2

    matched = [e for e in scoped if unit in e.get("cgroup", "")]
    missed = [e for e in scoped if unit not in e.get("cgroup", "")]
    log(f"  matched our unit: {len(matched)}  ({[tool_of(e) for e in matched]})")
    if missed:
        log(f"  did NOT match:    {len(missed)}  ({[tool_of(e) for e in missed]})")
        log(f"  a missed cgroup looked like: {missed[0].get('cgroup', '')!r}")

    if not matched:
        log(
            "FAIL: no hook invocation from the scoped turn carried our unit in "
            "its cgroup. The hook does not inherit the scope, so it cannot use "
            "one to identify us and this design is dead as measured."
        )
        return 1
    if missed:
        log(
            "FAIL: some of the scoped turn's hook calls carried our unit and "
            "some did not. A gate that identifies only some of its own calls "
            "is not a gate — the ones it misses are exactly the ungated ones."
        )
        return 1

    # Question 3 — the subagent, which is what residue 1 is about.
    child_calls = [e for e in matched if conversation_of(e) in children]
    if children and child_calls:
        log(
            f"a subagent's calls carried our unit too: "
            f"{[tool_of(e) for e in child_calls]} — delegation is covered "
            f"without anything being announced or claimed"
        )
    elif children:
        log(
            "NOTE: a subagent was announced but none of its calls reached the "
            "hook, so the delegation half is unproven by this run"
        )
    else:
        log(
            "NOTE: no subagent was announced, so residue 1's own case is "
            "unproven here — the model chose not to delegate"
        )

    # Question 4 — the control. Without this the run above proves nothing.
    if not unscoped:
        log(
            "INCONCLUSIVE: the unscoped control turn produced no hook calls, "
            "so the discriminator is untested and a matcher that says yes to "
            "everything would have passed everything above"
        )
        return 2
    leaked = [e for e in unscoped if unit in e.get("cgroup", "")]
    if leaked:
        log(
            f"FAIL: {len(leaked)} hook call(s) from the UNSCOPED turn carried "
            f"our unit. The discriminator does not discriminate, and AG-R-12 "
            f"says a session of the user's own must never be ours."
        )
        return 1
    log(
        f"the unscoped turn's {len(unscoped)} hook call(s) carried a different "
        f"cgroup, e.g. {unscoped[0].get('cgroup', '')[:90]!r}"
    )

    log("")
    log(
        "PASS: the hook inherits our cgroup, every scoped call matched, and a "
        "session started outside the scope did not. Identity can be answered "
        "from /proc/self/cgroup at the instant the hook runs, with nothing "
        "written down in advance — which is what closes both residues."
    )
    return 0


async def inner(work: Path, unit: str) -> int:
    """The scoped half. Runs *inside* the systemd scope, re-entered by `main`."""
    config_dir = Path.home() / ".config" / "aic-dc"
    record = work / "hook-records.jsonl"
    wrapper = write_wrapper(work / "hook-wrapper.sh", record)

    state = install.status(config_dir)
    restore = state["state"]
    log(f"installed gate before this run: {restore}")
    log("installing the instrumented hook for the duration of this probe")
    install.install(config_dir, python=str(wrapper))
    try:
        log(f"scoped turn, in {unit}")
        _, frames = await take_turn(work, DELEGATE_PROMPT, config_dir)
        children = subagent_ids(frames)
        log(f"subagents announced: {children or '(none)'}")
        scoped = records(record)
        (work / "scoped.json").write_text(
            json.dumps({"records": scoped, "children": children}), encoding="utf-8"
        )
        record.unlink(missing_ok=True)
    finally:
        if restore == "absent":
            install.uninstall()
        else:
            install.install(config_dir)
            if restore != "current":
                log(f"NOTE: the gate was {restore} before this run and is now current")
    return 0


async def outer() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2
    why_not = systemd_available()
    if why_not:
        log(f"cannot create a user scope: {why_not}")
        log(
            "This design is Linux-and-systemd only. Where it is unavailable the "
            "fallback is the recorded one: refuse a spawn that arrives from an "
            "already-claimed subagent conversation (AG-R-14), which costs the "
            "nested-delegation capability."
        )
        return 2

    work = probe_root("agy-cgroup-")
    unit = f"aic-dc-probe-{uuid.uuid4().hex[:8]}"
    log(f"workspace {work}")
    log(f"this process's cgroup: {own_cgroup()[:100]}")

    # The scoped half re-enters this file inside a systemd scope, so that
    # `agy` — and every hook it spawns — is a descendant of the scope rather
    # than of this shell. Running the session in-process here and only
    # wrapping `agy` would measure a narrower thing than the design needs.
    scoped_run = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit}",
            sys.executable,
            str(Path(__file__).resolve()),
            "--inner",
            str(work),
            unit,
        ],
        text=True,
    )
    if scoped_run.returncode != 0:
        log(f"the scoped half exited {scoped_run.returncode}")
        shutil.rmtree(work, ignore_errors=True)
        return 2

    scoped_blob = work / "scoped.json"
    if not scoped_blob.exists():
        log("the scoped half wrote no records")
        shutil.rmtree(work, ignore_errors=True)
        return 2
    blob = json.loads(scoped_blob.read_text(encoding="utf-8"))
    scoped, children = blob["records"], blob["children"]

    # The control: the same turn, from this process, which is NOT in the
    # scope — the shape a session the user started in a terminal has.
    config_dir = Path.home() / ".config" / "aic-dc"
    record = work / "hook-records.jsonl"
    wrapper = work / "hook-wrapper.sh"
    state = install.status(config_dir)
    restore = state["state"]
    install.install(config_dir, python=str(wrapper))
    try:
        log("")
        log("control turn, outside any scope of ours")
        await take_turn(work, READ_PROMPT, config_dir)
        unscoped = records(record)
    finally:
        if restore == "absent":
            install.uninstall()
        else:
            install.install(config_dir)

    code = check(unit, scoped, unscoped, children)
    if code == 0:
        shutil.rmtree(work, ignore_errors=True)
    else:
        log(f"evidence kept at {work}")
    return code


def main() -> int:
    if "--inner" in sys.argv:
        index = sys.argv.index("--inner")
        work = Path(sys.argv[index + 1])
        unit = sys.argv[index + 2]
        return asyncio.run(inner(work, unit))
    return asyncio.run(outer())


if __name__ == "__main__":
    raise SystemExit(main())
