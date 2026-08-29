"""Tests for what ``os._exit`` skips on shutdown — phases 2 and 5.

``main._signal_handler`` exits via ``os._exit`` to avoid hanging on
``_heavy_init``'s sentence-transformer load in the default executor. That
also skips ``atexit`` and every teardown that only runs inside the event
loop, and the SDK puts two things there.

**The CLI child** (phase 2). The SDK's *only* orphan guard is an
``atexit`` hook, so the child has to be killed explicitly. Measured
before the fix: SIGINT during a streaming turn left the ``claude``
process running for a further ~38 seconds, reparented to init, still
holding the repo as its working directory.

**The resumed session's temp config dir** (phase 5, found by the live
verification). The SDK removes it in ``disconnect()``, which this path
never reaches, so every Ctrl-C abandoned one holding a transcript copy
and a live access token — once per launch cycle, since auto-resume makes
every start after the first a resume.

These tests spawn real processes rather than mocking ``os.kill``, because
the thing under test *is* the signal delivery — a mock would pass whether
or not the signal reached anything. For the same reason the temp-dir
tests use real directories rather than asserting on a mocked ``rmtree``.

They cannot assert on ``Popen.returncode``: ``_kill_cli_children`` reaps
with ``waitpid`` by design, which consumes the status ``Popen`` would
have read, and ``Popen.wait`` then reports 0 for a child that was
signalled. So "it was asked politely first" is asserted from a marker
file the child writes from its own SIGTERM handler, and everything else
from the pid being gone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest

from aic_dc.claude_code import resume_cleanup
from aic_dc.main import (
    _SHUTDOWN_GRACE,
    _child_exited,
    _install_exit_handlers,
    _kill_cli_children,
    _kill_vite,
    _purge_resume_dirs,
    _shut_the_engine_down,
)

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


def _stopped(pid: int) -> bool:
    """Whether ``pid`` has stopped running, zombie or not.

    Needed for a process that is not ours to reap. ``_gone`` cannot answer
    it — a dead child whose own parent never calls ``wait`` is a zombie,
    and a zombie still answers signal 0, so ``_gone`` reports it as alive
    forever. ``_kill_cli_children`` sidesteps this by reaping what it
    kills; a *grandchild* leaves nothing we are allowed to reap.

    Reads ``/proc`` where there is one, and falls back to ``_gone``
    elsewhere — on a platform without ``/proc`` the zombie window is not
    what these tests are about.
    """
    stat = f"/proc/{pid}/stat"
    if not os.path.exists("/proc"):
        return _gone(pid)
    try:
        with open(stat, encoding="utf-8") as handle:
            # The comm field can contain spaces and parens; the state
            # letter is the first field after the closing paren.
            fields = handle.read().rpartition(")")[2].split()
        return fields[0] == "Z"
    except FileNotFoundError:
        return True


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
    spawned: list[tuple[subprocess.Popen, bool]] = []
    counter = [0]

    def _spawn(code: str, new_session: bool = False) -> tuple[subprocess.Popen, object]:
        """``new_session`` mirrors how Vite is launched: its own group."""
        counter[0] += 1
        marker = tmp_path / f"marker-{counter[0]}"
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(marker)],
            start_new_session=new_session,
        )
        spawned.append((process, new_session))
        ready = marker.with_suffix(marker.suffix + ".ready")
        if "ready" in code:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "child never signalled readiness"
        return process, marker

    yield _spawn

    for process, new_session in spawned:
        # A group leader may have spawned grandchildren that outlive it,
        # which is the whole point of the fixtures that use it — so the
        # group is what has to go, or a leaked ``node`` stand-in sleeps
        # for a minute after the suite ends.
        if new_session:
            with suppress(OSError, AttributeError, ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
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


class TestKillVite:
    """``npx vite`` is a chain, and ``Popen`` only knows the top of it.

    ``npx`` becomes ``npm exec vite`` → ``sh -c "vite"`` → ``node vite``.
    Before the fix, ``terminate()`` signalled the wrapper and the node
    process holding the port survived, reparented to init. Observed as
    orphan Vite servers of 22h40m and 50m still bound to ports.

    The fixture below is that shape, not a mock of it: a parent that
    spawns a grandchild and then ignores SIGTERM itself, so a test that
    only signals the parent's pid cannot pass by accident.
    """

    # Spawns a grandchild that outlives it, then declines SIGTERM. The
    # grandchild writes its pid so the test can check it independently.
    #
    # The order is load-bearing and cost an hour to notice: SIG_IGN is
    # inherited across ``fork`` *and* ``exec``, unlike a handler, which
    # resets to the default. Ignoring SIGTERM before spawning gives the
    # grandchild the same immunity and the test passes for the wrong
    # reason — it reads as "the group kill did not work".
    WRAPPER = (
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import sys, time\\n'\n"
        "    'open(sys.argv[1], \"w\").write(str(__import__(\"os\").getpid()))\\n'\n"
        "    'time.sleep(60)\\n', sys.argv[1] + '.grandchild'])\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1] + '.ready', 'w').write('1')\n"
        "time.sleep(60)\n"
    )

    def _grandchild_pid(self, marker) -> int:
        path = marker.with_suffix(marker.suffix + ".grandchild")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return int(path.read_text().strip())
            time.sleep(0.01)
        raise AssertionError("grandchild never reported its pid")

    def test_the_whole_group_goes_not_just_the_wrapper(self, spawn):
        """The actual regression: the node server holding the port.

        The wrapper ignores SIGTERM, so if this passes, the grandchild
        was reached by the group signal and not by the parent dying.
        """
        wrapper, marker = spawn(self.WRAPPER, new_session=True)
        grandchild = self._grandchild_pid(marker)

        _kill_vite(wrapper)

        deadline = time.monotonic() + 5
        while not _stopped(grandchild) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _stopped(grandchild), "the node process outlived the shutdown"

    def test_no_vite_is_not_an_error(self):
        """``--dev`` off, or a Vite that failed to start at all."""
        _kill_vite(None)  # must not raise

    def test_an_already_dead_vite_is_not_an_error(self, spawn):
        """Shutdown races a Vite that crashed on its own."""
        wrapper, _marker = spawn(SLEEPER, new_session=True)
        wrapper.kill()
        wrapper.wait()

        _kill_vite(wrapper)  # must not raise

    def test_a_process_without_a_group_falls_back_to_terminate(self, spawn):
        """Windows has no process groups, and the old behaviour is better
        than leaving Vite entirely unsignalled."""
        child, marker = spawn(POLITE)

        def _no_groups(_pid):
            raise AttributeError("no getpgid here")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "getpgid", _no_groups)
            _kill_vite(child)

        # Unlike ``_kill_cli_children``, this does not wait: shutdown has
        # nothing to gain from blocking on Vite.
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_text() == "term"


class _FakeMaterialized:
    """The one field of the SDK's ``MaterializedResume`` we read."""

    def __init__(self, config_dir) -> None:
        self.config_dir = config_dir


class _FakeClient:
    """Stands in for ``ClaudeSDKClient`` after ``connect()``.

    ``_materialized`` is ``None`` on a client that did not resume, which
    is the SDK's own initial value and the common case.
    """

    def __init__(self, materialized=None) -> None:
        self._materialized = materialized


@pytest.fixture
def dirs(monkeypatch):
    """An isolated stand-in for the module-level registry.

    Patched rather than added to: a leaked entry would make a later test
    — or a later ``purge()`` in this process — delete a directory it does
    not own.
    """
    registry: set = set()
    monkeypatch.setattr(resume_cleanup, "_DIRS", registry)
    return registry


def _resume_dir(root, name: str = "claude-resume-test"):
    """A temp config dir shaped like the one the SDK materialises."""
    config_dir = root / name
    (config_dir / "projects" / "-repo").mkdir(parents=True)
    (config_dir / "projects" / "-repo" / "s.jsonl").write_text('{"a": 1}\n')
    (config_dir / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    (config_dir / "settings.json").write_text("{}")
    return config_dir


class TestRememberingTheResumeDir:
    def test_a_resumed_client_is_registered(self, dirs, tmp_path):
        config_dir = _resume_dir(tmp_path)
        client = _FakeClient(_FakeMaterialized(config_dir))

        assert resume_cleanup.remember(client) == config_dir
        assert dirs == {config_dir}

    def test_a_client_that_did_not_resume_registers_nothing(self, dirs):
        """The common case: a fresh session materialises no directory."""
        assert resume_cleanup.remember(_FakeClient()) is None
        assert dirs == set()

    def test_an_sdk_that_moved_the_attribute_degrades_quietly(self, dirs):
        """Private SDK surface, and the connect must survive losing it.

        The cost of a rename is this cleanup — back to the old leak — and
        never a session the user cannot start.
        """
        assert resume_cleanup.remember(object()) is None
        assert dirs == set()

    def test_a_materialized_without_a_config_dir_degrades_quietly(self, dirs):
        broken = _FakeMaterialized(None)
        del broken.config_dir

        assert resume_cleanup.remember(_FakeClient(broken)) is None
        assert dirs == set()

    def test_reconnecting_registers_both_dirs(self, dirs, tmp_path):
        """Each connect materialises its own; the first is not overwritten.

        The SDK cleans the earlier one on its own disconnect, so this set
        is normally one live directory and some already-removed paths —
        which is why :func:`purge` ignores errors instead of pruning.
        """
        first = _resume_dir(tmp_path, "claude-resume-one")
        second = _resume_dir(tmp_path, "claude-resume-two")
        resume_cleanup.remember(_FakeClient(_FakeMaterialized(first)))
        resume_cleanup.remember(_FakeClient(_FakeMaterialized(second)))

        assert dirs == {first, second}


class TestPurgingTheResumeDirs:
    def test_the_transcript_and_the_token_both_go(self, dirs, tmp_path):
        """The whole point: neither outlives the process that made it."""
        config_dir = _resume_dir(tmp_path)
        dirs.add(config_dir)

        resume_cleanup.purge()

        assert not config_dir.exists()

    def test_the_registry_is_emptied(self, dirs, tmp_path):
        """A second purge must not try to remove paths again.

        ``purge`` is reachable twice if a second signal arrives while the
        handler is still running.
        """
        dirs.add(_resume_dir(tmp_path))

        resume_cleanup.purge()

        assert dirs == set()
        resume_cleanup.purge()  # must not raise

    def test_a_dir_the_sdk_already_removed_is_not_an_error(self, dirs, tmp_path):
        """The normal case for every client but the last.

        A graceful ``disconnect()`` cleans up, and the path stays
        registered — so this is what most entries look like at shutdown.
        """
        config_dir = _resume_dir(tmp_path)
        shutil.rmtree(config_dir)
        dirs.add(config_dir)

        resume_cleanup.purge()  # must not raise

    def test_an_unregistered_neighbour_is_untouched(self, dirs, tmp_path):
        """Registered, never discovered by prefix.

        Sweeping the temp dir for ``claude-resume-`` would also match the
        live directory of another AIC⚡DC or a plain ``claude`` running
        beside us. Deleting that is a worse bug than the leak.
        """
        ours = _resume_dir(tmp_path, "claude-resume-ours")
        theirs = _resume_dir(tmp_path, "claude-resume-theirs")
        dirs.add(ours)

        resume_cleanup.purge()

        assert not ours.exists()
        assert (theirs / ".credentials.json").exists()

    def test_one_unremovable_dir_does_not_spare_the_others(self, dirs, tmp_path):
        """Best-effort, per directory.

        A path that is not a directory at all stands in for any rmtree
        failure. The handler's next statement is ``os._exit``, so giving
        up on the rest would cost the fix for no gain.
        """
        wedged = tmp_path / "not-a-dir"
        wedged.write_text("x")
        config_dir = _resume_dir(tmp_path)
        dirs.update({wedged, config_dir})

        resume_cleanup.purge()

        assert not config_dir.exists()

    def test_mains_wrapper_removes_it_too(self, dirs, tmp_path):
        """What the signal handler actually calls."""
        config_dir = _resume_dir(tmp_path)
        dirs.add(config_dir)

        _purge_resume_dirs()

        assert not config_dir.exists()

    def test_mains_wrapper_survives_a_broken_import(self, monkeypatch):
        """An import failure at shutdown must not block the exit."""
        monkeypatch.setitem(sys.modules, "aic_dc.claude_code.resume_cleanup", object())

        _purge_resume_dirs()  # must not raise


class TestTheGracefulStep:
    """What the exit does *before* ``os._exit`` — phase 8, `next.md` § C8.

    ``ClaudeCodeService.shutdown`` had no caller for its whole life while
    its docstring reasoned about one. Three of its four steps are pointless
    on the way out (the CLI is about to be killed by hand, the temp dir
    purged by hand, and asyncio tasks die with the process); the fourth
    leaves the process, because the browser outlives the server. A pending
    permission dialog has to be told the server is going, or it stays on
    screen forever waiting for an answer nobody will send.

    So the contract here is narrow and entirely about not blocking the
    exit: run it, bound it, and never let it raise.
    """

    class _Service:
        def __init__(self, answer=None, delay=0.0, boom=None):
            self.answer = answer
            self.delay = delay
            self.boom = boom
            self.calls = 0

        async def shutdown(self):
            self.calls += 1
            if self.boom is not None:
                raise self.boom
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.answer

    async def test_the_service_teardown_runs(self):
        service = self._Service()
        await _shut_the_engine_down(service)
        assert service.calls == 1

    async def test_a_hung_teardown_is_abandoned_at_the_timeout(self):
        """Ctrl-C has to work on a wedged engine, which is the whole reason
        the await is bounded rather than plain."""
        service = self._Service(delay=30)
        started = time.monotonic()
        await _shut_the_engine_down(service, timeout=0.05)
        assert time.monotonic() - started < 5

    async def test_a_raising_teardown_does_not_propagate(self):
        """Anything that escapes here stops the exit, which is worse than
        skipping the courtesy it was doing."""
        service = self._Service(boom=RuntimeError("engine is wedged"))
        await _shut_the_engine_down(service)  # must not raise

    async def test_a_refusal_by_the_localhost_gate_is_reported(self, caplog):
        """``is_caller_localhost`` reads the *current* RPC caller, so a
        remote participant's call caught mid-dispatch refuses the teardown.
        Narrow, but a silent skip would look like it ran."""
        service = self._Service(answer={"error": "restricted", "reason": "nope"})
        with caplog.at_level(logging.WARNING, logger="aic_dc.main"):
            await _shut_the_engine_down(service)
        assert "refused by the localhost gate" in caplog.text

    async def test_an_ordinary_answer_is_not_reported(self, caplog):
        service = self._Service(answer=None)
        with caplog.at_level(logging.WARNING, logger="aic_dc.main"):
            await _shut_the_engine_down(service)
        assert "localhost gate" not in caplog.text


class TestTheSignalWiring:
    """That the grace period is reachable *from a real signal*.

    The step above is only worth having if a Ctrl-C actually runs it, and
    the mechanism that arranges that is the part with a platform split in
    it. So these fire real signals at the test process rather than calling
    the callback: what is being pinned is that
    ``loop.add_signal_handler`` was used, on the running loop, for both
    signals — a C-level handler could not await the coroutine, and the
    failure mode of getting that wrong is a graceful step that silently
    never runs.

    ``teardown`` is a fake here for the obvious reason: the real one ends
    in ``os._exit`` and would take the test runner with it.
    """

    class _Service(TestTheGracefulStep._Service):
        pass

    @staticmethod
    def _restore(loop):
        """Hand SIGINT/SIGTERM back, or the next test inherits our handler."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    @pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
    async def test_a_real_signal_runs_the_grace_period_then_the_teardown(self, sig):
        """Both signals, because wiring one and forgetting the other is the
        mistake that shows up only under a process manager."""
        loop = asyncio.get_running_loop()
        service = self._Service()
        done = asyncio.Event()
        _install_exit_handlers(loop, service, lambda *_: done.set())
        try:
            os.kill(os.getpid(), sig)
            await asyncio.wait_for(done.wait(), 5)
        finally:
            self._restore(loop)
        assert service.calls == 1, "the graceful step did not run before teardown"

    async def test_the_teardown_waits_for_the_grace_period(self):
        """Ordering, not just occurrence. A teardown that races the engine
        shutdown would kill the CLI while the denial was still in flight —
        which is the announce this whole item exists for."""
        loop = asyncio.get_running_loop()
        service = self._Service(delay=0.05)
        order: list[str] = []

        def teardown(*_args):
            order.append("teardown")

        _install_exit_handlers(loop, service, teardown)
        try:
            os.kill(os.getpid(), signal.SIGINT)
            for _ in range(500):
                if order:
                    break
                await asyncio.sleep(0.01)
        finally:
            self._restore(loop)
        assert order == ["teardown"] and service.calls == 1

    async def test_a_second_signal_withdraws_the_grace_period(self):
        """The courtesy is bounded at 2s, and a user who presses Ctrl-C
        again has said 2s is too long.

        The window below is deliberately far shorter than
        ``_SHUTDOWN_GRACE``, and that is the whole assertion: a version
        that ignores the second signal still tears down eventually, when
        the first attempt's timeout expires, so a generous poll would
        pass against code that does nothing here at all. Checked — with
        a 5s window this test passed with the escape deleted.
        """
        assert _SHUTDOWN_GRACE > 1, "the margin this test relies on is gone"
        loop = asyncio.get_running_loop()
        service = self._Service(delay=30)
        teardowns = []
        _install_exit_handlers(loop, service, lambda *_: teardowns.append(1))
        try:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0.05)
            assert teardowns == [], "the first signal should still be waiting"
            os.kill(os.getpid(), signal.SIGINT)
            for _ in range(20):
                if teardowns:
                    break
                await asyncio.sleep(0.01)
        finally:
            self._restore(loop)
        assert teardowns == [1], "the second signal did not exit immediately"
        # And it did not also queue a *second* graceful step against an
        # engine already being torn down.
        assert service.calls == 1

    async def test_no_loop_signal_handlers_falls_back_to_an_immediate_exit(
        self, monkeypatch, caplog
    ):
        """Windows' proactor loop owns no signals. The graceful step has
        nowhere to run there and the pre-§C8 behaviour has to stand, rather
        than the install raising and taking the whole startup with it."""
        loop = asyncio.get_running_loop()
        service = self._Service()

        def no_signals(*_args, **_kwargs):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", no_signals)
        installed = {}
        monkeypatch.setattr(
            signal, "signal", lambda sig, handler: installed.setdefault(sig, handler)
        )

        def teardown(*_args):
            pass

        with caplog.at_level(logging.DEBUG, logger="aic_dc.main"):
            _install_exit_handlers(loop, service, teardown)

        assert installed == {signal.SIGINT: teardown, signal.SIGTERM: teardown}
        assert "without the grace period" in caplog.text
        assert service.calls == 0
