"""Tests for aic_dc.antigravity.permissions — the AG-5 gate.

Three assertions are load-bearing.

**There is one ask path.** ``specs5/3-engine/permissions.md``'s invariants
are engine-agnostic, so the Antigravity gate must not own a second queue,
countdown or localhost rule. ``TestOneAskPath`` checks that it drives the
*shared* ``PermissionBroker`` — that a request appears in the same
``pending()`` list the Claude engine's dialog reads, and resolves through
the same ``resolve()``.

**The dialog gets a real diff.** That is what phase 2 measured and what
AG-5 calls the whole point of choosing this engine. The hook's arguments
arrive in CamelCase from the Go side, so a gate that did not translate
them would show an empty dialog for every edit — the failure is silent and
looks like "no diff available" rather than like a bug.

**A mutating tool is always asked about.** AG-R-11: a denied ``edit_file``
came back as ``sed -i`` through ``run_command`` on both probe runs, so the
seam is all of ``options.MUTATING_TOOLS`` and nothing may present one of
them as ungated.

Everything runs offline against a fake broadcast. No key, no harness, no
network, and — apart from the two registration tests — no SDK.
"""

from __future__ import annotations

import asyncio

import pytest

from aic_dc.antigravity import permissions as ag_permissions
from aic_dc.antigravity.options import MUTATING_TOOLS
from aic_dc.antigravity.permissions import (
    ALWAYS_ASK,
    TOOL_CLASSES,
    AntigravityPermissionGate,
    denormalise_args,
    normalise_args,
)
from aic_dc.claude_code.permissions import GATED_BY_DEFAULT


class FakeCall:
    """An Antigravity ``ToolCall``, with only what the gate reads."""

    def __init__(self, name, args, id="call-1"):
        self.name = name
        self.args = args
        self.id = id


class Recorder:
    """Captures broadcast events so a test can find the dialog."""

    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def requests(self):
        return [e for e in self.events if e.name == "permissionRequest"]


def gate(tmp_path, recorder, **kw) -> AntigravityPermissionGate:
    return AntigravityPermissionGate(
        tmp_path, broadcast=recorder, localhost_available=lambda: True, **kw
    )


async def ask(g, call, answer):
    """Run the hook and answer its dialog once it has been broadcast.

    Wrapped in a timeout because the failure mode without one is a hang,
    not an error: with a localhost client present there is deliberately no
    deadline (``permissions.md`` § Deadline), so a request nobody answers
    waits forever. That is right in production and useless in a test.
    """

    async def responder():
        for _ in range(2000):
            pending = g.broker.pending()
            if pending:
                await g.broker.resolve(
                    pending[0]["permission_id"], answer, resolved_by="127.0.0.1"
                )
                return
            await asyncio.sleep(0.001)
        raise AssertionError("no dialog was ever broadcast")

    task = asyncio.ensure_future(responder())
    try:
        async with asyncio.timeout(10):
            return await g.run(None, call)
    finally:
        await task


# ----------------------------------------------------------------------
# The first one that matters: one ask path
# ----------------------------------------------------------------------


class TestOneAskPath:
    def test_the_gate_uses_the_shared_broker(self, tmp_path):
        from aic_dc.claude_code.permissions import PermissionBroker

        assert isinstance(gate(tmp_path, Recorder()).broker, PermissionBroker)

    def test_a_request_reaches_the_same_dialog_queue(self, tmp_path):
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            return await ask(
                g,
                FakeCall("run_command", {"CommandLine": "ls"}),
                {"action": "allow"},
            )

        result = asyncio.run(go())
        assert result.allow
        assert len(recorder.requests()) == 1
        assert recorder.requests()[0].payload["tool_name"] == "run_command"

    def test_the_queue_is_empty_once_resolved(self, tmp_path):
        """Every request resolves exactly once, and leaves nothing behind."""
        g = gate(tmp_path, Recorder())

        async def go():
            await ask(g, FakeCall("run_command", {"CommandLine": "ls"}), {"action": "allow"})
            return g.broker.pending()

        assert asyncio.run(go()) == []

    def test_this_module_owns_no_second_broker(self):
        """The structural half of the claim, checked rather than described.

        A queue, a deadline or a presence check appearing here means the
        invariants have been re-derived rather than inherited, which is the
        way two engines end up disagreeing about whether a request was
        answered.
        """
        from pathlib import Path

        source = Path(ag_permissions.__file__).read_text(encoding="utf-8")
        for forbidden in ("_pending", "asyncio.wait", "expires_at =", "_localhost"):
            assert forbidden not in source, (
                f"{forbidden!r} appears in antigravity/permissions.py. The "
                "broker owns the queue, the countdown and the localhost "
                "rule; this module owns the callback's shape."
            )


# ----------------------------------------------------------------------
# The second: the dialog gets a real diff
# ----------------------------------------------------------------------


class TestTheDialogGetsADiff:
    def test_an_edit_renders_a_diff_from_the_hook_arguments(self, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(
                g,
                FakeCall(
                    "edit_file",
                    {
                        "TargetFile": str(target),
                        "TargetContent": "    return a + b",
                        "ReplacementContent": "    return a + b + 1",
                        "Instruction": "off by one",
                    },
                ),
                {"action": "deny", "reason": "no"},
            )

        asyncio.run(go())
        diff = recorder.requests()[0].payload["diff"]
        assert diff is not None, (
            "No diff reached the dialog for an edit_file. This is the "
            "capability AG-5 chose this engine for."
        )
        assert diff["path"] == "target.py"
        assert "return a + b + 1" in diff["proposed"]
        assert diff["additions"] and diff["deletions"]

    def test_a_new_file_carries_its_whole_content(self, tmp_path):
        """``create_file`` hands over ``CodeContent`` — the entire file."""
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(
                g,
                FakeCall(
                    "create_file",
                    {
                        "TargetFile": str(tmp_path / "new.py"),
                        "CodeContent": "print('hi')\n",
                    },
                ),
                {"action": "deny"},
            )

        asyncio.run(go())
        diff = recorder.requests()[0].payload["diff"]
        assert diff["is_new_file"]
        assert diff["proposed"] == "print('hi')\n"

    def test_a_command_renders_as_a_command(self, tmp_path):
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(
                g,
                FakeCall("run_command", {"CommandLine": "rm -rf /tmp/x"}),
                {"action": "deny"},
            )

        asyncio.run(go())
        payload = recorder.requests()[0].payload
        assert payload["tool_class"] == "exec"
        assert payload["command"] is not None

    def test_the_engines_own_tool_name_is_what_is_reported(self, tmp_path):
        """Not a Claude equivalent.

        Translating ``edit_file`` to ``Edit`` for the dialog would tell the
        user their agent called a tool it does not have, and is the kind of
        engine-name leak AG-R-4 exists to prevent. Only the *fields* are
        renamed.
        """
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(
                g,
                FakeCall("edit_file", {"TargetFile": str(tmp_path / "a.py")}),
                {"action": "deny"},
            )

        asyncio.run(go())
        assert recorder.requests()[0].payload["tool_name"] == "edit_file"

    def test_the_raw_arguments_survive_beside_the_translation(self, tmp_path):
        """The user sees what the agent actually asked for."""
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(
                g,
                FakeCall("edit_file", {"TargetFile": str(tmp_path / "a.py")}),
                {"action": "deny"},
            )

        asyncio.run(go())
        shown = recorder.requests()[0].payload["input"]
        assert "TargetFile" in shown and "file_path" in shown


class TestArgumentTranslation:
    def test_aliases_are_added_not_substituted(self):
        out = normalise_args("edit_file", {"TargetFile": "a.py"})
        assert out == {"TargetFile": "a.py", "file_path": "a.py"}

    def test_an_unknown_tool_passes_through(self):
        """Degrades to "full input shown", which is legible."""
        assert normalise_args("tool_from_the_future", {"X": 1}) == {"X": 1}

    def test_an_existing_key_is_not_overwritten(self):
        out = normalise_args("edit_file", {"TargetFile": "a", "file_path": "b"})
        assert out["file_path"] == "b"

    def test_an_amendment_goes_back_in_the_engines_spelling(self):
        """The amend path only works if the Go side can read the result."""
        assert denormalise_args("edit_file", {"new_string": "z"}) == {
            "ReplacementContent": "z"
        }

    def test_an_unknown_amended_key_passes_through(self):
        assert denormalise_args("edit_file", {"Whatever": 1}) == {"Whatever": 1}


# ----------------------------------------------------------------------
# The third: a mutating tool is always asked about
# ----------------------------------------------------------------------


class TestNothingMutatingIsUngated:
    def test_the_seam_is_read_from_options_not_restated(self):
        """One seam, so the two modules cannot drift."""
        assert ALWAYS_ASK is MUTATING_TOOLS

    def test_every_mutating_tool_is_classified(self):
        assert set(ALWAYS_ASK) <= set(TOOL_CLASSES)

    @pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
    def test_every_mutating_tool_is_gated_by_default(self, tmp_path, tool):
        """Including ``start_subagent``, whose class says otherwise.

        ``delegate`` is ungated by default — Claude's ``Task`` is, because
        the child's own calls are gated as they happen. That reasoning
        holds, and the flag is still forced true here, because the class
        shapes the dialog's wording and this decides whether a dialog can
        be presented as routine. The two must not be able to disagree.
        """
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(g, FakeCall(tool, {}), {"action": "deny"})

        asyncio.run(go())
        assert recorder.requests()[0].payload["gated_by_default"] is True

    def test_delegate_would_otherwise_have_been_ungated(self):
        """Pins the fact the override exists for."""
        assert GATED_BY_DEFAULT["delegate"] is False
        assert TOOL_CLASSES["start_subagent"] == "delegate"

    def test_an_unknown_tool_gets_the_most_cautious_dialog(self, tmp_path):
        """The direction that survives an SDK release adding a tool."""
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            await ask(g, FakeCall("tool_from_the_future", {}), {"action": "deny"})

        asyncio.run(go())
        payload = recorder.requests()[0].payload
        assert payload["tool_class"] == "exec"
        assert payload["gated_by_default"] is True


# ----------------------------------------------------------------------
# The callback's shape — the only thing that differs between engines
# ----------------------------------------------------------------------


class TestResultConversion:
    def test_a_denial_carries_the_reason_the_model_reads(self, tmp_path):
        g = gate(tmp_path, Recorder())

        async def go():
            return await ask(
                g,
                FakeCall("run_command", {"CommandLine": "ls"}),
                {"action": "deny", "reason": "not on my machine"},
            )

        result = asyncio.run(go())
        assert result.allow is False
        assert "not on my machine" in result.message

    def test_an_allow_with_no_amendment_is_a_bare_allow(self, tmp_path):
        g = gate(tmp_path, Recorder())

        async def go():
            return await ask(
                g, FakeCall("run_command", {"CommandLine": "ls"}), {"action": "allow"}
            )

        result = asyncio.run(go())
        assert result.allow is True
        assert not result.modified_args

    def test_an_amended_input_becomes_modified_args(self, tmp_path):
        """AG-5's reason for taking the raw hook over ``policy.ask_user``.

        ``ask_user`` returns a bare bool, so this capability would have
        been given away permanently and for nothing.
        """
        g = gate(tmp_path, Recorder())

        async def go():
            return await ask(
                g,
                FakeCall("edit_file", {"TargetFile": str(tmp_path / "a.py")}),
                {
                    "action": "allow",
                    "updated_input": {"new_string": "safer"},
                },
            )

        result = asyncio.run(go())
        assert result.allow is True
        assert result.modified_args == {"ReplacementContent": "safer"}

    def test_always_allow_degrades_to_allow_once_by_construction(self, tmp_path):
        """AG-5's one genuine loss, and it is closed at the right end.

        Antigravity has no ``updated_permissions`` at any layer, so a rule
        cannot be persisted. Rather than accepting an always-allow and
        quietly dropping the rule, the payload offers **no**
        ``suggested_rules`` at all — so the broker's own normalisation has
        nothing to bind an always-allow to and degrades it to allow-once,
        logging that it did.

        That ordering matters: an offer the engine cannot keep is worse
        than no offer, because the user believes they will not be asked
        again. This asserts the offer is absent, which is the thing that
        makes the degradation honest rather than a silent discard.
        """
        recorder = Recorder()
        g = gate(tmp_path, recorder)

        async def go():
            return await ask(
                g,
                FakeCall("run_command", {"CommandLine": "ls"}),
                {"action": "allow_always"},
            )

        result = asyncio.run(go())
        assert result.allow is True
        assert recorder.requests()[0].payload["suggested_rules"] == []
        assert recorder.requests()[0].payload["suggested_mode"] is None

    def test_a_rule_that_somehow_arrived_is_warned_about(self, tmp_path, caplog):
        """Defence in depth for a path the payload should make unreachable.

        Tested directly rather than through the dialog, because getting
        here through the dialog would mean the assertion above had already
        failed. If a future change starts offering rules, this is what
        stops the user's intent being discarded in silence.
        """
        g = gate(tmp_path, Recorder())

        class AllowWithRule:
            message = None
            updated_permissions = [object()]
            updated_input = None

        with caplog.at_level("WARNING"):
            result = g._to_hook_result("run_command", AllowWithRule())
        assert result.allow is True
        assert any("updated_permissions" in r.message for r in caplog.records)

    def test_a_broken_gate_denies_rather_than_raising(self, tmp_path):
        """A raise reaches the model as "the tool broke", not as a refusal."""
        g = gate(tmp_path, Recorder())

        async def explode(*a, **k):
            raise RuntimeError("boom")

        g.broker.can_use_tool = explode

        result = asyncio.run(g.run(None, FakeCall("edit_file", {})))
        assert result.allow is False
        assert "AIC-DC fault" in result.message


# ----------------------------------------------------------------------
# Registration, where the SDK is present
# ----------------------------------------------------------------------


class TestRegistration:
    def test_as_hook_passes_the_runners_isinstance_check(self, tmp_path):
        """``HookRunner.register_hook`` raises ``ValueError`` otherwise."""
        hooks = pytest.importorskip("google.antigravity.hooks.hooks")
        hook = gate(tmp_path, Recorder()).as_hook()
        assert isinstance(hook, hooks.PreToolCallDecideHook)

    def test_the_module_imports_without_the_sdk_at_module_scope(self):
        """AG-R-10: a base install is a one-engine install.

        The gate has to subclass an SDK class to be registrable, which is
        why the subclass is built in a factory rather than at import time.
        """
        from pathlib import Path

        source = Path(ag_permissions.__file__).read_text(encoding="utf-8")
        head = source.split("logger = logging.getLogger")[0]
        assert "google.antigravity" not in head

    def test_a_gated_config_can_be_built_from_it(self, tmp_path):
        """The end the gate exists for: writes enabled because it is there."""
        pytest.importorskip("google.antigravity")
        from aic_dc.antigravity import options
        from aic_dc.antigravity.credentials import GEMINI_API, Credentials

        kwargs = options.build_config_kwargs(
            repo_root=tmp_path,
            credentials=Credentials(mode=GEMINI_API, api_key="k", source="t"),
            decide_hook=gate(tmp_path, Recorder()).as_hook(),
        )
        assert MUTATING_TOOLS <= set(kwargs["capabilities"]["enabled_tools"])
        assert options.build_config(**kwargs).hooks
