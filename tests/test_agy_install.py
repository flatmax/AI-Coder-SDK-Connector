"""Tests for installing the gate into the user's own ``agy`` configuration.

This is the module that writes **outside the repository**, into a file
belonging to Google's CLI that the user may already be using. So the
assertions here are mostly about restraint: what it must not touch, what
it must not overwrite, and what happens to *their* sessions when ours is
not running.

The one that matters most is the fallback. ``agy`` **blocks a tool** when
a hook command cannot be run — exit 127, measured — so a stale entry
pointing at a deleted virtualenv would stop the user's own ``agy`` working
entirely. The installed command is wrapped so that cannot happen, and the
reasoning is only sound because our hook exits 0 on every path it
controls: a non-zero exit means the interpreter never started, which means
this host owns nothing, which means allow is right.

**And the second thing that matters is the edge of that reasoning**, added
2026-09-05. "A non-zero exit means the interpreter never started" is true
only while the command's *shape* is right. On a PyInstaller build it was
not: ``sys.executable`` is the frozen binary, which does not honour
``-m``, so the command exited 2 on every call of a session this host did
own — an ungated agent, with Settings reporting the gate installed,
because ``status`` compares command strings and the string was correct.
``TestTheCommandMustActuallyRun`` is that hole, closed from both ends.

Offline. Never touches the real ``~/.gemini``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from aic_dc.agy import install


@pytest.fixture
def hooks(tmp_path):
    return tmp_path / "hooks.json"


@pytest.fixture
def cfg(tmp_path):
    return tmp_path / "cfg"


def _write_entry(hooks, command):
    """Put a hook entry on disk without going through ``install``.

    For the states ``install`` now refuses to create. Same shape it
    writes, so ``status`` reads it the same way.
    """
    hooks.write_text(
        json.dumps(
            {
                install.HOOK_NAME: {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
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
        _write_entry(hooks, install.hook_command(cfg, "/somewhere/else/python"))
        report = install.status(cfg, path=hooks)
        assert report["state"] == "stale"
        assert "/somewhere/else/python" in report["command"]
        assert report["expected"] != report["command"]

    def test_unreadable_is_reported_rather_than_guessed(self, hooks, cfg):
        hooks.write_text("{ this is not json", encoding="utf-8")
        report = install.status(cfg, path=hooks)
        assert report["state"] == "unreadable"
        assert "not valid JSON" in report["detail"]


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

    def test_a_missing_interpreter_allows_rather_than_blocking(self, tmp_path):
        """The failure that would otherwise break standalone `agy` entirely.

        `agy` blocks a tool whose hook command cannot run — exit 127. A
        stale entry pointing at a deleted virtualenv would therefore stop
        the user's own sessions working, with an error naming a program
        they may not recognise. Run here as a real shell command, because
        the guarantee is the shell's `||` and not anything in Python.

        The reasoning is sound rather than merely convenient: our hook
        exits 0 on every path it controls, so a non-zero exit means the
        interpreter never started, so this host owns nothing, so allow is
        correct.
        """
        command = install.hook_command(tmp_path / "cfg", python="/no/such/python")
        done = subprocess.run(
            ["sh", "-c", command], input="{}", capture_output=True, text=True
        )
        assert json.loads(done.stdout) == {"decision": "allow"}

    def test_a_working_interpreter_answers_for_itself(self, tmp_path):
        """And the fallback does not fire when the hook does run."""
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
        # One decision, not two: the fallback appending a second object
        # would make the output unparseable.
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

    def test_the_fallback_survives_both_forms(self, cfg, monkeypatch):
        """It is what keeps a stranger's agy working when we are not."""
        for frozen in (True, False):
            monkeypatch.setattr(install.sys, "frozen", frozen, raising=False)
            assert install.hook_command(cfg).endswith(
                """|| printf '{"decision":"allow"}'"""
            )

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
