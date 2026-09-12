"""The authenticated HTTP MCP listener that makes ``claude`` a consultant.

``specs5/plan-ag/decisions.md`` AG-1 wants both directions and only ever
had one: ``agy`` can be consulted by a Claude master through an in-process
SDK MCP tool, and ``claude`` could not be consulted by anything, because
``agy`` reaches outside tools over MCP and nothing here spoke it. This
module is the other direction — one ``second_opinion`` tool, served over
authenticated HTTP on loopback, from *this* process.

Serving it from this process is the decision AG-22 turned on, and the
reason is the credential rather than the convenience: this process is the
sole holder of a single-use refresh token, R-14's lock is an in-process
mutex, and a separately spawned MCP server would have been a second holder
that no in-process lock could reach.

Five things here are measured rather than chosen, and each has a comment
where it is used rather than only an entry in the register:

- **Stateful MCP, never stateless.** ``stateless_http=True`` issues no
  session id, so a cancellation arriving on its own POST has no session in
  which to find the in-flight request. Measured: the notification is 202'd
  and dropped, the tool runs on, and the original request is never
  answered at all. Stateful routes it, cancels the handler immediately,
  and completes the original request with a proper JSON-RPC error.
- **A socket drop is not a cancellation.** Also measured, and it is what
  makes the point above safe: in stateful mode the session-manager
  transport outlives any single POST, so a dropped connection leaves the
  consultation running. Only the explicit notification stops it.
- **Uvicorn embedded, with its signal capture suppressed.**
  ``Server.serve()`` installs its own SIGINT and SIGTERM handlers whenever
  it is on the main thread, replacing this application's for the
  listener's whole lifetime.
- **A pre-bound socket.** The port is known synchronously, before the
  serve task is scheduled, so the config naming it cannot be written
  before the port exists.
- **The caller is identified by the credential, never by the model.** The
  token resolves to a repository. AG-R-3 is about writes escaping the
  repository on a model's answer to exactly that question.

Governing decisions: AG-1, AG-16, AG-21, AG-22, AG-24, AG-25.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Imported at module scope rather than lazily: both are hard requirements
# of ``mcp``, which is a hard requirement of ``claude-agent-sdk``, which
# is a base dependency. Deferring them would be guarding against an
# install that cannot exist.
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from aic_dc.claude_code.consultant import ConsultationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The name ``agy`` files this server under in ``mcp_config.json``, and the
#: first half of the allow-rule grammar ``mcp(<server>/<tool>)``. A rename
#: here is a rename in every allow-rule and in every hook payload, which
#: arrive as ``call_mcp_tool`` with this string in ``args.ServerName``.
SERVER_NAME = "aic-dc-claude"

#: Streamable HTTP is mounted here by the SDK's own app factory. The path
#: is part of the URL written into the config, so it is named once.
MCP_PATH = "/mcp"

#: Loopback, always. There is no deployment of this that wants a second
#: machine to reach an endpoint that spends the account's subscription.
HOST = "127.0.0.1"

#: How many consultations one turn may spend.
#:
#: **Two, not one, and the shape is the argument.** One is not a budget,
#: it is a structural refusal, and it breaks the pattern a master
#: legitimately has: consult on the approach, implement, then consult on
#: the diff. Under a limit of one the second call is refused and the user
#: has to send a message they do not otherwise need, purely to advance a
#: counter. Two gives identical protection against the thing actually
#: being guarded — a confused model calling the tool in a tight loop —
#: because both limits stop that at the same place.
#:
#: What this does *not* bound is cost: one consultation carrying a large
#: diff is dearer than three narrow ones. This is a loop guard, and
#: calling it a budget would be claiming a protection it does not provide.
TURN_BUDGET = 2

#: How many times a turn may *attempt* a consultation, successes included.
#:
#: The budget above counts answers, and a consultation that failed did not
#: produce one — charging a turn for the network having a bad second would
#: lock a master out of a tool that never ran. But refunding without limit
#: hands back the loop guard, because a model that retries an error is the
#: loop. Two counters, so a transient failure costs nothing and a
#: deterministic one still terminates.
TURN_ATTEMPTS = TURN_BUDGET + 2

#: How long a session may sit with no traffic before the SDK reclaims it.
#:
#: **Hours, and the two bounds are both measured.** The lower bound is
#: P1: one ``agy`` process idled 900 seconds between turns with no traffic
#: whatsoever and then reused the same session id, so minutes are far too
#: short. The upper bound is M12: ``agy`` never rebuilds a session the
#: server has forgotten — it reports ``session not found`` and gives up —
#: so a TTL that fires inside a live conversation kills the consultant for
#: the rest of it.
#:
#: A TTL is needed at all because of P8: the SDK reclaims a session in
#: exactly one place, the idle path, so with no timeout the rejected
#: ``server/discover`` that ``agy`` opens on every spawn accumulates a
#: transport that is never collected. Four hours is past any gap a live
#: conversation plausibly has and still reclaims an abandoned one the same
#: day. The cost is stated rather than hidden: a conversation left idle
#: longer than this loses its consultant, loudly, until the engine is
#: restarted.
SESSION_IDLE_TIMEOUT = 4 * 60 * 60

#: Said to the model when the budget is gone. Instructional rather than
#: bare, because measured (E1) it is the prose and not the ``isError`` flag
#: that decides whether ``agy`` retries — so this string is the only thing
#: standing between a spent budget and a retry loop.
BUDGET_SPENT = (
    "Consultation budget for this turn is spent ({spent}/{budget}). This is "
    "a per-turn limit, not a failure: proceed with your own reasoning and "
    "answer the user. Do not retry this tool."
)

#: Said when a call arrives with no turn open. Not a normal condition —
#: every consultation should sit inside a turn this host dispatched — so
#: it names the state rather than inviting a retry.
NO_ACTIVE_TURN = (
    "No turn is open on this session, so a consultation cannot be "
    "attributed to one. Nothing was spent. This is a fault in the host, "
    "not in your request."
)

#: Said when the bearer resolves to nothing. Reachable only by a caller
#: presenting a revoked or forged token, so it says nothing useful about
#: why.
UNKNOWN_CALLER = "Unauthorized."

#: Said when the user stopped the turn mid-consultation.
STOPPED = "The consultation was stopped by the user before it finished."

#: Said to a subagent. AG-R-24 required that a delegate not spend the
#: master's budget, and this module's first cut recorded that as
#: unbuildable — a bearer names a session, not which agent inside it is
#: asking. Measured otherwise (S1): ``agy`` puts the calling agent's own
#: conversation id in ``params._meta`` and, *only for a subagent*, its
#: parent's alongside it. Instructional rather than bare, for the same
#: reason as :data:`BUDGET_SPENT`: the delegate should report upward, not
#: retry.
SUBAGENT_REFUSED = (
    "Consultations are reserved for the agent that owns the turn, and this "
    "call came from a subagent. Nothing was spent. Answer from your own "
    "reasoning and say in your report that a second opinion was not "
    "available to you, so the agent that delegated to you can seek one."
)

#: The ``params._meta`` key ``agy`` sets *only* on a subagent's call —
#: measured on a real ``start_subagent`` delegation, where the master's own
#: call to the same tool on the same MCP session id carried
#: ``conversation_id`` and no parent, and the delegate's carried both.
#: Namespaced by the vendor, so it cannot collide with a key of ours.
SUBAGENT_META_KEY = "antigravity.google/parent_conversation_id"

#: The HTTP header ``agy`` sets on the session-less ``server/discover``
#: POST it opens every spawn with. Answering that one request at the gate
#: is what keeps it from allocating a transport — see :class:`_BearerGate`.
METHOD_HEADER = "mcp-method"
DISCOVER_METHOD = "server/discover"


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


@dataclass
class _Grant:
    """What one bearer token buys, and what it has spent.

    ``repo_root`` is here because **the model is never asked which
    workspace it is in** (AG-R-3). The token carries it, so two
    repositories consulting at once are two tokens and neither can answer
    the question wrongly.

    ``turn_id`` is pushed in by the host at the moment it dispatches a
    turn, and is ``None`` between turns. It cannot be inferred from the
    request: a token is minted per ``agy`` spawn and that spawn is
    explicitly *one process across many turns*, so every turn presents the
    same bearer. A server keying a budget on the token alone would let
    turn one spend it and refuse every turn after, permanently.
    """

    repo_root: Path
    session_id: str
    turn_id: str | None = None
    spent: int = 0
    attempts: int = 0
    in_flight: set[_InFlight] = field(default_factory=set)
    #: Every MCP session id seen on this bearer, recorded by the gate.
    #:
    #: Here because the SDK's own reclamation cannot be relied on and its
    #: gap is precisely the case this host cares about. Measured (U1): a
    #: session closed by ``DELETE`` is marked terminated and then kept, and
    #: one whose client simply *vanished* — which is what a killed ``agy``
    #: looks like on the wire — is not marked at all, so
    #: :meth:`ConsultationListener.sweep_terminated` steps over it and only
    #: the four-hour idle timer ever collects it. The host knows the
    #: subprocess died the moment it dies; this is what lets it act on that.
    sessions: set[str] = field(default_factory=set)
    #: Per grant, not per listener. The check-and-set it guards is
    #: in-memory and brief, but a listener-wide lock would make one
    #: repository's consultation wait behind another's for no reason.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(eq=False)
class _InFlight:
    """One running consultation, and how it came to stop.

    The consultant is held, not just its task, because ``ClaudeConsultant``
    has its own ``cancel()`` that sets a flag before cancelling — which is
    what makes it raise a typed ``ConsultationError`` instead of a bare
    ``CancelledError``. Going through it rather than round it is what lets
    this module tell "the user stopped this" apart from "something
    cancelled *me*", and those two must never be answered the same way.
    """

    consultant: Any
    task: asyncio.Task[Any]
    stopped_by_host: bool = False


class ConsultationListener:
    """One listener, many tokens, for the lifetime of this process.

    Not one listener per ``agy`` spawn: the port is written into each
    spawn's own config anyway, so a second socket would buy nothing and
    cost a shutdown path per spawn.
    """

    def __init__(
        self,
        consultant_factory: Callable[[], Any],
        *,
        budget: int = TURN_BUDGET,
        attempts: int = TURN_ATTEMPTS,
        idle_timeout: float = SESSION_IDLE_TIMEOUT,
        host: str = HOST,
    ) -> None:
        #: Called with no arguments for each consultation. A factory
        #: rather than one shared consultant because ``ClaudeConsultant``
        #: holds the running task on itself for its own ``cancel()``, so a
        #: shared instance would let one session's stop reach another's
        #: consultation.
        self._consultant_factory = consultant_factory
        self._budget = int(budget)
        self._attempts = max(int(budget), int(attempts))
        self._host = host
        self._idle_timeout = float(idle_timeout)
        self._grants: dict[str, _Grant] = {}
        self._mcp: Any = None
        self._server: Any = None
        self._serving: asyncio.Task[Any] | None = None
        self._sock: socket.socket | None = None
        self._port: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def port(self) -> int | None:
        """The bound port, or ``None`` before :meth:`start`."""
        return self._port

    async def start(self) -> int:
        """Bind, serve, and return the port. Idempotent."""
        if self._port is not None:
            return self._port

        # **Bound before anything is scheduled.** With ``port=0`` handed to
        # uvicorn instead, the port is not knowable until its startup has
        # run, and the config naming it is written at spawn time — so the
        # obvious ordering is a race whose losing side is a config file
        # pointing at port 0.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        self._sock = sock
        self._port = sock.getsockname()[1]

        config = uvicorn.Config(
            self._build_app(),
            log_level="error",
            # Uvicorn's access handler streams to ``ext://sys.stdout``
            # — measured — so at any level below ``error`` a line per
            # request lands on this process's standard output. Switched
            # off here rather than relied on through ``log_level``,
            # because a later reader raising the level to debug something
            # would silently bring the noise back.
            access_log=False,
        )
        self._server = _EmbeddedServer(config)
        self._serving = asyncio.create_task(
            self._server.serve(sockets=[sock]), name="aic-dc-consult-listener"
        )
        while not self._server.started:  # pragma: no branch - startup is fast
            if self._serving.done():
                # Surfaces a bind or lifespan failure as itself rather than
                # as a hang that nothing explains.
                await self._serving
                raise RuntimeError("The consultation listener stopped during startup")
            await asyncio.sleep(0.01)
        logger.info("Consultation listener serving on %s:%d", self._host, self._port)
        return self._port

    async def aclose(self) -> None:
        """Stop serving and cancel anything still in flight. Never raises."""
        for token in list(self._grants):
            await self.revoke(token)
        if self._server is not None:
            # Suppressing signal capture means uvicorn will never act on a
            # signal itself, so the only thing that stops it is this.
            self._server.should_exit = True
        if self._serving is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._serving, timeout=10)
            if not self._serving.done():  # pragma: no cover - slow shutdown
                self._serving.cancel()
        self._server = None
        self._serving = None
        self._port = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _build_app(self) -> Any:
        # Stateful. See the module docstring: this single argument is what
        # makes cancellation route at all.
        mcp = FastMCP(SERVER_NAME, stateless_http=False)

        # **Annotated ``-> CallToolResult``, and that is load-bearing.**
        # The refusals below have to travel with ``isError`` set correctly,
        # and returning a ``CallToolResult`` from a handler annotated
        # ``-> str`` is measured to *destroy the text*: FastMCP derives an
        # ``outputSchema`` from the annotation, the result validates
        # against it, and what reaches the model is a pydantic error where
        # the carefully worded instruction should have been. Annotating it
        # this way emits no ``outputSchema`` at all — measured — and both
        # paths arrive verbatim. The input schema is unchanged.
        @mcp.tool()
        async def second_opinion(ctx: Context, question: str, context: str = "") -> CallToolResult:
            """Ask Claude, running as an independent agent, to review a
            question you have already formed a view on — a diff, a design
            choice, a diagnosis. It answers from what you pass and has no
            access to the repository, so include the code it needs. It
            keeps no memory between calls.
            """
            return await self._consult(ctx, question, context)

        app = mcp.streamable_http_app()
        # Kept because the SDK's session table is the only place that knows
        # how many transports are alive, and a listener that cannot answer
        # that cannot be shown not to leak.
        self._mcp = mcp
        # Set here rather than passed to ``FastMCP``, which has no
        # settings key for it, and set before any traffic because the
        # deadline is read when a session is created.
        mcp.session_manager.session_idle_timeout = self._idle_timeout
        # Both tables this module reclaims from are private to the SDK. A
        # rename in a later release would turn eviction into a silent
        # no-op, which is the one failure this module keeps being bitten
        # by, so the check is made once here where saying so still helps.
        absent = [
            name
            for name in ("_server_instances", "_session_owners")
            if not hasattr(mcp.session_manager, name)
        ]
        if absent:  # pragma: no cover - a future SDK, not a reachable state
            logger.warning(
                "The MCP SDK no longer exposes %s: consultation transports "
                "cannot be reclaimed and will accumulate for the life of "
                "the host.",
                ", ".join(absent),
            )
        app.add_middleware(_BearerGate, listener=self)
        return app

    @property
    def sessions(self) -> dict[str, Any]:
        """The SDK's live transports, keyed by MCP session id."""
        if self._mcp is None:
            return {}
        return getattr(self._mcp.session_manager, "_server_instances", {})

    def _forget(self, sid: str) -> None:
        """Drop one session from both of the SDK's private tables.

        Guarded, because a failed lookup here would otherwise propagate out
        of a subprocess teardown; announced at startup rather than silently,
        because a reclamation that quietly stops happening is a leak that
        says nothing.
        """
        self.sessions.pop(sid, None)
        owners = getattr(getattr(self._mcp, "session_manager", None), "_session_owners", None)
        if owners is not None:
            owners.pop(sid, None)

    # ------------------------------------------------------------------
    # Tokens and turns — all driven by the host, never by the model
    # ------------------------------------------------------------------

    def mint(self, *, repo_root: Path | str, session_id: str) -> str:
        """Issue a token for one ``agy`` spawn, and return it."""
        self.sweep_terminated()
        token = secrets.token_urlsafe(32)
        self._grants[token] = _Grant(repo_root=Path(repo_root), session_id=session_id)
        return token

    async def revoke(self, token: str) -> None:
        """Drop a token and stop anything it still has running.

        Called when the ``agy`` child exits, however it exits. Lifetime is
        bound to the subprocess and to nothing else — in particular not to
        an idle timer, because ``agy`` was measured not to recover from a
        session the server has forgotten: it reports ``session not found``
        and never re-initialises, so reaping an idle session breaks the
        consultant for the rest of a conversation the user still has open.
        """
        grant = self._grants.pop(token, None)
        if grant is None:
            return
        await _cancel_all(grant)
        await self._evict(grant.sessions)
        self.sweep_terminated()

    async def _evict(self, session_ids: set[str]) -> None:
        """Terminate and forget named transports, whatever their flags say.

        The sweep below can only collect what the SDK has marked, and a
        child that died without closing its session marks nothing (U1).
        This is the other half: the host knows which sessions belonged to
        the subprocess it just buried, so it does not have to infer
        anything. ``terminate()`` is idempotent, so the two halves may
        overlap.
        """
        for sid in list(session_ids):
            transport = self.sessions.get(sid)
            if transport is not None:
                with contextlib.suppress(Exception):
                    await transport.terminate()
            self._forget(sid)
        session_ids.clear()

    def sweep_terminated(self) -> int:
        """Drop transports the SDK has finished with but still holds.

        Measured, and it is the tidy path that leaks: the SDK removes a
        session from its table in exactly one place, when the idle scope
        fires, and its other cleanup is guarded by ``not is_terminated``.
        So a session closed properly by ``DELETE /mcp`` — which ``agy``
        sends on every clean exit — is marked terminated and then kept
        forever, while the abandoned ones the timeout was written for are
        the ones that do get collected. Ten spawns left ten dead entries
        that no timeout will ever reach.

        Swept at the spawn boundaries rather than on a timer, because that
        is where the leak is generated, so the table stays flat across a
        long-lived host without anything having to wake up.
        """
        dead = [sid for sid, t in self.sessions.items() if getattr(t, "is_terminated", False)]
        for sid in dead:
            self._forget(sid)
        if dead:
            logger.debug("Swept %d terminated MCP session(s)", len(dead))
        return len(dead)

    def begin_turn(self, token: str, turn_id: str) -> None:
        """Open a turn and reset its budget.

        Pushed by the host at the moment it writes the prompt, because
        that is the only place the turn's identity exists. Nothing about
        the incoming request carries it.
        """
        grant = self._grants.get(token)
        if grant is None:
            return
        grant.turn_id = turn_id
        grant.spent = 0
        grant.attempts = 0

    def end_turn(self, token: str, turn_id: str) -> None:
        """Close the named turn. A consultation arriving after this is refused.

        Cleared rather than left standing, so that a late call — one
        ``agy`` dispatches after the host considers the turn over — cannot
        quietly spend the *next* turn's budget before it opens.

        The turn is named rather than assumed, and that is the whole
        reason ``turn_id`` is a string and not a flag. The host calls this
        from a ``finally``, so a slow turn one can close after turn two has
        already opened; closing whatever happens to be current would leave
        turn two unable to consult at all, with nothing to say why.
        """
        grant = self._grants.get(token)
        if grant is not None and grant.turn_id == turn_id:
            grant.turn_id = None

    async def cancel_session(self, session_id: str) -> bool:
        """Stop consultations belonging to one ``agy`` session. Returns whether any were.

        **This is the only thing that can stop a consultation the user has
        stopped**, and it exists because of a measurement. The host's ⏹ is
        not a process kill: it sets a latch the gate reads, which refuses
        *subsequent* tool calls and answers ``terminate`` at the next
        ``PostInvocation``. Around a 45-second MCP call the hook timeline
        is ``PreToolUse`` at the start, then nothing at all until
        ``PostInvocation`` when it returns. The gate is blind for the whole
        call, so the stop is queued behind the very thing it is stopping —
        and ``agy`` sends its own cancellation only at its 3m0s deadline,
        and a dropped socket does not cancel either. Left to the wire, a
        stopped turn keeps spending the subscription on an answer nobody
        will read, which is the failure AG-22 rejected the ``run_command``
        design for, arriving on the transport that replaced it.
        """
        cancelled = False
        for grant in self._grants.values():
            if grant.session_id == session_id:
                cancelled |= await _cancel_all(grant)
        return cancelled

    def config_entry(self, token: str) -> dict[str, Any]:
        """This server as ``agy``'s ``mcp_config.json`` wants it.

        ``serverUrl`` rather than ``url`` — measured; ``url`` is not read
        — with the credential in a sibling ``headers`` object.
        """
        if self._port is None:
            raise RuntimeError("The consultation listener is not running")
        return {
            "mcpServers": {
                SERVER_NAME: {
                    "serverUrl": f"http://{self._host}:{self._port}{MCP_PATH}",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }

    def resolve(self, token: str | None) -> _Grant | None:
        """The grant a bearer buys, compared in constant time."""
        if not token:
            return None
        for known, grant in self._grants.items():
            if hmac.compare_digest(known, token):
                return grant
        return None

    # ------------------------------------------------------------------
    # The consultation
    # ------------------------------------------------------------------

    async def _consult(self, ctx: Any, question: str, context: str) -> CallToolResult:
        """Serve one ``second_opinion`` call."""
        # **Read off this request, not out of a variable.** The handler
        # runs in the MCP session's task, not the HTTP request's, so a
        # token captured by middleware into a context variable is a copy
        # whose provenance is emergent. ``request_context.request`` is the
        # request that actually arrived, and its header is the credential
        # that was actually presented on it.
        request = getattr(ctx.request_context, "request", None)
        header = ""
        if request is not None:
            header = request.headers.get("authorization", "")
        grant = self.resolve(header.removeprefix("Bearer ").strip())
        if grant is None:
            return _no_answer(UNKNOWN_CALLER)

        # **Before the budget, because a subagent must not spend an
        # attempt either.** ``agy`` namespaces the calling agent's own
        # conversation id into ``params._meta`` and adds its parent's only
        # when the caller is a delegate — so the presence of the parent key
        # *is* the signal, and the master's own call on the very same MCP
        # session id does not carry it. Read from the request that arrived
        # rather than from anything the model wrote: ``_meta`` is set by
        # the CLI harness, not by the agent.
        if _is_subagent(ctx):
            return _no_answer(SUBAGENT_REFUSED)

        async with grant.lock:
            if grant.turn_id is None:
                return _no_answer(NO_ACTIVE_TURN)
            if grant.spent >= self._budget or grant.attempts >= self._attempts:
                return _no_answer(BUDGET_SPENT.format(spent=grant.spent, budget=self._budget))
            # Counted inside the lock with the check, so two calls that did
            # race could not both read an unspent budget. Attempts are
            # charged here and never given back; the answer counter below
            # is what a failure refunds.
            grant.attempts += 1
            grant.spent += 1
        # The refunds below run without the lock deliberately. They are a
        # single decrement with no await inside, so nothing can interleave
        # with them — and one of them sits on the cancelled path, where
        # acquiring a lock would re-raise instead of refunding.

        consultant = self._consultant_factory()
        flight = _InFlight(
            consultant=consultant,
            task=asyncio.ensure_future(consultant.second_opinion(question, context)),
        )
        grant.in_flight.add(flight)
        try:
            return _answer(await flight.task)
        except ConsultationError as exc:
            # The consultant's own vocabulary, and the stop path arrives
            # here: :meth:`ClaudeConsultant.cancel` flags itself before
            # cancelling, so it converts its own cancellation into this
            # rather than letting a bare ``CancelledError`` escape.
            grant.spent -= 1
            return _no_answer(STOPPED if flight.stopped_by_host else str(exc))
        except asyncio.CancelledError:
            # **Never swallowed.** Reaching here means something cancelled
            # *this handler* — `agy`'s own `notifications/cancelled` at its
            # deadline, or ASGI teardown — and answering it with a cheerful
            # 200 would tell the caller its cancellation failed.
            #
            # Nothing in this branch may await. Once the enclosing cancel
            # scope has fired, the next await point re-raises before its
            # body runs, so an awaited tidy-up here is tidy-up that
            # silently does not happen.
            if not flight.task.done():
                flight.task.cancel()
            grant.spent -= 1
            raise
        except Exception as exc:  # noqa: BLE001 - a failed consultation is a result
            logger.exception("A consultation failed")
            grant.spent -= 1
            return _no_answer(f"The consultation could not be completed: {exc}")
        finally:
            grant.in_flight.discard(flight)
            if not flight.task.done():  # pragma: no cover - belt and braces
                flight.task.cancel()


def _answer(text: str) -> CallToolResult:
    """A consultation that happened, delivered. The only unflagged path.

    ``isError`` answers exactly one question — *did this call produce the
    thing it was asked for?* — and only a review does. A budget refusal, a
    subagent refusal, a missing turn, a forged bearer, a crash and a user
    stop all answer no, and all travel through :func:`_no_answer`.
    """
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def _no_answer(text: str) -> CallToolResult:
    """Everything else, flagged so it cannot be read as a consultation.

    An earlier cut split this the other way, policy plain and failures
    flagged, on the theory that a flag would provoke a retry. Two things
    sank it.

    Measured (E1): the flag makes no difference to whether ``agy`` retries.
    A control run and an armed run, same prompt, same refusal text, the flag
    the only variable — **one tool call in each**, both models reporting the
    refusal plainly and then answering from their own reasoning. So the
    instruction in the prose is what stops a retry, not the status, and the
    flag is free to carry its actual meaning. The measurement's own limit is
    worth keeping: one run per arm, one model. It shows the flag cost
    nothing here, not that no client anywhere retries on it — which is why
    the steering prose stays on every path.

    And the split it replaced could not be stated without contradicting
    itself: a call with no turn open was flagged while a call with no budget
    left was not, though both are the same kind of refusal from the same
    kind of precondition. What ``isError=False`` asserts is *this string is
    the second opinion you asked for*. Said of a policy notice, that puts
    the notice into the transcript as the consultant's advice, and anything
    downstream asking whether a second opinion was obtained would find one.
    """
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


def _is_subagent(ctx: Any) -> bool:
    """Whether this call came from a delegate rather than the turn's owner.

    Measured on a real ``start_subagent`` delegation: the subagent's
    ``tools/call`` reached the same MCP session id, over the same bearer,
    with the same HTTP headers — nothing at the transport distinguished it.
    The one difference was in ``params._meta``, where the master's call
    carried ``conversation_id`` alone and the delegate's carried
    ``parent_conversation_id`` beside it.

    Absent metadata means *not* a subagent, deliberately. Every other MCP
    client sends no ``_meta`` at all, and refusing them would be refusing
    the master.
    """
    meta = getattr(getattr(ctx, "request_context", None), "meta", None)
    extra = getattr(meta, "model_extra", None) or {}
    return bool(extra.get(SUBAGENT_META_KEY))


async def _cancel_all(grant: _Grant) -> bool:
    """Stop every consultation a grant has in flight."""
    flights = [f for f in grant.in_flight if not f.task.done()]
    for flight in flights:
        await _stop(flight)
    grant.in_flight.clear()
    return bool(flights)


async def _stop(flight: _InFlight) -> None:
    """Stop one consultation through the consultant's own door.

    Not ``task.cancel()``: the consultant flags itself first, which is
    what turns the cancellation into a ``ConsultationError`` the handler
    can distinguish from being cancelled itself. Cancelling round it would
    tear down the same subprocess and lose that distinction.
    """
    flight.stopped_by_host = True
    with contextlib.suppress(Exception):
        await flight.consultant.cancel()
    if not flight.task.done():  # pragma: no cover - cancel() covers this
        flight.task.cancel()


# ---------------------------------------------------------------------------
# The HTTP edge
# ---------------------------------------------------------------------------


class _EmbeddedServer(uvicorn.Server):
    """Uvicorn with its signal capture removed.

    ``Server.serve()`` wraps itself in ``capture_signals()``, which calls
    ``signal.signal()`` for SIGINT and SIGTERM whenever it is on the main
    thread. Measured: embedding it in this application's own loop replaces
    this application's handlers with uvicorn's for the listener's whole
    lifetime, so Ctrl-C stops meaning what the rest of the program says it
    means. The cost of removing it is that uvicorn no longer stops itself
    — :meth:`ConsultationListener.aclose` is what stops it.
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


class _BearerGate(BaseHTTPMiddleware):
    """Bearer on ``/mcp``; everything else is not this server's business.

    Two paths, deliberately separated. ``agy`` probes
    ``/.well-known/oauth-protected-resource`` — twice, and with no
    ``Authorization`` header, which is correct of it: RFC 9728 discovery
    happens *before* a client presents a credential, so that a token is
    not leaked to an endpoint that turns out not to want one. Answering a
    bearer challenge there is a category error, since the probe is a REST
    GET and not a JSON-RPC call. It is answered 404, which says plainly
    that this server does not do OAuth discovery and the static
    configuration is the whole story. Measured: 404 and 401 are equally
    well tolerated, and neither delays the handshake.
    """

    def __init__(self, app: Any, listener: ConsultationListener) -> None:
        super().__init__(app)
        self._listener = listener

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith(MCP_PATH):
            return PlainTextResponse("Not Found", status_code=404)
        header = request.headers.get("authorization", "")
        grant = self._listener.resolve(header.removeprefix("Bearer ").strip())
        if grant is None:
            # A JSON-RPC shaped error, because on this path the caller is
            # a JSON-RPC client and a bare body would give it nothing to
            # report.
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Unauthorized"},
                },
                status_code=401,
            )

        # ``server/discover`` is answered here and never forwarded, which
        # is the whole of the fix for one of the two leaks P8 found. The
        # SDK allocates a transport for *every* session-less POST before it
        # decodes the body, then rejects this one 400 — leaving an entry no
        # client can ever DELETE. Measured (G1): answered at the gate,
        # ``agy`` proceeds straight to ``initialize`` and one spawn leaves
        # one transport where it used to leave two.
        #
        # On the header rather than the body, because reading a request
        # body inside ``BaseHTTPMiddleware`` and then calling through is a
        # known way to wedge the downstream app. ``agy`` sends its
        # JSON-RPC method in this header on every request, so nothing has
        # to be parsed. If a later version stops, this stops firing and the
        # behaviour degrades to the leak the idle timer already covers.
        if request.headers.get(METHOD_HEADER, "").strip() == DISCOVER_METHOD:
            return PlainTextResponse("Not Found", status_code=404)

        response = await call_next(request)
        # Recorded from whichever side of the exchange carries it: the
        # request header on every later call, and the *response* header on
        # ``initialize``, which is the one that creates the session.
        session_id = request.headers.get("mcp-session-id") or response.headers.get("mcp-session-id")
        if session_id and session_id not in grant.sessions:
            grant.sessions.add(session_id)
            # And this set is the tripwire for the filter above. One spawn
            # is measured to open exactly one session, so a second one
            # under the same bearer means the filter stopped matching — a
            # later ``agy`` that no longer sends the header — or that
            # ``agy`` re-initialised. Both are worth hearing about at the
            # moment they happen rather than inferring, months later, from
            # a transport count that grew.
            if len(grant.sessions) > 1:
                logger.warning(
                    "The bearer for agy session %s now holds %d MCP sessions; "
                    "one spawn should open one. The %r filter may no longer "
                    "be matching.",
                    grant.session_id,
                    len(grant.sessions),
                    DISCOVER_METHOD,
                )
        return response
