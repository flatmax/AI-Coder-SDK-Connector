"""Tests for the ``agy`` session — spawn, claim, pump, stop.

Driven by a **fake ``agy``** that is a real subprocess speaking the real
protocol: it reads ``{"event":"user",…}`` from stdin and writes the frame
shapes captured on 2026-09-03. A fake that answered in-process would not
exercise the two things most likely to be wrong here — the handshake's
*order*, and the pipes.

The property that carries the file is the **claim window**. A conversation
id is unknown before ``init`` and a tool call cannot precede the first
prompt, so ownership must be taken between those two events. Claim late
and the first tool call is waved through as a stranger's; that is the
whole gate failing, silently, on the first call of every session.

Offline. No real ``agy``, no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from aic_dc.agy import registry
from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.agy.session import (
    AgyNotInstalledError,
    AgySession,
    TurnInProgressError,
)
from aic_dc.agy.steps import AgyTranslator
from aic_dc.antigravity.permissions import AntigravityPermissionGate

CONV = "b1d377c5-ef66-4d58-a7ca-5aee75acc853"

FAKE_AGY = textwrap.dedent(
    '''
    import json, sys, os
    conv = "{conv}"
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    # Chatter on stderr, which a real agy also produces and which will
    # block the child if nobody drains it.
    sys.stderr.write("agy: starting\\n" * 200); sys.stderr.flush()
    emit({{"event": "init", "conversation_id": conv,
          "init": {{"cwd": os.getcwd(), "tools": ["view_file"]}}}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        json.loads(line)
        emit({{"event": "step_update", "step_update": {{
            "conversation_id": conv, "step_index": 0,
            "state": "DONE", "step_type": "user_input"}}}})
        emit({{"event": "step_update", "step_update": {{
            "conversation_id": conv, "step_index": 1, "state": "ACTIVE",
            "step_type": "agent_response", "text_delta": "Reading "}}}})
        emit({{"event": "step_update", "step_update": {{
            "conversation_id": conv, "step_index": 1, "state": "DONE",
            "step_type": "agent_response", "text_delta": "the file."}}}})
        emit({{"event": "result", "result": {{
            "conversation_id": conv, "status": "SUCCESS",
            "response": "Reading the file.", "num_turns": 1,
            "usage": {{"total_tokens": 1234}}}}}})
    '''
).format(conv=CONV)


@pytest.fixture
def wired(tmp_path):
    """A gate server and a session pointed at a fake ``agy``."""
    fake = tmp_path / "fake_agy.py"
    fake.write_text(FAKE_AGY, encoding="utf-8")
    launcher = tmp_path / "agy"
    launcher.write_text(
        f"#!/bin/sh\nexec {sys.executable} {fake}\n", encoding="utf-8"
    )
    launcher.chmod(0o755)

    events: list = []

    async def broadcast(event):
        events.append(event)

    gate = AntigravityPermissionGate(
        tmp_path, broadcast=broadcast, localhost_available=lambda: True,
        config_dir=tmp_path / "cfg",
    )
    config_dir = tmp_path / "cfg"
    server = AgyGateServer(tmp_path / "g.sock", gate=gate, config_dir=config_dir)
    session = AgySession(
        tmp_path, gate=server, executable=str(launcher)
    )
    return session, server, config_dir, events


def names(events):
    return [e.name for e in events]


class TestTheHandshake:
    def test_start_returns_the_conversation_id_from_init(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            cid = await session.start()
            await session.close()
            return cid

        assert asyncio.run(go()) == CONV

    def test_the_conversation_is_claimed_before_any_prompt(self, wired):
        """The claim window, and the reason this file exists.

        A tool call cannot precede the first prompt, so ownership taken by
        the time ``start()`` returns is ownership taken in time. Claiming
        after the prompt would wave the first tool call through as a
        stranger's — the gate failing silently on the first call of every
        session.
        """
        session, _server, config_dir, _events = wired

        async def go():
            await session.start()
            claimed = registry.lookup(CONV, config_dir=config_dir)
            await session.close()
            return claimed

        claimed = asyncio.run(go())
        assert claimed is not None
        assert claimed["socket"].endswith("g.sock")

    def test_the_claim_records_the_agy_process_that_was_spawned(self, wired):
        """What makes the claim outlive this host without lying about it.

        A registry entry is a file, and a killed host leaves it behind. The
        host pid alone cannot say whether anything is still running, because
        ``agy`` is our *child* and a child survives a killed parent — so the
        child's pid goes in the entry too, and only both being gone makes it
        a corpse (AG-R-14 residue 3).
        """
        session, _server, config_dir, _events = wired

        async def go():
            await session.start()
            claimed = registry.lookup(CONV, config_dir=config_dir)
            spawned = session._proc.pid
            await session.close()
            return claimed, spawned

        claimed, spawned = asyncio.run(go())
        assert claimed["agy_pid"] == spawned
        assert claimed["pid"] == os.getpid()
        assert registry.entry_is_live(claimed) is True

    def test_close_releases_the_claim(self, wired):
        session, _server, config_dir, _events = wired

        async def go():
            await session.start()
            await session.close()

        asyncio.run(go())
        assert registry.lookup(CONV, config_dir=config_dir) is None

    def test_a_missing_binary_is_a_named_failure(self, tmp_path):
        server = AgyGateServer(
            tmp_path / "g.sock",
            gate=AntigravityPermissionGate(
                tmp_path, broadcast=lambda e: None, localhost_available=lambda: True,
                config_dir=tmp_path / "cfg",
            ),
            config_dir=tmp_path / "cfg",
        )
        session = AgySession(tmp_path, gate=server, executable="agy-not-installed")

        async def go():
            with pytest.raises(AgyNotInstalledError, match="not on PATH"):
                await session.start()
            await session.close()

        asyncio.run(go())

    def test_start_is_idempotent(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            first = await session.start()
            second = await session.start()
            await session.close()
            return first, second

        first, second = asyncio.run(go())
        assert first == second == CONV


class TestATurn:
    def test_the_stream_becomes_browser_events(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            t = AgyTranslator("r1")
            out = [e async for e in session.stream_turn("read it", translator=t)]
            await session.close()
            return out

        events = asyncio.run(go())
        assert names(events) == [
            "streamChunk",
            "streamChunk",
            "turnUsage",
            "streamComplete",
        ]
        # Deltas accumulated, so the browser's replace-by-id is correct.
        assert events[1].payload["content"] == "Reading the file."
        assert events[-1].payload["response"] == "Reading the file."
        assert events[-1].payload["usage"]["total_tokens"] == 1234

    def test_the_context_is_held_across_turns(self, wired):
        """One process, not one per turn — the point of bidirectional mode."""
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            for _ in range(2):
                t = AgyTranslator("r")
                async for _e in session.stream_turn("go", translator=t):
                    pass
            alive = session.started
            await session.close()
            return alive

        assert asyncio.run(go()) is True

    def test_a_second_concurrent_turn_is_refused(self, wired):
        """Refused rather than queued, so the caller can say so at once."""
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            first = session.stream_turn("a", translator=AgyTranslator("r1"))
            await first.__anext__()  # in flight
            with pytest.raises(TurnInProgressError):
                second = session.stream_turn("b", translator=AgyTranslator("r2"))
                await second.__anext__()
            await first.aclose()
            await session.close()

        asyncio.run(go())

    def test_a_turn_before_start_is_an_error_not_a_hang(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            with pytest.raises(RuntimeError, match="not been started"):
                gen = session.stream_turn("x", translator=AgyTranslator("r"))
                await gen.__anext__()

        asyncio.run(go())

    def test_a_process_that_dies_mid_turn_still_closes_the_turn(self, wired):
        """With no RPC reply to carry a failure, the stream is the only channel.

        A turn that emitted no terminal event would leave the browser
        spinning forever — the lesson the SDK transport learned when its
        error path skipped ``stream_complete``.
        """
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            session._proc.kill()
            await asyncio.sleep(0.1)
            t = AgyTranslator("r1")
            return [e async for e in session.stream_turn("x", translator=t)]

        events = asyncio.run(go())
        assert "streamComplete" in names(events)


class TestStopStarvesTheTurn:
    """There is no halt frame, so ⏹ refuses tools instead.

    ``sdk-surface.md``: the input protocol accepts one event, ``user``.
    The SDK transport has ``conversation.cancel()``; this has the gate.
    """

    def test_cancel_makes_the_gate_refuse_without_asking(self, wired):
        session, server, _cfg, events = wired

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            await gen.__anext__()
            await session.cancel()
            decision = await server.decide(
                {
                    "conversationId": CONV,
                    "stepIdx": 3,
                    "toolCall": {"name": "run_command", "args": {}},
                }
            )
            await gen.aclose()
            await session.close()
            return decision

        decision = asyncio.run(go())
        assert decision["decision"] == "deny"
        assert "stopped this turn" in decision["reason"]
        # Not a dialog: the user answered by pressing stop.
        assert [e for e in events if e.name == "permissionRequest"] == []

    def test_the_reason_tells_the_agent_not_to_reroute(self, wired):
        """Otherwise a denial invites AG-R-11's try-another-way."""
        _session, server, _cfg, _events = wired
        server.refuse_all("The user stopped this turn in AIC-DC. Do not continue.")

        async def go():
            return await server.decide(
                {"conversationId": CONV, "toolCall": {"name": "run_command"}}
            )

        assert "Do not continue" in asyncio.run(go())["reason"]

    def test_a_stop_does_not_carry_into_the_next_turn(self, wired):
        """⏹ is an action, not a mode.

        Carrying it forward would leave the next turn refusing everything
        for no reason the user can see.
        """
        session, server, _cfg, _events = wired

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            await gen.__anext__()
            await session.cancel()
            await gen.aclose()
            # A new turn resumes the gate.
            gen2 = session.stream_turn("y", translator=AgyTranslator("r2"))
            await gen2.__anext__()
            decision = await server.decide(
                {
                    "conversationId": CONV,
                    "stepIdx": 1,
                    "toolCall": {"name": "view_file", "args": {}},
                }
            )
            await gen2.aclose()
            await session.close()
            return decision

        # `view_file` is read-class, so with the stop lifted it is allowed
        # without a dialog rather than refused by a stale cancel.
        assert asyncio.run(go())["decision"] == "allow"

    def test_cancelling_with_no_turn_running_does_nothing(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            await session.cancel()
            await session.close()

        asyncio.run(go())


class TestItSurvivesAChattySubprocess:
    def test_a_full_stderr_pipe_does_not_deadlock_the_turn(self, wired):
        """The fake writes 200 stderr lines before its first frame.

        A child blocks when its stderr pipe fills and nobody reads, and the
        symptom is a session that starts and then hangs on the first turn —
        which reads as the model being slow.
        """
        session, _server, _cfg, _events = wired

        async def go():
            async with asyncio.timeout(30):
                await session.start()
                t = AgyTranslator("r1")
                out = [e async for e in session.stream_turn("x", translator=t)]
                await session.close()
                return out

        assert "streamComplete" in names(asyncio.run(go()))


class TestTheAgentIsInTheRepository:
    """``--add-dir``, and why ``cwd=`` was never enough.

    Measured 2026-09-05 with everything else held constant — same parent
    directory, same ``git init``, same process cwd, one flag different::

        with --add-dir : pwd -> /tmp/temp/wstest
                         git rev-parse -> /tmp/temp/wstest
        without        : pwd -> ~/.gemini/antigravity-cli/scratch
                         git rev-parse -> fatal: not a git repository

    So without it the agent is not in the user's repository at all, and
    `agy`'s system prompt tells it that when it needs somewhere to write,
    its scratch directory is the place. Asked to "create a helloworld
    script" it did precisely that and reported success with a `file://`
    link — the symptom recorded as
    [AG-R-3](../specs5/plan-ag/risks.md#ag-r-3) for six days, under four
    causes that were each disproven.
    """

    def test_the_repo_is_added_to_the_workspace(self, tmp_path):
        from aic_dc.agy.session import AgySession

        argv = AgySession(tmp_path, gate=object())._argv()
        assert "--add-dir" in argv
        assert argv[argv.index("--add-dir") + 1] == str(tmp_path)

    def test_exactly_one_directory(self, tmp_path):
        """AG-10: one repo root, one working tree.

        The flag is repeatable and must not be repeated — the diff viewer
        and the file tree both resolve paths against a single root, and a
        second would hand them paths they cannot place.
        """
        from aic_dc.agy.session import AgySession

        argv = AgySession(tmp_path, gate=object())._argv()
        assert argv.count("--add-dir") == 1

    def test_it_is_the_resolved_repo_root(self, tmp_path):
        """A relative path would be resolved against `agy`'s cwd, not ours."""
        from aic_dc.agy.session import AgySession

        argv = AgySession(str(tmp_path), gate=object())._argv()
        target = argv[argv.index("--add-dir") + 1]
        assert Path(target).is_absolute()

    def test_the_process_cwd_is_the_repo_too(self, tmp_path):
        """Both, not either.

        `cwd=` is what makes a relative path the user types resolve, and
        `--add-dir` is what puts the *agent* there. Setting only the first
        is what shipped, and it looked correct in every log — `agy` even
        reported `workspaceDirs=[/tmp/temp]` while running its tools
        somewhere else entirely.
        """
        import inspect

        from aic_dc.agy import session as mod

        source = inspect.getsource(mod.AgySession.start)
        assert "cwd=str(self._repo_root)" in source


class TestASubagentIsGatedToo:
    """The containment fix of 2026-09-09.

    ``agy`` runs under ``--dangerously-skip-permissions``, so this host's
    gate is the only thing between the model and the tree — and the hook
    routes to it by conversation id, passing through everything unclaimed.
    A subagent gets a conversation of its own, so until the session
    claimed it too, a delegation was a route around the dialog: the user
    approved the *spawn*, and every call the subagent then made ran
    unreviewed. ``scripts/probe_agy_subagent_gate.py`` measured it — the
    subagent's edit landed with the gate asked about nothing but
    ``invoke_subagent``.
    """

    @staticmethod
    def _frame(state, child="child-abc"):
        return {
            "event": "step_update",
            "step_update": {
                "conversation_id": "parent-1",
                "step_index": 2,
                "state": state,
                "step_type": "subagent",
                "tool_name": "invoke_subagent",
                "subagent_info": {
                    "subagents": [{"role": "Reader", "conversation_id": child}]
                },
            },
        }

    def test_an_announced_subagent_is_claimed(self, wired):
        session, server, config_dir, _events = wired
        session._gate_subagents(self._frame("ACTIVE"))
        assert registry.lookup("child-abc", config_dir=config_dir) is not None

    def test_a_done_announcement_claims_rather_than_releases(self, wired):
        """`DONE` on a subagent step is *the launch*, not the subagent.

        The first cut read it the obvious way and the live re-run failed
        identically to the unfixed code: measured at
        `duration_seconds: 0.10` while the subagent's work ran for six
        more seconds. Some runs announce only `DONE`, so treating it as
        settled released a claim that had never been made.
        """
        session, server, config_dir, _events = wired
        session._gate_subagents(self._frame("DONE"))
        assert registry.lookup("child-abc", config_dir=config_dir) is not None

    def test_a_subagent_stays_claimed_across_both_announcements(self, wired):
        session, server, config_dir, _events = wired
        session._gate_subagents(self._frame("ACTIVE"))
        session._gate_subagents(self._frame("DONE"))
        assert registry.lookup("child-abc", config_dir=config_dir) is not None

    def test_every_subagent_in_one_step_is_claimed(self, wired):
        session, server, config_dir, _events = wired
        frame = self._frame("ACTIVE")
        frame["step_update"]["subagent_info"]["subagents"].append(
            {"role": "Second", "conversation_id": "child-two"}
        )
        session._gate_subagents(frame)
        assert registry.lookup("child-abc", config_dir=config_dir) is not None
        assert registry.lookup("child-two", config_dir=config_dir) is not None

    def test_other_step_types_claim_nothing(self, wired):
        session, _server, config_dir, _events = wired
        session._gate_subagents(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "view_file",
                },
            }
        )
        assert registry.lookup("child-abc", config_dir=config_dir) is None

    def test_a_claim_that_fails_does_not_kill_the_turn(self, wired, monkeypatch):
        """It is logged instead — and loudly, because the consequence is a
        subagent running ungated rather than a missing tab."""
        session, server, _config_dir, _events = wired

        def boom(_conversation_id):
            raise OSError("read-only registry")

        monkeypatch.setattr(server, "claim", boom)
        session._gate_subagents(self._frame("ACTIVE"))

    def test_the_claim_happens_before_the_frame_is_yielded(self):
        """The window is the fix's whole residue, so its size is pinned.

        `agy` spawns the child before we read the frame announcing it, so
        a tool call made in that window still passes through. Reading the
        frame and claiming in the same breath is what keeps the window a
        frame read rather than the subagent's whole life — and a refactor
        that moved the claim into the translator, or after the yield,
        would widen it silently.
        """
        import inspect

        from aic_dc.agy import session as mod

        source = inspect.getsource(mod.AgySession.stream_frames)
        assert source.index("_gate_subagents") < source.index("yield frame")


class TestTheStopIsAMechanism:
    """AG-19 — the layer above starvation.

    ``TestStopStarvesTheTurn`` above asserts the refusals, which still
    protect the tree and are unchanged. What is asserted here is that the
    *same latch* ends the loop, so there is no second stop state to fall
    out of step, and that a turn stopped this way says so in its footer.
    """

    def test_the_gate_terminates_the_loop_off_the_same_latch(self, wired):
        """One ``cancel``, two mechanisms, no new state between them."""
        session, server, _cfg, _events = wired

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            await gen.__anext__()
            before = server.decide_invocation({"conversationId": CONV})
            await session.cancel()
            after = server.decide_invocation({"conversationId": CONV})
            await gen.aclose()
            await session.close()
            return before, after

        before, after = asyncio.run(go())
        assert before == {}
        assert after == {"terminationBehavior": "terminate"}

    def test_a_stopped_turn_is_reported_cancelled(self, wired):
        """The correction AG-19 came with.

        A loop this app terminated reports ``status: "SUCCESS"`` with empty
        prose — success meaning only that it exited without an unhandled
        error. Rendered as it arrives that is a completed answer which
        happens to say nothing, and the browser reads ``cancelled`` to draw
        it as a stop instead.
        """
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            out = [await gen.__anext__()]
            await session.cancel()
            async for event in gen:
                out.append(event)
            await session.close()
            return out

        events = asyncio.run(go())
        footer = [e for e in events if e.name == "streamComplete"][-1]
        assert footer.payload["cancelled"] is True

    def test_an_ordinary_turn_is_not(self, wired):
        session, _server, _cfg, _events = wired

        async def go():
            await session.start()
            out = []
            async for event in session.stream_turn(
                "x", translator=AgyTranslator("r1")
            ):
                out.append(event)
            await session.close()
            return out

        events = asyncio.run(go())
        footer = [e for e in events if e.name == "streamComplete"][-1]
        assert footer.payload["cancelled"] is False

    def test_the_next_turn_is_not_cancelled_by_the_last_one(self, wired):
        """``resume`` clears the termination with the refusal, so the
        record cannot leak into the following turn's footer."""
        session, server, _cfg, _events = wired

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            await gen.__anext__()
            await session.cancel()
            await gen.aclose()
            out = []
            async for event in session.stream_turn(
                "y", translator=AgyTranslator("r2")
            ):
                out.append(event)
            terminated = server.was_terminated(CONV)
            await session.close()
            return out, terminated

        events, terminated = asyncio.run(go())
        assert terminated is False
        footer = [e for e in events if e.name == "streamComplete"][-1]
        assert footer.payload["cancelled"] is False


class TestAStopThatDoesNotLandIsReported:
    """AG-R-16 raised to high, and this is what the app owes for it.

    ⏹ is layered and both layers can be outlasted. The gate refuses tool
    calls, which a prose-only turn never makes. The `PostInvocation`
    terminate ends the loop, which a `Stop` hook this app does not own can
    undo — measured 2026-09-12 as a ping-pong, eight invocations in sixteen
    seconds, bounded only by `--print-timeout`, which is 12h here.

    From where the user sits both are one condition: **they pressed stop
    and the turn is still going.** So one report, once, naming the cause it
    can distinguish and ending nothing — AG-19 demoted killing the process
    to an explicit escalation, and a ceiling that fired on its own would
    eventually fire on a legitimate turn waiting in a permission dialog.
    """

    def _at(self, session, now):
        """Point the session's clock at a value the test controls."""
        session._clock = lambda: now[0]

    def _drain(self, wired, *, advance, cancel=True):
        session, server, _cfg, _events = wired
        now = [1000.0]
        self._at(session, now)

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            out = [await gen.__anext__()]
            if cancel:
                await session.cancel()
            now[0] += advance
            async for event in gen:
                out.append(event)
            await session.close()
            return out

        return asyncio.run(go()), server

    def _reports(self, events):
        return [
            e
            for e in events
            if e.name == "systemEvent" and e.payload.get("subtype") == "stop_ignored"
        ]

    def test_a_stop_that_lands_quickly_says_nothing(self, wired):
        events, _server = self._drain(wired, advance=1.0)
        assert self._reports(events) == []

    def test_an_uncancelled_turn_says_nothing_however_long_it_runs(self, wired):
        events, _server = self._drain(wired, advance=600.0, cancel=False)
        assert self._reports(events) == []

    def test_a_stop_still_running_past_the_threshold_is_reported(self, wired):
        events, _server = self._drain(wired, advance=30.0)
        reports = self._reports(events)
        assert len(reports) == 1
        assert reports[0].payload["data"]["seconds"] == 30.0

    def test_it_reports_once_however_many_frames_follow(self, wired):
        """A turn being overridden emits continuously, and one card per
        frame would bury the message it is trying to deliver."""
        events, _server = self._drain(wired, advance=30.0)
        assert len(self._reports(events)) == 1

    def test_it_ends_nothing(self, wired):
        """The whole point of reporting rather than acting.

        The turn runs to its own end and the session is still usable — the
        escalation is the user's to take, from the control the report
        offers them.
        """
        session, _server, _cfg, _events = wired
        events, _server2 = self._drain(wired, advance=30.0)
        assert [e.name for e in events][-1] == "streamComplete"
        assert session.started is False  # closed by the drain, not by the report

    def test_an_overridden_loop_is_named_as_one(self, wired):
        """`revived` is the difference between two very different stories.

        A loop put back after the gate ended it is somebody's hook
        overriding the user. A turn that simply cannot be starved is prose,
        asking permission for nothing — AG-19's residual gap, and not
        anyone's fault.
        """
        session, server, _cfg, _events = wired
        now = [1000.0]
        session._clock = lambda: now[0]

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            out = [await gen.__anext__()]
            await session.cancel()
            # The gate ends the loop twice: once for the stop, once because
            # something put the loop back.
            server.decide_invocation({"conversationId": CONV, "invocationNum": 0})
            server.decide_invocation({"conversationId": CONV, "invocationNum": 1})
            now[0] += 30
            async for event in gen:
                out.append(event)
            await session.close()
            return out

        reports = self._reports(asyncio.run(go()))
        assert len(reports) == 1
        assert reports[0].payload["data"]["revived"] is True

    def test_a_stop_that_was_merely_slow_is_not_blamed_on_a_hook(self, wired):
        events, _server = self._drain(wired, advance=30.0)
        assert self._reports(events)[0].payload["data"]["revived"] is False

    def test_the_next_turn_starts_clean(self, wired):
        session, _server, _cfg, _events = wired
        now = [1000.0]
        session._clock = lambda: now[0]

        async def go():
            await session.start()
            gen = session.stream_turn("x", translator=AgyTranslator("r1"))
            await gen.__anext__()
            await session.cancel()
            now[0] += 30
            async for _event in gen:
                pass
            out = []
            async for event in session.stream_turn(
                "y", translator=AgyTranslator("r2")
            ):
                out.append(event)
            await session.close()
            return out

        assert self._reports(asyncio.run(go())) == []
