"""Tests for the host end of the ``agy`` gate.

The claim worth checking here is **one ask path across a third transport**.
``specs5/3-engine/permissions.md``'s invariants are engine-agnostic, and
the way they get broken is a new transport growing its own queue because
the shared one did not quite fit. So the assertions are about the request
landing in the *same* ``pending()`` list, resolving through the *same*
``resolve()``, and carrying the same diff — not about this module's
internals.

The other half is the round trip. Everything in ``test_agy_gate.py``
injects ``ask``, which would keep passing if the wire format were wrong in
both directions at once; here a real ``hook.decide`` talks to a real
server over a real socket, which is the only test that would catch that.

Offline. No ``agy``, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from aic_dc.antigravity.agy import hook, registry
from aic_dc.antigravity.agy.gate_server import AgyGateServer
from aic_dc.antigravity.permissions import AntigravityPermissionGate

OURS = "cd4edb7f-6de3-468f-9815-e76b310a920a"


class Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)

    def requests(self):
        return [e for e in self.events if e.name == "permissionRequest"]


def payload(tool="replace_file_content", args=None, conversation=OURS, step=4):
    return {
        "conversationId": conversation,
        "modelName": "gemini-3.8-flash-low",
        "stepIdx": step,
        "workspacePaths": [],
        "toolCall": {"name": tool, "args": args if args is not None else {}},
    }


async def answer_next(server_gate, recorder, decision, *, timeout=10.0):
    """Wait for the dialog to be broadcast, then answer it.

    The request deliberately has no deadline while a localhost client is
    present (``permissions.md`` § Deadline), so an unanswered one waits
    forever. The timeout is the assertion that one was raised at all.
    """
    async with asyncio.timeout(timeout):
        while not recorder.requests():
            await asyncio.sleep(0.001)
        pending = server_gate.broker.pending()[0]
        await server_gate.broker.resolve(
            pending["permission_id"], decision, resolved_by="127.0.0.1"
        )


@pytest.fixture
def wired(tmp_path):
    """A gate, a server on a real socket, and a claimed conversation."""
    recorder = Recorder()
    gate = AntigravityPermissionGate(
        tmp_path, broadcast=recorder, localhost_available=lambda: True
    )
    config_dir = tmp_path / "cfg"
    server = AgyGateServer(
        tmp_path / "gate.sock", gate=gate, config_dir=config_dir
    )
    return recorder, gate, server, config_dir


class TestOneAskPathAcrossAThirdTransport:
    def test_a_request_reaches_the_shared_queue(self, wired):
        recorder, gate, server, _cfg = wired

        async def go():
            await server.start()
            task = asyncio.ensure_future(
                server.decide(payload("run_command", {"CommandLine": "ls"}))
            )
            await answer_next(gate, recorder, {"action": "allow"})
            result = await task
            await server.stop()
            return result

        assert asyncio.run(go()) == {"decision": "allow"}
        assert len(recorder.requests()) == 1
        # The name reported is the one agy actually called, not a Claude
        # equivalent — the transcript has to match what the agent did.
        assert recorder.requests()[0].payload["tool_name"] == "run_command"

    def test_the_queue_is_empty_afterwards(self, wired):
        recorder, gate, server, _cfg = wired

        async def go():
            await server.start()
            task = asyncio.ensure_future(server.decide(payload("run_command", {})))
            await answer_next(gate, recorder, {"action": "allow"})
            await task
            pending = gate.broker.pending()
            await server.stop()
            return pending

        assert asyncio.run(go()) == []

    def test_an_agy_edit_carries_a_diff_into_the_dialog(self, wired, tmp_path):
        """The capability AG-5 chose this whole design for.

        Keyed on ``replace_file_content`` — agy's name — which only works
        because ``agy/tools.py``'s vocabulary is merged into the shared
        tables. Without it this renders as a shell command with no diff.
        """
        recorder, gate, server, _cfg = wired
        target = tmp_path / "calc.py"
        target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        async def go():
            await server.start()
            task = asyncio.ensure_future(
                server.decide(
                    payload(
                        "replace_file_content",
                        {
                            "TargetFile": str(target),
                            "TargetContent": "    return a + b",
                            "ReplacementContent": "    return a * b",
                            "Instruction": "multiply instead",
                        },
                    )
                )
            )
            await answer_next(gate, recorder, {"action": "deny", "reason": "no"})
            result = await task
            await server.stop()
            return result

        result = asyncio.run(go())
        assert result["decision"] == "deny"
        request = recorder.requests()[0].payload
        assert request["tool_class"] == "write", "an edit is not a shell command"
        diff = request["diff"]
        assert diff is not None, "no diff reached the dialog for an agy edit"
        assert diff["path"] == "calc.py"
        assert "return a * b" in diff["proposed"]


class TestTheAmendPathSurvives:
    def test_an_amended_call_goes_back_in_agys_spelling(self, wired):
        """``overwrite`` is read by the Go side, so it must be CamelCase.

        Sending the dialog's ``command`` would merge a key agy does not
        know beside the one it does, leaving the original in place — an
        amend that silently does nothing.
        """
        recorder, gate, server, _cfg = wired

        async def go():
            await server.start()
            task = asyncio.ensure_future(
                server.decide(payload("run_command", {"CommandLine": "rm -rf /tmp/x"}))
            )
            await answer_next(
                gate,
                recorder,
                {"action": "allow", "updated_input": {"command": "ls /tmp/x"}},
            )
            result = await task
            await server.stop()
            return result

        result = asyncio.run(go())
        assert result["decision"] == "allow"
        assert result["overwrite"]["CommandLine"] == "ls /tmp/x"
        assert "command" not in result["overwrite"]


class TestItAlwaysAnswers:
    """The hook is blocked on this socket, so silence is the worst outcome."""

    @pytest.mark.parametrize(
        "bad",
        [
            {"conversationId": OURS},
            {"conversationId": OURS, "toolCall": None},
            {"conversationId": OURS, "toolCall": {}},
            {"conversationId": OURS, "toolCall": {"name": ""}},
        ],
    )
    def test_a_call_it_cannot_read_is_refused(self, wired, bad):
        recorder, _gate, server, _cfg = wired

        async def go():
            await server.start()
            result = await server.decide(bad)
            await server.stop()
            return result

        result = asyncio.run(go())
        assert result["decision"] == "deny"
        assert "not a refusal by the user" in result["reason"]
        assert recorder.requests() == [], "an unreadable call is not a dialog"

    def test_junk_on_the_socket_still_gets_a_reply(self, wired):
        """Rather than leaving the hook on its own hour-long deadline."""
        _recorder, _gate, server, _cfg = wired

        async def go():
            await server.start()
            reader, writer = await asyncio.open_unix_connection(
                str(server.socket_path)
            )
            writer.write(b"not json at all\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            writer.close()
            await server.stop()
            return line

        import json as _json

        assert _json.loads(asyncio.run(go()))["decision"] == "deny"


class TestOwnershipLifecycle:
    def test_claim_makes_the_hook_recognise_us(self, wired):
        _recorder, _gate, server, config_dir = wired
        assert hook.decide(payload(), config_dir=config_dir) == hook.ALLOW
        server.claim(OURS)
        entry = registry.lookup(OURS, config_dir=config_dir)
        assert entry is not None
        assert entry["socket"] == str(server.socket_path)

    def test_stop_releases_before_it_closes(self, wired):
        """Order matters, and the wrong order refuses a racing tool call.

        While the registry entry stands the hook denies anything it cannot
        get an answer for, so closing the socket first would turn a call
        arriving during shutdown into a refusal. Releasing first makes it
        pass through as unowned, which is what it is.
        """
        _recorder, _gate, server, config_dir = wired

        async def go():
            await server.start()
            server.claim(OURS)
            await server.stop()

        asyncio.run(go())
        assert registry.lookup(OURS, config_dir=config_dir) is None
        # And the hook is back to treating it as somebody else's.
        assert hook.decide(payload(), config_dir=config_dir) == hook.ALLOW

    def test_a_stale_socket_file_does_not_stop_a_restart(self, wired):
        """A killed process leaves the file behind; bind would fail on it."""
        _recorder, _gate, server, _cfg = wired
        server.socket_path.parent.mkdir(parents=True, exist_ok=True)
        server.socket_path.write_text("stale", encoding="utf-8")

        async def go():
            await server.start()
            await server.stop()

        asyncio.run(go())


class TestTheRoundTrip:
    """A real ``hook.decide`` over a real socket into the real broker.

    Every other test here calls ``server.decide`` directly, and every test
    in ``test_agy_gate.py`` injects ``ask``. Both would keep passing if the
    two sides disagreed about the wire format, so this is the one that
    would catch it.
    """

    def test_a_denial_travels_hook_to_dialog_and_back(self, wired):
        recorder, gate, server, config_dir = wired

        async def go():
            await server.start()
            server.claim(OURS)
            loop = asyncio.get_running_loop()
            # The hook is synchronous and blocking by design — it is a
            # separate process in production — so it runs off the loop.
            decision = loop.run_in_executor(
                None, lambda: hook.decide(payload(), config_dir=config_dir)
            )
            await answer_next(
                gate, recorder, {"action": "deny", "reason": "the user declined"}
            )
            result = await decision
            await server.stop()
            return result

        result = asyncio.run(go())
        assert result == {"decision": "deny", "reason": "the user declined"}
        assert len(recorder.requests()) == 1

    def test_a_stranger_never_reaches_the_dialog(self, wired):
        """The property that makes a global hook shippable."""
        recorder, _gate, server, config_dir = wired

        async def go():
            await server.start()
            server.claim(OURS)
            result = hook.decide(
                payload(conversation="11111111-0000-0000-0000-000000000000"),
                config_dir=config_dir,
            )
            await server.stop()
            return result

        assert asyncio.run(go()) == hook.ALLOW
        assert recorder.requests() == []
