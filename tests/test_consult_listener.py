"""The consultation listener, exercised over its own wire.

These go through a real MCP client against a real socket rather than
calling the handler directly, because every interesting property of this
module is a property of the wire: that a forged bearer never reaches the
consultant, that a discovery probe needs no credential, that a stopped
turn's in-flight call actually completes with an answer instead of
hanging. Calling ``_consult`` in-process would assert that the Python is
self-consistent and would have passed just as happily with the middleware
detached.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aic_dc.claude_code.consult_listener import (
    DISCOVER_METHOD,
    MCP_PATH,
    METHOD_HEADER,
    SERVER_NAME,
    STOPPED,
    SUBAGENT_META_KEY,
    SUBAGENT_REFUSED,
    ConsultationListener,
)
from aic_dc.claude_code.consultant import ConsultationError

SESSION = "agy-session-1"


class StubConsultant:
    """Stands in for ``ClaudeConsultant``, which would spend real tokens.

    Records what it was asked so a test can assert the question crossed
    the wire intact, and counts constructions so a test can prove an
    unauthorized call never got as far as building one.
    """

    constructed = 0
    cancelled = 0
    interrupted = 0
    asked: list[tuple[str, str]] = []

    def __init__(
        self,
        answer: str = "the answer",
        hang: bool = False,
        fail: Exception | None = None,
    ) -> None:
        self._answer = answer
        self._hang = hang
        self._fail = fail
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def second_opinion(self, question: str, context: str = "") -> str:
        type(self).asked.append((question, context))
        self._task = asyncio.current_task()
        if self._fail is not None:
            raise self._fail
        if self._hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                type(self).interrupted += 1
                # What the real consultant does: it flags itself in
                # ``cancel()`` and converts its own cancellation into a
                # typed error rather than letting ``CancelledError`` out.
                if self._stopped:
                    raise ConsultationError("The consultation was stopped.") from None
                raise
        return self._answer

    async def cancel(self) -> bool:
        type(self).cancelled += 1
        if self._task is None or self._task.done():
            return False
        self._stopped = True
        self._task.cancel()
        return True


@pytest.fixture(autouse=True)
def _reset_stub():
    StubConsultant.constructed = 0
    StubConsultant.cancelled = 0
    StubConsultant.interrupted = 0
    StubConsultant.asked = []
    yield


def _factory(**kwargs):
    def make():
        StubConsultant.constructed += 1
        return StubConsultant(**kwargs)

    return make


@contextlib.asynccontextmanager
async def running(
    factory=None, *, budget: int = 2, open_turn: bool = True, idle_timeout: float = 3600.0
):
    """A started listener with one minted token, torn down afterwards."""
    listener = ConsultationListener(factory or _factory(), budget=budget, idle_timeout=idle_timeout)
    port = await listener.start()
    token = listener.mint(repo_root="/tmp/some-repo", session_id=SESSION)
    if open_turn:
        listener.begin_turn(token, "turn-1")
    try:
        yield listener, f"http://127.0.0.1:{port}{MCP_PATH}", token
    finally:
        await listener.aclose()


@contextlib.asynccontextmanager
async def connected(url: str, token: str | None):
    """An initialized MCP client session, as ``agy`` would hold one."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


class TestTheWire:
    async def test_a_valid_bearer_gets_an_answer_from_the_consultant(self):
        async with running(_factory(answer="use a lock")) as (_, url, token):
            async with connected(url, token) as session:
                result = await session.call_tool(
                    "second_opinion",
                    {"question": "is this safe?", "context": "some diff"},
                )
        assert _text(result) == "use a lock"
        assert StubConsultant.asked == [("is this safe?", "some diff")]

    async def test_the_tool_is_advertised_under_the_configured_name(self):
        async with running() as (_, url, token):
            async with connected(url, token) as session:
                listed = await session.list_tools()
        names = [t.name for t in listed.tools]
        assert names == ["second_opinion"]
        # The injected request context must not appear as an argument the
        # model is invited to supply.
        schema = listed.tools[0].inputSchema
        assert set(schema["properties"]) == {"question", "context"}

    async def test_a_forged_bearer_never_reaches_the_consultant(self):
        """401 at the edge, and nothing behind it was constructed."""
        async with running() as (_, url, _token):
            response = await _post_initialize(url, "not-a-real-token")
        assert response.status_code == 401
        assert StubConsultant.constructed == 0

    async def test_a_missing_bearer_is_refused_too(self):
        async with running() as (_, url, _token):
            response = await _post_initialize(url, None)
        assert response.status_code == 401

    async def test_the_discovery_probe_is_404_and_carries_no_credential(self):
        """``agy`` probes this unauthenticated, twice, before it has a token.

        Answering 401 would be inviting an OAuth flow this server does not
        run; 404 says the static configuration is the whole story.
        """
        async with running() as (listener, _url, _token):
            base = f"http://127.0.0.1:{listener.port}"
            async with httpx.AsyncClient() as client:
                for path in (
                    "/.well-known/oauth-protected-resource",
                    "/.well-known/oauth-protected-resource/mcp",
                ):
                    response = await client.get(f"{base}{path}")
                    assert response.status_code == 404, path


class TestTheBudget:
    async def test_a_turn_buys_two_consultations_and_the_third_is_refused(self):
        async with running(budget=2) as (_, url, token):
            async with connected(url, token) as session:
                first = _text(await session.call_tool("second_opinion", {"question": "a"}))
                second = _text(await session.call_tool("second_opinion", {"question": "b"}))
                third = _text(await session.call_tool("second_opinion", {"question": "c"}))
        assert first == second == "the answer"
        assert "budget for this turn is spent" in third
        # Refused before the consultant, not after.
        assert StubConsultant.constructed == 2

    async def test_the_next_turn_restores_it(self):
        """The regression this guards is permanent refusal.

        ``agy`` is one process across many turns, so every turn presents
        the same bearer. A budget keyed on the token alone would be spent
        by turn one and gone for the life of the conversation.
        """
        async with running(budget=1) as (listener, url, token):
            async with connected(url, token) as session:
                await session.call_tool("second_opinion", {"question": "a"})
                spent = _text(await session.call_tool("second_opinion", {"question": "b"}))
                listener.begin_turn(token, "turn-2")
                after = _text(await session.call_tool("second_opinion", {"question": "c"}))
        assert "budget for this turn is spent" in spent
        assert after == "the answer"

    async def test_a_call_outside_a_turn_is_refused_and_spends_nothing(self):
        async with running(open_turn=False) as (listener, url, token):
            async with connected(url, token) as session:
                refused = _text(await session.call_tool("second_opinion", {"question": "a"}))
                listener.begin_turn(token, "turn-1")
                allowed = _text(await session.call_tool("second_opinion", {"question": "b"}))
                listener.end_turn(token, "turn-1")
                late = _text(await session.call_tool("second_opinion", {"question": "c"}))
        assert "No turn is open" in refused
        assert allowed == "the answer"
        assert "No turn is open" in late
        assert StubConsultant.constructed == 1

    async def test_each_consultation_gets_its_own_consultant(self):
        """A shared instance would let one session's stop reach another's call.

        ``ClaudeConsultant`` holds its running task on itself for its own
        ``cancel()``, so instances are not shareable.
        """
        async with running() as (_, url, token):
            async with connected(url, token) as session:
                await session.call_tool("second_opinion", {"question": "a"})
                await session.call_tool("second_opinion", {"question": "b"})
        assert StubConsultant.constructed == 2


class TestStopping:
    async def test_stop_ends_an_in_flight_consultation_with_an_answer(self):
        """The ⏹ hole, closed.

        The host's stop is not a process kill: it latches a gate that
        refuses *subsequent* tool calls. Around an in-flight MCP call no
        hook fires at all until the call returns, so left to the wire a
        stopped turn keeps spending on an answer nobody will read. The
        assertion is on the artefact — the call comes back, and it comes
        back saying it was cancelled — rather than on ``cancel`` having
        been reached.
        """
        async with running(_factory(hang=True)) as (listener, url, token):
            async with connected(url, token) as session:
                call = asyncio.create_task(session.call_tool("second_opinion", {"question": "a"}))
                await _wait_until(lambda: StubConsultant.asked)
                assert not call.done(), "the consultation should still be running"

                assert await listener.cancel_session(SESSION) is True
                result = await asyncio.wait_for(call, timeout=10)
        assert _text(result) == STOPPED

    async def test_stopping_one_session_leaves_another_alone(self):
        listener = ConsultationListener(_factory(hang=True))
        port = await listener.start()
        url = f"http://127.0.0.1:{port}{MCP_PATH}"
        mine = listener.mint(repo_root="/tmp/a", session_id="session-a")
        theirs = listener.mint(repo_root="/tmp/b", session_id="session-b")
        listener.begin_turn(mine, "t1")
        listener.begin_turn(theirs, "t1")
        try:
            async with connected(url, mine) as a, connected(url, theirs) as b:
                call_a = asyncio.create_task(a.call_tool("second_opinion", {"question": "a"}))
                call_b = asyncio.create_task(b.call_tool("second_opinion", {"question": "b"}))
                await _wait_until(lambda: len(StubConsultant.asked) == 2)

                await listener.cancel_session("session-a")
                assert _text(await asyncio.wait_for(call_a, timeout=10)) == STOPPED
                await asyncio.sleep(0.2)
                assert not call_b.done(), "session-b's consultation was collateral"
                call_b.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await call_b
        finally:
            await listener.aclose()

    async def test_revoking_a_token_stops_its_work_and_refuses_it_after(self):
        """Lifetime is the ``agy`` subprocess and nothing else.

        Not an idle TTL. Two measurements settle that between them: a
        session survived a 15-minute idle gap and ``agy`` reused it for
        the next turn without re-initializing, and a session the server
        had forgotten was never rebuilt — ``agy`` reported ``session not
        found`` and gave up. A reaper with any TTL shorter than a
        conversation would therefore break the consultant for the rest of
        one the user still has open.

        The suppression is not incidental: revoking mid-session makes the
        client's own teardown ``DELETE`` fail the bearer check too, which
        is correct and harmless because in production revocation follows
        the subprocess exit, so there is no client left to tear down.
        """
        async with running(_factory(hang=True)) as (listener, url, token):
            with contextlib.suppress(Exception):
                async with connected(url, token) as session:
                    call = asyncio.create_task(
                        session.call_tool("second_opinion", {"question": "a"})
                    )
                    await _wait_until(lambda: StubConsultant.asked)
                    await listener.revoke(token)
                    assert _text(await asyncio.wait_for(call, timeout=10)) == STOPPED
            assert listener.resolve(token) is None
            response = await _post_initialize(url, token)
        assert response.status_code == 401


class TestTheHostSurface:
    async def test_the_port_is_known_before_the_server_is_scheduled(self):
        """The config naming the port is written at spawn time.

        With ``port=0`` handed to uvicorn the port is not knowable until
        its startup has run, so the obvious ordering is a race whose
        losing side is a config file pointing at port 0.
        """
        listener = ConsultationListener(_factory())
        assert listener.port is None
        try:
            port = await listener.start()
            assert port == listener.port > 0
            assert await listener.start() == port, "start should be idempotent"
        finally:
            await listener.aclose()

    async def test_the_config_entry_is_what_agy_reads(self):
        async with running() as (listener, url, token):
            entry = listener.config_entry(token)
            server = entry["mcpServers"][SERVER_NAME]
            # ``serverUrl`` rather than ``url``: measured, ``url`` is not read.
            assert server["serverUrl"] == url
            assert server["headers"]["Authorization"] == f"Bearer {token}"
        # A stopped listener has no address to advertise, and saying so is
        # better than handing out a config pointing at a dead port.
        with pytest.raises(RuntimeError):
            listener.config_entry(token)

    async def test_the_token_carries_the_repository_the_model_is_never_asked_for(self):
        async with running() as (listener, _url, token):
            grant = listener.resolve(token)
        assert str(grant.repo_root) == "/tmp/some-repo"
        assert grant.session_id == SESSION

    async def test_two_spawns_get_different_tokens(self):
        async with running() as (listener, _url, first):
            second = listener.mint(repo_root="/tmp/other", session_id="session-b")
            assert first != second
            assert str(listener.resolve(second).repo_root) == "/tmp/other"
            assert str(listener.resolve(first).repo_root) == "/tmp/some-repo"

    async def test_closing_twice_is_harmless(self):
        listener = ConsultationListener(_factory())
        await listener.start()
        await listener.aclose()
        await listener.aclose()
        assert listener.port is None

    async def test_the_listener_does_not_take_over_the_signal_handlers(self):
        """Uvicorn installs its own SIGINT and SIGTERM handlers in ``serve()``.

        Embedded in this application's loop that would replace the
        application's handlers for the listener's whole lifetime, so
        Ctrl-C would stop meaning what the rest of the program says it
        means.
        """
        import signal

        before = (
            signal.getsignal(signal.SIGINT),
            signal.getsignal(signal.SIGTERM),
        )
        listener = ConsultationListener(_factory())
        try:
            await listener.start()
            assert (
                signal.getsignal(signal.SIGINT),
                signal.getsignal(signal.SIGTERM),
            ) == before
        finally:
            await listener.aclose()


async def _post_initialize(url: str, token: str | None) -> httpx.Response:
    """An ``initialize`` exactly as a client would send it, bearer or not."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    async with httpx.AsyncClient() as client:
        return await client.post(url, json=body, headers=headers)


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    """Poll rather than sleep a fixed interval, so a slow machine still passes."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0.02)


class TestWhatRoundEightChanged:
    """Each of these guards a defect that review found and measurement confirmed."""

    async def test_a_failed_consultation_is_not_charged_to_the_turn(self):
        """A transient failure must not cost a turn a tool that never ran.

        The consultant is reached over the network, so a 429 or a dropped
        connection is an ordinary event. Charging for it would let two bad
        seconds lock a master out of consulting for the rest of its turn.
        """
        async with running(_factory(fail=RuntimeError("upstream 529")), budget=2) as (
            _,
            url,
            token,
        ):
            async with connected(url, token) as session:
                first = _text(await session.call_tool("second_opinion", {"question": "a"}))
                second = _text(await session.call_tool("second_opinion", {"question": "b"}))
        assert "upstream 529" in first
        assert "upstream 529" in second, "a refunded failure should leave the budget intact"

    async def test_but_retrying_a_broken_consultant_still_terminates(self):
        """Refunding without limit would hand back the loop guard.

        A model that retries an error is the loop the budget exists to
        stop, so attempts are counted separately and never refunded.
        """
        async with running(_factory(fail=RuntimeError("always broken")), budget=2) as (
            _,
            url,
            token,
        ):
            async with connected(url, token) as session:
                replies = [
                    _text(await session.call_tool("second_opinion", {"question": str(i)}))
                    for i in range(6)
                ]
        assert all("always broken" in r for r in replies[:4])
        assert all("budget for this turn is spent" in r for r in replies[4:])

    async def test_stopping_goes_through_the_consultants_own_cancel(self):
        """Not ``task.cancel()`` round the side of it.

        ``ClaudeConsultant.cancel`` flags itself before cancelling, which
        is what makes it raise a typed error instead of letting a bare
        ``CancelledError`` escape — and that distinction is the only way
        this module can tell "the user stopped this" from "something
        cancelled *me*", which must never be answered the same way.
        """
        async with running(_factory(hang=True)) as (listener, url, token):
            async with connected(url, token) as session:
                call = asyncio.create_task(session.call_tool("second_opinion", {"question": "a"}))
                await _wait_until(lambda: StubConsultant.asked)
                await listener.cancel_session(SESSION)
                await asyncio.wait_for(call, timeout=10)
        assert StubConsultant.cancelled == 1

    async def test_a_clients_own_cancellation_is_not_answered_with_success(self):
        """``agy`` cancels at its own 3m0s deadline; that must not become a 200.

        Swallowing the protocol cancellation and returning cheerful text
        would tell the caller its cancellation failed, and would put words
        in the consultant's mouth: to the model, a successful tool result
        reads as Claude's considered advice.

        Driven as the notification actually arrives on the wire rather
        than through the client's ``read_timeout_seconds``, which was
        measured to time out locally without sending anything at all — a
        test built on it passes against a mechanism that never fired.
        """
        async with running(_factory(hang=True)) as (_, url, token):
            frames = await _call_then_cancel(url, token)
        assert len(frames) == 1
        reply = json.loads(frames[0])
        assert "error" in reply, f"a cancelled call must not answer with a result: {reply}"
        assert reply["error"]["message"] == "Request cancelled"
        assert "result" not in reply
        # And the consultation behind it actually stopped. The task is
        # scheduled independently, so without this it would run on
        # unreferenced — spending the subscription with no handle
        # anywhere able to stop it.
        assert StubConsultant.interrupted == 1

    async def test_a_late_end_turn_cannot_close_the_turn_that_followed_it(self):
        """The host closes turns from a ``finally``, so they can arrive late.

        Closing whatever happens to be current would leave the next turn
        unable to consult at all, with nothing anywhere saying why.
        """
        async with running() as (listener, url, token):
            listener.begin_turn(token, "turn-2")
            listener.end_turn(token, "turn-1")  # turn one, arriving late
            async with connected(url, token) as session:
                reply = _text(await session.call_tool("second_opinion", {"question": "a"}))
        assert reply == "the answer"

    async def test_terminated_sessions_do_not_accumulate(self):
        """The tidy path is the one that leaks.

        Measured against the SDK: a session closed properly by ``DELETE``
        is marked terminated and then kept forever, because the only
        removal the SDK does is on the idle path and its other cleanup is
        guarded by ``not is_terminated``. Ten spawns left ten dead entries.
        """
        async with running() as (listener, url, token):
            for _ in range(3):
                async with connected(url, token) as session:
                    await session.call_tool("second_opinion", {"question": "a"})
            assert listener.sessions, "sessions should exist before the sweep"
            await asyncio.sleep(0.2)
            listener.sweep_terminated()
            assert listener.sessions == {}

    async def test_a_spawn_sweeps_what_the_last_one_left(self):
        """Swept at the spawn boundary, which is where the leak is made."""
        async with running() as (listener, url, token):
            for _ in range(3):
                async with connected(url, token) as session:
                    await session.call_tool("second_opinion", {"question": "a"})
            await asyncio.sleep(0.2)
            listener.mint(repo_root="/tmp/next", session_id="session-next")
            assert listener.sessions == {}

    async def test_the_idle_reaper_is_armed(self):
        """Without it nothing ever collects the transports ``agy`` orphans.

        Every spawn opens one with a ``server/discover`` the SDK rejects,
        and no ``DELETE`` ever follows it.
        """
        async with running(idle_timeout=1234.0) as (listener, _url, _token):
            assert listener._mcp.session_manager.session_idle_timeout == 1234.0


async def _call_then_cancel(url: str, token: str) -> list[str]:
    """Make a tool call, cancel it mid-flight, and return what came back.

    Raw rather than through ``ClientSession`` because the point is the
    wire: this is the exact frame ``agy`` sends when its own deadline
    expires, and nothing else reproduces it.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            },
        )
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
        await client.post(
            url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        frames: list[str] = []

        async def call() -> None:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 42,
                    "method": "tools/call",
                    "params": {
                        "name": "second_opinion",
                        "arguments": {"question": "a"},
                    },
                },
            ) as streamed:
                async for line in streamed.aiter_lines():
                    if line.startswith("data:"):
                        frames.append(line[5:].strip())

        pending = asyncio.create_task(call())
        await _wait_until(lambda: StubConsultant.asked)
        await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 42, "reason": "deadline"},
            },
        )
        await asyncio.wait_for(pending, timeout=10)
        return frames


async def _raw_session(client: httpx.AsyncClient, url: str, token: str) -> dict[str, str]:
    """Initialize over raw HTTP and return the headers for later calls."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    response = await client.post(
        url,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
    )
    headers["mcp-session-id"] = response.headers["mcp-session-id"]
    await client.post(
        url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    return headers


def _sse_result(response: httpx.Response) -> dict:
    """The JSON-RPC ``result`` object out of an SSE body."""
    for line in response.text.splitlines():
        if line.startswith("data:"):
            frame = json.loads(line[5:].strip())
            if "result" in frame:
                return frame["result"]
    raise AssertionError(f"no result frame in {response.text!r}")


async def _call_with_meta(url: str, token: str, meta: dict) -> dict:
    """One ``tools/call`` carrying an explicit ``params._meta``.

    Raw, because ``_meta`` is the whole point and ``ClientSession`` owns
    that field — it is where ``agy`` puts the marker that says a subagent
    is asking, and no client API offers to forge it.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        headers = await _raw_session(client, url, token)
        response = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "_meta": meta,
                    "name": "second_opinion",
                    "arguments": {"question": "a"},
                },
            },
        )
        return _sse_result(response)


class TestWhatTheProbesSettled:
    """Rulings the probes reversed, and the ones they confirmed."""

    async def test_only_a_consultation_comes_back_unflagged(self):
        """``isError`` answers one question: was a second opinion produced?"""
        async with running(_factory(), budget=1) as (listener, url, token):
            async with httpx.AsyncClient(timeout=30) as client:
                headers = await _raw_session(client, url, token)
                call = {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "second_opinion", "arguments": {"question": "a"}},
                }
                served = _sse_result(await client.post(url, headers=headers, json=call))
                refused = _sse_result(await client.post(url, headers=headers, json=call))

        assert served.get("isError") is False
        assert served["content"][0]["text"] == "the answer"
        # No review happened, so this must not be readable as one — even
        # though the tool worked and the refusal is true. Unflagged, it
        # enters the transcript as the thing the consultant said.
        assert refused.get("isError") is True
        assert "budget for this turn is spent" in refused["content"][0]["text"]

    async def test_a_stopped_consultation_is_flagged_so_it_cannot_pass_for_advice(self):
        async with running(_factory(hang=True)) as (listener, url, token):
            async with httpx.AsyncClient(timeout=30) as client:
                headers = await _raw_session(client, url, token)
                task = asyncio.ensure_future(
                    client.post(
                        url,
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 7,
                            "method": "tools/call",
                            "params": {
                                "name": "second_opinion",
                                "arguments": {"question": "a"},
                            },
                        },
                    )
                )
                await _wait_until(lambda: StubConsultant.asked)
                assert await listener.cancel_session(SESSION)
                result = _sse_result(await task)

        assert result["isError"] is True
        # Verbatim, and this is the assertion that matters. A handler
        # annotated ``-> str`` returning a ``CallToolResult`` is measured to
        # deliver a pydantic validation error in place of this sentence —
        # flagged correctly and saying nothing anyone can act on.
        assert result["content"][0]["text"] == STOPPED

    async def test_a_subagent_is_refused_and_spends_nothing(self):
        """``agy``'s delegate marker is in ``_meta``, and it is the only signal."""
        async with running(_factory()) as (listener, url, token):
            refused = await _call_with_meta(
                url,
                token,
                {
                    "antigravity.google/conversation_id": "child",
                    SUBAGENT_META_KEY: "master",
                },
            )
            grant = next(iter(listener._grants.values()))
            spent, attempts = grant.spent, grant.attempts

        assert refused["content"][0]["text"] == SUBAGENT_REFUSED
        # Flagged, for the same reason a spent budget is: the delegate
        # asked for a review and did not get one. The prose, not the flag,
        # is what tells it to report upward rather than retry.
        assert refused.get("isError") is True
        assert StubConsultant.constructed == 0
        # Neither counter moved, so a subagent cannot exhaust the turn its
        # master is still going to want.
        assert (spent, attempts) == (0, 0)

    async def test_the_master_carries_the_same_shape_minus_the_parent_and_is_served(self):
        """The control for the test above: absent the parent key, it is the master."""
        async with running(_factory()) as (listener, url, token):
            served = await _call_with_meta(
                url,
                token,
                {
                    "antigravity.google/conversation_id": "master",
                    "progressToken": "p:1",
                },
            )
        assert served.get("isError") is False
        assert served["content"][0]["text"] == "the answer"
        assert StubConsultant.constructed == 1

    async def test_the_discovery_probe_allocates_no_transport(self):
        """P8's first leak, closed at the gate.

        ``agy`` opens every spawn with a session-less ``server/discover``.
        Forwarded, the SDK builds a transport for it before rejecting the
        body, and nothing can ever delete that entry — no client session
        exists to send the ``DELETE``.
        """
        async with running(_factory()) as (listener, url, token):
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {token}",
                        METHOD_HEADER: DISCOVER_METHOD,
                    },
                    json={"jsonrpc": "2.0", "id": 0, "method": DISCOVER_METHOD, "params": {}},
                )
                assert response.status_code == 404
                # The assertion is the table, not the status code. A 404
                # with a transport behind it is the leak with better manners.
                assert listener.sessions == {}

    async def test_revoke_reclaims_a_session_the_client_never_closed(self):
        """The half ``sweep_terminated`` structurally cannot reach.

        Measured: a session closed by ``DELETE`` is flagged terminated and
        kept; one whose client simply vanished is not flagged at all. A
        killed ``agy`` is the second kind, so the sweep steps over exactly
        the case the host most wants collected, and only the four-hour idle
        timer would ever take it.
        """
        async with running(_factory()) as (listener, url, token):
            async with httpx.AsyncClient(timeout=30) as client:
                headers = await _raw_session(client, url, token)
                session_id = headers["mcp-session-id"]
                assert session_id in listener.sessions
                assert listener.sessions[session_id].is_terminated is False
                # What the sweep alone would have achieved: nothing.
                assert listener.sweep_terminated() == 0
                assert session_id in listener.sessions

                await listener.revoke(token)
                assert session_id not in listener.sessions
                assert session_id not in listener._mcp.session_manager._session_owners

    async def test_a_second_session_under_one_bearer_is_announced(self, caplog):
        """The tripwire behind the ``server/discover`` filter.

        That filter matches on a header ``agy`` happens to send today. If a
        later version stops sending it, the filter silently stops firing
        and the transport leak comes back — quietly, which is the way this
        module has been bitten before. One spawn opens one session, so a
        second one under the same bearer says so out loud.
        """
        async with running(_factory()) as (listener, url, token):
            async with httpx.AsyncClient(timeout=30) as client:
                with caplog.at_level("WARNING"):
                    first = await _raw_session(client, url, token)
                    assert not [r for r in caplog.records if "MCP sessions" in r.message]
                    second = await _raw_session(client, url, token)

                assert first["mcp-session-id"] != second["mcp-session-id"]
                grant = next(iter(listener._grants.values()))
                assert len(grant.sessions) == 2
                tripped = [r for r in caplog.records if "MCP sessions" in r.message]
                assert tripped and DISCOVER_METHOD in tripped[0].getMessage()
