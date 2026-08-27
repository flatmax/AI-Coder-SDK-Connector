"""The engine session: one ``ClaudeSDKClient``, one turn at a time.

Owns the client's lifecycle and the message pump. Everything about
*meaning* — which SDK message becomes which browser event — lives in
:mod:`aic_dc.claude_code.messages`; this module owns *timing*: when to
connect, when a turn may start, how a cancelled turn winds down, and what
happens when the subprocess dies underneath us.

Three behaviours here are load-bearing and easy to lose in a refactor
(``specs5/3-engine/session.md`` § Invariants):

- **One client, never silently re-created.** A dead session is reported as
  lost and the user is offered resume. Reconnecting behind their back
  produces a session with no context — a conversation that appears to
  develop amnesia.
- **The pump always runs to ``ResultMessage``.** Cancelling is a flag plus
  ``interrupt()``; the loop still runs to the end. ``break``-ing out of
  the SDK's iterator causes asyncio cleanup failures, and a client
  disconnecting mid-turn is AIC-DC's normal case, not an edge case.
- **A result message ends a turn, not the run.** While a background task is
  in flight the stream keeps carrying that task's life *and* main's reply to
  its notification, so consumption continues past the turn's result — see
  :meth:`EngineSession._drain_background`.
- **A turn's lifetime is independent of any WebSocket.** The pump writes
  into the translator, which accumulates the transcript, so a client that
  reconnects mid-turn replays from server state.

Governing spec: ``specs5/3-engine/session.md``.
Reference: ``specs-reference/3-engine/session.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aic_dc.claude_code.cost import CostLedger
from aic_dc.claude_code.engine_config import EngineConfig
from aic_dc.claude_code.health import EngineHealth, EngineStartupError, resolve_cli
from aic_dc.claude_code.messages import Event, TurnTranslator
from aic_dc.claude_code.options import build_options, file_checkpointing_available
from aic_dc.claude_code import resume_cleanup

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numeric constants (specs-reference/3-engine/session.md § Numeric constants)
# ---------------------------------------------------------------------------

# CLI cold start. The bundled binary is ~295 MB and its first exec on a
# cold page cache is the slow case; this also covers MCP servers starting
# during the initialize handshake.
CONNECT_TIMEOUT = 60.0

# How long an interrupted turn gets to reach its result message. On expiry
# the client is disconnected and the session reported lost, rather than
# reading the next turn's messages over an undrained buffer.
INTERRUPT_DRAIN_TIMEOUT = 30.0


# Task types whose completion the engine waits for past a result message —
# the ones that make a result end a turn rather than the run. Mirrors
# `claude_agent_sdk._internal.query.DEFERRING_TASK_TYPES`, copied rather
# than imported because it is private: if the SDK's set grows, the cost is
# a background task we stop following, not an ImportError at startup.
DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


Emit = Callable[[Event], Awaitable[None]]


class EngineNotReadyError(RuntimeError):
    """A turn arrived before the engine finished connecting.

    Distinct from :class:`EngineStartupError`: this is transient and the
    user-facing answer is "still starting", the same as under the native
    engine.
    """


class TurnInProgressError(RuntimeError):
    """A second user turn arrived while one was in flight.

    Rejected rather than queued: queuing reads as a hang, and the user's
    intent is almost always "stop and do this instead" — which is a cancel
    followed by a send.
    """


class SessionLostError(RuntimeError):
    """The CLI subprocess is gone. Resume is the recovery, not reconnect."""


# ---------------------------------------------------------------------------
# Turn inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewerFraming:
    """What the user is looking at, for turn framing. Never file content."""

    path: str
    start_line: int | None = None
    end_line: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ViewerFraming | None:
        """Build from the RPC payload, tolerating a missing or bad shape."""
        if not isinstance(data, dict):
            return None
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return None
        return cls(
            path=path,
            start_line=_as_int(data.get("start_line")),
            end_line=_as_int(data.get("end_line")),
        )


@dataclass(frozen=True)
class Turn:
    """One user turn's inputs, as they arrive from the browser."""

    request_id: str
    message: str
    images: list[str] = field(default_factory=list)
    viewer: ViewerFraming | None = None
    # Review-mode facts, when review is active. Shaped by
    # specs5/4-features/code-review.md; passed through opaquely here.
    review: dict[str, Any] | None = None


@dataclass
class ActiveTurn:
    """Server-side state for the turn currently in flight."""

    request_id: str
    translator: TurnTranslator
    started_at: str
    cancelled: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    drain_watchdog: asyncio.Task[None] | None = None
    # Task ids started but not yet finished. Non-empty at the result message
    # means the run has not ended, only this turn has — see
    # `EngineSession._drain_background`.
    tasks_in_flight: set[str] = field(default_factory=set)
    # Engine-side counters summed over every result this turn produces, for
    # the same reason the cost baseline is per-turn — see `accrue`.
    accrued: dict[str, int] = field(default_factory=dict)

    def accrue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Sum the engine's per-result counters over the whole turn.

        `duration_ms`, `duration_api_ms` and `num_turns` describe the result
        that carries them, and a turn with a background task in flight produces
        more than one: main's spawning turn, then main's reply to the task
        notification. Passed through as they arrive, the footer would report the
        *last* result's figures — "2.7s · 1 engine turn" for a turn that took
        four times that across two of them — because the browser renders the
        last result it receives.

        So they are summed here, next to the cost baseline and for the same
        reason: cumulative is a property this layer has to maintain rather than
        one it can read off. Duration is the engine's *working* time, not the
        wall clock, which is why the two are not the same figure and why the
        gap while a background agent worked and main slept belongs to neither.
        The browser's own run timer is what reports wall clock.

        A turn that ends once gets its own numbers back unchanged.
        """
        summed = dict(payload)
        for name in ("duration_ms", "duration_api_ms", "num_turns"):
            value = payload.get(name)
            # A bool is an int in Python, and a synthetic footer may carry
            # anything; only real counters accumulate.
            if isinstance(value, int) and not isinstance(value, bool):
                self.accrued[name] = self.accrued.get(name, 0) + value
            if name in self.accrued:
                summed[name] = self.accrued[name]
        return summed

    def note_task_flight(self, payload: Mapping[str, Any]) -> None:
        """Track one ``subagentEvent`` against the in-flight task set.

        The rule is the SDK's own (`Query._note_task_flight`), applied to the
        translated payload rather than the message so this module keeps its
        distance from the SDK's task dataclasses. A notification is what the
        parent agent is woken by, so it counts as finished even when no
        terminal status ever arrives — and a terminal status counts even when
        no notification does, which is how `stop_task()` reports a kill.
        """
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        kind = payload.get("type")
        if kind == "started":
            if payload.get("task_type") in DEFERRING_TASK_TYPES:
                self.tasks_in_flight.add(task_id)
        elif kind == "notification" or payload.get("terminal"):
            self.tasks_in_flight.discard(task_id)

    def to_dict(self) -> dict[str, Any]:
        """The ``ActiveStream`` shape a reconnecting client replays from."""
        return {
            "request_id": self.request_id,
            "session_id": self.translator.session_id,
            "started_at": self.started_at,
            "blocks": self.translator.rendered_blocks(),
            # The subagents this turn started, so a browser that refreshed
            # while one was running rebuilds its tab instead of showing the
            # feed's blocks with nowhere to put them
            # (specs5/5-webapp/subagent-browser.md § Refresh and Reconnect).
            "subagents": self.translator.rendered_subagents(),
            # This turn's token counters so far, in the `turn_model_usage`
            # shape the live `turnUsage` pushes. Same reason as the blocks:
            # the next push is one whole assistant message away, so without
            # the snapshot a refreshed browser's counter would read empty
            # through a long tool call.
            "usage": {"turn_model_usage": self.translator.turn_usage()},
        }


# ---------------------------------------------------------------------------
# Turn framing
# ---------------------------------------------------------------------------


def build_framing(turn: Turn) -> str:
    """Describe UI state the agent cannot otherwise see.

    Answers exactly one question — "what is the user looking at?" — which
    no tool can answer. Everything the agent might want to *read* it reads
    with its own tools, so this is paths, ranges, and mode facts only, and
    never file content (``specs5/3-engine/session.md`` § Turn framing,
    ``specs5/plan/decisions.md`` CC-14).

    Only facts the user could not reasonably have typed. A file the user
    wants named is named in the prompt — the picker inserts the path there
    rather than into a block out here (``specs5/plan/decisions.md`` CC-21),
    so framing carries the viewer's live cursor and the review's shape and
    nothing else.

    Returns the empty string when there is nothing to say, so an ordinary
    turn is sent exactly as the user typed it.
    """
    lines: list[str] = []

    if turn.viewer is not None:
        where = f"- {turn.viewer.path}"
        if turn.viewer.start_line is not None:
            if turn.viewer.end_line is not None and turn.viewer.end_line != turn.viewer.start_line:
                where += f" (lines {turn.viewer.start_line}-{turn.viewer.end_line} selected)"
            else:
                where += f" (cursor on line {turn.viewer.start_line})"
        lines.append("Open in the user's editor pane:")
        lines.append(where)

    review = turn.review or {}
    if review.get("active"):
        lines.append("Code review is active:")
        for key in ("branch", "base_branch", "merge_base"):
            value = review.get(key)
            if value:
                lines.append(f"- {key.replace('_', ' ')}: {value}")

    if not lines:
        return ""
    body = "\n".join(lines)
    # A named wrapper so the model can tell our framing from the user's
    # own words, and so a user who pastes similar text is not confused
    # with the real thing.
    return f"<aic-dc-ui-context>\n{body}\n</aic-dc-ui-context>"


def compose_prompt(turn: Turn) -> str:
    """Framing plus the user's text, in that order."""
    framing = build_framing(turn)
    if not framing:
        return turn.message
    return f"{framing}\n\n{turn.message}"


def build_content_blocks(turn: Turn) -> list[dict[str, Any]]:
    """The multimodal content blocks for a turn that carries images.

    Images are content, not framing: they go into the message as image
    blocks and reach the CLI untouched through ``query()``'s verbatim dict
    path (``specs5/4-features/images.md``).
    """
    blocks: list[dict[str, Any]] = []
    for data_uri in turn.images:
        block = _image_block(data_uri)
        if block is not None:
            blocks.append(block)
    blocks.append({"type": "text", "text": compose_prompt(turn)})
    return blocks


def _image_block(data_uri: str) -> dict[str, Any] | None:
    """Convert a ``data:image/png;base64,…`` URI to an image block."""
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        logger.warning("Ignoring an image that is not a data URI")
        return None
    try:
        header, payload = data_uri.split(",", 1)
        media_type = header[len("data:") :].split(";", 1)[0]
    except ValueError:
        logger.warning("Ignoring a malformed image data URI")
        return None
    if not media_type.startswith("image/") or not payload:
        logger.warning("Ignoring a data URI with media type %r", media_type)
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": payload},
    }


# ---------------------------------------------------------------------------
# EngineSession
# ---------------------------------------------------------------------------


class EngineSession:
    """One connected Claude Code session for one repository.

    Parameters
    ----------
    repo_root:
        Becomes the session's ``cwd``.
    config:
        Parsed ``engine.json``.
    can_use_tool, hooks, mcp_servers, session_store:
        Collaborators landing in later conversion phases. Omitted while
        ``None``, so the engine runs without them.
    clock:
        ISO-timestamp source, injectable for tests.
    """

    def __init__(
        self,
        repo_root: Path | str,
        config: EngineConfig | None = None,
        *,
        can_use_tool: Any = None,
        hooks: Any = None,
        mcp_servers: Any = None,
        session_store: Any = None,
        clock: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
    ) -> None:
        self.repo_root = Path(repo_root)
        self.config = config or EngineConfig()
        self._can_use_tool = can_use_tool
        self._hooks = hooks
        self._mcp_servers = mcp_servers
        self._session_store = session_store
        self._clock = clock

        self.health = EngineHealth()
        # The engine reports cost and per-model usage as session running
        # totals, so the turn's own share is a difference against the
        # previous result — and the baseline outlives the turn.
        self._cost = CostLedger()
        self._client: Any = None
        self._active_turn: ActiveTurn | None = None
        # Consumes the stream between turns while a background task is still
        # running. Never concurrent with a pump: see _stop_background_drain.
        self._drain: asyncio.Task[None] | None = None
        # Guards connect/disconnect against each other; turn admission is
        # a synchronous check, not a lock, because a rejected turn must be
        # rejected rather than made to wait.
        self._lifecycle_lock = asyncio.Lock()
        self._session_lost = False
        self._last_session_id: str | None = None
        self._permission_mode = self.config.effective_permission_mode
        self._model = self.config.model
        # When the engine last said it was compacting, or None. Monotonic,
        # because this is only ever read as a duration and a wall clock that
        # steps backwards would produce a negative one.
        self._compacting_since: float | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._session_lost

    @property
    def ready(self) -> bool:
        """Connected and past the initialize handshake."""
        return self.connected and self.health.connected

    @property
    def session_id(self) -> str | None:
        """The SDK's session ID, or ``None`` before the init message."""
        if self._active_turn is not None and self._active_turn.translator.session_id:
            return self._active_turn.translator.session_id
        return self._last_session_id

    @property
    def streaming_active(self) -> bool:
        return self._active_turn is not None

    @property
    def compaction_state(self) -> dict[str, Any] | None:
        """The compaction in progress, or ``None`` — for a client's first paint.

        Compaction was live-only: the engine's status frames were translated
        into ``compactionEvent`` broadcasts and nothing kept the fact, so a
        browser refreshed during the pause reconnected into a session that
        looked idle while the engine was still summarising. On a long session
        that is tens of seconds of a UI that appears hung — which is precisely
        the failure the indicator exists to prevent, reintroduced by the one
        action a confused user is most likely to take.

        Same class as the compaction divider phase 2 shipped client-side only,
        and the same fix: a broadcast is not a record.

        **Elapsed is computed here, not sent as a start timestamp.** The
        browser would have to compare a server clock against its own to make
        that into a duration, and a collaborating client can be on another
        machine entirely. A number of seconds needs no shared clock.

        The trigger — automatic or manual — is deliberately absent. It is the
        ``PreCompact`` hook's, not the status frame's, and this state is set
        from the frame because the frame is the half that only fires for a real
        compaction. A restored indicator says how long, not why.
        """
        if self._compacting_since is None:
            return None
        elapsed = time.monotonic() - self._compacting_since
        return {"elapsed_seconds": max(0.0, elapsed)}

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def file_checkpointing(self) -> bool:
        """Whether :meth:`rewind_files` has checkpoints to rewind to.

        False whenever the transcript is mirrored, which is every run with
        a repo — the SDK refuses a session store alongside checkpointing
        and the mirror wins
        (:func:`aic_dc.claude_code.options.file_checkpointing_available`).
        Read from the store this session was *built* with rather than from
        the options, so callers can ask before the first connect.
        """
        return file_checkpointing_available(self._session_store)

    @property
    def active_request_id(self) -> str | None:
        """The request ID of the turn in flight, if there is one."""
        return self._active_turn.request_id if self._active_turn is not None else None

    def active_streams(self) -> list[dict[str, Any]]:
        """Replay payload for a client that connects mid-turn."""
        return [self._active_turn.to_dict()] if self._active_turn else []

    def note_permission_prompt(self, tool_use_id: str | None = None) -> str | None:
        """Record a permission prompt against the turn in flight.

        The permission layer calls this once per request: it counts the
        prompt for the turn footer's click-through metric, marks the tool
        card as gated, and hands back the request ID so the dialog can be
        attributed to a turn. Returns ``None`` when no turn is running,
        which is a request raised outside a turn — legal, and rendered
        without a turn attribution.
        """
        active = self._active_turn
        if active is None:
            return None
        active.translator.note_permission_prompt(tool_use_id)
        return active.request_id

    def _note_cli_stderr(self, line: str) -> None:
        """Receive one line of the CLI's own stderr.

        Handed to the SDK as ``options.stderr``, which is what makes the
        subprocess's stderr *piped* rather than inherited. That inversion is
        the whole reason this logs as well as records: before this callback
        existed the text went to the server's terminal, and a callback that
        only fed the health record would have taken that away from whoever
        is reading the terminal.

        Called from the SDK's stderr reader task, synchronously, one
        rstripped non-empty line at a time. The SDK swallows exceptions
        from here at debug level, so a bug in this method would otherwise
        be invisible — hence the explicit catch, which reports it once and
        keeps the reader alive.
        """
        try:
            # The CLI's words, not ours: prefixed so a stack trace in the
            # server log is attributable to the subprocess that raised it.
            logger.info("claude CLI stderr: %s", line)
            self.health.note_cli_stderr(line)
        except Exception:  # noqa: BLE001 - a diagnostic path, never a turn's
            logger.exception("Failed to record a line of CLI stderr")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, resume: str | None = None, fork_session: bool = False) -> None:
        """Resolve the CLI, build options, and connect the client.

        Raises
        ------
        EngineStartupError
            Missing or too-old CLI, an SDK options surface that moved, a
            connect that exceeds :data:`CONNECT_TIMEOUT`, or any failure
            from the SDK's own connect. Startup failure is deliberate:
            nothing in the engine works without a session, so a degraded
            mode would only hide the cause.
        """
        async with self._lifecycle_lock:
            if self._client is not None:
                logger.debug("Engine already connected; ignoring connect()")
                return

            from claude_agent_sdk import ClaudeSDKClient

            resolution = resolve_cli(self.config.cli_path)
            self.health.apply_cli(resolution)
            self.health.apply_credentials()
            if resolution.version_warning:
                logger.warning("%s", resolution.version_warning)
            if self.health.auth_warning:
                logger.warning("%s", self.health.auth_warning)

            options = build_options(
                repo_root=self.repo_root,
                config=self.config,
                # The binary we just version-checked, not whatever SDK
                # discovery finds a moment later.
                cli_path=str(resolution.path),
                can_use_tool=self._can_use_tool,
                hooks=self._hooks,
                mcp_servers=self._mcp_servers,
                session_store=self._session_store,
                stderr=self._note_cli_stderr,
                resume=resume,
                fork_session=fork_session,
                # Our current posture, not the configured one. They differ
                # when something set a mode before the first connect — see
                # prefer_permission_mode — and connecting in the config's
                # mode there would quietly discard the request.
                permission_mode=self._permission_mode,
            )
            client = ClaudeSDKClient(options=options)
            try:
                # connect() spawns the CLI and completes the control-protocol
                # initialize handshake, which is also when MCP servers start.
                # No prompt: the session stays open for query() to feed.
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError as exc:
                await _quiet_disconnect(client)
                self.health.last_error = f"connect timed out after {CONNECT_TIMEOUT:.0f}s"
                raise EngineStartupError(
                    f"The Claude Code CLI at {resolution.path} did not finish "
                    f"starting within {CONNECT_TIMEOUT:.0f}s. It may be waiting "
                    f"on an MCP server, or on a first-run login."
                ) from exc
            except Exception as exc:
                await _quiet_disconnect(client)
                self.health.last_error = str(exc)
                raise EngineStartupError(
                    f"Could not start a Claude Code session: {exc}"
                ) from exc

            self._client = client
            self._session_lost = False
            self.health.connected = True
            self.health.last_error = None
            # The CLI's cost ledger is per-process and, in its own words,
            # "resumed sessions start fresh". Carrying our baseline across a
            # connect would price the new session's first turn as a refund.
            self._cost.reset()
            # A resume with a store materialises a temp CLAUDE_CONFIG_DIR
            # that only disconnect() cleans up, and the signal handler
            # never reaches disconnect(). Recorded here, removed there.
            resume_cleanup.remember(client)
            if resume and not fork_session:
                # The init message will report the resumed ID; recording it
                # now means get_current_state() is right before the first
                # turn rather than only after it.
                #
                # Not for a fork: that mints a *new* session ID, which only
                # the init message knows. Recording the origin here would
                # name the wrong session in `get_current_state()` and, worse,
                # point a restart's auto-resume at the session the user
                # forked away from.
                self._last_session_id = resume
            logger.info(
                "Claude Code session connected (cwd=%s, permission_mode=%s%s)",
                self.repo_root,
                self._permission_mode,
                f", resume={resume}" if resume else "",
            )

    def adopt_config(self, config: EngineConfig) -> None:
        """Replace the config the *next* :meth:`connect` builds options from.

        What a session restart needs: ``engine.json`` is read once, at
        startup, so re-reading it is the only way a saved ``effort`` or
        ``cli_path`` can reach the CLI.

        The mode and model go back to the file too, and that is the point
        rather than a side effect. Both are live state a mid-session
        :meth:`set_permission_mode` or :meth:`set_model` may have moved away
        from what the file says; a restart is "start the session this file
        describes", and silently keeping an override would make the
        restart's own confirmation — which names the fields it is about to
        apply — wrong about two of them.

        Raises
        ------
        RuntimeError
            While connected. Options are read at connect time, so swapping
            the config under a live client would change what
            :attr:`permission_mode` and :attr:`model` report without
            changing anything the CLI is doing. Callers
            :meth:`reset` or :meth:`disconnect` first.
        """
        if self._client is not None:
            raise RuntimeError(
                "adopt_config() while connected: disconnect first, or the "
                "reported options would not be the running ones"
            )
        self.config = config
        self._permission_mode = config.effective_permission_mode
        self._model = config.model

    async def reset(self) -> None:
        """Disconnect and forget which session this was.

        What "New Session" needs, and the reason it is not just
        :meth:`disconnect`: a disconnect keeps ``session_id`` so a lost
        session can be resumed, which is the opposite of what starting a
        fresh one means. Leaving the old ID in place would have
        ``get_current_state()`` name the abandoned session until the first
        turn of the new one replaced it.

        The next :meth:`connect` decides what to attach to; this only
        clears the ground.
        """
        await self.disconnect()
        self._last_session_id = None
        # A fresh session is not a lost one. Leaving this set would have
        # `admit` refuse the first turn of the session we just made room
        # for, telling the user to start a new session they just started.
        self._session_lost = False

    async def disconnect(self) -> None:
        """Shut the session down as part of graceful shutdown."""
        await self._stop_background_drain()
        async with self._lifecycle_lock:
            client, self._client = self._client, None
            self.health.connected = False
            # Nothing is compacting once there is no engine, and a stale flag
            # here would outlive the process that set it: the browser survives
            # a server restart and would ask for state before the first turn.
            self._compacting_since = None
            if client is not None:
                await _quiet_disconnect(client)
                logger.info("Claude Code session disconnected")

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def admit(self, request_id: str) -> None:
        """Check that a turn may start, raising if it may not.

        Separate from :meth:`run_turn` because the RPC must answer the
        browser synchronously — the caller admits the turn, returns
        ``{"status": "started"}``, and only then spawns the pump.
        """
        if self._session_lost:
            raise SessionLostError(
                "The Claude Code session was lost. Start a new session or "
                "resume the previous one."
            )
        if not self.ready:
            raise EngineNotReadyError(
                "The Claude Code engine is still starting. Try again in a moment."
            )
        if self._active_turn is not None:
            raise TurnInProgressError(
                f"A turn is already running (request {self._active_turn.request_id}). "
                f"Stop it before sending another message."
            )
        if not request_id:
            raise ValueError("A turn needs a request ID; the browser generates it.")

    async def run_turn(self, turn: Turn, emit: Emit | None = None) -> dict[str, Any]:
        """Send ``turn`` to the engine and pump its messages to ``emit``.

        Returns the ``streamComplete`` result, so a caller that wants the
        turn's outcome does not have to intercept the event stream.

        The pump runs to the result message even when the turn is
        cancelled or the consumer goes away. Exceptions from ``emit`` are
        logged and swallowed: a browser-side failure must not truncate a
        turn that the engine is still running.
        """
        self.admit(turn.request_id)
        # Before the client is touched: this turn's pump is about to become the
        # stream's only consumer, and the previous turn's drain may still be on
        # it.
        await self._stop_background_drain()
        # Anchor the cost baseline here, so every result this turn produces —
        # including the ones the drain reads after the first — is differenced
        # against the same point and reports the turn's running total.
        self._cost.start_turn()
        translator = TurnTranslator(turn.request_id)
        active = ActiveTurn(
            request_id=turn.request_id,
            translator=translator,
            started_at=self._clock(),
        )
        self._active_turn = active

        try:
            await self._send(turn)
            return await self._pump(active, emit)
        finally:
            self._last_session_id = translator.session_id or self._last_session_id
            active.done.set()
            watchdog = active.drain_watchdog
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
            # Cleared before the drain starts, so the next turn is admitted
            # while a background agent is still being followed. Waiting for a
            # background agent to finish before accepting a message would
            # defeat the point of running one.
            self._active_turn = None
            self._start_background_drain(active, emit)

    async def _send(self, turn: Turn) -> None:
        """Write the user turn to the CLI."""
        client = self._client
        if client is None:
            raise SessionLostError("Not connected.")
        if turn.images:
            # The verbatim dict path: the SDK JSON-encodes each dict as
            # given, so multimodal content blocks survive untouched.
            await client.query(_single_message_stream(turn))
        else:
            await client.query(compose_prompt(turn))

    async def _pump(self, active: ActiveTurn, emit: Emit | None) -> dict[str, Any]:
        """Iterate to ``ResultMessage``, translating and emitting.

        Never exits the iterator early. The one exception is the engine
        itself failing, which raises out of the iterator rather than being
        a decision we make.
        """
        client = self._client
        translator = active.translator
        result: dict[str, Any] | None = None

        try:
            async for message in client.receive_response():
                for event in translator.translate(message):
                    event = self._fold_session_state(active, event)
                    if event.name == "streamComplete":
                        result = event.payload
                    await self._emit(emit, event)
        except asyncio.CancelledError:
            # Someone cancelled the pump task itself. The turn is still
            # running inside the CLI, so say so rather than reporting a
            # clean finish.
            logger.warning("Message pump cancelled for request %s", active.request_id)
            raise
        except Exception as exc:
            logger.exception("Message pump failed for request %s", active.request_id)
            result = await self._fail_turn(active, emit, exc)

        if result is None:
            # The iterator ended without a result message, which
            # receive_response() only does if the stream closed under it.
            result = await self._fail_turn(
                active,
                emit,
                SessionLostError("The engine closed the stream before the turn finished."),
            )
        return result

    def _fold_session_state(self, active: ActiveTurn, event: Event) -> Event:
        """Complete one translated event with state only the session holds.

        A ``TurnTranslator`` knows one turn. Two events need more than that,
        and one needs to be *counted* as well as emitted, so all three are
        finished here rather than in the translator — which keeps the pump and
        the background drain agreeing on the arithmetic by construction.
        """
        if event.name == "engineHealth":
            self.health.note_mirror_gap()
            self.health.last_error = event.payload.get("error") or None
            return Event("engineHealth", self.health.to_dict(), turn_scoped=False)
        if event.name == "compactionEvent":
            # Held, not just forwarded, so a browser that reloads during the
            # pause can be told about it — see `compaction_state`. Only the
            # two status-frame stages move it: `compact_boundary` is the
            # transcript's record and can arrive for a microcompaction that
            # never paused anything, so ending on it would clear a state it
            # never set.
            stage = event.payload.get("stage")
            if stage == "compaction_started":
                self._compacting_since = time.monotonic()
            elif stage == "compaction_ended":
                self._compacting_since = None
            return event
        if event.name == "streamComplete":
            # A compaction never outlives the turn it happened in. Without
            # this a turn that died mid-compaction would leave the flag set,
            # and every browser connecting afterwards would be handed a
            # spinner for a pause that ended when the engine went away.
            self._compacting_since = None
            # What a turn *cost* is only visible against the session's running
            # total. Every result is priced, and a turn that ends more than
            # once is priced against its own start rather than against its
            # previous result, so each answer is the whole turn's — see
            # `CostLedger.start_turn`. `ActiveTurn.accrue` does the same for
            # the counters the engine reports per result.
            return Event(
                "streamComplete",
                {
                    **self._price_turn(active.accrue(event.payload)),
                    # Which background tasks this result did *not* end. The
                    # browser settles a live subagent tab at the turn's result
                    # and has no other way to tell "its terminal event never
                    # arrived" from "it is still working" — and reporting the
                    # second as the first is what puts an amber "status
                    # unknown" LED on a subagent that goes on to succeed.
                    "background_tasks": sorted(active.tasks_in_flight),
                },
                turn_scoped=event.turn_scoped,
            )
        if event.name == "subagentEvent":
            active.note_task_flight(event.payload)
        return event

    # ------------------------------------------------------------------
    # Background drain
    # ------------------------------------------------------------------

    def _start_background_drain(self, active: ActiveTurn, emit: Emit | None) -> None:
        """Follow the stream past this turn's result, if anything is still on it."""
        if not active.tasks_in_flight or active.cancelled or self._client is None:
            return
        logger.info(
            "Turn %s finished with %d background task(s) still running; "
            "following the stream past its result",
            active.request_id,
            len(active.tasks_in_flight),
        )
        self._drain = asyncio.create_task(
            self._drain_background(active, emit), name=f"cc-drain-{active.request_id}"
        )

    async def _stop_background_drain(self) -> None:
        """Give the stream back before another consumer takes it.

        Two iterators over the SDK's message stream would split its messages
        between them arbitrarily, so the drain must be gone — not merely asked
        to go — before the next turn's pump starts.
        """
        task, self._drain = self._drain, None
        if task is None or task.done():
            return
        logger.info("Ending the background drain; the stream has a new consumer")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _drain_background(self, active: ActiveTurn, emit: Emit | None) -> None:
        """Keep translating while background tasks outlive their turn.

        **A result message ends a turn, not the run.** The SDK says so in as
        many words — "Result received with N task(s) in flight; keeping stdin
        open" — and goes on emitting for the tasks still going, plus main's
        own reply once a task notification wakes it. ``receive_response()``
        stops at the result regardless, so stopping with it left a background
        subagent's entire life unread: an empty tab, an activity LED stuck at
        "status unknown at turn end", a permission request attributed to no
        turn at all, and main's closing answer missing — all of it written to
        the transcript meanwhile, so it appeared only if the user reloaded.

        Nothing was being lost to the buffer, which is why the symptom read as
        a rendering bug: the SDK's stream is a 100-slot anyio buffer owned by
        the client, so those messages sat there and a later iterator would
        still get them. What was lost was *when* — they arrived attributed to
        whatever turn happened to be reading next, long after they meant
        anything live. This drain reads them while they still do.

        Ends on a result message that arrives with the in-flight set empty,
        which is the SDK's own definition of the run ending, or when
        :meth:`_stop_background_drain` hands the stream to the next turn.
        """
        client = self._client
        if client is None:
            return
        try:
            async for message in client.receive_messages():
                for event in active.translator.translate(message):
                    event = self._fold_session_state(active, event)
                    if event.name != "streamComplete":
                        await self._emit(emit, event)
                        continue
                    # Main's own follow-up turn ends here — the one a task
                    # notification woke it for — and its answer is in this
                    # payload. Flagged so the browser revises the turn it has
                    # already settled instead of appending a second footer to
                    # a turn the user has read: every field is cumulative over
                    # the request, so this result *supersedes* the last one
                    # rather than adding to it.
                    #
                    # `background_finished` marks the *last* of these. The
                    # service's post-turn housekeeping runs when `run_turn`
                    # returns, which is at the first result — this drain is a
                    # detached task that outlives it — so the Context tab and
                    # the file tree were left describing the session as it was
                    # before the background work, until the next turn moved
                    # them on. The flag is what lets the service run that
                    # housekeeping a second time, at the point the run really
                    # ends.
                    finished = not active.tasks_in_flight
                    await self._emit(
                        emit,
                        Event(
                            "streamComplete",
                            {
                                **event.payload,
                                "continuation": True,
                                "background_finished": finished,
                            },
                            turn_scoped=event.turn_scoped,
                        ),
                    )
                    if finished:
                        logger.info(
                            "Background work for turn %s finished; drain ending",
                            active.request_id,
                        )
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never fatal: the turn is long over and the next one re-reads the
            # stream from scratch. Losing the tail of a background agent is
            # worth strictly less than a raise out of a detached task.
            logger.exception(
                "Background drain for turn %s failed; stopping it",
                active.request_id,
            )

    async def _fail_turn(
        self, active: ActiveTurn, emit: Emit | None, exc: BaseException
    ) -> dict[str, Any]:
        """Finalise a turn the engine could not finish.

        The UI must never be left with a spinner it cannot clear, so a
        synthetic ``streamComplete`` is emitted with the failure in it. If
        the subprocess is gone the session is marked lost — not re-created,
        because a fresh session would have none of this conversation's
        context.
        """
        lost = _is_connection_failure(exc)
        if lost:
            self._session_lost = True
            self.health.connected = False
        self.health.last_error = str(exc) or type(exc).__name__

        result = {
            "session_id": active.translator.session_id,
            "response": active.translator.response_text(),
            "subtype": "error_during_execution",
            "terminal_reason": "session_lost" if lost else "engine_error",
            "is_error": True,
            "num_turns": 0,
            "duration_ms": 0,
            "duration_api_ms": 0,
            "usage": None,
            "model_usage": None,
            "total_cost_usd": None,
            "tool_calls": active.translator.stats.tool_calls,
            "permission_prompts": active.translator.stats.permission_prompts,
            "files_modified": list(active.translator.stats.files_modified),
            "cancelled": active.cancelled,
            "mirror_gap": active.translator.stats.mirror_gap,
            "user_message_id": active.translator.user_message_id,
            "errors": [str(exc) or type(exc).__name__],
        }
        # Through the ledger like any other result, which prices it `unpriced`:
        # this footer is ours, the engine never sent one, and whatever the turn
        # spent before it died is still on the session's running total for the
        # next turn to be measured against.
        result = self._price_turn(result)
        await self._emit(emit, Event("streamComplete", result))
        if lost:
            await self._emit(
                emit, Event("engineHealth", self.health.to_dict(), turn_scoped=False)
            )
        return result

    def _price_turn(self, result: dict[str, Any]) -> dict[str, Any]:
        """Add this turn's own cost and per-model usage to a result payload.

        ``total_cost_usd`` and ``model_usage`` stay exactly as the engine sent
        them — they are its numbers and they are cumulative. The three fields
        added beside them are ours and are per-turn; see
        :mod:`aic_dc.claude_code.cost` for why the difference has to be taken
        here rather than in the browser, and what makes it unavailable.
        """
        return {**result, **self._cost.price(result)}

    async def _emit(self, emit: Emit | None, event: Event) -> None:
        """Deliver one event, absorbing consumer failures.

        A broadcast that raises — a closed WebSocket, a slow client — must
        not end the turn.
        """
        if emit is None:
            return
        try:
            await emit(event)
        except Exception:
            logger.exception("Failed to emit %s; continuing the turn", event.name)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def interrupt(self, request_id: str | None = None) -> dict[str, Any]:
        """Ask the engine to stop the turn in flight.

        Sets a flag and calls ``interrupt()``; the pump keeps running to
        the result message, whose ``terminal_reason`` will be
        ``aborted_streaming`` or ``aborted_tools``. Skipping that drain
        routes the interrupted turn's tail into the next turn's UI.
        """
        active = self._active_turn
        if active is None:
            return {"status": "idle"}
        if request_id and request_id != active.request_id:
            # Almost always a stale Stop click from a client whose turn
            # already finished; the in-flight turn is someone else's.
            logger.info(
                "Ignoring cancel for request %s; the active turn is %s",
                request_id,
                active.request_id,
            )
            return {"status": "not_active", "request_id": request_id}
        if active.cancelled:
            return {"status": "interrupting", "request_id": active.request_id}

        active.cancelled = True
        active.translator.cancelled = True
        client = self._client
        if client is None:
            return {"status": "interrupting", "request_id": active.request_id}
        try:
            await client.interrupt()
        except Exception as exc:
            logger.warning("interrupt() failed for %s: %s", active.request_id, exc)
            return {"error": f"Could not interrupt the turn: {exc}"}
        active.drain_watchdog = asyncio.create_task(
            self._watch_drain(active), name=f"drain-watchdog-{active.request_id}"
        )
        return {"status": "interrupting", "request_id": active.request_id}

    async def _watch_drain(self, active: ActiveTurn) -> None:
        """Bound the wait for an interrupted turn to reach its result.

        On expiry the session is lost deliberately: reading the next turn
        over a buffer still holding this one's tail is worse than a clean
        failure the user can act on.
        """
        try:
            await asyncio.wait_for(active.done.wait(), timeout=INTERRUPT_DRAIN_TIMEOUT)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return

        logger.error(
            "Interrupted turn %s did not drain within %.0fs; disconnecting",
            active.request_id,
            INTERRUPT_DRAIN_TIMEOUT,
        )
        self._session_lost = True
        self.health.connected = False
        self.health.last_error = (
            f"The interrupted turn did not stop within {INTERRUPT_DRAIN_TIMEOUT:.0f}s; "
            f"the session was disconnected. Resume to continue this conversation."
        )
        await self._stop_background_drain()
        client, self._client = self._client, None
        if client is not None:
            await _quiet_disconnect(client)

    # ------------------------------------------------------------------
    # Live controls
    # ------------------------------------------------------------------

    async def set_permission_mode(self, mode: str) -> str:
        """Switch the safety posture without reconnecting."""
        from aic_dc.claude_code.engine_config import PERMISSION_MODES

        if mode not in PERMISSION_MODES:
            raise ValueError(
                f"Unknown permission mode {mode!r}. Valid modes: "
                f"{', '.join(PERMISSION_MODES)}."
            )
        await self._require_client().set_permission_mode(mode)
        self._permission_mode = mode
        return mode

    def note_permission_mode(self, mode: str) -> None:
        """Record a mode the CLI has *already* been told about.

        A permission decision can carry a ``setMode`` update back on its
        result, which the CLI applies without saying so on the message
        stream. Sending it a second time through ``set_permission_mode``
        would be a redundant control request; leaving this cached value
        stale would make ``permission_mode`` report the mode the session
        started in rather than the one it is in.
        """
        self._permission_mode = mode

    def prefer_permission_mode(self, mode: str) -> str:
        """Set the posture a *future* connect starts in. No client needed.

        For a mode change requested before the CLI exists — review mode
        entered on a cold engine. :meth:`connect` builds its options from
        this value, so the session comes up in the requested posture rather
        than in ``engine.json``'s and then having to be corrected.

        Not a substitute for :meth:`set_permission_mode`: this cannot move
        a running session, and the caller is expected to check
        :attr:`ready` and choose.
        """
        from aic_dc.claude_code.engine_config import PERMISSION_MODES

        if mode not in PERMISSION_MODES:
            raise ValueError(
                f"Unknown permission mode {mode!r}. Valid modes: "
                f"{', '.join(PERMISSION_MODES)}."
            )
        self._permission_mode = mode
        return mode

    async def set_model(self, model: str | None = None) -> str | None:
        """Switch models mid-session. ``None`` restores the CLI default."""
        await self._require_client().set_model(model)
        self._model = model
        return model

    async def rewind_files(self, user_message_id: str) -> None:
        """Restore tracked files to their state at a user message.

        Needs both ``enable_file_checkpointing`` and the
        ``--replay-user-messages`` flag, which
        :mod:`aic_dc.claude_code.options` sets together — and only when the
        transcript is *not* mirrored, because the SDK refuses a session
        store alongside checkpointing. Callers ask
        :attr:`file_checkpointing` first; here it would raise from inside
        the SDK. The SDK returns nothing either way, so the caller cannot
        report *which* files were restored from this call alone.
        """
        await self._require_client().rewind_files(user_message_id)

    async def stop_task(self, task_id: str) -> None:
        """Kill one subagent. Reported back as ``status="killed"``."""
        await self._require_client().stop_task(task_id)

    async def get_context_usage(self) -> dict[str, Any]:
        """The live context breakdown, passed through unmodified."""
        return dict(await self._require_client().get_context_usage())

    async def get_mcp_status(self) -> dict[str, Any]:
        """Per-server MCP status, passed through unmodified."""
        return dict(await self._require_client().get_mcp_status())

    async def reconnect_mcp_server(self, name: str) -> None:
        await self._require_client().reconnect_mcp_server(name)

    async def toggle_mcp_server(self, name: str, enabled: bool) -> None:
        await self._require_client().toggle_mcp_server(name, enabled)

    async def get_server_info(self) -> dict[str, Any] | None:
        """Advertised commands, tools, and output styles from initialize."""
        return await self._require_client().get_server_info()

    def _require_client(self) -> Any:
        if self._session_lost:
            raise SessionLostError(
                "The Claude Code session was lost. Start a new session or "
                "resume the previous one."
            )
        if self._client is None:
            raise EngineNotReadyError("The Claude Code engine is not connected.")
        return self._client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _single_message_stream(turn: Turn) -> Any:
    """Yield one user message dict, for ``query()``'s verbatim path.

    ``query()`` iterates the stream and writes each dict as JSON, filling
    in ``session_id`` when absent. The wire shape mirrors the string path
    it builds internally, with a content *list* instead of a string.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": build_content_blocks(turn)},
        "parent_tool_use_id": None,
    }


async def _quiet_disconnect(client: Any) -> None:
    """Disconnect without letting teardown failures mask the real error."""
    try:
        await client.disconnect()
    except Exception as exc:
        logger.debug("Ignoring error while disconnecting: %s", exc)


def _is_connection_failure(exc: BaseException) -> bool:
    """Whether ``exc`` means the CLI subprocess is gone.

    Matched on class name rather than by importing the SDK's error
    hierarchy, so a renamed or removed error class degrades to "not a
    connection failure" instead of an ImportError at the worst moment.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & _CONNECTION_FAILURE_NAMES)


_CONNECTION_FAILURE_NAMES = frozenset(
    {
        "CLIConnectionError",
        "ProcessError",
        "CLIJSONDecodeError",
        "SessionLostError",
        "BrokenPipeError",
        "ConnectionResetError",
    }
)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
