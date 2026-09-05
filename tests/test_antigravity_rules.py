"""AG-15's standing rules, and the one bug they must never have.

`rules.py` decides whether a call the user already approved can run again
without asking. **Its only possible bug is an ungated write**, so most of
this file is about matching being narrower than it looks, not about the
happy path.

Offline. No engine, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aic_dc.antigravity.rules import (
    MATCH_KEY,
    RuleStore,
    derive_rules,
)


def store(tmp_path, repo=None):
    return RuleStore(repo or tmp_path, config_dir=tmp_path / "cfg")


def command_rule(tmp_path, command, *, which=0):
    rules = derive_rules(tmp_path, "run_command", {"command": command}, "exec")
    return rules[which]


class TestCommandRulesDoNotWiden:
    """The tripwire AG-15 names: a rule must not grow past what was shown."""

    def test_a_literal_rule_matches_only_that_command(self, tmp_path):
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "rm -rf build/"))
        assert s.allows(command="rm -rf build/", path=None, tool_name="run_command")
        for other in ("rm -rf /", "rm -rf build/ /", "rm -rf", "rm -rf build"):
            assert s.allows(
                command=other, path=None, tool_name="run_command"
            ) is None, other

    def test_a_prefix_rule_stops_at_a_token_boundary(self, tmp_path):
        # `git push:*` covering `git pushover` is the whole reason the
        # prefix form is offered as a second, separately-labelled choice
        # rather than as the default.
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "git push origin main", which=1))
        assert s.allows(command="git push", path=None, tool_name="run_command")
        assert s.allows(
            command="git push --force origin main", path=None, tool_name="run_command"
        )
        assert s.allows(
            command="git pushover", path=None, tool_name="run_command"
        ) is None
        assert s.allows(
            command="git pull", path=None, tool_name="run_command"
        ) is None

    def test_whitespace_is_not_silently_normalised_into_a_match(self, tmp_path):
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "ls -la"))
        # Surrounding whitespace is stripped on both sides, which is the
        # same command. Internal whitespace is not: `ls  -la` is a different
        # string and collapsing it would be inventing a match.
        assert s.allows(command="  ls -la  ", path=None, tool_name="run_command")
        assert s.allows(command="ls  -la", path=None, tool_name="run_command") is None


class TestPathRulesDoNotWiden:
    def test_a_path_rule_matches_one_file_and_not_its_directory(self, tmp_path):
        s = store(tmp_path)
        rules = derive_rules(
            tmp_path, "replace_file_content",
            {"file_path": str(tmp_path / "src" / "a.py")}, "write",
        )
        s.add(rules[0])
        assert s.allows(
            command=None, path=str(tmp_path / "src" / "a.py"),
            tool_name="replace_file_content",
        )
        for other in ("src", "src/b.py", "src/a.py.bak"):
            assert s.allows(
                command=None, path=str(tmp_path / other),
                tool_name="replace_file_content",
            ) is None, other

    def test_a_rule_for_one_tool_does_not_permit_another(self, tmp_path):
        # The conservative choice recorded in `path_match`: a grant made by
        # reading a *diff* must not also permit a whole-file overwrite.
        s = store(tmp_path)
        rules = derive_rules(
            tmp_path, "replace_file_content",
            {"file_path": str(tmp_path / "a.py")}, "write",
        )
        s.add(rules[0])
        assert s.allows(
            command=None, path=str(tmp_path / "a.py"),
            tool_name="replace_file_content",
        )
        assert s.allows(
            command=None, path=str(tmp_path / "a.py"), tool_name="write_to_file"
        ) is None

    @pytest.mark.parametrize("directory", [".claude", ".gemini", ".aic-dc"])
    def test_no_standing_rule_for_a_directory_that_grants_permissions(
        self, tmp_path, directory
    ):
        """A rule here would let the agent edit the gate that asks.

        CC-16's argument, carried across and widened by two: `.gemini` is
        where `agy`'s hook configuration lives, and a write there is a write
        to the only thing standing between the model and the tree.
        """
        rules = derive_rules(
            tmp_path, "replace_file_content",
            {"file_path": str(tmp_path / directory / "settings.json")}, "write",
        )
        assert rules == []


class TestTheStorePersists:
    def test_a_rule_survives_a_new_store_on_the_same_file(self, tmp_path):
        """The exit criterion's "survives a server restart", at this layer."""
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "ls"))
        fresh = store(tmp_path)
        assert fresh.allows(command="ls", path=None, tool_name="run_command")

    def test_rules_are_keyed_by_repository(self, tmp_path):
        # One store file, many repos. A grant made in one must not apply in
        # another — they are different working trees with different stakes.
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        store(tmp_path, a).add(command_rule(tmp_path, "ls"))
        assert store(tmp_path, b).allows(
            command="ls", path=None, tool_name="run_command"
        ) is None

    def test_a_rule_with_no_matching_data_is_refused(self, tmp_path):
        # It could never permit anything, so storing it would leave an entry
        # that looks like a grant and is not — the user would be asked again
        # while believing they had said always.
        s = store(tmp_path)
        assert s.add({"label": "Always allow x", "tool_name": "x"}) is False
        assert s.rules() == []

    def test_an_unreadable_store_grants_nothing(self, tmp_path):
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "ls"))
        s.path.write_text("{ not json", encoding="utf-8")
        fresh = store(tmp_path)
        assert fresh.allows(command="ls", path=None, tool_name="run_command") is None

    def test_adding_the_same_rule_twice_stores_one(self, tmp_path):
        s = store(tmp_path)
        s.add(command_rule(tmp_path, "ls"))
        s.add(command_rule(tmp_path, "ls"))
        assert len(s.rules()) == 1


class TestWhatIsOffered:
    def test_a_command_offers_the_literal_first_and_the_prefix_second(self, tmp_path):
        rules = derive_rules(
            tmp_path, "run_command", {"command": "git push origin main"}, "exec"
        )
        assert [r["rule_content"] for r in rules] == [
            "git push origin main", "git push:*",
        ]
        # Narrowest first, because the dialog's default is index 0.
        assert rules[0][MATCH_KEY]["kind"] == "command"
        assert rules[1][MATCH_KEY]["kind"] == "command_prefix"

    def test_the_prefix_is_stored_without_its_pattern_suffix(self, tmp_path):
        # `:*` is label syntax. Matching against it literally would mean no
        # command ever matched, and the control would look broken rather
        # than absent — the quiet failure this engine keeps producing.
        rules = derive_rules(
            tmp_path, "run_command", {"command": "git push origin main"}, "exec"
        )
        assert rules[1][MATCH_KEY]["value"] == "git push"

    def test_no_rule_is_offered_for_a_delegate_call(self, tmp_path):
        # A standing grant to spawn subagents cannot be described in one
        # line, and the child's calls are not knowable at this point.
        assert derive_rules(tmp_path, "start_subagent", {}, "delegate") == []

    def test_nothing_is_offered_when_there_is_no_command(self, tmp_path):
        assert derive_rules(tmp_path, "run_command", {}, "exec") == []
