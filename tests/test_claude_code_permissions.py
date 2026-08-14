"""Tests for ac_dc.claude_code.permissions — the phase-2 gate.

The properties under test are the three the module docstring pins, because
each has a failure mode worse than a broken dialog:

- **Every request resolves exactly once, and the SDK always gets a
  result.** A request that never resolved wedges the turn; one that
  resolved twice answers one control request and leaves another hanging.
- **The callback never raises.** An exception answers the CLI's control
  request with an error, which it reports as a *tool failure* — so an
  AC-DC bug would look to the user like the tool broke.
- **Denials carry a reason.** A blank denial produces an agent that
  retries the same call immediately.

Plus the diff, which is the feature: a write dialog that showed only a
tool name would be a permission prompt the user cannot answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ac_dc.claude_code.permissions import (
    DENY_DEFAULT_REASON,
    DIFF_CEILING_BYTES,
    PermissionBroker,
    build_answer_input,
    build_command_payload,
    build_diff_payload,
    build_question_payload,
    classify_tool,
    command_flags,
    derive_suggested_rules,
    read_denied_read_files,
    summarise_request,
    write_denied_read_files,
)


class FakeContext:
    """Stands in for ``ToolPermissionContext``."""

    def __init__(self, tool_use_id="toolu_01", suggestions=None, **extra):
        self.tool_use_id = tool_use_id
        self.suggestions = suggestions or []
        self.agent_id = extra.get("agent_id")
        self.blocked_path = extra.get("blocked_path")
        self.decision_reason = extra.get("decision_reason")
        self.title = extra.get("title")
        self.display_name = extra.get("display_name")
        self.description = extra.get("description")


class Events:
    """Collects broadcasts in order."""

    def __init__(self):
        self.calls: list[tuple[str, object, bool]] = []

    async def __call__(self, event):
        self.calls.append((event.name, event.payload, event.turn_scoped))

    def named(self, name):
        return [payload for event, payload, _ in self.calls if event == name]

    def only(self, name):
        matches = self.named(name)
        assert len(matches) == 1, f"expected one {name}, got {len(matches)}"
        return matches[0]


@pytest.fixture
def events():
    return Events()


@pytest.fixture
def broker(tmp_path, events):
    return PermissionBroker(
        tmp_path,
        broadcast=events,
        note_prompt=lambda tool_use_id: "req-1",
        decision_timeout=5.0,
        no_localhost_timeout=1.0,
    )


async def ask(broker, tool_name="Bash", tool_input=None, context=None):
    """Start a ``can_use_tool`` call and hand back its task once broadcast."""
    task = asyncio.create_task(
        broker.can_use_tool(
            tool_name, tool_input if tool_input is not None else {"command": "ls"},
            context or FakeContext(),
        )
    )
    await settle(lambda: bool(broker.pending()), task)
    return task


async def settle(condition, *tasks):
    """Wait for ``condition``. A real sleep, because the write path builds
    its diff in an executor thread and a bare ``sleep(0)`` never lets it
    land."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.002)
    for task in tasks:
        task.cancel()
    raise AssertionError("the broker never reached the expected state")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            ("Read", "read"),
            ("Grep", "read"),
            ("TodoWrite", "read"),
            ("Edit", "write"),
            ("MultiEdit", "write"),
            ("Write", "write"),
            ("NotebookEdit", "write"),
            ("Bash", "exec"),
            ("KillShell", "exec"),
            ("Task", "delegate"),
            ("AskUserQuestion", "interact"),
            ("mcp__playwright__click", "mcp"),
            ("mcp__ac-dc__repo_map", "read"),
        ],
    )
    def test_known_tools(self, tool_name, expected):
        assert classify_tool(tool_name) == expected

    def test_an_unknown_tool_gets_the_most_cautious_dialog(self):
        """A built-in added in a CLI upgrade must not arrive under-described."""
        assert classify_tool("Teleport") == "exec"

    def test_a_malformed_mcp_name_is_still_mcp(self):
        assert classify_tool("mcp__halfway") == "mcp"


# ---------------------------------------------------------------------------
# The diff is the feature
# ---------------------------------------------------------------------------


class TestDiffPayload:
    def test_an_edit_shows_original_and_proposed(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("one\ntwo\nthree\n")
        payload = build_diff_payload(
            tmp_path,
            "Edit",
            {"file_path": "a.py", "old_string": "two", "new_string": "TWO"},
        )
        assert payload["path"] == "a.py"
        assert payload["original"] == "one\ntwo\nthree\n"
        assert payload["proposed"] == "one\nTWO\nthree\n"
        assert (payload["additions"], payload["deletions"]) == (1, 1)
        assert payload["is_new_file"] is False

    def test_a_write_to_a_missing_file_is_flagged_new(self, tmp_path):
        payload = build_diff_payload(
            tmp_path, "Write", {"file_path": "new.txt", "content": "hi\n"}
        )
        assert payload["is_new_file"] is True
        assert payload["original"] is None
        assert payload["proposed"] == "hi\n"
        assert payload["additions"] == 1

    def test_multiedit_applies_edits_in_order(self, tmp_path):
        (tmp_path / "a.txt").write_text("a\nb\n")
        payload = build_diff_payload(
            tmp_path,
            "MultiEdit",
            {
                "file_path": "a.txt",
                "edits": [
                    {"old_string": "a", "new_string": "x"},
                    {"old_string": "x\nb", "new_string": "x\ny"},
                ],
            },
        )
        assert payload["proposed"] == "x\ny\n"

    def test_a_replacement_that_does_not_match_yields_no_proposal(self, tmp_path):
        """Better an honest 'cannot show' than a diff the agent isn't asking for."""
        (tmp_path / "a.txt").write_text("a\n")
        payload = build_diff_payload(
            tmp_path, "Edit", {"file_path": "a.txt", "old_string": "zzz", "new_string": "q"}
        )
        assert payload["proposed"] is None
        assert payload["original"] == "a\n"

    def test_a_binary_file_is_labelled_not_diffed(self, tmp_path):
        (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        payload = build_diff_payload(
            tmp_path, "Edit", {"file_path": "blob.bin", "old_string": "a", "new_string": "b"}
        )
        assert payload["is_binary"] is True
        assert payload["original"] is None

    def test_a_huge_file_is_labelled_not_loaded(self, tmp_path):
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * (DIFF_CEILING_BYTES + 1))
        payload = build_diff_payload(
            tmp_path, "Write", {"file_path": "big.txt", "content": "small"}
        )
        assert payload["too_large"] is True
        assert payload["original"] is None

    def test_a_huge_proposal_is_labelled_too(self, tmp_path):
        payload = build_diff_payload(
            tmp_path,
            "Write",
            {"file_path": "out.txt", "content": "y" * (DIFF_CEILING_BYTES + 1)},
        )
        assert payload["too_large"] is True

    def test_a_path_outside_the_repo_shows_absolute(self, tmp_path):
        outside = tmp_path.parent / "elsewhere.txt"
        payload = build_diff_payload(
            tmp_path, "Write", {"file_path": str(outside), "content": "x"}
        )
        assert payload["path"] == str(outside)

    def test_a_notebook_shows_the_new_cell_source(self, tmp_path):
        (tmp_path / "nb.ipynb").write_text(json.dumps({"cells": []}))
        payload = build_diff_payload(
            tmp_path,
            "NotebookEdit",
            {"notebook_path": "nb.ipynb", "new_source": "print(1)"},
        )
        assert payload["proposed"] == "print(1)"
        assert payload["original"] is None

    def test_no_path_is_no_payload(self, tmp_path):
        assert build_diff_payload(tmp_path, "Write", {"content": "x"}) is None


# ---------------------------------------------------------------------------
# Command and question payloads
# ---------------------------------------------------------------------------


class TestCommandPayload:
    def test_flags_are_advisory_labels(self):
        assert "deletes" in command_flags("rm -rf build")
        assert "network" in command_flags("git push origin main")
        assert "sudo" in command_flags("sudo apt install x")
        assert "writes" in command_flags("echo hi > out.txt")
        assert command_flags("ls -la") == []

    def test_a_long_command_is_truncated_visibly(self, tmp_path):
        payload = build_command_payload(
            tmp_path, "Bash", {"command": "echo " + "a" * 9000}
        )
        assert payload["truncated"] is True
        assert len(payload["command"]) == 4_000

    def test_cwd_defaults_to_the_repo(self, tmp_path):
        payload = build_command_payload(tmp_path, "Bash", {"command": "ls"})
        assert payload["cwd"] == str(tmp_path)
        assert payload["truncated"] is False


class TestQuestionPayload:
    def test_the_sdk_list_shape_is_promoted(self):
        payload = build_question_payload(
            {
                "questions": [
                    {
                        "question": "Which?",
                        "header": "Pick",
                        "options": [{"label": "A", "description": "first"}, "B"],
                        "multiSelect": True,
                    }
                ]
            }
        )
        assert payload["question"] == "Which?"
        assert payload["multi_select"] is True
        assert [o["label"] for o in payload["options"]] == ["A", "B"]
        assert len(payload["questions"]) == 1

    def test_a_single_question_shape_still_works(self):
        payload = build_question_payload({"question": "Why?", "options": ["x"]})
        assert payload["question"] == "Why?"

    def test_nothing_renderable_is_none(self):
        assert build_question_payload({}) is None


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


class TestAnswerInput:
    """``AskUserQuestion`` reads its answers off its own input.

    The CLI's tool implementation takes ``answers`` as a map keyed by
    question text, one string per question, multi-select joined with
    ``", "``. Allowing the call without it is not neutral: the tool result
    becomes "The user did not answer the questions".
    """

    INPUT = {
        "questions": [
            {
                "question": "Which branch?",
                "options": [{"label": "main"}, {"label": "dev5"}],
            },
            {
                "question": "Which files?",
                "multiSelect": True,
                "options": [{"label": "a"}, {"label": "b"}, {"label": "c"}],
            },
        ]
    }

    def question(self):
        return build_question_payload(self.INPUT)

    def test_answers_key_off_the_question_text(self):
        updated = build_answer_input(self.INPUT, self.question(), [[1], [0]])
        assert updated["answers"] == {"Which branch?": "dev5", "Which files?": "a"}
        # The rest of the input travels unchanged: `updated_input` replaces
        # the whole input, it does not patch it.
        assert updated["questions"] == self.INPUT["questions"]

    def test_a_multi_select_joins_with_a_comma_and_space(self):
        # The CLI splits on exactly ", " to check the parts back against
        # the option labels, so the separator is part of the contract.
        updated = build_answer_input(self.INPUT, self.question(), [[0], [0, 2]])
        assert updated["answers"]["Which files?"] == "a, c"

    def test_the_verbatim_question_text_wins_over_our_normalisation(self):
        # `build_question_payload` falls back to `header` when a question
        # carries no text. Keying an answer off that fallback would produce
        # a map the CLI never looks in.
        raw = {"questions": [{"header": "Pick", "options": [{"label": "A"}]}]}
        normalised = build_question_payload(raw)
        assert normalised["questions"][0]["question"] == "Pick"
        raw_with_text = {
            "questions": [
                {"question": "Really?", "header": "Pick", "options": [{"label": "A"}]}
            ]
        }
        updated = build_answer_input(raw_with_text, normalised, [[0]])
        assert updated["answers"] == {"Really?": "A"}

    def test_a_question_left_unanswered_gets_no_key(self):
        # A missing key reads to the CLI as a question the user declined,
        # which is at least true. Inventing an answer would not be.
        updated = build_answer_input(self.INPUT, self.question(), [[], [1]])
        assert updated["answers"] == {"Which files?": "b"}

    def test_an_out_of_range_index_is_dropped_not_guessed(self):
        updated = build_answer_input(self.INPUT, self.question(), [[7], [1]])
        assert "Which branch?" not in updated["answers"]

    def test_nothing_to_say_is_none(self):
        assert build_answer_input(self.INPUT, self.question(), []) is None
        assert build_answer_input(self.INPUT, self.question(), [[], []]) is None
        assert build_answer_input(self.INPUT, self.question(), None) is None
        assert build_answer_input(self.INPUT, None, [[0]]) is None
        assert build_answer_input(self.INPUT, self.question(), [True, "x"]) is None


# ---------------------------------------------------------------------------
# Suggested rules
# ---------------------------------------------------------------------------


class FakeRule:
    def __init__(self, tool_name, rule_content=None):
        self.tool_name = tool_name
        self.rule_content = rule_content


class FakeSuggestion:
    def __init__(self, rules, kind="addRules", behavior="allow", destination="projectSettings"):
        self.type = kind
        self.rules = rules
        self.behavior = behavior
        self.destination = destination


class TestSuggestedRules:
    def test_the_cli_suggestion_wins(self, tmp_path):
        rules = derive_suggested_rules(
            tmp_path,
            "Bash",
            {"command": "git status"},
            "exec",
            [FakeSuggestion([FakeRule("Bash", "git status:*")])],
        )
        assert len(rules) == 1
        assert rules[0]["origin"] == "cli"
        assert rules[0]["rule_content"] == "git status:*"

    def test_a_non_rule_suggestion_is_ignored(self, tmp_path):
        """setMode has no place on a control labelled 'always allow this call'."""
        rules = derive_suggested_rules(
            tmp_path,
            "Bash",
            {"command": "ls"},
            "exec",
            [FakeSuggestion([], kind="setMode")],
        )
        assert all(rule["origin"] == "derived" for rule in rules)

    def test_a_derived_command_rule_keeps_the_subcommand(self, tmp_path):
        rules = derive_suggested_rules(
            tmp_path, "Bash", {"command": "git push origin main"}, "exec", None
        )
        assert rules[0]["rule_content"] == "git push:*"
        assert rules[0]["origin"] == "derived"

    def test_a_plain_command_gets_one_token(self, tmp_path):
        rules = derive_suggested_rules(tmp_path, "Bash", {"command": "ls -la"}, "exec", None)
        assert rules[0]["rule_content"] == "ls:*"

    def test_a_derived_write_rule_grants_only_the_approved_file(self, tmp_path):
        """The CLI's own rule "matches only the literal path you approved".

        Not ``src/ac_dc/**``, which reads like "this directory" but is
        recursive in gitignore syntax.
        """
        rules = derive_suggested_rules(
            tmp_path, "Edit", {"file_path": "src/ac_dc/x.py"}, "write", None
        )
        assert rules[0]["rule_content"] == "src/ac_dc/x.py"

    def test_a_file_at_the_repo_root_does_not_grant_the_repo(self, tmp_path):
        """The regression this pins.

        Deriving the *parent directory* of a root-level file produced ``**``
        — every file in the repository — from one click on a dialog that
        named a single file.
        """
        rules = derive_suggested_rules(
            tmp_path, "Write", {"file_path": "README.md"}, "write", None
        )
        assert rules[0]["rule_content"] == "README.md"

    @pytest.mark.parametrize(
        ("tool", "tool_class", "expected"),
        [
            ("Write", "write", "Edit"),
            ("Edit", "write", "Edit"),
            ("MultiEdit", "write", "Edit"),
            ("Read", "read", "Read"),
            ("Glob", "read", "Read"),
        ],
    )
    def test_a_path_rule_names_the_tool_the_cli_checks(
        self, tmp_path, tool, tool_class, expected
    ):
        """Claude Code consults path rules for Edit and Read only.

        A rule written for Write/MultiEdit/NotebookEdit/Glob is accepted,
        never consulted, and warned about at startup — so "always allow"
        would write a rule that does nothing and the user would be asked
        again on the very next call.
        """
        rules = derive_suggested_rules(
            tmp_path, tool, {"file_path": "a/b.py", "path": "a/b.py"}, tool_class, None
        )
        assert rules[0]["tool_name"] == expected

    def test_the_label_names_the_rule_that_will_be_written(self, tmp_path):
        """A Write request whose rule is an Edit rule must say Edit."""
        rules = derive_suggested_rules(
            tmp_path, "Write", {"file_path": "a/b.py"}, "write", None
        )
        assert rules[0]["label"] == "Always allow Edit(a/b.py)"

    def test_a_notebook_edit_uses_its_own_path_key(self, tmp_path):
        rules = derive_suggested_rules(
            tmp_path, "NotebookEdit", {"notebook_path": "nb/x.ipynb"}, "write", None
        )
        assert rules[0]["tool_name"] == "Edit"
        assert rules[0]["rule_content"] == "nb/x.ipynb"

    def test_a_read_tool_with_no_consulted_rule_derives_nothing(self, tmp_path):
        """Grep takes a path but is not a name the CLI checks path rules for.

        A rule whose effect we cannot predict is worse than no rule: it
        would read as a grant while doing nothing.
        """
        assert derive_suggested_rules(
            tmp_path, "Grep", {"path": "src"}, "read", None
        ) == []

    def test_a_path_outside_the_repo_is_anchored_absolutely(self, tmp_path):
        """``//`` is the CLI's anchor for the filesystem root.

        A single leading slash means "relative to the settings file", so
        ``/etc/hosts`` would resolve under the project root and the rule
        would never match the file the user approved.
        """
        rules = derive_suggested_rules(
            tmp_path, "Read", {"file_path": "/etc/hosts"}, "read", None
        )
        assert rules[0]["rule_content"] == "//etc/hosts"

    def test_gitignore_metacharacters_in_a_path_are_escaped(self, tmp_path):
        """Otherwise a rule fails to match its own path, or matches siblings.

        The CLI escapes these for the same reason; a directory named
        ``[2024-06] Reports`` is the documented case.
        """
        rules = derive_suggested_rules(
            tmp_path, "Edit", {"file_path": "[2024-06] Reports/x.py"}, "write", None
        )
        assert rules[0]["rule_content"] == r"\[2024-06\] Reports/x.py"

    def test_no_bare_tool_grant_is_ever_derived(self, tmp_path):
        """The one rule we could derive for these is a bare grant. Never."""
        for tool, klass in (("Task", "delegate"), ("mcp__x__y", "mcp"), ("AskUserQuestion", "interact")):
            assert derive_suggested_rules(tmp_path, tool, {}, klass, None) == []


# ---------------------------------------------------------------------------
# The callback contract
# ---------------------------------------------------------------------------


class TestCanUseTool:
    async def test_an_allow_reaches_the_sdk(self, broker, events):
        task = await ask(broker)
        payload = events.only("permissionRequest")
        result = await broker.resolve(payload["permission_id"], {"action": "allow"})
        assert result == {"status": "accepted"}
        allow = await task
        assert type(allow).__name__ == "PermissionResultAllow"
        assert allow.updated_permissions is None

    async def test_a_deny_carries_the_reason(self, broker, events):
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "deny", "reason": "not on main"})
        deny = await task
        assert type(deny).__name__ == "PermissionResultDeny"
        assert deny.message == "not on main"
        assert deny.interrupt is False

    async def test_a_reasonless_deny_still_says_something(self, broker, events):
        """A blank denial produces an agent that retries the same call."""
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "deny"})
        assert (await task).message == DENY_DEFAULT_REASON

    async def test_deny_interrupt_interrupts(self, broker, events):
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "deny_interrupt", "reason": "stop"})
        assert (await task).interrupt is True

    async def test_allow_always_carries_a_permission_update(self, broker, events):
        task = await ask(broker)
        payload = events.only("permissionRequest")
        assert payload["suggested_rules"], "an exec call must offer a rule"
        await broker.resolve(
            payload["permission_id"], {"action": "allow_always", "rule_index": 0}
        )
        allow = await task
        assert len(allow.updated_permissions) == 1
        assert allow.updated_permissions[0].type == "addRules"

    async def test_allow_always_with_a_bad_index_allows_once(self, broker, events):
        """Allow the call the user allowed; do not invent a rule."""
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "allow_always", "rule_index": 99})
        allow = await task
        assert allow.updated_permissions is None
        assert events.only("permissionResolved")["rule_written"] is None

    async def test_updated_input_is_passed_through(self, broker, events):
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(
            pid, {"action": "allow", "updated_input": {"command": "ls -l"}}
        )
        assert (await task).updated_input == {"command": "ls -l"}

    async def test_answers_reach_the_tool_as_its_own_input(self, broker, events):
        """The dialog sends indices; the tool needs an ``answers`` map.

        Nothing else closes that gap, and an allow without it means the
        agent hears "The user did not answer the questions" after the user
        answered them.
        """
        tool_input = {
            "questions": [
                {
                    "question": "Which branch?",
                    "options": [{"label": "main"}, {"label": "dev5"}],
                }
            ]
        }
        task = await ask(broker, "AskUserQuestion", tool_input)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "allow", "answers": [[1]]})
        allow = await task
        assert allow.updated_input == {**tool_input, "answers": {"Which branch?": "dev5"}}

    async def test_an_unanswered_question_is_allowed_without_an_answers_key(
        self, broker, events
    ):
        task = await ask(
            broker, "AskUserQuestion", {"questions": [{"question": "Which?"}]}
        )
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "allow"})
        assert (await task).updated_input is None

    async def test_a_denied_question_carries_no_answers(self, broker, events):
        task = await ask(
            broker,
            "AskUserQuestion",
            {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]},
        )
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "deny", "reason": "ask me later"})
        deny = await task
        assert type(deny).__name__ == "PermissionResultDeny"

    async def test_an_unrecognised_action_denies_with_an_explanation(self, broker, events):
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "maybe"})
        deny = await task
        assert type(deny).__name__ == "PermissionResultDeny"
        assert "maybe" in deny.message

    async def test_a_second_decision_is_refused_with_attribution(self, broker, events):
        task = await ask(broker)
        pid = events.only("permissionRequest")["permission_id"]
        await broker.resolve(pid, {"action": "allow"}, resolved_by="tab-1")
        await task
        again = await broker.resolve(pid, {"action": "deny"})
        assert again == {"error": "already_resolved", "resolved_by": "tab-1"}

    async def test_an_unknown_id_is_reported_as_unknown(self, broker):
        result = await broker.resolve("perm-nope", {"action": "allow"})
        assert result["error"] == "unknown"
        assert "perm-nope" in result["reason"]

    async def test_a_timeout_denies_without_wedging_the_turn(self, tmp_path, events):
        broker = PermissionBroker(
            tmp_path, broadcast=events, decision_timeout=0.01, no_localhost_timeout=0.01
        )
        deny = await broker.can_use_tool("Bash", {"command": "ls"}, FakeContext())
        assert type(deny).__name__ == "PermissionResultDeny"
        assert "denied automatically" in deny.message
        assert events.only("permissionResolved")["action"] == "timeout"
        assert broker.pending() == []

    async def test_no_localhost_takes_the_short_timeout(self, tmp_path, events):
        broker = PermissionBroker(
            tmp_path,
            broadcast=events,
            localhost_available=lambda: False,
            decision_timeout=30.0,
            no_localhost_timeout=0.01,
        )
        deny = await broker.can_use_tool("Bash", {"command": "ls"}, FakeContext())
        assert "No local AC-DC client" in deny.message

    async def test_an_unknowable_localhost_state_fails_closed(self, tmp_path, events):
        def boom():
            raise RuntimeError("collab is confused")

        broker = PermissionBroker(
            tmp_path,
            broadcast=events,
            localhost_available=boom,
            decision_timeout=30.0,
            no_localhost_timeout=0.01,
        )
        deny = await broker.can_use_tool("Bash", {"command": "ls"}, FakeContext())
        assert "No local AC-DC client" in deny.message

    async def test_a_payload_failure_denies_rather_than_raising(self, broker, monkeypatch):
        """Raising would surface to the user as a broken tool, not a denial."""
        import ac_dc.claude_code.permissions as module

        monkeypatch.setattr(
            module, "classify_tool", lambda name: (_ for _ in ()).throw(RuntimeError("x"))
        )
        deny = await broker.can_use_tool("Bash", {"command": "ls"}, FakeContext())
        assert type(deny).__name__ == "PermissionResultDeny"
        assert "AC-DC fault" in deny.message

    async def test_a_broadcast_failure_does_not_lose_the_decision(self, tmp_path):
        calls = []

        async def flaky(event):
            calls.append(event.name)
            if event.name == "permissionResolved":
                raise RuntimeError("socket gone")

        broker = PermissionBroker(tmp_path, broadcast=flaky, decision_timeout=5.0)
        task = asyncio.create_task(
            broker.can_use_tool("Bash", {"command": "ls"}, FakeContext())
        )
        await settle(lambda: bool(broker.pending()))
        pid = broker.pending()[0]["permission_id"]
        assert await broker.resolve(pid, {"action": "allow"}) == {"status": "accepted"}
        assert type(await task).__name__ == "PermissionResultAllow"

    async def test_a_note_prompt_failure_does_not_stop_the_dialog(self, tmp_path, events):
        def boom(tool_use_id):
            raise RuntimeError("no turn")

        broker = PermissionBroker(
            tmp_path, broadcast=events, note_prompt=boom, decision_timeout=5.0
        )
        task = await ask(broker)
        assert events.only("permissionRequest")["request_id"] is None
        await broker.resolve(broker.pending()[0]["permission_id"], {"action": "allow"})
        await task

    async def test_cancel_all_answers_everything_outstanding(self, broker, events):
        first = await ask(broker, tool_input={"command": "ls"})
        second = asyncio.create_task(
            broker.can_use_tool("Bash", {"command": "pwd"}, FakeContext("toolu_02"))
        )
        await settle(lambda: len(broker.pending()) >= 2)

        await broker.cancel_all()
        for task in (first, second):
            result = await task
            assert type(result).__name__ == "PermissionResultDeny"
            assert "shut down" in result.message
        assert len(events.named("permissionResolved")) == 2

    async def test_shutdown_after_the_fact_is_a_no_op(self, broker, events):
        task = await ask(broker)
        await broker.resolve(broker.pending()[0]["permission_id"], {"action": "allow"})
        await task
        await broker.cancel_all()
        assert len(events.named("permissionResolved")) == 1


# ---------------------------------------------------------------------------
# The broadcast payload
# ---------------------------------------------------------------------------


class TestPayload:
    async def test_the_request_is_session_wide(self, broker, events):
        """Every browser must see the dialog, not just the turn's originator."""
        task = await ask(broker)
        assert events.calls[0][0] == "permissionRequest"
        assert events.calls[0][2] is False
        await broker.resolve(broker.pending()[0]["permission_id"], {"action": "deny"})
        await task

    async def test_a_write_carries_a_diff(self, tmp_path, events, broker):
        (tmp_path / "a.txt").write_text("old\n")
        task = await ask(
            broker,
            "Edit",
            {"file_path": "a.txt", "old_string": "old", "new_string": "new"},
        )
        payload = events.only("permissionRequest")
        assert payload["tool_class"] == "write"
        assert payload["diff"]["proposed"] == "new\n"
        assert payload["command"] is None
        await broker.resolve(payload["permission_id"], {"action": "deny"})
        await task

    async def test_an_exec_carries_a_command_and_no_diff(self, broker, events):
        task = await ask(broker, "Bash", {"command": "rm -rf build"})
        payload = events.only("permissionRequest")
        assert payload["diff"] is None
        assert payload["command"]["command"] == "rm -rf build"
        assert "deletes" in payload["command"]["flags"]
        assert payload["gated_by_default"] is True
        await broker.resolve(payload["permission_id"], {"action": "deny"})
        await task

    async def test_a_read_says_it_is_not_gated_by_default(self, broker, events):
        """A Read reaching us means a rule or a directory bound — say so."""
        task = await ask(broker, "Read", {"file_path": "/etc/hosts"})
        payload = events.only("permissionRequest")
        assert payload["tool_class"] == "read"
        assert payload["gated_by_default"] is False
        await broker.resolve(payload["permission_id"], {"action": "deny"})
        await task

    async def test_the_cli_copy_is_carried_through(self, broker, events):
        context = FakeContext(
            title="Run a shell command",
            display_name="Bash",
            description="lists files",
            decision_reason={"type": "other", "reason": "no rule matched"},
        )
        task = await ask(broker, context=context)
        payload = events.only("permissionRequest")
        assert payload["title"] == "Run a shell command"
        assert payload["display_name"] == "Bash"
        assert payload["description"] == "lists files"
        assert payload["decision_reason"]["reason"] == "no rule matched"
        await broker.resolve(payload["permission_id"], {"action": "deny"})
        await task

    async def test_pending_is_ordered_by_expiry(self, tmp_path, events):
        ticks = iter([100.0, 100.0, 100.0, 50.0, 50.0, 50.0] + [200.0] * 50)
        broker = PermissionBroker(
            tmp_path, broadcast=events, decision_timeout=5.0, clock=lambda: next(ticks)
        )
        first = asyncio.create_task(
            broker.can_use_tool("Bash", {"command": "a"}, FakeContext("toolu_a"))
        )
        await settle(lambda: bool(broker.pending()))
        second = asyncio.create_task(
            broker.can_use_tool("Bash", {"command": "b"}, FakeContext("toolu_b"))
        )
        await settle(lambda: len(broker.pending()) >= 2)

        queue = broker.pending()
        assert [entry["input"]["command"] for entry in queue] == ["b", "a"]
        await broker.cancel_all()
        await asyncio.gather(first, second)

    async def test_ids_are_never_reused(self, broker, events):
        seen = set()
        for index in range(5):
            task = await ask(broker, tool_input={"command": f"echo {index}"})
            pid = events.named("permissionRequest")[-1]["permission_id"]
            assert pid not in seen
            seen.add(pid)
            await broker.resolve(pid, {"action": "deny"})
            await task


# ---------------------------------------------------------------------------
# Deny-read rules
# ---------------------------------------------------------------------------


class TestDeniedReadFiles:
    def test_nothing_written_reads_as_nothing(self, tmp_path):
        assert read_denied_read_files(tmp_path) == []

    def test_a_round_trip(self, tmp_path):
        written = write_denied_read_files(tmp_path, ["secrets.env", ".ssh/**"])
        assert written == ["secrets.env", ".ssh/**"]
        assert read_denied_read_files(tmp_path) == ["secrets.env", ".ssh/**"]
        settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert settings["permissions"]["deny"] == ["Read(secrets.env)", "Read(.ssh/**)"]

    def test_other_rules_and_keys_survive(self, tmp_path):
        """This file is the user's, and the CLI writes to it too."""
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "permissions": {
                        "allow": ["Bash(ls:*)"],
                        "deny": ["Bash(rm:*)", "Read(old.env)"],
                    },
                }
            )
        )
        write_denied_read_files(tmp_path, ["new.env"])
        settings = json.loads(path.read_text())
        assert settings["model"] == "opus"
        assert settings["permissions"]["allow"] == ["Bash(ls:*)"]
        assert settings["permissions"]["deny"] == ["Bash(rm:*)", "Read(new.env)"]

    def test_clearing_removes_the_key_not_the_file(self, tmp_path):
        write_denied_read_files(tmp_path, ["a"])
        write_denied_read_files(tmp_path, [])
        settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "permissions" not in settings

    def test_duplicates_and_blanks_are_dropped(self, tmp_path):
        assert write_denied_read_files(tmp_path, ["a", " a ", "", "  ", "b"]) == ["a", "b"]

    def test_malformed_json_is_never_overwritten(self, tmp_path):
        """Deleting a user's rules to fix their typo is not a repair."""
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir()
        path.write_text("{ not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            write_denied_read_files(tmp_path, ["a"])
        assert path.read_text() == "{ not json"
        assert read_denied_read_files(tmp_path) == []

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        write_denied_read_files(tmp_path, ["a"])
        assert list((tmp_path / ".claude").glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------


class TestSummaries:
    def test_a_write_leads_with_the_path(self):
        assert summarise_request("Edit", {"file_path": "src/a.py"}, "write") == "Edit src/a.py"

    def test_an_exec_leads_with_the_command(self):
        assert summarise_request("Bash", {"command": "ls  -la"}, "exec") == "Bash: ls -la"

    def test_a_long_command_is_elided(self):
        summary = summarise_request("Bash", {"command": "x" * 300}, "exec")
        assert len(summary) <= 130
        assert summary.endswith("…")
