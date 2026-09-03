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
        """
        install.install(cfg, path=hooks, python="/somewhere/else/python")
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
