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
import os
import subprocess
import sys

import pytest

from aic_dc.agy import hook, registry
from aic_dc.agy.gate_server import AgyGateServer
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
        tmp_path, broadcast=recorder, localhost_available=lambda: True,
        config_dir=tmp_path / "cfg",
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

    def test_a_second_conversation_can_be_claimed_at_once(self, wired):
        """A session owns its own conversation *and* its subagents'.

        One claim per host was a containment hole, not a simplification:
        a subagent gets a conversation id of its own, the hook passes
        through everything unclaimed, and so every call a subagent made
        went ungated past a dialog that had approved only the spawn.
        """
        _recorder, _gate, server, config_dir = wired
        server.claim(OURS)
        server.claim("child-1")
        assert registry.lookup(OURS, config_dir=config_dir) is not None
        assert registry.lookup("child-1", config_dir=config_dir) is not None

    def test_releasing_a_subagent_leaves_the_session_claimed(self, wired):
        """A subagent settling must not un-gate the turn that spawned it."""
        _recorder, _gate, server, config_dir = wired
        server.claim(OURS)
        server.claim("child-1")
        server.release("child-1")
        assert registry.lookup("child-1", config_dir=config_dir) is None
        assert registry.lookup(OURS, config_dir=config_dir) is not None

    def test_stop_releases_every_claim(self, wired):
        """Including a subagent still running at teardown.

        A registry entry is a file that outlives the process, so a claim
        left behind makes this host intercept a *later* session of the
        user's own that resumes that conversation.
        """
        _recorder, _gate, server, config_dir = wired

        async def go():
            await server.start()
            server.claim(OURS)
            server.claim("child-1")
            await server.stop()

        asyncio.run(go())
        assert registry.lookup(OURS, config_dir=config_dir) is None
        assert registry.lookup("child-1", config_dir=config_dir) is None

    def test_releasing_something_never_claimed_is_quiet(self, wired):
        _recorder, _gate, server, _cfg = wired
        server.release("never-claimed")

    def test_a_blank_conversation_is_not_claimed(self, wired):
        """A claim on "" would be an entry no hook payload can match."""
        _recorder, _gate, server, config_dir = wired
        server.claim("")
        assert registry.lookup("", config_dir=config_dir) is None

    def test_a_stale_socket_file_does_not_stop_a_restart(self, wired):
        """A killed process leaves the file behind; bind would fail on it."""
        _recorder, _gate, server, _cfg = wired
        server.socket_path.parent.mkdir(parents=True, exist_ok=True)
        server.socket_path.write_text("stale", encoding="utf-8")

        async def go():
            await server.start()
            await server.stop()

        asyncio.run(go())

    def test_a_claim_records_the_agy_process(self, wired):
        """So a reader after this host is gone can still tell what is running.

        The pid is the caller's to supply — this class spawns nothing — and
        it is what separates an entry we abandoned from one whose agent
        outlived us (AG-R-14 residue 3).
        """
        _recorder, _gate, server, config_dir = wired
        server.claim(OURS, agy_pid=4242)
        entry = registry.lookup(OURS, config_dir=config_dir)
        assert entry["agy_pid"] == 4242
        assert entry["pid"] == os.getpid()

    def test_starting_reaps_a_dead_hosts_claims(self, wired):
        """The same cause as the stale socket above, one layer up.

        A corpse entry costs the *user's* own sessions, not ours: it keeps
        ``owns_anything`` true, so every unparseable payload from their own
        ``agy`` denies. Start is where a host exists again to sweep.
        """
        _recorder, _gate, server, config_dir = wired
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        registry.claim(
            "abandoned", "/tmp/gone.sock", config_dir=config_dir,
            pid=dead.pid, agy_pid=dead.pid,
        )
        live = registry.claim(
            "other-host", "/tmp/live.sock", config_dir=config_dir
        )

        async def go():
            await server.start()
            await server.stop()

        asyncio.run(go())
        assert not (registry.registry_dir(config_dir) / "abandoned.json").exists()
        # A second host's live claim shares the directory and survives.
        assert live.exists()


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
class TestStoppingOneSubagentRatherThanTheTurn:
    """``refuse_conversation`` — ⏹ on a row, not on the turn.

    The mechanism that already existed was ``refuse_all``, which is
    turn-wide: wiring ⏹ on a subagent to it would have stopped the parent
    as well as the subagent the user aimed at, and that is why
    ``subagent_stop`` stayed UNBUILT with its design written out rather
    than being wired to the nearest thing.

    What makes aiming possible is the fact AG-R-14 was raised *about*: a
    subagent runs in a conversation of its own, and the hook payload names
    it on every call.
    """

    CHILD = "c21acd4d-0000-4000-8000-000000000001"

    @pytest.mark.asyncio
    async def test_the_stopped_conversation_is_denied_with_its_reason(self, wired):
        _recorder, _gate, server, _config_dir = wired
        server.refuse_conversation(self.CHILD, "the user stopped this subagent")
        answer = await server.decide(payload(conversation=self.CHILD))
        assert answer["decision"] == "deny"
        assert answer["reason"] == "the user stopped this subagent"

    @pytest.mark.asyncio
    async def test_the_parent_is_untouched(self, wired):
        """The whole difference from ``refuse_all``, in one assertion.

        Answered by the dialog rather than by the refusal: the parent's
        call goes down the ordinary path, so it is *asked about*, which is
        what "the rest of the turn keeps running" means here.
        """
        recorder, gate, server, _config_dir = wired
        server.refuse_conversation(self.CHILD, "stopped")
        decide = asyncio.ensure_future(server.decide(payload(conversation=OURS)))
        await answer_next(gate, recorder, {"action": "allow"})
        assert (await decide)["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_a_stopped_subagent_is_never_put_to_the_user_again(self, wired):
        """Pressing stop is an answer, and re-asking would be the opposite.

        The same rule ``refuse_all`` follows. Asserted on the broadcast
        rather than on the verdict, because a dialog that appears and is
        then auto-answered would give the same verdict and a very
        different experience.
        """
        recorder, _gate, server, _config_dir = wired
        server.refuse_conversation(self.CHILD, "stopped")
        await server.decide(payload(conversation=self.CHILD))
        assert recorder.requests() == []

    @pytest.mark.asyncio
    async def test_a_turn_wide_stop_still_covers_everything(self, wired):
        """``refuse_all`` subsumes an aimed refusal, and is checked first."""
        _recorder, _gate, server, _config_dir = wired
        server.refuse_all("the user stopped this turn")
        answer = await server.decide(payload(conversation=OURS))
        assert answer["decision"] == "deny"
        assert answer["reason"] == "the user stopped this turn"

    @pytest.mark.asyncio
    async def test_resume_clears_the_aimed_refusals_too(self, wired):
        """A stop applies to the turn it was pressed during.

        This matters more than for the turn-wide refusal: a subagent's id
        is ``agy``'s own conversation id, so one left standing would still
        be aimed at that conversation if the user resumed it later — the
        interception ``registry.release`` exists to prevent, arriving
        through a different door.
        """
        recorder, gate, server, _config_dir = wired
        server.refuse_conversation(self.CHILD, "stopped")
        server.resume()
        assert server.is_refusing(self.CHILD) is False
        decide = asyncio.ensure_future(server.decide(payload(conversation=self.CHILD)))
        await answer_next(gate, recorder, {"action": "allow"})
        assert (await decide)["decision"] == "allow"

    def test_an_empty_id_aims_at_nothing(self, wired):
        """A missing row id must not become a refusal that matches "".

        ``decide`` looks the payload's conversation up in this map, and a
        payload with no conversation id reads as ``""`` — so an empty key
        would stop every call this host could not attribute.
        """
        _recorder, _gate, server, _config_dir = wired
        server.refuse_conversation("", "stopped")
        assert server.is_refusing("") is False
