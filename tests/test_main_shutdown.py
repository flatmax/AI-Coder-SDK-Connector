"""Tests for the CLI-child kill on shutdown — conversion phase 2.

``main._signal_handler`` exits via ``os._exit`` to avoid hanging on
``_heavy_init``'s sentence-transformer load in the default executor. That
also skips ``atexit``, and the Claude Agent SDK's *only* orphan guard is
an ``atexit`` hook, so the CLI child has to be killed explicitly.

Measured before the fix: SIGINT during a streaming turn left the
``claude`` process running for a further ~38 seconds, reparented to init,
still holding the repo as its working directory.

These tests spawn real processes rather than mocking ``os.kill``, because
the thing under test *is* the signal delivery — a mock would pass whether
or not the signal reached anything.

They cannot assert on ``Popen.returncode``: ``_kill_cli_children`` reaps
with ``waitpid`` by design, which consumes the status ``Popen`` would
have read, and ``Popen.wait`` then reports 0 for a child that was
signalled. So "it was asked politely first" is asserted from a marker
file the child writes from its own SIGTERM handler, and everything else
from the pid being gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from ac_dc.main import _child_exited, _kill_cli_children

# Sleeps until killed. No SIGTERM handler, so it dies on the polite one.
SLEEPER = "import time; time.sleep(60)"

# Writes a marker from its SIGTERM handler, then exits. The marker is how
# a test can tell SIGTERM from SIGKILL after the status has been reaped.
POLITE = (
    "import signal, sys, time\n"
    "def _bye(*_):\n"
    "    open(sys.argv[1], 'w').write('term')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, _bye)\n"
    "open(sys.argv[1] + '.ready', 'w').write('1')\n"
    "time.sleep(60)\n"
)

# Declines SIGTERM outright: only the escalation can end it.
STUBBORN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1] + '.ready', 'w').write('1')\n"
    "time.sleep(60)\n"
)


class _FakeChild:
    """Stands in for an ``anyio.abc.Process`` in the SDK's registry.

    The registry is only ever read for ``.pid``, so a real subprocess
    plus a pid holder is a truer fixture than a mocked Process.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid


def _gone(pid: int) -> bool:
    """Whether ``pid`` is neither running nor waiting to be reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture
def registry(monkeypatch):
    """An isolated stand-in for the SDK's module-level child set.

    Patched rather than added to: a leaked entry would make a later test
    signal a pid it does not own.
    """
    from claude_agent_sdk._internal.transport import subprocess_cli

    children: set = set()
    monkeypatch.setattr(subprocess_cli, "_ACTIVE_CHILDREN", children)
    return children


@pytest.fixture
def spawn(tmp_path):
    """Spawn test processes; guarantee they are dead and reaped after.

    Waits for the child's own readiness marker where it has one, so a
    test never races the installation of the handler it is testing.
    """
    spawned: list[subprocess.Popen] = []
    counter = [0]

    def _spawn(code: str) -> tuple[subprocess.Popen, object]:
        counter[0] += 1
        marker = tmp_path / f"marker-{counter[0]}"
        process = subprocess.Popen([sys.executable, "-c", code, str(marker)])
        spawned.append(process)
        ready = marker.with_suffix(marker.suffix + ".ready")
        if "ready" in code:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "child never signalled readiness"
        return process, marker

    yield _spawn

    for process in spawned:
        with suppress(OSError):
            if process.poll() is None:
                process.kill()
        with suppress(ChildProcessError, subprocess.TimeoutExpired):
            process.wait(timeout=5)


class TestKillCliChildren:
    def test_a_tracked_child_is_asked_politely_first(self, registry, spawn):
        """SIGTERM, not SIGKILL: the CLI may flush transcript state on it.

        Asserted from the child's own SIGTERM handler, because the exit
        status has been reaped by the code under test.
        """
        child, marker = spawn(POLITE)
        registry.add(_FakeChild(child.pid))

        _kill_cli_children(grace=5.0)

        assert marker.read_text() == "term"
        assert _gone(child.pid)

    def test_a_child_that_ignores_sigterm_is_killed_anyway(self, registry, spawn):
        """The case the SDK's signal-and-go guard does not cover.

        A wedged CLI declining SIGTERM is the only way this function can
        leave the orphan it exists to prevent.
        """
        child, _marker = spawn(STUBBORN)
        registry.add(_FakeChild(child.pid))

        _kill_cli_children(grace=0.2)

        # The hard kill lands after the grace; the child is not ours to
        # reap at that point, so allow the kernel a moment.
        deadline = time.monotonic() + 5
        while not _gone(child.pid) and time.monotonic() < deadline:
            try:
                os.waitpid(child.pid, os.WNOHANG)
            except ChildProcessError:
                break
            time.sleep(0.02)
        assert _gone(child.pid)

    def test_the_wait_ends_when_the_child_does(self, registry, spawn):
        """Ctrl-C has to feel immediate when the child goes quietly.

        The regression this pins: probing liveness with ``kill(pid, 0)``
        instead of reaping sees every killed child as a live zombie and
        serves out the whole grace period every time.
        """
        child, _marker = spawn(POLITE)
        registry.add(_FakeChild(child.pid))

        start = time.monotonic()
        _kill_cli_children(grace=5.0)
        elapsed = time.monotonic() - start

        assert _gone(child.pid)
        assert elapsed < 1.0

    def test_an_empty_registry_returns_at_once(self, registry):
        """No child means no grace period to serve out."""
        start = time.monotonic()
        _kill_cli_children(grace=5.0)
        assert time.monotonic() - start < 0.1

    def test_every_child_is_signalled_not_just_the_first(self, registry, spawn):
        """Subagent transports are separate children of the same server."""
        children = [spawn(POLITE) for _ in range(3)]
        for child, _marker in children:
            registry.add(_FakeChild(child.pid))

        _kill_cli_children(grace=5.0)

        assert [marker.read_text() for _child, marker in children] == ["term"] * 3

    def test_a_dead_child_is_not_an_error(self, registry, spawn):
        """The registry is not pruned on exit, so stale entries are normal."""
        child, _marker = spawn(SLEEPER)
        child.kill()
        child.wait()
        registry.add(_FakeChild(child.pid))

        _kill_cli_children(grace=0.05)  # must not raise

    def test_a_child_without_a_pid_is_skipped(self, registry, spawn):
        """One unusable entry must not spare the others."""
        broken = _FakeChild(0)
        del broken.pid
        registry.add(broken)
        child, marker = spawn(POLITE)
        registry.add(_FakeChild(child.pid))

        _kill_cli_children(grace=5.0)

        assert marker.read_text() == "term"

    def test_a_moved_sdk_registry_degrades_quietly(self, monkeypatch):
        """Private SDK surface.

        An SDK that relocates the set should cost us the fix, not the
        ability to exit: the handler's next statement is ``os._exit``.
        """
        monkeypatch.setitem(
            sys.modules, "claude_agent_sdk._internal.transport", object()
        )
        _kill_cli_children()  # must not raise


class TestChildExited:
    def test_a_running_child_has_not_exited(self, spawn):
        child, _marker = spawn(SLEEPER)
        assert _child_exited(child.pid) is False

    def test_a_zombie_counts_as_exited_and_is_reaped(self, spawn):
        """The whole reason this is not a ``kill(pid, 0)`` probe."""
        child, _marker = spawn(SLEEPER)
        child.kill()
        deadline = time.monotonic() + 5
        while not _child_exited(child.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _child_exited(child.pid) is True
        assert _gone(child.pid)

    def test_a_pid_that_is_not_our_child_counts_as_exited(self):
        """We cannot wait on it, so there is nothing left to wait for.

        PID 1 is never a child of ours, and never exits.
        """
        assert _child_exited(1) is True
