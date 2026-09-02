"""Session lifecycle for the Antigravity engine.

The Antigravity half of ``claude_code/session.py``, and deliberately a
much smaller one: this is the phase-3 spike. It starts a harness, holds a
``Conversation``, sends a prompt, pumps ``receive_steps()`` through
:class:`~aic_dc.antigravity.steps.StepTranslator`, and cancels. It is not
registered with the RPC service and nothing in the webapp reaches it.

``Agent`` for the lifetime, ``Conversation`` for the turn
---------------------------------------------------------
``Agent.__aenter__`` is not just sugar. It builds the ``HookRunner`` and
registers ``config.hooks`` into it, builds the ``ToolRunner`` for AG-4's
callables, and — the part worth keeping most — performs the write-tools-
without-a-policy check that refuses to start a config where a write tool
has nothing gating it (``agent.py:93-103``). Going under it to
``config.create_strategy(tool_runner=…, hook_runner=…)`` would mean
reimplementing all three, and the third is the SDK enforcing AG-5 for us.

So the session owns an ``Agent`` for its lifetime and then drives
``agent.conversation`` for each turn. That is the SDK's own sanctioned
path — the property's docstring says it is *"for advanced session
introspection: history, turn count, compaction indices, usage, or direct
send/receive_steps control"* — and it is where AG-R-9's boundary actually
sits. The risk that register entry names is not "importing ``Agent``"; it
is an adapter shaped by the consultant's *call pattern*: one shot, no
resume, no permissions, no streaming. ``chat()`` is that pattern, and
``chat()`` is what this module does not call.

The lazy-stream trap, which cost a hang once already
----------------------------------------------------
``chat()`` returns a ``ChatResponse`` that is a *cursor over a stream
nothing has pulled*. Phase 1 handed one back to a caller who
awaited ``.text()`` after the context manager had torn the connection
down; it hung until killed, and the ``asyncio.timeout`` did not fire
because it wrapped *starting* the agent rather than the model work.

This module never constructs one. :meth:`AntigravitySession.run_turn`
drives ``send()`` and ``receive_steps()`` itself, so the work happens
inside the loop that is being timed and inside the lifetime that owns the
connection. The timeout wraps the pump, which is the part that can hang.

Cancellation
------------
``conversation.cancel()`` sends ``halt_request`` and the step iterator
ends. :meth:`AntigravitySession.cancel` is safe to call when no turn is
running and safe to call twice, because the caller is a browser button and
neither of those is a programming error there.

Governing spec: ``specs5/plan-ag/`` — AG-2, AG-3, AG-5, AG-10, AG-R-9.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from aic_dc.antigravity import options
from aic_dc.antigravity.credentials import Credentials
from aic_dc.antigravity.credentials import resolve as resolve_credentials
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

#: How long one turn may run before the pump abandons it.
#:
#: Generous, because an agentic turn is many model calls and the free tier
#: throttles at 5 RPM — phase 1 watched the SDK retry through a 429
#: mid-turn without the caller seeing anything, which is the behaviour a
#: short timeout would turn into a spurious failure.
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

#: Emit callback: one server-push per event. ``None`` drops them, which is
#: what the smoke test uses before there is a browser to push to.
Emit = Callable[[Event], Awaitable[None]] | None


class SessionNotStartedError(RuntimeError):
    """A turn was requested before the harness was started."""


class TurnInProgressError(RuntimeError):
    """A second turn was requested while one was still running.

    Named rather than queued. ``Conversation.send`` *does* handle it — it
    drains the in-flight turn into history first — but doing so silently
    would mean a user's second prompt arrives with the first turn's tool
    calls half-rendered and no event saying why. The Claude session raises
    the same way, for the same reason.
    """


class AntigravitySession:
    """One Antigravity conversation and the pump that renders it.

    Parameters
    ----------
    repo_root:
        The single workspace root (AG-10).
    credentials:
        Resolved once at construction so a missing key is reported when
        the session is created rather than mid-turn. ``None`` resolves
        from the environment and the key file.
    decide_hook:
        The AG-5 permission gate. **Without one this session is
        read-only** — not gated-then-denied, but with no mutating tool
        enabled at all, which is what ``options.build_config_kwargs``
        enforces. Phase 4 supplies the real hook.
    tools:
        Plain Python callables for the symbol and document indexes
        (AG-4). Empty in phase 3.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        credentials: Credentials | None = None,
        model: str = options.DEFAULT_MODEL,
        decide_hook: Any = None,
        tools: tuple[Any, ...] = (),
        write_tools: frozenset[str] | None = None,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._credentials = credentials or resolve_credentials()
        self._model = model
        self._decide_hook = decide_hook
        self._tools = tools
        self._write_tools = write_tools
        self._turn_timeout = turn_timeout

        self._agent: Any = None
        self._conversation: Any = None
        self._exit: Any = None
        self._turn_active = False

    # ------------------------------------------------------------------
    # Introspection — safe before start, and the reason it is separate
    # ------------------------------------------------------------------

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    @property
    def started(self) -> bool:
        return self._conversation is not None

    @property
    def read_only(self) -> bool:
        """Whether this session can change the working tree.

        A property rather than a comment because it is the fact the
        capability descriptor will report (AG-3) and the fact a reviewer
        checks first: a session with no decide hook cannot write, and
        that is enforced in :func:`options.build_config_kwargs` rather
        than promised here.
        """
        return self._decide_hook is None

    def config_kwargs(self) -> dict[str, Any]:
        """What this session would ask the SDK for, without asking it.

        Public because it is the honest way to test the posture — the
        assertion that matters is "no write tool without a hook", and it
        should not need a 119 MB binary to make.
        """
        return options.build_config_kwargs(
            repo_root=self._repo_root,
            credentials=self._credentials,
            model=self._model,
            decide_hook=self._decide_hook,
            tools=self._tools,
            write_tools=self._write_tools,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn ``localharness`` and open a conversation.

        ``validate_endpoint()`` raises on the connect path, so a session
        without a usable credential fails here rather than lazily — which
        is the behaviour AG-R-8 wants, since the most likely first
        experience of this engine is a credential that does not exist yet.

        The context manager is entered by hand and its exit callback kept,
        because a session's lifetime is bounded by the user closing it
        rather than by a lexical block. That is the one place this module
        does something the SDK's own examples do not, so it is done in one
        pair of methods and nowhere else.
        """
        if self._conversation is not None:
            return
        from google.antigravity import Agent

        self._credentials.require()
        config = options.build_config(**self.config_kwargs())

        stack = contextlib.AsyncExitStack()
        try:
            agent = await stack.enter_async_context(Agent(config))
        except BaseException:
            # Including CancelledError: a half-started harness is a live
            # subprocess, and the exit stack is the only thing that knows
            # how to reap it.
            await stack.aclose()
            raise
        self._exit = stack
        self._agent = agent
        self._conversation = agent.conversation
        logger.info(
            "Antigravity session started on %s in %s (%s)",
            self._model,
            self._repo_root,
            "read-only" if self.read_only else "gated writes",
        )

    async def close(self) -> None:
        """Tear the harness down. Idempotent, and never raises.

        A close that raises leaves a 119 MB subprocess alive and the
        caller believing it is gone, so the failure is logged and
        swallowed. This is the one place where swallowing is right: there
        is nothing a caller could do with the exception that is better
        than the process being marked closed.
        """
        stack, self._exit = self._exit, None
        self._agent = None
        self._conversation = None
        self._turn_active = False
        if stack is None:
            return
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("Antigravity session did not shut down cleanly")

    async def __aenter__(self) -> AntigravitySession:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        prompt: str,
        request_id: str,
        *,
        emit: Emit = None,
    ) -> StepTranslator:
        """Send a prompt, pump its steps, and return the turn's translator.

        The translator is returned rather than a result dict because it
        carries the whole turn — rendered blocks for a reconnecting
        client, token counters, stats — and phase 4 needs all of it. A
        dict here would be the shape that had to be widened three times.
        """
        translator = StepTranslator(request_id)
        async for event in self.stream_turn(prompt, translator=translator):
            if emit is not None:
                await _emit(emit, event)
        return translator

    async def stream_turn(
        self,
        prompt: str,
        *,
        translator: StepTranslator | None = None,
    ) -> AsyncIterator[Event]:
        """The pump itself: prompt in, events out, one turn.

        Separate from :meth:`run_turn` so the smoke test can print events
        as they arrive without an emit callback and a broadcast layer —
        the phase-3 exit criterion is exactly "print the streamed step
        taxonomy", and a generator is the smallest thing that serves it
        and the RPC layer both.
        """
        if self._conversation is None:
            raise SessionNotStartedError(
                "start() the session before sending a prompt. The harness is "
                "a subprocess and a turn cannot spawn it lazily without "
                "hiding a startup failure inside a user's first message."
            )
        if self._turn_active:
            raise TurnInProgressError(
                "A turn is already running on this session. Cancel it or "
                "wait for it to finish."
            )

        translator = translator or StepTranslator("smoke")
        conversation = self._conversation
        self._turn_active = True
        try:
            async with asyncio.timeout(self._turn_timeout):
                await conversation.send(prompt)
                async for step in conversation.receive_steps():
                    for event in translator.translate(step):
                        yield event
        except TimeoutError:
            # Reported as an event rather than raised, because the turn's
            # partial output is real and the browser is already rendering
            # it. An exception here would replace a half-finished
            # transcript with a stack trace.
            logger.warning("Antigravity turn exceeded %.0fs", self._turn_timeout)
            yield Event(
                "systemEvent",
                {
                    "subtype": "turn_timeout",
                    "data": {"seconds": self._turn_timeout},
                },
            )
        finally:
            self._turn_active = False

        translator.note_stop_reason(self._stop_reason())
        translator.note_turn_usage(self._turn_usage())
        for event in translator.stream_complete():
            yield event

    async def cancel(self) -> None:
        """Halt the running turn. Safe when there is none.

        ``cancel()`` on the conversation sends ``halt_request`` and the
        step iterator ends normally, so the pump's ``finally`` runs and
        ``stream_complete`` still fires — a cancelled turn is a completed
        render of a shorter turn, not a dropped one.
        """
        conversation = self._conversation
        if conversation is None or not self._turn_active:
            return
        try:
            await conversation.cancel()
        except Exception:  # noqa: BLE001 - a cancel that fails is not fatal
            logger.exception("Antigravity cancel failed")

    def _stop_reason(self) -> Any:
        """Why the last turn ended, read off the connection.

        Defensive because it is private SDK surface: ``StopReason`` lives
        on the trajectory state update rather than on any step, and the
        attribute path to it is not part of a documented contract. A turn
        that ended for an unreportable reason is a missing label, not a
        failed turn.

        **The underscored names are first because they are the ones that
        exist.** The public-looking ``stop_reason`` was the only name
        tried until 2026-09-02, when a live turn reported an empty reason:
        the SDK spells it ``_last_turn_stop_reason``, on the conversation
        as a property delegating to the connection
        (``conversation.py:326-328``), and its own ``Response.stop_reason``
        reads it through that private path too (``types.py:1262``). The
        public name is kept in the list after them, because a later SDK
        promoting it is the change this should survive rather than break
        on.
        """
        conversation = self._conversation
        owners = (conversation, getattr(conversation, "_connection", None))
        for name in ("_last_turn_stop_reason", "stop_reason"):
            for owner in owners:
                if owner is None:
                    continue
                reason = getattr(owner, name, None)
                if reason is not None:
                    return reason
        return None

    def _turn_usage(self) -> Any:
        """The turn's tokens, as the conversation's own difference.

        ``last_turn_usage`` is ``cumulative_usage - turn_start_usage``
        (``conversation.py:311-319``), which is the only place the figure
        exists: no step carries it. Measured live on 2026-09-02, reading
        the steps alone produced an empty ``turnUsage`` on a turn that had
        really billed tokens — and tokens are what AG-6 has this engine
        report in place of a cost, so the descriptor promised a figure the
        engine never sent.

        Guarded like :meth:`_stop_reason`: a usage read that raises must
        not take down a turn whose output is already rendered.
        """
        try:
            return getattr(self._conversation, "last_turn_usage", None)
        except Exception:  # noqa: BLE001 - a missing figure is not a failed turn
            logger.exception("Reading the turn's usage failed")
            return None


async def _emit(emit: Callable[[Event], Awaitable[None]], event: Event) -> None:
    """Push one event, and never let a dead client end the turn.

    Same rule as the Claude session's ``_emit``: a broadcast that raises —
    a closed WebSocket, a slow client — must not take the pump down with
    it, because the other clients and the transcript still need the rest
    of the turn.
    """
    try:
        await emit(event)
    except Exception:  # noqa: BLE001 - a broadcast failure is not a turn failure
        logger.exception("Dropping Antigravity event %s: emit failed", event.name)
