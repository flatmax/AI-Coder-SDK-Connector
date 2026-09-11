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
from aic_dc.agy.gate_server import AgyGateServer, StaticPolicy
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


def invocation(conversation=OURS, num=2):
    """A ``PostInvocation`` payload. No tool, so nothing to put in a dialog."""
    return {
        "conversationId": conversation,
        "modelName": "gemini-3.8-flash-low",
        "workspacePaths": [],
        "invocationNum": num,
        "initialNumSteps": 4,
    }


def stopped(conversation=OURS, reason="NO_TOOL_CALL"):
    """A ``Stop`` payload, carrying the one field the stream never has."""
    return {
        "conversationId": conversation,
        "executionNum": 1,
        "terminationReason": reason,
        "error": "",
        "fullyIdle": True,
    }


class TestTheStopEndsTheLoop:
    """``decide_invocation`` — AG-19.

    ⏹ used to be a refusal the agent could read and then keep talking
    through. This is the same latch, asked at the one point ``agy`` will
    act on it mechanically. **No new state**: the assertions below are all
    about ``refuse_all`` and ``refuse_conversation`` driving both answers.
    """

    CHILD = "c21acd4d-0000-4000-8000-000000000001"

    def test_a_running_turn_is_left_alone(self, wired):
        _recorder, _gate, server, _cfg = wired
        assert server.decide_invocation(invocation()) == {}
        assert server.was_terminated(OURS) is False

    def test_a_stopped_turn_is_terminated(self, wired):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("the user stopped this turn")
        assert server.decide_invocation(invocation()) == {
            "terminationBehavior": "terminate"
        }
        assert server.was_terminated(OURS) is True

    def test_an_aimed_stop_ends_only_that_conversations_loop(self, wired):
        """The subagent case, and it closes ``refuse_conversation``'s first
        stated limit one level up.

        A subagent producing only prose asks permission for nothing and
        cannot be starved — that is written into ``refuse_conversation``
        as a known hole. Its *loop* still ends here, at the end of the
        invocation it is in, while the parent's keeps running.
        """
        _recorder, _gate, server, _cfg = wired
        server.refuse_conversation(self.CHILD, "the user stopped this subagent")
        assert server.decide_invocation(invocation(self.CHILD)) == {
            "terminationBehavior": "terminate"
        }
        assert server.decide_invocation(invocation(OURS)) == {}
        assert server.was_terminated(self.CHILD) is True
        assert server.was_terminated(OURS) is False

    def test_it_raises_no_dialog(self, wired):
        """There is no user in this question — they already answered it."""
        recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.decide_invocation(invocation())
        assert recorder.requests() == []

    def test_a_consultation_has_no_stop_to_read(self, wired):
        """A static-policy gate is never given one, so it never terminates."""
        _recorder, _gate, _server, config_dir = wired
        consultation = AgyGateServer(
            config_dir / "c.sock",
            policy=StaticPolicy.of(["FINISH"], "not allowed"),
            config_dir=config_dir,
        )
        assert consultation.decide_invocation(invocation()) == {}

    def test_resume_forgets_the_termination(self, wired):
        """A stop applies to the turn it was pressed during, and so does
        the record of having acted on it — otherwise the *next* turn's
        footer would report itself cancelled."""
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.decide_invocation(invocation())
        server.resume()
        assert server.was_terminated(OURS) is False
        assert server.decide_invocation(invocation()) == {}

    def test_an_unnamed_conversation_is_still_terminated(self, wired):
        """Recorded against nothing, but the loop still ends.

        The record is for the footer; the termination is the mechanism,
        and it must not depend on a field being present.
        """
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        assert server.decide_invocation({"invocationNum": 1}) == {
            "terminationBehavior": "terminate"
        }
        assert server.was_terminated("") is False


class TestTheStopEventIsRecordedAndNeverBlocked:
    """``note_stop`` — AG-R-16."""

    @pytest.mark.parametrize(
        "reason", ["NO_TOOL_CALL", "TERMINAL_CUSTOM_HOOK", "max_steps_exceeded", ""]
    )
    def test_the_answer_is_always_empty(self, wired, reason):
        _recorder, _gate, server, _cfg = wired
        assert server.note_stop(stopped(reason=reason)) == {}

    def test_our_own_termination_is_not_reported_as_a_stranger(self, wired, caplog):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.decide_invocation(invocation())
        with caplog.at_level("WARNING"):
            server.note_stop(stopped(reason="TERMINAL_CUSTOM_HOOK"))
        assert "does not own" not in caplog.text

    def test_a_hook_we_do_not_own_ending_our_loop_is_named(self, wired, caplog):
        """AG-R-16 arriving from the other direction.

        ``hooks.json`` merges named hooks per event, so a user's own
        ``Stop`` or a plugin's ``PostInvocation`` fires on this app's
        conversations exactly as this app's fires on theirs. Logged rather
        than acted on: it is a fact about their configuration.
        """
        _recorder, _gate, server, _cfg = wired
        with caplog.at_level("WARNING"):
            server.note_stop(stopped(reason="TERMINAL_CUSTOM_HOOK"))
        assert "does not own" in caplog.text
        assert OURS in caplog.text

    def test_an_ordinary_end_says_nothing(self, wired, caplog):
        _recorder, _gate, server, _cfg = wired
        with caplog.at_level("WARNING"):
            server.note_stop(stopped(reason="NO_TOOL_CALL"))
        assert caplog.text == ""


class TestTheEventRoutesTheAnswer:
    """The dispatch in ``_handle``, over a real socket.

    Everything above calls the three methods directly, which would keep
    passing if the router sent every payload to ``decide``. It would not
    fail quietly: a ``Stop`` answered by the gate would come back
    ``{"decision": "deny"}``, which ``agy`` reads as *permit the stop* —
    right by accident, on a turn nobody stopped.
    """

    def _over_the_socket(self, server, config_dir, call):
        async def go():
            await server.start()
            server.claim(OURS)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, call)
            await server.stop()
            return result

        return asyncio.run(go())

    def test_a_stopped_turn_terminates_hook_to_host_and_back(self, wired):
        _recorder, _gate, server, config_dir = wired
        server.refuse_all("the user stopped this turn")
        result = self._over_the_socket(
            server,
            config_dir,
            lambda: hook.decide_invocation(invocation(), config_dir=config_dir),
        )
        assert result == {"terminationBehavior": "terminate"}

    def test_a_running_turn_proceeds_hook_to_host_and_back(self, wired):
        _recorder, _gate, server, config_dir = wired
        result = self._over_the_socket(
            server,
            config_dir,
            lambda: hook.decide_invocation(invocation(), config_dir=config_dir),
        )
        assert result == {}

    def test_a_stop_is_recorded_without_a_decision_coming_back(self, wired, caplog):
        _recorder, _gate, server, config_dir = wired
        with caplog.at_level("WARNING"):
            result = self._over_the_socket(
                server,
                config_dir,
                lambda: hook.report_stop(
                    stopped(reason="TERMINAL_CUSTOM_HOOK"), config_dir=config_dir
                ),
            )
        assert result == {}
        # The side effect the round trip exists for: the host saw the
        # reason, and said so, having never been asked for a decision.
        assert "does not own" in caplog.text

    def test_a_tool_call_is_still_answered_by_the_gate(self, wired):
        """The router's other half: an unstamped payload is the gate.

        Which is also what an older hook process sends, so this is the
        version-skew case as well as the default.
        """
        recorder, gate, server, config_dir = wired

        async def go():
            await server.start()
            server.claim(OURS)
            loop = asyncio.get_running_loop()
            decision = loop.run_in_executor(
                None, lambda: hook.decide(payload(), config_dir=config_dir)
            )
            await answer_next(gate, recorder, {"action": "allow"})
            result = await decision
            await server.stop()
            return result

        assert asyncio.run(go())["decision"] == "allow"


class TestTheRefusalOutlivesTheTurnItStopped:
    """AG-R-16's surviving mitigation, pinned now that it is load-bearing.

    The risk named two defences against a third-party `Stop` hook reviving
    a stopped turn. The first — this app registering a `Stop` handler that
    always permits the stop — shipped on 2026-09-11 and was **refuted by
    measurement on 2026-09-12**: `scripts/probe_agy_stop_merge.py` shows a
    rival `continue` holding the turn open in either key order, and
    short-circuiting this app's handler entirely when it runs first.

    That leaves the second, which was already the behaviour and had no
    test: `resume` is called when a **new turn starts**, never when one
    ends, so the gate goes on refusing between turns. A revived loop
    therefore reaches the working tree through a gate that is still saying
    no — the damage is bounded to spend and prose, which is why AG-R-16 is
    moderate rather than critical.

    Written as an assertion about the *gate*, not about the session,
    because the property is that nothing clears the latch except the next
    turn beginning.
    """

    @pytest.mark.asyncio
    async def test_a_call_arriving_after_the_turn_ended_is_still_refused(self, wired):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("the user stopped this turn")
        # The turn ends. Nothing calls `resume`, because nothing does until
        # a new turn is sent.
        answer = await server.decide(payload(conversation=OURS))
        assert answer["decision"] == "deny"
        assert answer["reason"] == "the user stopped this turn"

    @pytest.mark.asyncio
    async def test_a_revived_loop_is_terminated_again(self, wired):
        """The same latch answers the revived loop's next `PostInvocation`.

        Whether `terminate` beats a concurrent `continue` is **not** known —
        it is the question the merge probe opened and did not close — so
        this asserts only what this app does, which is to keep saying stop.
        """
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("the user stopped this turn")
        server.decide_invocation(invocation())
        assert server.decide_invocation(invocation(num=9)) == {
            "terminationBehavior": "terminate"
        }

    def test_only_a_new_turn_clears_it(self, wired):
        """Stated as the whole rule, because the mitigation *is* the timing."""
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.note_stop(stopped(reason="TERMINAL_CUSTOM_HOOK"))
        assert server._refusal is not None
        server.resume()
        assert server._refusal is None


class TestARevivedLoopIsNamedInTheLog:
    """AG-R-16's harm, made legible rather than silent.

    Measured 2026-09-12 by `scripts/probe_agy_terminate_vs_continue.py`:
    with a third-party `Stop` hook answering `continue`, AG-19's
    `PostInvocation` terminate and that `continue` **ping-pong** — eight
    invocations in sixteen seconds, every one ended by this app and revived
    by the rival, bounded only by `--print-timeout`, which `AgySession`
    sets to 12h.

    So what this app shipped for the stop makes that scenario *cost more*
    than it did before: without the terminate the turn merely hangs, and
    with it the loop cycles. That is not an argument against the terminate —
    an unopposed one ends a loop in a single invocation, which is the whole
    of AG-19 — but it is a condition the log has to name, because from the
    outside it is a turn the user stopped that will not stop.

    Nothing is *acted* on here. Ending the process is an explicit user
    escalation (AG-19) and bounding the turn is a decision above this
    class; what this owes is to stop the condition being invisible.
    """

    def test_one_termination_is_the_ordinary_stop_and_says_nothing(self, wired, caplog):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        with caplog.at_level("WARNING"):
            assert server.decide_invocation(invocation()) == {
                "terminationBehavior": "terminate"
            }
        assert caplog.text == ""

    def test_the_second_one_says_the_stop_is_being_overridden(self, wired, caplog):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.decide_invocation(invocation(num=0))
        with caplog.at_level("WARNING"):
            server.decide_invocation(invocation(num=1))
        assert "revived" in caplog.text
        assert "AG-R-16" in caplog.text
        assert OURS in caplog.text

    def test_it_does_not_repeat_itself_for_every_cycle(self, wired, caplog):
        """Eight cycles is one fact, not eight — and the log is the one
        place a runaway turn stays legible."""
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        with caplog.at_level("WARNING"):
            for number in range(8):
                server.decide_invocation(invocation(num=number))
        assert caplog.text.count("revived") == 1

    def test_the_loop_is_still_ended_every_time(self, wired):
        """The warning is a report; the mechanism does not give up.

        Answering anything but `terminate` after the first cycle would hand
        the revived loop exactly what it wants.
        """
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        for number in range(5):
            assert server.decide_invocation(invocation(num=number)) == {
                "terminationBehavior": "terminate"
            }

    def test_a_new_turn_starts_the_count_again(self, wired, caplog):
        _recorder, _gate, server, _cfg = wired
        server.refuse_all("stopped")
        server.decide_invocation(invocation(num=0))
        server.resume()
        server.refuse_all("stopped again")
        with caplog.at_level("WARNING"):
            server.decide_invocation(invocation(num=0))
        assert caplog.text == ""
