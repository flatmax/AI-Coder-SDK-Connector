"""Stop, with a permission dialog still on screen — the whole chain.

Everything here is real except the CLI: a real ``ClaudeCodeService``, the
real ``EngineSession`` it builds, the real ``PermissionBroker`` wired into
it as ``can_use_tool``, and a fake ``ClaudeSDKClient`` standing in for the
subprocess. That is the point of a separate file. The service tests in
``test_claude_code_service.py`` use a ``FakeSession``, so the piece that
made this a bug — the **drain watchdog**, which loses the session when an
interrupted turn does not reach its result in time — never runs there.

These began as throwaway probes written to answer a question the specs did
not: *what does Stop actually do while a request is pending?* The answer
was that nothing resolved it, so the CLI stayed blocked on the control
response, the drain expired, and the session was lost — and the 300-second
decision deadline was the only thing that ever cleared the request, long
after the turn it belonged to was gone. The probes printed; these assert
the fix.

The fake CLI is driven in the two shapes a real one could take, because
the probes could not tell which one it is:

- **Blocked** — it cannot finish the turn until the permission control
  request is answered. This is the shape that cost the session.
- **Abandoning** — it honours the interrupt and emits a result with the
  control request still outstanding, leaving a dialog behind.

Both are parametrised over the same assertions, which is the substance of
the fix: *the outcome no longer depends on which shape the CLI has.* The
deny goes out before the interrupt, so a blocked CLI is released and can
finish, and the end of the turn sweeps whatever an abandoning one left.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from aic_dc.claude_code import session as session_module
from aic_dc.claude_code.engine_config import EngineConfig
from aic_dc.claude_code.health import CliResolution
from aic_dc.claude_code.service import ClaudeCodeService

pytestmark = pytest.mark.asyncio

REQUEST_ID = "1736956800000-a1b2c3"

# Far below the old 300 s decision deadline and far below the real 30 s
# drain window, so the test is quick *and* it fails if the deny stops
# being prompt. Nothing here is allowed to take a second.
DRAIN_WINDOW = 1.5


class FakeConfig:
    def __init__(self, repo_root):
        self.repo_root = repo_root
        self.config_dir = None
        self.aic_dc_dir = Path(repo_root) / ".aic-dc"

    def get_commit_prompt(self):
        return "Write a conventional commit message for this diff."


class FakeToolContext:
    """Stands in for ``ToolPermissionContext``."""

    tool_use_id = "toolu_01"
    suggestions = ()
    agent_id = None
    blocked_path = None
    decision_reason = None
    title = None
    display_name = None
    description = None


class FakeCli:
    """The CLI, as far as ``EngineSession`` can tell.

    Asks permission by calling the callback off the options it was handed,
    rather than one a test wired directly — so a service that forgot to
    pass ``can_use_tool`` at all fails here instead of passing.
    """

    instances: list["FakeCli"] = []

    #: Set by the fixture: does this CLI need an answer before it can end
    #: the turn, or will it abandon the tool and emit a result anyway?
    blocks_until_answered = True

    def __init__(self, options=None):
        self.options = options
        self.interrupt_calls = 0
        self.disconnect_calls = 0
        self.asked = asyncio.Event()
        self.permission: asyncio.Task | None = None
        self._ended = asyncio.Event()
        FakeCli.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnect_calls += 1
        # ``Query.close()`` closes the send side and the read task pushes an
        # end sentinel, so a consumer parked in ``receive_response()`` sees
        # EndOfStream rather than hanging. Modelled, because the first probe
        # hung instead and made the session look permanently wedged.
        self._ended.set()

    async def query(self, prompt, session_id="default"):
        pass

    async def interrupt(self):
        self.interrupt_calls += 1

    async def get_context_usage(self):
        return {"total_tokens": 1}

    async def get_server_info(self):
        return {"commands": []}

    def _ask_permission(self):
        """The control request: a task, the way the SDK dispatches it.

        ``Query._read_messages`` hands a control request to a detached
        task, so the message pump is not what is blocked — which is why a
        pending request can sit there costing nothing.
        """
        self.permission = asyncio.create_task(
            self.options.can_use_tool(
                "Bash", {"command": "rm -rf build"}, FakeToolContext()
            )
        )
        self.asked.set()

    async def receive_response(self):
        yield SystemMessage(
            subtype="init", data={"session_id": "sess-1", "model": "m"}
        )
        yield AssistantMessage(
            content=[TextBlock(text="running a command")],
            model="m",
            message_id="msg_1",
        )
        self._ask_permission()
        if self.blocks_until_answered:
            # The turn cannot end until the tool call is settled one way or
            # the other. Before the fix, this never came.
            await self.permission
        else:
            # Honours the interrupt and gives up on the tool, with the
            # control request still outstanding.
            await self._interrupted()
        yield ResultMessage(
            subtype="error_during_execution",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
        )

    async def _interrupted(self):
        for _ in range(500):
            if self.interrupt_calls or self._ended.is_set():
                return
            await asyncio.sleep(0.002)


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, name, *args):
        self.calls.append((name, *args))

    def names(self):
        return [call[0] for call in self.calls]

    def payloads_of(self, name):
        return [call[-1] for call in self.calls if call[0] == name]


@pytest.fixture(autouse=True)
def fake_cli(monkeypatch):
    """A CLI-shaped fake, and a drain window a test can outlive."""
    import claude_agent_sdk

    FakeCli.instances.clear()
    FakeCli.blocks_until_answered = True
    monkeypatch.setattr(
        session_module,
        "resolve_cli",
        lambda cli_path: CliResolution(
            path="/fake/claude", source="bundled", version="2.1.229"
        ),
    )
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeCli)
    monkeypatch.setattr(session_module, "INTERRUPT_DRAIN_TIMEOUT", DRAIN_WINDOW)
    return FakeCli


@pytest.fixture
def events():
    return Recorder()


@pytest.fixture
async def service(tmp_path, events):
    svc = ClaudeCodeService(
        FakeConfig(tmp_path), event_callback=events, engine_config=EngineConfig()
    )
    await svc.connect_engine()
    events.calls.clear()
    yield svc
    await svc.permissions.cancel_all()
    for task in list(svc._turn_tasks):
        task.cancel()
    await asyncio.gather(*svc._turn_tasks, return_exceptions=True)


async def start_turn_and_wait_for_the_dialog(service, cli):
    """Send a message, and get as far as the dialog being on screen."""
    await service.chat_streaming(REQUEST_ID, "run something")
    await asyncio.wait_for(cli.asked.wait(), timeout=2)
    for _ in range(500):
        if service.permissions.pending():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("the permission request never reached the queue")


async def finish_turns(service):
    for _ in range(50):
        tasks = [task for task in service._turn_tasks if not task.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# What the request costs while it waits
# ---------------------------------------------------------------------------


class TestAPendingRequestHasNoClock:
    async def test_a_request_a_host_can_answer_has_no_deadline(self, service, fake_cli):
        """The claim the removal of the timeout rests on.

        If this payload carried an ``expires_at``, every assertion below
        about Stop being the way out would be provable by simply waiting
        instead — and a user who walked away would come back to a denial.
        """
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        request = service.permissions.pending()[0]
        assert request["expires_at"] is None
        assert request["localhost_available"] is True

    async def test_the_pump_is_not_what_is_blocked(self, service, fake_cli, events):
        """The reason a pending request consumes nothing.

        The assistant text that preceded the tool call reaches the browser
        while the request sits unanswered: what is blocked is one control
        request, not the message stream and not an API call. If the pump
        were the thing waiting, a timer on the answer would be protecting
        something real.
        """
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        streamed = "".join(
            payload.get("content", "") for payload in events.payloads_of("streamChunk")
        )
        assert "running a command" in streamed
        assert "streamComplete" not in events.names()


# ---------------------------------------------------------------------------
# Stop, in both CLI shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blocks_until_answered", [True, False])
class TestStopIsTheWayOut:
    @pytest.fixture(autouse=True)
    def cli_shape(self, fake_cli, blocks_until_answered):
        fake_cli.blocks_until_answered = blocks_until_answered

    async def test_the_dialog_closes_and_the_callback_is_answered(
        self, service, fake_cli
    ):
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        await service.cancel_streaming(REQUEST_ID)
        result = await asyncio.wait_for(cli.permission, timeout=2)

        assert type(result).__name__ == "PermissionResultDeny"
        assert "stopped this turn" in result.message
        assert service.permissions.pending() == []

    async def test_the_session_survives_the_stop(self, service, fake_cli):
        """The regression the probes were written to find.

        A blocked CLI could not finish the turn while the request was
        unanswered, so ``_watch_drain`` expired, set ``_session_lost``, and
        disconnected — over a dialog. The user's conversation was gone
        because they clicked Stop.
        """
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        await service.cancel_streaming(REQUEST_ID)
        await asyncio.wait_for(finish_turns(service), timeout=2)
        # Well inside the drain window, which the fixture shortened.
        await asyncio.sleep(0.05)

        assert service.session.connected is True
        assert service.session._session_lost is False
        assert service.session.health.last_error is None
        assert cli.disconnect_calls == 0

    async def test_the_turn_ends_so_the_next_one_can_start(self, service, fake_cli):
        """``streaming_active`` is what ``new_session`` and ``resume`` refuse on.

        A turn that never ends is not just a stuck spinner: it locks the
        user out of the two ways they could have escaped it.
        """
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        await service.cancel_streaming(REQUEST_ID)
        await asyncio.wait_for(finish_turns(service), timeout=2)

        assert service.session.streaming_active is False
        service.session.admit("1736956900000-d4e5f6")

    async def test_nothing_is_announced_after_the_turn_is_over(
        self, service, fake_cli, events
    ):
        """The denial belongs to the turn, not to whatever comes after it.

        The old expiry announced a denial minutes later, against a turn
        that had already finished — a dialog vanishing on its own, with a
        transcript entry to match.
        """
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        await service.cancel_streaming(REQUEST_ID)
        await asyncio.wait_for(finish_turns(service), timeout=2)
        names = events.names()
        await asyncio.sleep(0.2)

        assert events.names() == names
        resolved = events.payloads_of("permissionResolved")
        assert [entry["action"] for entry in resolved] == ["cancelled"]
        assert names.index("permissionResolved") < names.index("streamComplete")


# ---------------------------------------------------------------------------
# The other ways a turn ends
# ---------------------------------------------------------------------------


class TestATurnThatEndsWithoutAStop:
    async def test_a_dialog_never_outlives_its_turn(self, service, fake_cli):
        """The backstop, on the real session rather than a fake one.

        No Stop here: the CLI abandons the tool and ends the turn on its
        own, which is what an engine-side error looks like. The sweep in
        ``_run_turn``'s ``finally`` is the only thing that resolves the
        request.
        """
        fake_cli.blocks_until_answered = False
        cli = FakeCli.instances[-1]
        await start_turn_and_wait_for_the_dialog(service, cli)

        # Nobody interrupts; the fake gives up waiting and sends its result.
        await asyncio.wait_for(finish_turns(service), timeout=3)

        result = await asyncio.wait_for(cli.permission, timeout=2)
        assert type(result).__name__ == "PermissionResultDeny"
        assert "The turn ended" in result.message
        assert service.permissions.pending() == []
