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
  its notification, so the stream is read between turns too, and every
  message goes to the turn that owns it rather than the one that happens to
  be current — see :meth:`EngineSession._route`.
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

from aic_dc import framing
from aic_dc.claude_code.cost import CostLedger
from aic_dc.claude_code.engine_config import EngineConfig
from aic_dc.claude_code.health import (
    EngineHealth,
    EngineStartupError,
    cli_config_dir,
    resolve_cli,
    subscription_credential_path,
)
from aic_dc.claude_code.messages import Event, TurnTranslator
from aic_dc.claude_code.options import build_options, file_checkpointing_available
from aic_dc.claude_code import resume_cleanup, token_refresh

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

# Floor on the token watchdog's sleep. The delay it computes is normally
# hours; this only bites when the token is already inside the refresh
# margin, and it exists so a token that cannot be renewed produces one
# attempt a minute rather than a spin loop against a subprocess.
TOKEN_WATCHDOG_MIN_SLEEP = 60.0


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


#: What the user is looking at, for turn framing. Never file content.
#:
#: The class itself moved to :mod:`aic_dc.framing` on 2026-09-14, because
#: the other two engines could not reach it here and so never framed the
#: user's screen at all (AG-33). The alias stays: this is the name the RPC
#: inventory, the webapp and this module's own tests know it by, and
#: renaming it would have made an internal move into a surface change.
ViewerFraming = framing.Viewer


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
    # `EngineSession._complete`.
    tasks_in_flight: set[str] = field(default_factory=set)
    # Engine-side counters summed over every result this turn produces, for
    # the same reason the cost baseline is per-turn — see `accrue`.
    accrued: dict[str, int] = field(default_factory=dict)
    # Where this turn's events go. Held on the turn rather than passed to
    # whichever loop is reading, because a turn's events can now be read by a
    # *later* turn's pump — see `EngineSession._route`.
    emit: Emit | None = None
    # Results this turn has produced. The first is the turn's own footer and
    # every later one a continuation.
    results: int = 0
    # The last `streamComplete` payload emitted for this turn, so a turn whose
    # background work ends inside another turn can still be closed with one.
    last_result: dict[str, Any] | None = None
    # The cost fields that payload carried. Reused, not re-priced, for a
    # result read after a newer turn took the pricing anchor — see
    # `EngineSession._price_for`.
    priced: dict[str, Any] | None = None

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
    """This turn's framing block, or ``""`` when there is nothing to say.

    The sentences live in :mod:`aic_dc.framing` and the reasons for them
    live in its docstring — what stays here is the mapping from a
    :class:`Turn` to that module's two arguments, which is the only part of
    it that is Claude-specific.

    **Kept as a function rather than inlined at the call sites.** It is the
    documented name in ``specs5/3-engine/session.md`` § Turn framing, and
    the engine-agnostic module cannot depend on this one's ``Turn``.
    """
    return framing.build(viewer=turn.viewer, review=turn.review)


def compose_prompt(turn: Turn) -> str:
    """Framing plus the user's text, in that order."""
    return framing.compose(build_framing(turn), turn.message)


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
        # Consumes the stream between turns. Never concurrent with a pump: see
        # _stop_background_drain.
        self._drain: asyncio.Task[None] | None = None
        # Turns whose result arrived with background tasks still in flight,
        # by request id. They outlive their turn and go on receiving events
        # while later turns run — see `_route`.
        self._background: dict[str, ActiveTurn] = {}
        # Routing keys — `Task` call ids, task ids, agent ids, nested tool
        # call ids — to the turn that owns them. Pruned when a turn retires,
        # because the value holds the turn's whole translator.
        self._owners: dict[str, ActiveTurn] = {}
        # The turn main is speaking for, and whether it is still speaking.
        # Closed by a result; opened by a user turn or by main waking up.
        self._main: ActiveTurn | None = None
        self._main_open = False
        # The turn whose background task most recently finished, which is the
        # one main's next unprompted reply answers.
        self._woken: ActiveTurn | None = None
        # The turn that anchored the cost ledger last — see `_price_for`.
        self._pricing: ActiveTurn | None = None
        # Routing keys of turns that have retired. A late message keyed to one
        # is dropped rather than handed to whichever turn is current.
        self._retired_keys: set[str] = set()
        # Task ids the CLI typed as something other than a subagent — in
        # practice a backgrounded `Bash` command. Held here rather than on the
        # translator because a slow command outlives the turn that ran it and
        # only its *first* message says what it is; a per-turn latch lets the
        # trailing `task_notification` through as a subagent row and an empty
        # tab. See `TurnTranslator._is_not_a_subagent`.
        self._non_subagent_tasks: set[str] = set()
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
        # Rate-limit records, **keyed by `rate_limit_type`**. Held rather than
        # only forwarded, for the reason the SDK's own docstring gives: the
        # event fires when the status *transitions*, not per turn, so a
        # browser that reloads an hour into a five-hour window would have
        # nothing to show until the next transition — which may be the
        # rejection this figure exists to give warning of.
        #
        # **Keyed rather than a single slot, which is what it was until
        # 2026-08-29.** An account has several windows open at once — the CLI's
        # own `/usage` panel draws a gauge for the five-hour window, one for
        # the week across all models, and one per model with its own weekly
        # cap — and each arrives as its own `RateLimitInfo` with its own
        # `rate_limit_type`. One slot meant the newest event overwrote a
        # different window's record, so the browser could only ever show
        # whichever window happened to transition last, and a five-hour figure
        # would silently become a seven-day one under the same section. An
        # untyped record still gets a slot, under a placeholder key, because
        # dropping it would lose the only figure a future CLI might send.
        self._rate_limits: dict[str, dict[str, Any]] = {}
        # Insertion order is not arrival order once a key is overwritten, so
        # the most recent arrival is tracked separately for the readers that
        # want one record rather than all of them.
        self._rate_limit_latest: str | None = None
        # When this session's engine connected, on a monotonic clock. The wall
        # duration `/usage` reports is measured here rather than summed from
        # the CLI's `duration_ms`, because the SDK does not say whether that
        # field is per-turn or cumulative in a streaming-input session and
        # reading it the wrong way makes the figure silently wrong — which is
        # the exact failure `cost.py` exists to prevent for its neighbour.
        self._connected_since: float | None = None
        # The SDK's temp CLAUDE_CONFIG_DIR for this connect, when it made
        # one. Where `_token_watchdog` puts a refreshed access token.
        self._materialized_dir: Path | None = None
        self._token_watchdog_task: asyncio.Task[None] | None = None
        # The binary this session's connect resolved and version-checked.
        # Kept so `_token_watchdog` does not have to call `resolve_cli`,
        # which probes `claude --version` with a *blocking* subprocess and
        # would stall the event loop for up to CLI_VERSION_PROBE_TIMEOUT.
        self._cli_path: str | None = None

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
    def rate_limit(self) -> dict[str, Any] | None:
        """The last rate-limit record, or ``None`` — for a client's first paint.

        Same shape as the ``rateLimit`` broadcast, and the same reasoning as
        :attr:`compaction_state`: a broadcast is not a record. The difference
        is how long the gap is. A compaction lasts tens of seconds, so the
        window in which a reload loses the fact is small; the CLI emits a rate
        limit only when the status *changes*, so a reload loses it for as long
        as the status holds — hours, on a seven-day window.

        **Not cleared by a new session, unlike compaction.** A five-hour window
        belongs to the account, not to the conversation, so ``/clear`` and a
        resume leave it exactly as true as it was. Nor by ``disconnect``: the
        engine going away does not give the tokens back.

        **Whether the window has since reset is the browser's question, not
        this one.** The record is served raw. A pushed record has to be aged by
        the client anyway — the HUD can hold one for hours without another
        arriving — so expiring it here as well would be a second definition of
        "still open" that could only come to disagree with the first
        (``specs5/next.md`` § C3). ``resets_at`` is a wall-clock instant that
        the browser already renders against its own clock, and minutes of skew
        do not matter to a five-hour window.

        **The most recent arrival, now that several are kept.** This is the
        HUD's reading, which shows one window and always has; the Context tab
        takes :attr:`rate_limits` and draws all of them. Recency rather than
        "the most constrained" on purpose — the HUD's section exists to report
        a *transition*, and the window that just transitioned is the one worth
        interrupting for, whatever its utilisation says.
        """
        if self._rate_limit_latest is None:
            return None
        record = self._rate_limits.get(self._rate_limit_latest)
        return dict(record) if record is not None else None

    @property
    def rate_limits(self) -> list[dict[str, Any]]:
        """Every rate-limit window the CLI has reported, newest arrival last.

        An account has several open at once and each is its own record; see
        the note beside ``_rate_limits``. Served raw, like the singular
        reading: **whether a window has since reset is the browser's
        question**, and answering it here as well would be a second definition
        of "still open" that could only come to disagree with the first
        (``specs5/next.md`` § C3). ``rate-limit.js``'s ``windowIsOpen`` is the
        one definition, and both surfaces call it.
        """
        return [dict(record) for record in self._rate_limits.values()]

    @property
    def session_duration_seconds(self) -> float | None:
        """Wall time since this session's engine connected, or ``None``.

        AIC⚡DC's own monotonic clock rather than a sum of the CLI's
        ``duration_ms``. The SDK documents ``total_cost_usd`` and
        ``modelUsage`` as cumulative in a streaming-input session and says
        nothing either way about the duration fields — so summing them would
        double-count if they are cumulative and undercount if a turn ends more
        than once, and neither error announces itself. A clock we own answers
        the question the panel actually asks and cannot be wrong about which
        kind of number it holds.

        This is why no *API* duration is reported beside it: there is no
        second clock here that measures time inside the engine, and
        ``duration_api_ms`` carries the same unresolved question. Stated
        rather than approximated — ``specs5/5-webapp/viewers-hud.md``
        § *Session Usage*.
        """
        if self._connected_since is None:
            return None
        return max(0.0, time.monotonic() - self._connected_since)

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
        """Replay payload for a client that connects mid-turn.

        Background turns first, oldest first, then the turn in flight. A
        background entry is flagged ``background``: its own presentation has
        settled and only its subagents are still working, so a client must
        not resume it as the foreground stream.
        """
        streams = [
            {**turn.to_dict(), "background": True}
            for turn in self._background.values()
            if turn is not self._active_turn
        ]
        if self._active_turn is not None:
            streams.append(self._active_turn.to_dict())
        return streams

    def note_permission_prompt(
        self, tool_use_id: str | None = None, agent_id: str | None = None
    ) -> str | None:
        """Record a permission prompt against the turn that raised it.

        The permission layer calls this once per request: it counts the
        prompt for the turn footer's click-through metric, marks the tool
        card as gated, and hands back the request ID so the dialog can be
        attributed to a turn. A subagent's request belongs to the turn that
        spawned the subagent, which is not the turn in flight once a
        background subagent has outlived its own. Returns ``None`` when no
        turn owns it, which is a request raised outside a turn — legal, and
        rendered without a turn attribution.
        """
        owner = next(
            (
                self._owners[key]
                for key in (agent_id, tool_use_id)
                if key and key in self._owners
            ),
            self._active_turn,
        )
        if owner is None:
            return None
        owner.translator.note_permission_prompt(tool_use_id)
        return owner.request_id

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

            # Before the SDK materialises a temp config dir and snapshots
            # the access token into it, make sure the token being copied
            # has a full lifetime ahead of it. The copy has its
            # `refreshToken` redacted by the SDK, so a short-lived token
            # captured here is one the CLI child can never renew — see
            # `token_refresh` for the whole failure and why it is ours to
            # prevent rather than the SDK's.
            self._cli_path = str(resolution.path)
            await self._refresh_access_token(self._cli_path)

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
            # Nothing a previous process started can still be running.
            self._forget_turns()
            # The wall clock `/usage` reports restarts with the ledger it sits
            # beside, and for the same reason: both describe what this engine
            # process has done, and a duration carried across a reconnect
            # would be measured against a total that was not.
            self._connected_since = time.monotonic()
            # A resume with a store materialises a temp CLAUDE_CONFIG_DIR
            # that only disconnect() cleans up, and the exit path reaches
            # disconnect() on a 2s budget at best (next.md § C8) and not at
            # all on Windows. Recorded here, removed by resume_cleanup.
            #
            # Kept as well as registered: it is also the directory whose
            # `.credentials.json` this session's CLI child reads, and
            # `_token_watchdog` writes a refreshed access token into it.
            self._materialized_dir = resume_cleanup.remember(client)
            self._token_watchdog_task = asyncio.create_task(
                self._token_watchdog(), name="aic-dc-token-watchdog"
            )
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

    async def _refresh_access_token(self, cli_path: str) -> None:
        """Renew the subscription access token if it is close to lapsing.

        Called once per connect, before options are built, and again on
        every watchdog tick. Never raises: a pre-flight check that could
        refuse a session which would have worked is worse than the expiry
        it guards against, so a failure becomes a health degradation and
        the connect continues.

        Because it runs repeatedly, it also *withdraws* what it reported.
        A refresh that works now is evidence against a sentence an earlier
        one left standing, and the watchdog is the only thing in a position
        to notice — see ``token_refresh.DEGRADATION_SENTENCES``.
        """
        try:
            outcome = await token_refresh.ensure_fresh(
                credential_path=subscription_credential_path(),
                config_dir=cli_config_dir(),
                cli_path=cli_path,
                cwd=self.repo_root,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a pre-flight check, never a connect
            logger.exception("Access-token pre-flight refresh failed; connecting anyway")
            return
        if outcome.detail:
            if outcome.ok:
                logger.info("%s", outcome.detail)
            else:
                self.health.note_degradation(outcome.detail)
        if outcome.ok:
            # A usable token retires both of this module's sentences, by
            # name so that nothing else's is touched. Not folded into the
            # branch above: the *refreshed* outcome carries a detail of its
            # own, and it is the strongest evidence there is that an earlier
            # failure has stopped standing.
            for sentence in token_refresh.DEGRADATION_SENTENCES:
                self.health.clear_degradation(sentence)

    async def _token_watchdog(self) -> None:
        """Keep a *running* session's access token from lapsing under it.

        The connect-time refresh only fixes the token a session starts
        with. A session that outlives its access token — eight hours,
        which an editor left open comfortably exceeds — hits the same
        wall, and reconnecting to fix it would be a heavier remedy than
        the problem needs.

        So this refreshes the parent and then copies the new access token
        into the config dir the CLI child is already reading. No reconnect,
        no refresh token in the child's file, and the parent stays the only
        thing that ever holds one.
        """
        credential_path = subscription_credential_path()
        if credential_path is None:
            return
        while True:
            expires_at = token_refresh.read_expiry(credential_path)
            if expires_at is None:
                return
            delay = token_refresh.seconds_remaining(expires_at) - token_refresh.REFRESH_MARGIN_SECONDS
            await asyncio.sleep(max(delay, TOKEN_WATCHDOG_MIN_SLEEP))
            if self._client is None or self._cli_path is None:
                return
            await self._refresh_access_token(self._cli_path)
            token_refresh.propagate(
                credential_path=credential_path,
                materialized_dir=self._materialized_dir,
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
        await self._stop_token_watchdog()
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
        translator = TurnTranslator(
            turn.request_id, non_subagent_tasks=self._non_subagent_tasks
        )
        active = ActiveTurn(
            request_id=turn.request_id,
            translator=translator,
            started_at=self._clock(),
            emit=emit,
        )
        self._active_turn = active
        self._pricing = active

        try:
            await self._take_main(active)
            await self._send(turn)
            return await self._pump(active)
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
            if active.results == 0:
                # Failed before any result was routed to it — `_fail_turn`
                # emitted the footer directly — so nothing retired it.
                self._retire(active)
            self._start_background_drain()

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

    async def _pump(self, active: ActiveTurn) -> dict[str, Any]:
        """Iterate to this turn's ``ResultMessage``, routing and emitting.

        Leaves the iterator only at this turn's result, exactly where
        ``receive_response()`` would, or when the engine itself fails and
        raises out of it.

        Reads ``receive_messages()`` rather than ``receive_response()``,
        which returns at the first result of any turn. With a background
        turn still being followed, the stream carries that turn's messages
        too, and every message is handed to the turn that owns it
        (:meth:`_route`) — so the pump ends on *its own* result, the same
        way ``receive_response()`` ends on the result, just without that
        method's single-turn assumption.
        """
        client = self._client
        result: dict[str, Any] | None = None

        try:
            async for message in client.receive_messages():
                await self._deliver(message)
                if active.results:
                    result = active.last_result
                    break
        except asyncio.CancelledError:
            # Someone cancelled the pump task itself. The turn is still
            # running inside the CLI, so say so rather than reporting a
            # clean finish.
            logger.warning("Message pump cancelled for request %s", active.request_id)
            raise
        except Exception as exc:
            logger.exception("Message pump failed for request %s", active.request_id)
            result = await self._fail_turn(active, active.emit, exc)

        if result is None:
            # The iterator ended without a result message, which it only
            # does if the stream closed under it.
            result = await self._fail_turn(
                active,
                active.emit,
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
        if event.name == "rateLimit":
            # Held as well as forwarded — see `rate_limit`. The payload is
            # kept whole rather than reduced to the fields the HUD renders
            # today, because it is the CLI's own record and the translator
            # already chose which of its fields to name.
            payload = event.payload
            if isinstance(payload, dict):
                # Keyed by window, so a seven-day record cannot overwrite the
                # five-hour one the account also has open. An untyped record
                # still gets a slot rather than being dropped: the enum is the
                # CLI's to extend, and a figure under a placeholder key is
                # more use than no figure.
                kind = payload.get("rate_limit_type")
                key = kind if isinstance(kind, str) and kind else "_untyped"
                self._rate_limits[key] = dict(payload)
                self._rate_limit_latest = key
            return event
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
                    **self._price_for(active, active.accrue(event.payload)),
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
    # Routing
    # ------------------------------------------------------------------

    async def _deliver(self, message: Any) -> None:
        """Translate one SDK message for the turn that owns it, and emit it.

        Whichever loop is reading — a turn's pump or the drain between turns
        — hands every message here, so a background subagent's messages reach
        the turn that spawned it however many turns later they arrive.
        """
        owner = self._route(message)
        if owner is None:
            return
        # Registered before translating: a nested tool call's own events can
        # follow in the same breath, and they route by the id this message
        # introduces.
        self._register(owner, message)
        for event in owner.translator.translate(message):
            event = self._fold_session_state(owner, event)
            if event.name == "streamComplete":
                await self._complete(owner, event)
                continue
            if event.name == "subagentEvent" and (
                event.payload.get("type") == "notification"
                or event.payload.get("terminal")
            ):
                # Main's next unprompted reply is to this.
                self._woken = owner
            await self._emit(owner.emit, event)

    def _route(self, message: Any) -> ActiveTurn | None:
        """The turn ``message`` belongs to.

        **Every message carries enough to say whose it is, and that is the
        whole design.** A subagent's messages carry the spawning ``Task``
        call's id as ``parent_tool_use_id``; a task's lifecycle messages carry
        its ``task_id`` and the id of the call that started it; a nested
        ``Bash`` inside a subagent is a task whose ``tool_use_id`` is the
        subagent's own tool call. Each of those was registered to a turn when
        it first appeared (:meth:`_register`), so a chain of lookups reaches
        the owner however deep the nesting — measured against the CLI on
        2026-09-23, not assumed.

        What carries none of those is main speaking, and main speaks for
        :attr:`_main`: the user turn that last took it, or — once a result
        has closed it — the turn whose task notification woke main up.

        A message keyed to a turn that has already retired is dropped rather
        than handed to whoever is current, which is the mis-attribution this
        routing exists to end.
        """
        keys = _routing_keys(message)
        for key in keys:
            owner = self._owners.get(key)
            if owner is not None:
                return owner
        if any(key in self._retired_keys for key in keys):
            return None
        if _is_main_speech(message) and not self._main_open:
            # Main speaking unprompted: the follow-up a task notification
            # woke it for, which answers the turn that owns the task.
            self._main = self._woken or self._main or self._pricing
            self._main_open = True
            self._woken = None
        return self._main or self._pricing

    def _register(self, owner: ActiveTurn, message: Any) -> None:
        """File every id ``message`` introduces under ``owner``."""
        from claude_agent_sdk import AssistantMessage

        if isinstance(message, AssistantMessage):
            for block in message.content or ():
                block_id = getattr(block, "id", None)
                if isinstance(block_id, str) and block_id:
                    self._owners[block_id] = owner
            return
        for key in _task_keys(message):
            self._owners[key] = owner

    async def _complete(self, owner: ActiveTurn, event: Event) -> None:
        """Emit a result for ``owner`` and settle what it ends.

        A result is always main's, and it closes main. The owner's *first*
        result is its footer; any later one is main's reply to a task
        notification and is flagged ``continuation`` so the browser revises
        the settled turn instead of appending a second footer —
        ``background_finished`` marks the last of them, which is what the
        service runs its post-turn housekeeping a second time on.
        """
        self._main_open = False
        owner.results += 1
        payload = dict(event.payload)
        if owner.results > 1:
            payload["continuation"] = True
            payload["background_finished"] = not owner.tasks_in_flight
        owner.last_result = payload
        await self._emit(
            owner.emit, Event("streamComplete", payload, turn_scoped=event.turn_scoped)
        )
        if owner.tasks_in_flight and not (owner.results == 1 and owner.cancelled):
            if owner.request_id not in self._background:
                logger.info(
                    "Turn %s finished with %d background task(s) still running; "
                    "following them past its result",
                    owner.request_id,
                    len(owner.tasks_in_flight),
                )
            self._background[owner.request_id] = owner
        else:
            self._retire(owner)
        await self._finalize_background(skip=owner)

    async def _take_main(self, turn: ActiveTurn) -> None:
        """Give main to a user turn as it is sent.

        Immediately, not once main's current reply ends: the CLI folds a
        prompt that arrives mid-reply into that reply, so the one result both
        produce is the only one the new turn will ever get.
        """
        self._main = turn
        self._main_open = True
        self._woken = None
        await self._finalize_background(skip=turn)

    async def _finalize_background(self, skip: ActiveTurn | None) -> None:
        """Close every background turn whose tasks have all finished.

        A turn whose task notification landed inside another turn gets no
        result of its own — the CLI answers it within that turn — so this is
        the only thing that ends it. The footer is its last one, revised with
        what the background work added.
        """
        for turn in list(self._background.values()):
            if turn is skip or turn.tasks_in_flight:
                continue
            base = turn.last_result or {}
            stats = turn.translator.stats
            # The engine's cumulative figures move on after `base` was
            # reported, and a browser adopts them as the session's total from
            # every result — so a stale pair here would wind that total back.
            current = {
                name: value
                for name, value in self._cost.session_totals().items()
                if value is not None
            }
            await self._emit(
                turn.emit,
                Event(
                    "streamComplete",
                    {
                        **base,
                        **current,
                        "response": turn.translator.response_text()
                        or base.get("response", ""),
                        "tool_calls": stats.tool_calls,
                        "permission_prompts": stats.permission_prompts,
                        "files_modified": list(stats.files_modified),
                        "background_tasks": [],
                        "continuation": True,
                        "background_finished": True,
                    },
                ),
            )
            self._retire(turn)

    def _retire(self, turn: ActiveTurn) -> None:
        """Stop routing to ``turn``; nothing more of it will be shown."""
        self._background.pop(turn.request_id, None)
        for key in [key for key, owner in self._owners.items() if owner is turn]:
            del self._owners[key]
            self._retired_keys.add(key)
        if self._woken is turn:
            self._woken = None

    def _forget_turns(self) -> None:
        """Drop every turn the router holds, for a new CLI process."""
        self._background.clear()
        self._owners.clear()
        self._retired_keys.clear()
        self._main = None
        self._main_open = False
        self._woken = None
        self._pricing = None

    def _price_for(self, turn: ActiveTurn, payload: dict[str, Any]) -> dict[str, Any]:
        """Price one result for ``turn``.

        The ledger's anchor is per turn and belongs to the newest one, so a
        result read for an older turn after a newer one started would be
        differenced against the wrong point. That turn keeps the cost its
        own last result was priced at; spend in the overlap is charged to the
        newer turn, whose anchor it falls after. The ledger still sees every
        result, which is what keeps its session totals current.
        """
        answer = self._cost.price(payload)
        if turn is self._pricing or turn.priced is None:
            turn.priced = answer
        return {**payload, **turn.priced}

    # ------------------------------------------------------------------
    # Background drain
    # ------------------------------------------------------------------

    def _start_background_drain(self) -> None:
        """Read the stream between turns.

        **A result message ends a turn, not the run.** The SDK goes on
        emitting for background tasks past a result, plus main's own reply
        once a task notification wakes it. Left unread, that sat in the
        client's buffer until the next turn read it — attributed to whatever
        turn happened to be reading, long after it meant anything live.
        Always on between turns, rather than only while a task is known to
        be in flight, so an unprompted reply is shown as it happens.
        """
        if self._client is None or self._session_lost:
            return
        if self._drain is not None and not self._drain.done():
            return
        self._drain = asyncio.create_task(self._drain_background(), name="cc-drain")

    async def _stop_background_drain(self) -> None:
        """Give the stream back before another consumer takes it.

        Two iterators over the SDK's message stream would split its messages
        between them arbitrarily, so the drain must be gone — not merely asked
        to go — before the next turn's pump starts.
        """
        task, self._drain = self._drain, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _stop_token_watchdog(self) -> None:
        """Cancel the token watchdog, if one is running.

        Awaited rather than fired and forgotten: the watchdog can be inside
        a refresh subprocess, and a cancel it never gets to observe would
        leave that CLI running past the session that spawned it.
        """
        task, self._token_watchdog_task = self._token_watchdog_task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _drain_background(self) -> None:
        """Deliver every message that arrives between turns.

        Runs until :meth:`_stop_background_drain` hands the stream to the
        next turn, or the stream ends.
        """
        client = self._client
        if client is None:
            return
        try:
            async for message in client.receive_messages():
                await self._deliver(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never fatal: no turn is running, and the next one re-reads the
            # stream from scratch. Losing the tail of a background agent is
            # worth strictly less than a raise out of a detached task.
            logger.exception("Background drain failed; stopping it")

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
        self._main_open = False
        if lost:
            self._session_lost = True
            self.health.connected = False
            # Nothing more will arrive for them either.
            for turn in self._background.values():
                turn.tasks_in_flight.clear()
            await self._finalize_background(skip=active)
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
        result = self._price_for(active, result)
        await self._emit(emit, Event("streamComplete", result))
        if lost:
            await self._emit(
                emit, Event("engineHealth", self.health.to_dict(), turn_scoped=False)
            )
        return result

    @property
    def session_usage(self) -> dict[str, Any]:
        """What this session has spent, for the surface that asks that question.

        The Context tab's Usage section and the CLI's own ``/usage`` panel ask
        "what has this session cost", which is the one question the engine's
        cumulative figures answer directly — see
        :meth:`~aic_dc.claude_code.cost.CostLedger.session_totals` for why they
        keep the engine's own field names here rather than being renamed.
        ``duration_seconds`` is ours; there is no API-time twin, and
        :attr:`session_duration_seconds` says why.
        """
        return {
            **self._cost.session_totals(),
            "duration_seconds": self.session_duration_seconds,
        }

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


def _task_keys(message: Any) -> list[str]:
    """The ids a ``Task*Message`` is known by, most specific first."""
    from claude_agent_sdk import (
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
        TaskUpdatedMessage,
    )

    if not isinstance(
        message,
        (TaskStartedMessage, TaskProgressMessage, TaskUpdatedMessage, TaskNotificationMessage),
    ):
        return []
    data = getattr(message, "data", None) or {}
    patch = getattr(message, "patch", None) or {}
    candidates = (
        getattr(message, "task_id", None),
        getattr(message, "tool_use_id", None),
        data.get("agent_id") if isinstance(data, dict) else None,
        patch.get("agent_id") if isinstance(patch, dict) else None,
    )
    return [key for key in candidates if isinstance(key, str) and key]


def _routing_keys(message: Any) -> list[str]:
    """The registered ids that can say which turn ``message`` belongs to.

    A subagent's messages name their parent call. A task's name the task.
    Main's own tool results name the call they answer, which is how the
    result of a backgrounded main-scope command reaches the turn that ran it.
    """
    from claude_agent_sdk import ToolResultBlock, UserMessage

    parent = getattr(message, "parent_tool_use_id", None)
    if isinstance(parent, str) and parent:
        return [parent]
    keys = _task_keys(message)
    if keys:
        return keys
    if isinstance(message, UserMessage) and isinstance(message.content, list):
        return [
            block.tool_use_id
            for block in message.content
            if isinstance(block, ToolResultBlock) and block.tool_use_id
        ]
    return []


def _is_main_speech(message: Any) -> bool:
    """Whether ``message`` is main starting or continuing a reply of its own."""
    from claude_agent_sdk import AssistantMessage, StreamEvent, SystemMessage, UserMessage

    if isinstance(message, SystemMessage):
        return getattr(message, "subtype", None) == "init"
    return isinstance(message, (AssistantMessage, StreamEvent, UserMessage))


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


# `_as_int` moved to `aic_dc.framing` with `ViewerFraming`, unchanged. It
# was this module's only caller, and leaving a copy behind would be a
# second coercion rule for the same JavaScript-shaped input.
