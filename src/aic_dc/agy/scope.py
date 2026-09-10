"""Put ``agy`` in a cgroup of ours, so the hook can recognise its own.

The gate of [AG-14](../../../specs5/plan-ag/decisions.md#ag-14) routes by
**conversation id**: the host writes a claim, the hook looks it up, and a
conversation nobody claimed is passed through ungated. That passthrough is
load-bearing — it is what keeps a second ``agy`` session belonging to the user
out of our dialog ([AG-R-12](../../../specs5/plan-ag/risks.md#ag-r-12)) — and it
is also where two holes live
([AG-R-14](../../../specs5/plan-ag/risks.md#ag-r-14)):

- a subagent that spawns a **further** subagent announces the grandchild to
  nobody, so nobody claims it and it runs unreviewed;
- ``agy`` starts a child *before* the frame announcing it arrives, so the
  child's first call can beat the claim onto disk.

Both are properties of "identity is something somebody has to write down in
advance". This module removes that premise for the platforms that can: launch
``agy`` inside a **systemd user scope**, and let the hook ask the kernel which
group it is in.

Why the kernel and not a marker of our own
------------------------------------------
Two userspace schemes were tried first and both are unsound:

- **An inherited environment variable.** It is mutable data belonging to the
  process it identifies, and [AG-R-11](../../../specs5/plan-ag/risks.md#ag-r-11)
  already measured this agent reaching for ``run_command`` when a write was
  refused. It is worse than the adversarial case: ``env -i``, build runners and
  ``subprocess(env={})`` scrub environments as ordinary practice, so it fails
  open on benign tooling.
- **Walking the process ancestry.** One step better, and still defeated by a
  standard double-fork, which reparents to ``init``.

cgroup v2 has neither weakness, because membership is enforced by the kernel
rather than carried by the process: every descendant is bound to the group and
an unprivileged process cannot move itself out. Measured on 2026-09-09 —
a direct child, a subshell, an ``env -i`` and a ``setsid`` double-fork all
reported the same scope — and then end to end by
``scripts/probe_agy_cgroup_identity.py``, where the hook subprocess ``agy``
spawns inherited it, **a subagent's calls carried it**, and a turn taken outside
the scope reported the terminal's own cgroup instead.

What this does not cover
------------------------
This is Linux-and-systemd, and the packaging is not. :func:`available` answers
that question once, and everything here degrades to "no scope", which is the
behaviour that shipped before it — conversation-id routing, with AG-R-14's
residues intact. That is a per-platform gap rather than a design one, and it is
stated rather than hidden: the alternative for those platforms is refusing
nested delegation outright, which costs a capability.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-R-12, AG-R-14.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where a process's cgroup membership is readable. Linux only, and its
#: absence is one of the ways :func:`available` says no.
CGROUP_FILE = Path("/proc/self/cgroup")

#: Prefix for the scope units this host creates. Only ever used to *build* a
#: name — never to recognise one, because recognition matches against the
#: units this host actually registered rather than against a naming
#: convention a stranger could imitate.
UNIT_PREFIX = "aic-dc"

#: How long the availability probe may take. It runs ``true`` inside a scope,
#: which is milliseconds when systemd is healthy; a bound exists because this
#: sits in the path of starting a session and a hung probe would look like a
#: hung engine.
PROBE_TIMEOUT_SECONDS = 10.0


def unit_name(suffix: str | None = None) -> str:
    """A scope unit name for one session.

    Unique per session rather than per host: two windows of this app on one
    machine are two independent gates with two sockets, and a shared unit
    would route one's subagents to the other's dialog.
    """
    return f"{UNIT_PREFIX}-{suffix or uuid.uuid4().hex[:12]}"


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether this machine can put a child in a user scope.

    Cached, because it is a property of the machine and it costs a
    subprocess. The check **runs a scope** rather than testing for the
    binary: ``systemd-run`` exists in containers and on machines with no
    running user manager, where it is present and fails, and a capability
    that is asserted rather than exercised is how this directory has been
    wrong before.
    """
    if not CGROUP_FILE.exists():
        logger.debug("No %s; agy will run unscoped", CGROUP_FILE)
        return False
    if not shutil.which("systemd-run"):
        logger.debug("systemd-run is not on PATH; agy will run unscoped")
        return False
    try:
        probe = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                f"--unit={unit_name('probe-' + uuid.uuid4().hex[:8])}",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not create a user scope (%s); agy will run unscoped", exc)
        return False
    if probe.returncode != 0:
        logger.debug(
            "systemd-run --user --scope failed (%s); agy will run unscoped",
            probe.stderr.strip()[:200],
        )
        return False
    return True


def wrap(argv: list[str], unit: str | None) -> list[str]:
    """``argv``, launched inside ``unit`` — or unchanged when there is none.

    ``--quiet`` because systemd otherwise writes *"Running as unit …"* to
    stderr, and this transport's stderr is read for diagnostics when a
    session fails to start (``The startup wall of ERROR``): a line that
    always appears trains a reader to skip the ones that matter.
    """
    if not unit:
        return argv
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit}",
        *argv,
    ]


def current_cgroup() -> str:
    """This process's cgroup path, or ``""`` where there is none to read.

    Read fresh every time rather than cached: the hook is a short-lived
    process that reads it once, and the *host* must not answer this question
    from a value it captured before it was placed in a scope.
    """
    try:
        return CGROUP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
