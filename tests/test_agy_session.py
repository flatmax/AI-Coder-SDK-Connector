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
import sys
import textwrap
from pathlib import Path

import pytest

from aic_dc.antigravity.agy import registry
from aic_dc.antigravity.agy.gate_server import AgyGateServer
from aic_dc.antigravity.agy.session import (
    AgyNotInstalledError,
    AgySession,
    TurnInProgressError,
)
from aic_dc.antigravity.agy.steps import AgyTranslator
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
        tmp_path, broadcast=broadcast, localhost_available=lambda: True
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
                tmp_path, broadcast=lambda e: None, localhost_available=lambda: True
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
        assert events[-1].payload["response_text"] == "Reading the file."
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
