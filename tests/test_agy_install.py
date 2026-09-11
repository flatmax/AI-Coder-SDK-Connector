"""Tests for installing the gate into an ``agy`` configuration root.

This module writes **outside the repository**, into a file belonging to
Google's CLI. Until 2026-09-11 that file was the user's own, shared with
every ``agy`` they started by hand, so the assertions here were mostly
about restraint: what it must not touch, what it must not overwrite, and
what happened to *their* sessions when ours was not running.

**AG-21 changed who owns the file, and the sharpest test in this module
inverted with it.** The hook now goes into a config root this app creates
and spawns ``agy`` against, so the only sessions it can reach are ours.
``agy`` still **blocks a tool** when a hook command cannot be run — exit
127, measured — and that is now the *correct* outcome rather than
collateral damage, so the ``|| printf '{"decision":"allow"}'`` clause
every command used to end with is deleted rather than replaced. What the
old clause bought was a stranger's uninterrupted session; what it cost was
a gate that reported itself installed and allowed everything. The trade
only ever made sense while the file was somebody else's.

The clause is still *recognised*: an entry written before 2026-09-11 still
carries it, and reading such an entry as healthy would be the same failure
one release later. ``TestTheCommandMustActuallyRun`` covers that from both
ends, along with the hole it was written for — on a PyInstaller build
``sys.executable`` is the frozen binary, which does not honour ``-m``, so
the command exited 2 on every call of a session this host did own, with
Settings reporting the gate installed because ``status`` compares command
strings and the string was correct.

Offline. Never touches the real ``~/.gemini``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from aic_dc.agy import install


@pytest.fixture(autouse=True)
def _forget_probes():
    """The probe cache is per-process, and a test process is many installs.

    :func:`install.hook_runs` memoises a verdict per ``(command, event)``
    so that AG-21's fresh-root-per-consultation does not pay three
    interpreter startups every time. That reasoning holds in the server —
    the command names *this* interpreter and *this* package, and neither
    moves while it runs — and does not hold here, where a test's whole job
    is to make the same string answer differently. Cleared between tests so
    one test's monkeypatch cannot decide the next one's verdict.
    """
    install._PROBE_CACHE.clear()
    yield
    install._PROBE_CACHE.clear()


@pytest.fixture
def hooks(tmp_path):
    return tmp_path / "hooks.json"


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "cfg"


def _write_entry(hooks, config_dir, python=None):
    """Put a whole hook entry on disk without going through ``install``.

    For the states ``install`` now refuses to create — an entry naming an
    interpreter or a config directory that is not this one. Built by
    ``install.hook_entry``, so it is the shape this build writes and
    ``status`` reads it the same way; what varies is only what the test
    varies.
    """
    hooks.write_text(
        json.dumps({install.HOOK_NAME: install.hook_entry(config_dir, python)}),
        encoding="utf-8",
    )


def _write_raw_entry(hooks, command, *, events=install.hook.EVENTS):
    """An entry built from one command string, for the hand-edited cases.

    ``events`` narrows what is written, which is how a **pre-AG-19**
    install is reproduced: a gate and nothing else, in a file that predates
    the two invocation handlers.
    """
    entry = {}
    if install.hook.PRE_TOOL_USE in events:
        entry[install.hook.PRE_TOOL_USE] = [
            {"matcher": "*", "hooks": [{"type": "command", "command": command}]}
        ]
    for event in (install.hook.POST_INVOCATION, install.hook.STOP):
        if event in events:
            entry[event] = [{"type": "command", "command": command}]
    hooks.write_text(
        json.dumps({install.HOOK_NAME: entry}), encoding="utf-8"
    )


class TestDetection:
    def test_absent_when_there_is_no_file(self, hooks, cfg):
        assert install.status(cfg, path=hooks)["state"] == "absent"

    def test_absent_when_the_file_holds_only_the_users_hooks(self, hooks, cfg):
        hooks.write_text(json.dumps({"my-linter": {"PostToolUse": []}}), encoding="utf-8")
        report = install.status(cfg, path=hooks)
        assert report["state"] == "absent"
        assert report["other_hooks"] == ["my-linter"]

    def test_current_after_installing(self, hooks, cfg):
        install.install(cfg, path=hooks)
        assert install.status(cfg, path=hooks)["state"] == "current"

    def test_stale_when_it_points_at_another_interpreter(self, hooks, cfg):
        """The case a moved or deleted virtualenv produces.

        Reported loudly rather than silently repaired: it usually means a
        second checkout is also installed, and quietly taking the hook over
        would break whichever one the user was actually using.

        The entry is **written rather than installed**, and that is the
        change rather than the test drifting: this describes a file left
        behind by an installation that has since moved, and since
        2026-09-05 ``install`` refuses to write a command that does not
        run — which is exactly what ``/somewhere/else/python`` is. Reaching
        the state through the function that now prevents it would be
        asserting the old behaviour with new words.
        """
        _write_entry(hooks, cfg, "/somewhere/else/python")
        report = install.status(cfg, path=hooks)
        assert report["state"] == "stale"
        assert "/somewhere/else/python" in report["command"]
        assert report["expected"] != report["command"]

    def test_one_interpreter_spelled_two_ways_is_not_stale(
        self, hooks, cfg, tmp_path
    ):
        """The probe of 2026-09-09 read a working gate as ``stale``.

        ``.venv/bin/python3`` in the file and ``.venv/bin/python`` from
        ``sys.executable`` are one program, and calling them two refuses to
        start the engine: ``connect`` answers ``gate_not_installed`` and the
        consultant reports itself unavailable.
        """
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        real = venv / "python3"
        real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real.chmod(0o755)
        alias = venv / "python"
        alias.symlink_to(real)

        _write_entry(hooks, cfg, str(real))
        assert install.status(cfg, path=hooks, python=str(alias))["state"] == "current"

    def test_two_virtualenvs_are_stale_even_sharing_one_binary(
        self, hooks, cfg, tmp_path
    ):
        """Which is why the resolved target is not the test.

        A venv's ``bin/python`` is usually a symlink to the system
        interpreter, so following the link would call two checkouts' gates
        equal and report somebody else's install as ours. The comparison is
        *same directory, same file within it* — one interpreter spelled two
        ways, and nothing wider.
        """
        system = tmp_path / "python3"
        system.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        system.chmod(0o755)
        ours, theirs = tmp_path / "a" / "bin", tmp_path / "b" / "bin"
        for where in (ours, theirs):
            where.mkdir(parents=True)
            (where / "python").symlink_to(system)

        _write_entry(hooks, cfg, str(theirs / "python"))
        report = install.status(cfg, path=hooks, python=str(ours / "python"))
        assert report["state"] == "stale"

    def test_a_different_config_directory_is_still_stale(self, hooks, cfg, tmp_path):
        """Only the interpreter is compared loosely; every other token is not.

        The command names the config dir the hook must read the registry
        from, and an entry naming another one would gate a different
        registry — the same interpreter is not the same install.
        """
        _write_entry(hooks, tmp_path / "other-cfg")
        assert install.status(cfg, path=hooks)["state"] == "stale"

    def test_an_unbalanced_quote_reads_as_stale(self, hooks, cfg):
        """Hand-edited and unparseable, so it is not claimed as ours."""
        _write_raw_entry(hooks, "'/usr/bin/python -m aic_dc.agy.hook /cfg")
        assert install.status(cfg, path=hooks)["state"] == "stale"

    def test_unreadable_is_reported_rather_than_guessed(self, hooks, cfg):
        hooks.write_text("{ this is not json", encoding="utf-8")
        report = install.status(cfg, path=hooks)
        assert report["state"] == "unreadable"
        assert "not valid JSON" in report["detail"]


class TestOneInterpreterWithTwoNames:
    """A venv's ``python`` and ``python3`` are one program, and ``status``
    used to call them two installs.

    Found 2026-09-10 by ``probe_agy_cgroup_identity.py``'s setup, which
    reported the gate ``stale`` on a machine where it was working: the
    entry had been written by a process started as ``.venv/bin/python3``
    and the probe asked as ``.venv/bin/python``. A ``stale`` reading makes
    ``AgySession`` refuse to start, so an install that works presents as a
    broken one — and the cause is nothing but which name resolved the
    interpreter. See ``specs5/plan-ag/risks.md`` AG-R-14.

    The second test is the one that keeps the first honest. Forgiving the
    spelling by resolving both paths would resolve *every* venv's python to
    the system interpreter and call two checkouts the same install, which
    is precisely what ``stale`` exists to report.
    """

    def _venv(self, root, name="venv"):
        """A ``bin`` directory with ``python`` and ``python3`` in it.

        Shaped like the real thing: ``python3`` is a symlink to ``python``,
        which is itself a symlink to the interpreter running these tests.
        """
        binaries = root / name / "bin"
        binaries.mkdir(parents=True)
        (binaries / "python").symlink_to(sys.executable)
        (binaries / "python3").symlink_to(binaries / "python")
        return binaries

    def test_the_other_spelling_of_one_interpreter_is_current(
        self, hooks, cfg, tmp_path
    ):
        binaries = self._venv(tmp_path)
        _write_entry(hooks, cfg, str(binaries / "python3"))
        report = install.status(cfg, path=hooks, python=str(binaries / "python"))
        assert report["state"] == "current"

    def test_a_second_checkout_is_still_stale(self, hooks, cfg, tmp_path):
        ours = self._venv(tmp_path, "ours")
        theirs = self._venv(tmp_path, "theirs")
        _write_entry(hooks, cfg, str(theirs / "python"))
        report = install.status(cfg, path=hooks, python=str(ours / "python"))
        assert report["state"] == "stale"

    def test_only_the_interpreter_is_forgiven_not_the_arguments(
        self, hooks, cfg, tmp_path
    ):
        binaries = self._venv(tmp_path)
        _write_entry(hooks, tmp_path / "another-config", str(binaries / "python3"))
        report = install.status(cfg, path=hooks, python=str(binaries / "python"))
        assert report["state"] == "stale"

    def test_an_interpreter_that_is_gone_is_stale_rather_than_an_error(
        self, hooks, cfg, tmp_path
    ):
        binaries = self._venv(tmp_path)
        _write_entry(hooks, cfg, str(binaries / "python9"))
        report = install.status(cfg, path=hooks, python=str(binaries / "python"))
        assert report["state"] == "stale"


class TestItRespectsSomebodyElsesFile:
    def test_installing_preserves_other_hooks(self, hooks, cfg):
        mine = {"my-linter": {"PostToolUse": [{"matcher": "*", "hooks": []}]}}
        hooks.write_text(json.dumps(mine), encoding="utf-8")
        install.install(cfg, path=hooks)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        assert data["my-linter"] == mine["my-linter"]
        assert install.HOOK_NAME in data

    def test_uninstalling_removes_only_ours(self, hooks, cfg):
        hooks.write_text(json.dumps({"my-linter": {"PostToolUse": []}}), encoding="utf-8")
        install.install(cfg, path=hooks)
        assert install.uninstall(path=hooks) is True
        data = json.loads(hooks.read_text(encoding="utf-8"))
        assert list(data) == ["my-linter"]

    def test_the_file_goes_away_if_ours_was_the_only_entry(self, hooks, cfg):
        """An empty `{}` would be litter in somebody else's config."""
        install.install(cfg, path=hooks)
        install.uninstall(path=hooks)
        assert not hooks.exists()

    def test_uninstalling_what_was_never_installed_is_not_an_error(self, hooks):
        assert install.uninstall(path=hooks) is False

    def test_an_unparseable_file_is_never_overwritten(self, hooks, cfg):
        """It is the user's, and it may hold hooks they depend on."""
        hooks.write_text("{ broken", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            install.install(cfg, path=hooks)
        assert hooks.read_text(encoding="utf-8") == "{ broken"

    def test_an_unparseable_file_is_not_deleted_either(self, hooks):
        hooks.write_text("{ broken", encoding="utf-8")
        assert install.uninstall(path=hooks) is False
        assert hooks.exists()

    def test_installing_twice_leaves_one_entry(self, hooks, cfg):
        install.install(cfg, path=hooks)
        install.install(cfg, path=hooks)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        assert len(data[install.HOOK_NAME]["PreToolUse"]) == 1


class TestTheInstalledCommand:
    def test_the_matcher_is_every_tool(self, hooks, cfg):
        """AG-R-12: a gate is only as wide as its matcher.

        Measured live — denied an edit, the model tried `run_command` and
        then `list_dir`. Three routes to one write.
        """
        install.install(cfg, path=hooks)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        assert data[install.HOOK_NAME]["PreToolUse"][0]["matcher"] == "*"

    def test_the_timeout_outlasts_a_human_reading_a_diff(self, hooks, cfg):
        """A hook killed at its deadline exits non-zero, which agy refuses.

        So a short timeout here would refuse a call because the user was
        slow, which is the failure this whole design exists to avoid.
        """
        install.install(cfg, path=hooks)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        handler = data[install.HOOK_NAME]["PreToolUse"][0]["hooks"][0]
        assert handler["timeout"] >= 600

    def test_a_missing_interpreter_blocks_rather_than_allowing(self, tmp_path):
        """AG-21, and the inversion of what this file used to assert.

        The command prints nothing and exits non-zero, which is what `agy`
        reads as "block the tool". That used to be unacceptable because the
        entry lived in the user's own configuration and their interactive
        sessions would have stopped working; it is now correct, because the
        only sessions that read this entry are ones this app spawned, and a
        session whose gate cannot start is a session that must not run.

        Run as a real shell command, because the claim is about what the
        shell does with the string and not about anything in Python.
        """
        command = install.hook_command(tmp_path / "cfg", python="/no/such/python")
        done = subprocess.run(
            ["sh", "-c", command], input="{}", capture_output=True, text=True
        )
        assert done.returncode != 0
        assert done.stdout.strip() == "", "a failed hook printed a decision anyway"

    def test_a_working_interpreter_answers_for_itself(self, tmp_path):
        """And there is exactly one object on stdout, not two.

        The deleted clause *appended*: a runner that died after emitting a
        partial object would have produced that object followed by a
        second, which is not JSON at all — so the fallback broke parsing in
        precisely the case it was added for.
        """
        command = install.hook_command(tmp_path / "cfg", python=sys.executable)
        done = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps(
                {"conversationId": "not-ours", "toolCall": {"name": "view_file"}}
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert json.loads(done.stdout) == {"decision": "allow"}


class TestTheCommandMustActuallyRun:
    """The gap between "the string is right" and "the command works".

    `status` judges `current` by comparing command strings, which is the
    right check for *whose* gate is installed and says nothing about
    whether it runs. On a PyInstaller build those two answers came apart:
    the string was exactly the one we meant to write, and the command
    exited 2 on every call.
    """

    def test_a_frozen_build_gets_a_command_the_binary_can_run(
        self, cfg, monkeypatch
    ):
        """`-m` is a Python thing; a frozen binary is not a Python.

        The bug, in one assertion. `sys.executable` under PyInstaller is
        the binary, `<binary> -m aic_dc.agy.hook …` exits 2 with
        "unrecognized arguments", and `agy` then takes the allow-fallback
        for a session this host owns.
        """
        monkeypatch.setattr(install.sys, "frozen", True, raising=False)
        monkeypatch.setattr(install.sys, "executable", "/opt/aic-dc-linux")
        command = install.hook_command(cfg)
        assert "--agy-hook" in command
        assert " -m aic_dc.agy.hook" not in command
        assert command.startswith(f"/opt/aic-dc-linux --agy-hook {cfg}")

    def test_a_source_install_still_uses_the_module_form(self, cfg, monkeypatch):
        monkeypatch.delattr(install.sys, "frozen", raising=False)
        command = install.hook_command(cfg)
        assert " -m aic_dc.agy.hook " in command
        assert "--agy-hook" not in command

    def test_an_explicit_interpreter_is_never_treated_as_frozen(
        self, cfg, monkeypatch
    ):
        """A caller passing one is describing an install that is not us."""
        monkeypatch.setattr(install.sys, "frozen", True, raising=False)
        command = install.hook_command(cfg, "/elsewhere/bin/python")
        assert " -m aic_dc.agy.hook " in command

    def test_neither_form_carries_a_fallback(self, cfg, monkeypatch):
        """Frozen or not, the command is the command and nothing else.

        The shape mattered: the frozen build got the *invocation* wrong and
        the fallback is what turned that into a silent allow instead of a
        loud failure. Asserted on both forms because the bug lived in the
        difference between them.
        """
        for frozen in (True, False):
            monkeypatch.setattr(install.sys, "frozen", frozen, raising=False)
            command = install.hook_command(cfg)
            assert "||" not in command
            assert "printf" not in command

    def test_the_real_command_answers_a_probe(self, cfg):
        assert install.hook_runs(install.hook_command(cfg)) == ""

    def test_an_unrunnable_command_is_reported_with_its_reason(self, cfg):
        problem = install.hook_runs("/nope/not/a/python -m aic_dc.agy.hook x")
        assert problem
        assert "127" in problem or "not found" in problem

    def test_a_command_that_prints_no_decision_is_not_accepted(self):
        """Exit 0 is not the assertion — a JSON decision is.

        A command that succeeds and prints nothing is the exact shape
        `agy` reads as allow, so "it ran" is not the question.
        """
        assert install.hook_runs("true") != ""

    def test_the_probe_does_not_run_the_fallback(self):
        """Running the whole command would mask the failure it looks for.

        `false || printf '{"decision":"allow"}'` succeeds and prints a
        perfectly good decision, which is precisely the outcome that must
        not read as a working gate.
        """
        assert install.hook_runs(
            """false || printf '{"decision":"allow"}'"""
        ) != ""

    def test_install_refuses_rather_than_writing_a_broken_gate(
        self, hooks, cfg, monkeypatch
    ):
        """Fails closed, at the moment the user asked for a gate.

        The alternative is what shipped: an entry that looks installed,
        reports `current`, and allows everything.
        """
        monkeypatch.setattr(install.sys, "executable", "/nope/not/a/python")
        report = install.install(cfg, path=hooks)
        assert report["state"] == "unrunnable"
        assert "detail" in report
        assert not hooks.exists(), "a gate that cannot run was written anyway"

    def test_a_refusal_leaves_somebody_elses_hooks_alone(
        self, hooks, cfg, monkeypatch
    ):
        mine = {"my-linter": {"PostToolUse": [{"matcher": "*", "hooks": []}]}}
        hooks.write_text(json.dumps(mine), encoding="utf-8")
        monkeypatch.setattr(install.sys, "executable", "/nope/not/a/python")
        install.install(cfg, path=hooks)
        assert json.loads(hooks.read_text(encoding="utf-8")) == mine


class TestTheCliEntryPointTheFrozenBuildNeeds:
    def test_the_flag_dispatches_to_the_hook(self, cfg, monkeypatch):
        """`--agy-hook` is the frozen build's replacement for `-m`.

        Asserted through `cli.main` rather than by importing the hook,
        because the thing that was broken was the *entry point*: the
        parser rejected the arguments before any of our code ran.
        """
        import io

        from aic_dc import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert cli.main(["--agy-hook", str(cfg)]) == 0

    def test_it_prints_one_json_decision_and_nothing_else(
        self, cfg, monkeypatch, capsys
    ):
        """It is stdout in the middle of agy's protocol. A banner here is
        a parse failure there."""
        import io

        from aic_dc import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        cli.main(["--agy-hook", str(cfg)])
        out = capsys.readouterr().out.strip()
        assert "decision" in json.loads(out)

    def test_the_flag_is_hidden_from_help(self):
        """Not a thing a user runs — agy runs it, once per tool call."""
        from aic_dc import cli

        assert "--agy-hook" not in cli._build_parser().format_help()


class TestThreeHandlersUnderOneName:
    """AG-19 added a ``PostInvocation`` handler and AG-R-16 a ``Stop`` one.

    They go under the same hook name as the gate, because ``agy`` merges
    named hooks per event and one name is what lets ``uninstall`` remove
    exactly what was added — and because a user reading their own
    ``hooks.json`` should see one thing belonging to this app, not three.
    """

    def test_every_event_is_registered(self, hooks, cfg):
        install.install(cfg, path=hooks)
        entry = json.loads(hooks.read_text())[install.HOOK_NAME]
        assert set(entry) == {"PreToolUse", "PostInvocation", "Stop"}

    def test_the_gate_is_grouped_and_the_others_are_flat(self, hooks, cfg):
        """Two shapes, and writing the wrong one is a handler that never
        fires. ``hooks.md``: the invocation events take a list of handlers
        directly, with no ``matcher`` — there is no tool to match on.
        """
        install.install(cfg, path=hooks)
        entry = json.loads(hooks.read_text())[install.HOOK_NAME]
        assert entry["PreToolUse"][0]["matcher"] == "*"
        assert "hooks" in entry["PreToolUse"][0]
        for event in ("PostInvocation", "Stop"):
            assert "matcher" not in entry[event][0]
            assert entry[event][0]["type"] == "command"
            assert entry[event][0]["command"]

    def test_each_event_names_itself_in_its_command(self, hooks, cfg):
        install.install(cfg, path=hooks)
        entry = json.loads(hooks.read_text())[install.HOOK_NAME]
        assert "--event" not in entry["PreToolUse"][0]["hooks"][0]["command"]
        assert "--event PostInvocation" in entry["PostInvocation"][0]["command"]
        assert "--event Stop" in entry["Stop"][0]["command"]

    def test_the_gates_command_is_unchanged(self, cfg):
        """An install written before AG-19 must still read as *this*
        interpreter, so the thing that is wrong with it is the handlers it
        does not have rather than the one it does."""
        assert install.hook_command(cfg) == install.hook_command(
            cfg, event="PreToolUse"
        )
        assert "--event" not in install.hook_command(cfg)

    def test_no_event_carries_a_shell_fallback(self, cfg):
        """All three lost theirs together, and for one reason.

        The gate's was ``{"decision":"allow"}`` and the invocation hooks'
        was ``{}`` — different verdicts, the same premise, that the entry
        lived in a file shared with sessions this app knows nothing about.
        AG-21 retired the premise, so all three go rather than the loudest
        one.
        """
        for event, command in install.hook_commands(cfg).items():
            assert "||" not in command, event
            assert "printf" not in command, event

    def test_the_invocation_deadline_is_not_the_dialogs(self, hooks, cfg):
        """An hour is for a human reading a diff. Nothing waits on a human
        here, and the documented default of 30s is a value we never chose.
        """
        install.install(cfg, path=hooks)
        entry = json.loads(hooks.read_text())[install.HOOK_NAME]
        assert entry["PreToolUse"][0]["hooks"][0]["timeout"] == 3600
        for event in ("PostInvocation", "Stop"):
            assert entry[event][0]["timeout"] == install.INVOCATION_TIMEOUT_SECONDS
            assert entry[event][0]["timeout"] < 3600

    def test_uninstalling_still_removes_exactly_one_entry(self, hooks, cfg):
        hooks.write_text(json.dumps({"my-linter": {"PostToolUse": []}}), encoding="utf-8")
        install.install(cfg, path=hooks)
        assert install.uninstall(path=hooks) is True
        assert json.loads(hooks.read_text()) == {"my-linter": {"PostToolUse": []}}


class TestAnEntryThatPredatesTheStop:
    """A gate with no stop is ``stale``, and the reasoning is the subject.

    It is a working gate: the tree is still reviewed on every tool call.
    What it does not have is the mechanism ⏹ is documented to be, so a
    user pressing stop would get the starvation AG-19 replaced while the
    panel said the gate was current. A control that reports itself a
    mechanism while being a request is the same shape of untruth as an
    ungated agent reporting itself gated, and this state exists to refuse
    it. The cost is one click, and it converges; calling it ``current``
    would leave the mechanism unarmed and silent forever.
    """

    def test_a_gate_only_entry_is_stale(self, hooks, cfg):
        _write_raw_entry(hooks, install.hook_command(cfg), events=("PreToolUse",))
        assert install.status(cfg, path=hooks)["state"] == "stale"

    def test_it_names_what_is_missing(self, hooks, cfg):
        _write_raw_entry(hooks, install.hook_command(cfg), events=("PreToolUse",))
        report = install.status(cfg, path=hooks)
        assert report["missing_events"] == ["PostInvocation", "Stop"]

    def test_the_detail_says_the_gate_still_works(self, hooks, cfg):
        """Two causes reach ``stale`` and a user can act on only one of
        them if they are told which."""
        _write_raw_entry(hooks, install.hook_command(cfg), events=("PreToolUse",))
        detail = install.status(cfg, path=hooks)["detail"]
        assert "still reviewing every tool call" in detail
        assert "starve" in detail

        _write_entry(hooks, cfg, "/somewhere/else/python")
        assert "different install" in install.status(cfg, path=hooks)["detail"]

    def test_reinstalling_makes_it_current(self, hooks, cfg):
        _write_raw_entry(hooks, install.hook_command(cfg), events=("PreToolUse",))
        install.install(cfg, path=hooks)
        report = install.status(cfg, path=hooks)
        assert report["state"] == "current"
        assert "detail" not in report

    def test_a_missing_stop_alone_is_enough(self, hooks, cfg):
        _write_raw_entry(
            hooks,
            install.hook_command(cfg),
            events=("PreToolUse", "PostInvocation"),
        )
        report = install.status(cfg, path=hooks)
        assert report["state"] == "stale"
        assert report["missing_events"] == ["Stop"]


class TestEveryCommandIsProbed:
    """The frozen-binary bug was a correct string naming an unrunnable
    command. The three commands differ by an argument, which is exactly
    what that bug got wrong — so probing one and writing three would leave
    the same gap one event to the left.
    """

    def test_the_real_commands_all_answer(self, cfg):
        for event, command in install.hook_commands(cfg).items():
            assert install.hook_runs(command, event=event) == ""

    def test_an_invocation_hook_may_answer_with_no_decision(self, cfg):
        """``{}`` is this event's correct answer and the gate's forbidden
        one, so the probe cannot demand a ``decision`` from both."""
        command = install.hook_commands(cfg)["PostInvocation"]
        assert install.hook_runs(command, event="PostInvocation") == ""
        assert install.hook_runs(command, event="PreToolUse") != ""

    def test_a_command_that_is_not_an_object_is_refused(self):
        assert install.hook_runs("printf '[]'", event="Stop") != ""

    def test_a_broken_invocation_command_refuses_the_whole_install(
        self, hooks, cfg, monkeypatch
    ):
        """And names the event, because "the gate does not run" would send
        the user looking at the part that does."""
        real = install.hook_runs

        def only_the_gate_works(command, *, event=install.hook.PRE_TOOL_USE, **kw):
            if event == "Stop":
                return "exit 2: unrecognized arguments"
            return real(command, event=event, **kw)

        monkeypatch.setattr(install, "hook_runs", only_the_gate_works)
        report = install.install(cfg, path=hooks)
        assert report["state"] == "unrunnable"
        assert report["event"] == "Stop"
        assert "Stop command" in report["detail"]
        assert not hooks.exists()


class TestALegacyEntryIsNotHealthy:
    """An install written before AG-21 still carries the fail-open clause.

    Recognising it is the whole reason :data:`install.LEGACY_FALLBACKS`
    survived the deletion. An entry that ends in ``|| printf
    '{"decision":"allow"}'`` names this interpreter, this package and this
    config directory — everything ``status`` used to compare — and is
    exactly the gate that reports itself current while waving tool calls
    through. Reading it as healthy would repeat the frozen-binary incident
    one release later.
    """

    def _legacy(self, hooks, cfg):
        entry = install.hook_entry(cfg)
        for event, tail in install.LEGACY_FALLBACKS.items():
            handlers = entry[event]
            handler = handlers[0].get("hooks", [handlers[0]])[0]
            handler["command"] += tail
        hooks.write_text(json.dumps({install.HOOK_NAME: entry}), encoding="utf-8")

    def test_it_reads_as_stale_rather_than_current(self, hooks, cfg):
        self._legacy(hooks, cfg)
        assert install.status(cfg, path=hooks)["state"] == "stale"

    def test_the_reason_names_the_fallback_and_not_another_checkout(
        self, hooks, cfg
    ):
        """The two causes of `stale` need different actions from a reader.

        "Another checkout owns this file" sends someone looking at their
        other clone; "this predates AG-21" tells them the truth, which is
        that the gate in front of them fails open.
        """
        self._legacy(hooks, cfg)
        detail = install.status(cfg, path=hooks)["detail"]
        assert "fail-open" in detail
        assert "different install" not in detail

    def test_reinstalling_removes_it(self, hooks, cfg):
        self._legacy(hooks, cfg)
        report = install.install(cfg, path=hooks)
        assert report["state"] == "current"
        assert "printf" not in hooks.read_text(encoding="utf-8")

    def test_the_probe_strips_it_before_judging(self):
        """Otherwise the clause answers the probe on the hook's behalf.

        ``false || printf '{"decision":"allow"}'`` exits 0 and prints a
        perfectly well-formed decision, so a probe that ran the whole
        string would certify a broken gate as working — which is the one
        outcome this function exists to prevent.
        """
        assert install.hook_runs(
            """false || printf '{"decision":"allow"}'"""
        ) != ""


class TestInstallable:
    """"Would it install" — the question AG-21's ephemeral roots created.

    A consultation gets a fresh config root and writes its own hook into
    it, so before one starts there is no file to inspect: `status` would
    answer ``absent`` about a gate that is going to work perfectly. What a
    caller actually wants to know is whether the commands can run.
    """

    def test_a_working_install_is_ready(self, cfg):
        assert install.installable(cfg)["state"] == "ready"

    def test_it_writes_nothing(self, cfg, tmp_path):
        before = sorted(p.name for p in tmp_path.iterdir())
        install.installable(cfg)
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        assert not (tmp_path / "hooks.json").exists()

    def test_a_broken_interpreter_is_unrunnable_and_says_why(
        self, cfg, monkeypatch
    ):
        monkeypatch.setattr(install.sys, "executable", "/nope/not/a/python")
        report = install.installable(cfg)
        assert report["state"] == "unrunnable"
        assert report["detail"]

    def test_it_names_the_event_that_failed(self, cfg, monkeypatch):
        """Three commands differ by an argument, and an argument is what
        the frozen build got wrong. "The gate does not run" would send a
        reader to the part that does."""
        real = install.hook_runs

        def only_stop_is_broken(command, *, event=install.hook.PRE_TOOL_USE, **kw):
            if event == "Stop":
                return "exit 2: unrecognized arguments"
            return real(command, event=event, **kw)

        monkeypatch.setattr(install, "hook_runs", only_stop_is_broken)
        report = install.installable(cfg)
        assert report["state"] == "unrunnable"
        assert report["event"] == "Stop"


class TestRetiringTheGlobalEntry:
    """The hook this app used to install in the user's own configuration.

    Before AG-21 it *was* the gate, and it fired for every ``agy`` the user
    started by hand — including sessions this app has nothing to do with,
    routed to a socket belonging to a process that may not be running. It
    carried the ``|| printf '{"decision":"allow"}'`` fallback, so those
    sessions failed open rather than hanging, which is exactly what made
    the arrangement easy not to notice.

    AG-21's private root replaces it. A new machine never gets one; an
    upgraded machine keeps one forever unless something takes it out, and
    "the user's own ``agy`` is untouched" is false the whole time.
    """

    def test_it_removes_our_entry(self, tmp_path, monkeypatch):
        theirs = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)
        install.install(tmp_path / "cfg", path=theirs)

        assert install.retire_global() is True
        assert not theirs.exists()

    def test_a_clean_machine_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "GLOBAL_HOOKS", tmp_path / "hooks.json")
        assert install.retire_global() is False

    def test_it_is_idempotent(self, tmp_path, monkeypatch):
        theirs = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)
        install.install(tmp_path / "cfg", path=theirs)

        assert install.retire_global() is True
        assert install.retire_global() is False

    def test_it_leaves_the_rest_of_the_file_alone(self, tmp_path, monkeypatch):
        """Ours to remove; the file is not ours to delete."""
        theirs = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)
        install.install(tmp_path / "cfg", path=theirs)
        data = json.loads(theirs.read_text(encoding="utf-8"))
        data["their-own-hook"] = {"PreToolUse": [{"command": "true"}]}
        theirs.write_text(json.dumps(data), encoding="utf-8")

        install.retire_global()

        assert list(json.loads(theirs.read_text(encoding="utf-8"))) == [
            "their-own-hook"
        ]

    def test_an_unparseable_file_is_not_touched(self, tmp_path, monkeypatch):
        """They may be mid-edit, and a broken file is not a licence."""
        theirs = tmp_path / "hooks.json"
        theirs.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)

        assert install.retire_global() is False
        assert theirs.read_text(encoding="utf-8") == "{not json"
