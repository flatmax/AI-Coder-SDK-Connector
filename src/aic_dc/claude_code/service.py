"""``ClaudeCodeService`` — the browser-facing RPC surface for the engine.

The RPC namespace is the class name: ``server.add_service(instance)``
derives it from ``type(instance).__name__``, so every method here is
reachable as ``ClaudeCodeService.<method>`` from the browser. **The class
name is interface, not implementation detail** — renaming it renames every
RPC and breaks every frontend call site
(``specs-reference/3-engine/session.md`` § Dependency quirks).

This service owns the *outward* half of a turn: admitting it, persisting
and broadcasting the user message, spawning the pump, and turning
:class:`~aic_dc.claude_code.messages.Event` objects into ``AcApp.*`` calls
on every connected browser. :class:`~aic_dc.claude_code.session.
EngineSession` owns the engine.

Since phase 3 it also owns the AIC-DC subsystems that outlived the native
engine and had nowhere else to live once ``LLMService`` was deleted: the
symbol index's LSP surface, the doc index's background build, review mode,
and the two git writes the user performs by hand. They are grouped in their
own sections below and share nothing with the turn path except this class.

Since phase 4 those indexes face the agent as well as the browser. This
class owns both halves of that: the :class:`~aic_dc.claude_code.mcp_server.
McpBridge` the session is handed as an MCP server, and the
:class:`~aic_dc.claude_code.hooks.Reindexer` behind the ``PostToolUse``
hook that keeps it honest after the agent writes.

Since phase 5 it owns history too: the transcript mirror, the events log
the transcript could never hold, reading a past session back for the
browser — its images and its subagents included — searching and deleting
them, the index that keeps both cheap, and choosing which session the
engine attaches to.

**The engine connects lazily**, on the first turn or an explicit
``connect_engine()`` call, so a launch that never chats never pays for a
second ``claude`` subprocess (~295 MB resident).

``resolve_permission`` is the most powerful method in the RPC inventory —
it authorises arbitrary ``Bash`` — and is localhost-only for that reason
(``specs5/3-engine/permissions.md`` § Collaboration and Authority).

Governing spec: ``specs5/3-engine/session.md``.
Reference: ``specs-reference/3-engine/session.md`` § Service:
ClaudeCodeService.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aic_dc.claude_code import sdk_surface
from aic_dc.claude_code.cost import UNPRICED
from aic_dc.claude_code.engine_config import PERMISSION_MODES, EngineConfig
from aic_dc.claude_code.events_log import (
    permission_mode_content,
    review_end_content,
    review_start_content,
    session_switch_content,
)
from aic_dc.claude_code.health import (
    DEFAULT_MIRROR_GAP_TOLERANCE,
    EngineStartupError,
)
from aic_dc.claude_code.hooks import Reindexer, build_hook_matchers
from aic_dc.claude_code.mcp_server import SERVER_NAME, McpBridge
from aic_dc.claude_code.messages import Event
from aic_dc.claude_code.permissions import (
    DENY_CANCELLED_REASON,
    PermissionBroker,
    read_denied_read_files,
    write_denied_read_files,
)
from aic_dc.claude_code.review import ReviewMode
from aic_dc.claude_code.session import (
    EngineNotReadyError,
    EngineSession,
    SessionLostError,
    Turn,
    TurnInProgressError,
    ViewerFraming,
)
from aic_dc.claude_code.session_store import DISK_WARNING_BYTES, RepoSessionStore

logger = logging.getLogger(__name__)

EventCallback = Callable[..., Awaitable[Any]]


# What becomes of a leading `/command`. Three outcomes, and the default is
# the one the old refusal table treated as impossible: pass it through.
#
# The premise that table was built on — built-in slash commands are terminal
# interface, so none of them can work here — turned out to be wrong about
# the mechanism. The CLI dispatches its own built-ins locally when they
# arrive as a prompt: no model turn, no cost, and the reply comes back as a
# synthetic assistant message. `/config`, `/effort`, `/autocompact`,
# `/reload-skills` and the rest have therefore been working all along,
# because they were never in the table. What the table did was refuse
# seventeen commands that mostly would have answered.
#
# So the tables below hold only the cases where passing through is actually
# wrong, and everything absent from both reaches the engine untouched —
# built-ins, skills, and custom commands from `.claude/commands/` alike
# (specs5/3-engine/session.md § Slash Commands).
#
# SLASH_ROUTES — the command's job is done better by an AIC⚡DC surface than
# by a block of CLI text. The reply names a `target` for the webapp to act
# on and no turn starts. `surface` completes "…is $surface here"; `palette`
# is the one-line description the `/` palette shows.
#
# `during_turn` is whether the command is still reachable while a turn
# streams. It is not a property of being routed — it is the answer to "does
# answering this need a model turn?". Only `query()` starts one; the SDK's
# other client methods are control requests on a channel that runs *beside*
# a live stream, which is how `can_use_tool` answers a permission dialog
# while the turn blocks on it. So a command whose answer is a control
# request stays available mid-turn — and routing is the mechanism that makes
# it so, because routing is what keeps it off `query()`. The two `False`
# entries are the session swaps, which would orphan the stream the user is
# watching (specs5/3-engine/session.md § Mid-turn availability).
#
# A `target` may name a section within a surface, after a `#`. Naming the tab
# alone is not enough when the tab has a segmented control: the Context tab
# remembers the section its reader last chose, so `/mcp` without the anchor
# opens onto whatever that was — usually Usage, which has no MCP status on it
# at all. The command would then have opened the right tab and still not shown
# the thing it names. What the section *means* is the surface's business and
# not this table's: on the Context tab it picks a segment; on the Settings tab
# it scrolls a panel into view and marks it, or opens a config card and selects
# the field the command named. All three answer the same question — "which part
# of this tab did the command mean?" — and the shell forwards the fragment
# without knowing any of the answers.
SLASH_ROUTES: dict[str, dict[str, Any]] = {
    "context": {
        "target": "tab:context#usage",
        "surface": "the Context tab, which is live rather than a one-shot print",
        "palette": "Show context usage in the Context tab",
        "during_turn": True,
    },
    # `/usage` and `/cost` both print a one-shot summary in the CLI. AIC⚡DC
    # computes the same figures itself from every result message and keeps
    # them cumulative over the session (`cost.py`), so the tab does not just
    # show them prettier — it stays true afterwards, and it is where the
    # per-model breakdown already lives.
    "usage": {
        "target": "tab:context#usage",
        "surface": "the Context tab's cost and per-model token breakdown",
        "palette": "Session cost and token usage, in the Context tab",
        "during_turn": True,
    },
    "cost": {
        "target": "tab:context#usage",
        "surface": "the Context tab's cost and per-model token breakdown",
        "palette": "Session cost and token usage, in the Context tab",
        "during_turn": True,
    },
    # Routed to reach `get_mcp_status()` — the live per-server connection
    # state, which the CLI's text block cannot keep current — and because
    # the reconnect and toggle controls are on that surface too.
    "mcp": {
        "target": "tab:context#session",
        "surface": "the Context tab's MCP section, with live per-server connection state",
        "palette": "MCP server status and tools, in the Context tab",
        "during_turn": True,
    },
    "agents": {
        "target": "tab:context#session",
        "surface": "the Context tab's subagent list",
        "palette": "The subagents this session can delegate to",
        "during_turn": True,
    },
    # Routed because the model *in force* is not in any file. `engine.json`
    # holds a request the CLI resolves — an alias like `opus`, or nothing at
    # all — and a `set_model` since then overrides it. Only the session knows
    # the answer, so the surface reads it from `get_current_state` and pairs it
    # with the CLI's own alias→`resolvedModel` mapping from the handshake.
    "model": {
        "target": "tab:settings#model",
        "surface": "the Settings tab's model control, which names what the alias resolves to",
        "palette": "The model in force, and switch it for this session",
        "during_turn": True,
    },
    # The mode this opens onto is `engine.json`'s — the one the *next* session
    # starts in. The running session's mode is the composer's own selector,
    # which is deliberately always visible and is not reachable by a route.
    # Said plainly here because the earlier wording promised "the Settings
    # tab's permission-mode control and rules list", and neither is there.
    #
    # The anchor is what makes that honest rather than merely accurate. Without
    # it the command opened the tab and left the reader in front of a card grid,
    # with the field it named one click and a scroll away inside a JSON file.
    # `permission-mode` opens the engine.json card and selects the
    # `permission_mode` line — the same treatment `/model` got, adapted to a
    # surface whose "control" is a line of text.
    "permissions": {
        "target": "tab:settings#permission-mode",
        "surface": (
            "the Settings tab, where engine.json holds the mode the next session "
            "starts in — the live mode is the selector beside the composer"
        ),
        "palette": "Permission mode in engine.json, in the Settings tab",
        "during_turn": True,
    },
    # Routed rather than passed through because the CLI's own `/clear` would
    # start a session the store never saw minted, and every other client
    # would still be rendering the old transcript.
    "clear": {
        "target": "new-session",
        "surface": "New Session",
        "palette": "Start a new session",
        "during_turn": False,
    },
    "resume": {
        "target": "history",
        "surface": "the history browser",
        "palette": "Browse and resume an earlier session",
        "during_turn": False,
    },
}

# SLASH_DENIED — passing through would reach for something this deployment
# does not have, or act on the host rather than the conversation. Answered
# explicitly; never forwarded as prose, which would turn a command into a
# question. Also filtered out of the `/` palette, so the only way to reach
# one is to type it.
SLASH_DENIED: dict[str, str] = {
    # Not the undo affordance it once named: file checkpoints are off
    # whenever the transcript is mirrored, which is every run with a repo
    # (specs5/plan/decisions.md CC-20).
    "rewind": (
        "the engine keeps no file checkpoints while the transcript is "
        "mirrored into the repo — use git"
    ),
    "heapdump": "it writes a heap snapshot to the CLI host's desktop",
    "login": "credentials are resolved from the environment at startup",
    "logout": "credentials are resolved from the environment at startup",
    "vim": "it is a terminal editing mode",
    "terminal-setup": "there is no terminal here to configure",
    "__remote-workflow": "it belongs to server-launched CLI sessions",
    "workflow-launch-exec": "it belongs to server-launched CLI sessions",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_control_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is the SDK's control-request deadline firing.

    The SDK gives this failure no class of its own — ``Query._send_control_request``
    raises a bare ``Exception(f"Control request timeout: {subtype}")`` — so the
    durable evidence is the ``TimeoutError`` that ``anyio.fail_after`` chains
    underneath it. Matched on the cause rather than on the message, because the
    string is prose the SDK is free to reword and a check that a reword silences
    is worse than no check.

    Read defensively in the same spirit as :func:`_is_connection_failure`: an SDK
    that stops chaining degrades to "not a timeout", which logs a full stack. The
    failure mode of being wrong here is a noisy log, never a quiet one.
    """
    return isinstance(exc.__cause__, TimeoutError)


def _log_control_failure(exc: Exception, what: str) -> None:
    """Log a failed control request, with a stack only when it earns one.

    A control-request timeout is an *expected* failure with a known shape and
    nothing in the traceback a reader can act on: every frame is SDK plumbing
    between here and an ``anyio.fail_after``. What it means is that the CLI did
    not answer inside 60s, and the two reasons for that — an engine busy with a
    turn, or an engine whose reader task has died and will never answer another
    control request — are indistinguishable from this side. See
    ``specs5/5-webapp/viewers-hud.md`` § *When the Engine Is Gone*, which states
    that residue rather than guessing at it.

    So a timeout gets one sentence and everything else keeps its stack. The
    polled caller is what made this worth separating: four tracebacks in one log,
    all of them about a loss the health banner had already reported in better
    words. ``reconnect_mcp_server`` and ``toggle_mcp_server`` already logged this
    shape without a stack; this makes the rule general instead of incidental.
    """
    if _is_control_timeout(exc):
        logger.warning("%s: %s", what, exc)
        return
    logger.exception("%s failed", what)


def _review_file_paths(changed_files: Any) -> list[str]:
    """Just the paths out of a review's changed-file dicts.

    The events log records which files a review covered, not their diff
    stats: the stats are recomputed from git whenever anyone asks, and
    archiving them would freeze numbers that the record cannot keep true.
    Tolerant of a malformed entry because this runs after the review has
    already started or ended.
    """
    paths: list[str] = []
    for entry in changed_files or []:
        path = entry.get("path") if isinstance(entry, dict) else None
        if path:
            paths.append(str(path))
    return paths


def _doc_convert_available() -> bool:
    """Whether document conversion can run — i.e. ``markitdown`` imports.

    An optional extra (``pip install 'aic-dc[docs]'``), so the import is
    probed rather than assumed, and any failure means "not available"
    rather than a broken snapshot.
    """
    try:
        from aic_dc.doc_convert import DocConvert

        return bool(DocConvert._probe_import("markitdown"))
    except Exception:
        return False


class ClaudeCodeService:
    """Browser → engine RPC. One instance per process.

    Parameters
    ----------
    config:
        The AIC-DC :class:`~aic_dc.config.Config`. Supplies the repo root
        (the session's ``cwd``) and the config directory holding
        ``engine.json``.
    repo:
        The ``Repo`` service, used to resolve and validate selected file
        paths. Optional so the engine can be exercised standalone.
    event_callback:
        ``async (event_name, *args)`` → dispatches to ``AcApp.<event_name>``
        on every connected browser. Wired after the RPC server starts, so
        it arrives as the indirection closure rather than the real thing.
    engine_config:
        Pre-parsed ``engine.json``, for tests. Loaded from ``config`` when
        omitted.
    """

    def __init__(
        self,
        config: Any,
        *,
        repo: Any = None,
        event_callback: EventCallback | None = None,
        engine_config: EngineConfig | None = None,
        session_store: Any = None,
    ) -> None:
        self._config = config
        self._repo = repo
        self._event_callback = event_callback
        # Set by main.py post-construction, matching every other service.
        self._collab: Any = None

        repo_root = getattr(config, "repo_root", None) or Path.cwd()
        self._repo_root = Path(repo_root)
        self.engine_config = engine_config or EngineConfig.load(
            getattr(config, "config_dir", None)
        )
        # Derived from config rather than injected by main.py, like
        # `engine_config` above and unlike DocConvert: it is a path, not a
        # collaborator, and a startup path that forgets to pass it would
        # lose the transcript mirror *silently* — the CLI keeps its own
        # copy, so the session works until the CLI's retention timer
        # expires it, which is days later and looks like data loss rather
        # than a missing argument. Still injectable, for tests.
        self.session_store = session_store or self._build_session_store()
        # Watch what the mirror writes, for the one fact about a message
        # that only exists after the CLI has written it: the entry `uuid` an
        # image pointer is built from. Registered by capability rather than
        # unconditionally, because an injected store need not have the hook —
        # and losing it costs a collaborator's thumbnails, not the turn.
        add_observer = getattr(self.session_store, "add_append_observer", None)
        if callable(add_observer):
            add_observer(self._on_entries_mirrored)
        # The events the transcript never holds — commits, resets, mode
        # switches. Built the same way as the store and for the same reason:
        # a path derived from config, not a collaborator injected by the
        # startup path. `None` without a repo, where there is nowhere to
        # write and nothing to browse.
        self.events_log = self._build_events_log()
        # Derived from the store, so it is built with it and `None` for the
        # same reason: nothing to derive from without a repo.
        self.history_index = self._build_history_index()
        # The permission gate. Constructed before the session because the
        # session is built *around* its callback: attaching it afterwards
        # would need a reconnect, and a session running without it would
        # write files without asking.
        self.permissions = PermissionBroker(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            note_mode=self._note_permission_mode,
            localhost_available=self._localhost_available,
        )
        # AIC-DC's own indexes. These are not engine state and did not come
        # from the native engine — they back the symbol/outline tools the
        # CLI has no equivalent of, so they outlive it (specs5/plan/
        # inventory.md § Backend — KEEP). Attached by the startup path
        # because both are built after the RPC server is already serving.
        #
        # Constructed here, ahead of the session, because the session is
        # built *around* them: the MCP bridge and the post-write re-index
        # are constructor arguments to it, for the same reason the
        # permission gate is.
        self.symbol_index: Any = None
        # Three states, not two. "Absent" and "still building" and "built"
        # are different answers to a tool call, and a lone `is None` check
        # cannot tell the middle one from either edge — a half-built index
        # answers queries happily, with half the repo missing.
        self._symbol_index_ready = False
        self._symbol_index_failed = False
        self.doc_builder = self._build_doc_builder()

        self.reindexer = Reindexer(
            symbol_index=self._live_symbol_index,
            doc_builder=self.doc_builder,
            broadcast=self._broadcast,
            repo_root=self._repo_root,
        )
        self.mcp_bridge = McpBridge(
            symbol_index=self._live_symbol_index,
            symbol_index_ready=lambda: self._symbol_index_ready,
            doc_index=self._live_doc_index,
            doc_index_ready=lambda: self.doc_builder.ready,
            review_state=self.get_review_state,
            ui_state=self._ui_state_snapshot,
            flush=self.reindexer.flush,
        )
        # Filled by the call below, drained onto the health record once the
        # session that owns it exists.
        self._degradations: list[str] = []
        hooks, mcp_servers = self._build_bridge_wiring()

        self.session = EngineSession(
            self._repo_root,
            self.engine_config,
            can_use_tool=self.permissions.can_use_tool,
            hooks=hooks,
            mcp_servers=mcp_servers,
            session_store=self.session_store,
        )
        # When a run of failed mirror appends stops being bad luck. Handed
        # over as a callable, not a number, so an edited `app.json` takes on
        # the next broadcast the way its other keys do — and set here rather
        # than in the session's constructor because the threshold is ours to
        # know: the session has `engine.json`, not the app config.
        self.session.health.mirror_gap_tolerance = self._mirror_gap_tolerance
        # What the bridge wiring above could not give this session. Set here
        # for the same reason the tolerance is: the wiring is built before the
        # session exists, and the record it belongs on is the session's.
        for sentence in self._degradations:
            self.session.health.note_degradation(sentence)

        # Last-known viewer state, pushed by the browser on navigation.
        # Held on the service rather than passed per turn because a tool
        # call can ask for it mid-turn, long after the prompt was composed.
        self._viewer_state: dict[str, Any] | None = None
        # Serialises connect attempts from concurrent first turns, so two
        # clients sending at once cannot spawn two CLI subprocesses.
        self._connect_lock = asyncio.Lock()
        self._connect_error: str | None = None
        # What the next connect should attach to, when `resume_session` has
        # asked for something specific: (session_id, fork). Held rather than
        # passed so the decision is made *inside* the connect lock — a
        # concurrent first turn would otherwise connect without it and get a
        # blank session where the user asked for their old one.
        self._resume_request: tuple[str, bool] | None = None
        # Whether a connect nobody gave a target for should continue the
        # previous conversation. True at startup, because a server restart is
        # meant to be invisible (``specs5/plan/README.md`` phase 5: "restarting
        # the server resumes the previous conversation with context intact").
        # `new_session` is the one thing that clears it.
        self._auto_resume = True
        self._turn_tasks: set[asyncio.Task[Any]] = set()
        # One commit at a time. The button stays disabled until the
        # commitResult broadcast arrives, and two overlapping runs would
        # stage each other's half-finished work.
        self._committing = False
        # The session-directory size warning fires at most once per server
        # lifetime. A user who has decided a gigabyte of transcripts is
        # fine should not be told again every turn — that is how a warning
        # worth reading becomes one nobody reads.
        self._disk_warned = False

        self.review = ReviewMode(
            repo=repo,
            broadcast=self._broadcast,
            set_permission_mode=self._set_review_permission_mode,
            current_permission_mode=lambda: self.session.permission_mode,
            restricted=self._check_localhost_only,
        )
        self.review.doc_builder = self.doc_builder

    def _build_session_store(self) -> Any:
        """The repo-local transcript mirror, or ``None`` without a repo.

        No repo means no ``.aic-dc/`` to mirror into, and the engine runs
        fine without a store — the CLI keeps its own transcript under
        ``~/.claude/projects/`` either way. What is lost is survival past
        the CLI's retention window and the history browser with it.
        """
        aic_dc_dir = getattr(self._config, "aic_dc_dir", None)
        if aic_dc_dir is None:
            logger.info(
                "No repo directory, so sessions are not mirrored; history "
                "will only last as long as the CLI keeps its own transcript."
            )
            return None
        return RepoSessionStore(Path(aic_dc_dir) / "sessions")

    def _history_config(self) -> dict[str, Any]:
        """``app.json``'s ``history`` section, or an empty dict.

        Read per use rather than cached, so ``reload_app_config`` takes
        without a restart — both values are consulted at the moment they
        matter, one on a size check and one when health is serialised.
        Empty when the config manager is a test stub without the property,
        which leaves each caller on its own documented default.
        """
        section = getattr(self._config, "history_config", None)
        return section if isinstance(section, dict) else {}

    def _mirror_gap_tolerance(self) -> int:
        """The configured gap tolerance, for :class:`EngineHealth` to read."""
        return int(
            self._history_config().get(
                "mirror_gap_tolerance", DEFAULT_MIRROR_GAP_TOLERANCE
            )
        )

    def _build_events_log(self) -> Any:
        """``.aic-dc/events.jsonl``, or ``None`` without a repo.

        Nothing derives this file, so nothing can rebuild it — but its
        absence costs only the operational lines in a browsed transcript,
        never a session. So a repoless run logs a note and carries on
        rather than failing to start.
        """
        aic_dc_dir = getattr(self._config, "aic_dc_dir", None)
        if aic_dc_dir is None:
            return None
        from aic_dc.claude_code.events_log import EventsLog

        return EventsLog(Path(aic_dc_dir) / "events.jsonl")

    def _build_history_index(self) -> Any:
        """``.aic-dc/index/<project_key>.json``, or ``None`` without a store.

        Unlike the events log, losing this file costs nothing but time: it
        is derived from the transcripts, rebuilt on the next search, and
        deleting it is a supported operation
        (``specs5/3-engine/history.md`` § The Derived Index). Per
        ``project_key``, because two worktrees of one repo have two session
        directories and an index that mixed them would answer for the wrong
        one.
        """
        aic_dc_dir = getattr(self._config, "aic_dc_dir", None)
        if aic_dc_dir is None or self.session_store is None:
            return None
        from aic_dc.claude_code.history_index import HistoryIndex

        return HistoryIndex(
            Path(aic_dc_dir) / "index" / f"{self._session_project_key()}.json",
            self.session_store,
            str(self._repo_root),
        )

    async def _record_event(
        self,
        event: str,
        content: str,
        *,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        """Append one operational event to this session's history.

        Every failure path here is a no-op on purpose. The caller is a
        commit, a reset or a mode switch that has **already happened**;
        raising from the record of it would fail a completed action, and
        the log counts its own losses (``EventsLog.write_failures``).

        Silent when there is no session yet: the log drops those itself,
        because a record with no session has no transcript to appear in.

        ``session_id`` overrides which session the record belongs to, for
        the one caller whose event is *about* a session other than the live
        one: forking, where the new session has no ID yet and the record
        belongs in the transcript the user forked away from.
        """
        if self.events_log is None:
            return
        try:
            await self.events_log.append(
                event,
                session_id=session_id or self.session.session_id,
                content=content,
                request_id=request_id,
                payload=payload,
            )
        except Exception:
            logger.exception("Could not record a %r event", event)

    def _session_project_key(self) -> str:
        """The store's project key for this repo.

        Underscored deliberately: `ExposeClass` publishes every public
        method as an RPC, and this is an internal detail of how the
        history methods find their sessions, not a question a browser asks.

        Uses the SDK's own helper rather than sanitising the path here:
        the key must match what the CLI computes, or ``list_sessions`` and
        every ``*_from_store`` reader look in a directory nothing writes
        to. Two worktrees of one repo get different keys, which is correct.
        """
        from claude_agent_sdk import project_key_for_directory

        return project_key_for_directory(str(self._repo_root))

    def _build_doc_builder(self) -> Any:
        """Construct the doc index and the builder that fills it.

        Unconditional, because construction is cheap: no grammars, no
        model. The sentence-transformer behind keyword enrichment loads
        lazily on first use and is absent entirely from a stripped install,
        where the outlines simply carry no keywords.

        Imported here rather than at module scope so the doc-index package
        is only pulled in when a service is actually built — the RPC layer
        imports this module to introspect the surface.
        """
        from aic_dc.doc_index.background import DocIndexBuilder
        from aic_dc.doc_index.index import DocIndex
        from aic_dc.doc_index.keyword_enricher import (
            EnrichmentConfig,
            KeywordEnricher,
        )

        doc_config = getattr(self._config, "doc_index_config", None) or {}
        enricher = KeywordEnricher(
            model_name=doc_config.get("keyword_model", "BAAI/bge-small-en-v1.5")
        )
        doc_index = DocIndex(
            repo_root=self._repo.root if self._repo is not None else None,
            enricher=enricher,
            enrichment_config=EnrichmentConfig.from_dict(doc_config),
        )
        return DocIndexBuilder(
            doc_index=doc_index,
            enricher=enricher,
            enrichment_enabled=self._keyword_enrichment_enabled,
            repo=self._repo,
            progress=self._send_startup_progress,
        )

    def _keyword_enrichment_enabled(self) -> bool:
        """``app.json``'s ``doc_index.keywords_enabled``, read fresh.

        Handed to :class:`DocIndexBuilder` as a callable, so a reload of
        ``app.json`` — which the Settings tab performs after a save —
        reaches the next enrichment pass without a relaunch. Reading it
        once here and passing the boolean would make the preference an
        app-restart field, and there is no third disposition on that tab
        between "now" and "next session".

        The key was parsed by ``ConfigManager.doc_index_config`` from the
        day the section existed and read by nothing until this method: it
        was a documented switch with no wire behind it, so enrichment ran
        whatever the file said. See ``specs5/2-indexing/keyword-enrichment.md``
        § Switching Enrichment Off.
        """
        doc_config = getattr(self._config, "doc_index_config", None) or {}
        return bool(doc_config.get("keywords_enabled", True))

    def _build_bridge_wiring(self) -> tuple[Any, Any]:
        """The ``hooks`` and ``mcp_servers`` the session is built with.

        Degrades to ``(None, None)``: without the bridge the agent loses
        the symbol map and the outline tools and keeps every built-in, and
        without the hook the file tree needs a manual refresh. Both are
        worth losing to keep a session that starts. Refusing to construct
        would trade a missing feature for a dead editor.

        Each loss is also recorded in ``self._degradations``, which the
        constructor hands to ``session.health`` the moment the session
        exists. A log line was the whole report until phase 6, and
        ``mcp-bridge.md`` § Availability and Degradation had always asked for
        a banner: without one the agent simply appears inexplicably worse at
        repo-wide questions, which is the hardest kind of fault to attribute.
        """
        try:
            hooks = build_hook_matchers(self.reindexer, self._broadcast)
        except Exception as exc:
            logger.warning(
                "Hooks unavailable: the file tree and the indexes will not "
                "follow the agent's writes, and a compaction pause will go "
                "unannounced: %s",
                exc,
            )
            hooks = None
            self._degradations.append(
                "The post-write re-index hook did not start, so the file tree "
                "and the symbol map will not follow the agent's writes — "
                "refresh them by hand after it edits files."
            )
        try:
            mcp_servers = {SERVER_NAME: self.mcp_bridge.build_server()}
        except Exception as exc:
            logger.warning(
                "The aic-dc MCP bridge failed to build; the agent will fall "
                "back to Glob/Grep/Read for repo structure: %s",
                exc,
            )
            mcp_servers = None
            self._degradations.append(
                "The aic-dc repo tools did not start, so the agent has no "
                "symbol map, no document outlines and no reference graph — it "
                "will fall back to Glob, Grep and Read, which answer "
                "repo-wide questions less well."
            )
        return hooks, mcp_servers

    def _live_symbol_index(self) -> Any:
        """The symbol index the bridge and the re-index should read.

        None once construction or the initial walk has failed, rather than
        the partially-built index that is sitting right there. A map that
        silently omits half the repo is read as "these files have no
        symbols", and the agent does not go back to check — so the honest
        answer is to report the index as unavailable and let it use Grep.
        """
        if self._symbol_index_failed:
            return None
        return self.symbol_index

    def _live_doc_index(self) -> Any:
        """The doc index, or None when its build failed outright."""
        if getattr(self.doc_builder, "failed", False):
            return None
        return self.doc_builder.doc_index

    def _ui_state_snapshot(self) -> dict[str, Any]:
        """What the user is pointing at, for the ``ui_state`` tool.

        Paths and modes only — never file content. The agent reads files
        with its own tools; this answers the one question those cannot
        (``specs5/plan/decisions.md`` CC-14).

        No file list: pointing at a file is something the user does in the
        prompt now, where the agent already sees it, so there is no
        browser-side set for this to report (CC-21).
        """
        return {
            "viewer": dict(self._viewer_state) if self._viewer_state else None,
            "review_state": self.get_review_state(),
            "permission_mode": self.session.permission_mode,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect_engine(self, resume: str | None = None) -> dict[str, Any]:
        """Connect the engine, or report why it will not connect.
        **Localhost only.**

        Idempotent, and safe to call concurrently. Returns rather than
        raises on failure: the caller is a browser, and an RPC exception
        would surface as a generic transport error instead of the
        actionable message the failure carries.

        Gated because of ``resume``: this is the phase-2 shape of "resume
        session", which ``specs5/4-features/collaboration.md`` § Localhost-Only
        Operations restricts. A participant passing a session id would decide
        which conversation the host's engine attaches to. Starting the CLI at
        all also spends the host's credentials and rate-limit headroom.

        ``chat_streaming``'s lazy connect calls this from inside its own
        gate, which is fine — the caller identity outlives the ``await``, so
        the inner check sees the localhost caller that got past the outer one.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        async with self._connect_lock:
            if self.session.ready:
                return {"status": "ready", "health": self.session.health.to_dict()}
            attach, fork = await self._resume_attachment(resume)
            try:
                await self.session.connect(resume=attach, fork_session=fork)
            except EngineStartupError as exc:
                self._connect_error = str(exc)
                logger.error("Claude Code engine failed to start: %s", exc)
                await self._broadcast(
                    Event("engineHealth", self.session.health.to_dict(), turn_scoped=False)
                )
                return {"error": str(exc), "reason": "startup_failed"}
            self._connect_error = None
            # Consumed: a request is for one connect. Auto-resume comes back
            # on, so a session lost mid-conversation reattaches to itself
            # rather than silently continuing as a blank one.
            self._resume_request = None
            self._auto_resume = True
            await self._broadcast(
                Event("engineHealth", self.session.health.to_dict(), turn_scoped=False)
            )
            return {"status": "ready", "health": self.session.health.to_dict()}

    async def _resume_attachment(self, requested: str | None) -> tuple[str | None, bool]:
        """Which session the imminent connect attaches to, and whether to fork.

        Called with the connect lock held, which is the point: the choice
        has to be made where a concurrent first turn cannot connect around
        it. Four cases, in order of how explicit the ask was:

        1. An ID passed to ``connect_engine`` — an explicit caller wins.
        2. A pending ``resume_session`` request, carrying its fork flag.
        3. Auto-resume: the session we already had this process, else the
           most recent in the store. This is what makes a restart
           invisible.
        4. Nothing, after ``new_session`` — a blank session is the ask.

        Cases 3 and 4 are :meth:`_visible_session_id`, deliberately: the
        session the engine attaches to and the session the browser is shown
        have to be the same one, and two functions answering that
        separately is how they come to disagree.
        """
        if requested:
            return requested, False
        if self._resume_request is not None:
            return self._resume_request
        return await self._visible_session_id(), False

    async def _most_recent_session_id(self) -> str | None:
        """The newest session in the store, or ``None`` if there is none.

        No stored pointer to the "current" session: the store already sorts
        by ``last_modified``, and a pointer file is one more thing that can
        disagree with the transcripts it names.
        """
        if self.session_store is None:
            return None
        try:
            from claude_agent_sdk import list_sessions_from_store

            recent = await list_sessions_from_store(
                self.session_store, str(self._repo_root), limit=1
            )
        except Exception:
            # Never fatal. Failing to find the previous conversation costs
            # continuity; refusing to start costs the user everything.
            logger.exception("Could not work out which session to resume")
            return None
        return recent[0].session_id if recent else None

    async def shutdown(self) -> dict[str, Any] | None:
        """Disconnect the engine as part of graceful shutdown.
        **Localhost only.**

        Called from :func:`aic_dc.main._shut_the_engine_down`, on the loop,
        bounded, before the signal handler's ``os._exit``. For most of its
        life it had no caller at all and this docstring reasoned about one
        anyway (``next.md`` § C8); the wiring, and the reason only one of
        the four steps below justifies it, are recorded there.

        **What survives the process is the point.** The kill and the temp-dir
        purge that ``session.disconnect()`` would do are already done by hand
        in ``main.py``, and cancelling asyncio tasks in a process about to
        ``os._exit`` achieves nothing. Denying pending permissions does: the
        deny reaches the CLI's waiting control request, and
        ``permissionResolved`` reaches the *browser*, which outlives the
        server. Without it a dialog open at Ctrl-C stayed on screen forever.

        Gated because ``add_service`` exposes every public method, which
        makes process teardown reachable from a browser: without the check
        a participant could kill the host's engine mid-turn, which is a
        broader denial than ``cancel_streaming``. The gate does not get in
        the way of the real caller — ``is_caller_localhost`` trusts a call
        with no RPC caller behind it, so the teardown hook passes. It reads
        the *current* RPC caller, though, so a remote participant's call
        caught mid-dispatch by the signal can refuse the teardown; the
        caller logs that rather than papering over it.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            logger.warning("Rejected a non-localhost attempt to shut the engine down")
            return restricted
        await self.permissions.cancel_all()
        for task in list(self._turn_tasks):
            task.cancel()
        self.doc_builder.close()
        await self.session.disconnect()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def get_engine_health(self) -> dict[str, Any]:
        """Which CLI, which version, which credentials, and any warnings."""
        health = self.session.health.to_dict()
        if self._connect_error and not health.get("last_error"):
            health["last_error"] = self._connect_error
        return health

    async def get_current_state(self) -> dict[str, Any]:
        """Everything a freshly connected browser needs to render.

        ``messages`` is the current session's transcript, rendered from the
        mirror. Read here, on demand, rather than pre-loaded by a startup
        step as an earlier draft of ``specs5/6-deployment/startup.md`` had
        it: the guarantee that step wanted — "previous messages to the first
        browser connection" — holds by construction this way, and there is
        no second copy of the conversation to go stale. It is a **read, not a
        resume**: no engine is started to answer this.

        Before the first connect the session shown is the one the next
        connect will auto-resume, which is what makes a server restart
        invisible: the model gets its context back from ``resume`` and the
        user gets the matching transcript on first paint.

        Note the frontend contract: a browser that reconnects *during* a
        turn takes the live turn from ``active_streams`` and ignores
        ``messages`` entirely (``onStateLoaded``'s ``wasStreaming``
        early-return), so the two can never double-render the same blocks.

        ``pending_permissions`` is how a client that connects while a
        dialog is open gets the dialog: the request was broadcast before it
        was listening, and the callback is still waiting.

        The ``doc_index_*`` / ``enrichment_status`` fields and
        ``review_state`` are not engine state either — they belong to
        AIC-DC's own subsystems, which this service now owns. The shell reads
        them on first paint to decide whether to show the doc-index progress
        overlay and the review banner.

        ``disk_warning`` is the startup half of the session-directory size
        check (``specs-reference/3-engine/history.md`` § Numeric constants:
        "checked at startup and after each turn"). First paint is where
        "startup" is observable — a warning broadcast before any browser was
        listening would be a warning nobody saw — and the one-shot flag is
        shared with the post-turn check, so it appears exactly once whichever
        of the two notices first.

        ``doc_convert_available`` is not engine state — it is a server
        capability probe that the shell has nowhere else to read. Document
        conversion survives the conversion untouched
        (``specs5/plan/inventory.md`` § Frontend — KEEP unchanged), and this
        snapshot is the only one the shell fetches once the chat path moves
        off ``LLMService``, so the probe comes with it.

        ``repo_root`` is the one absolute path the browser is given, and it
        is given it so that it can stop sending absolute paths back. Claude
        Code's file tools take absolute paths, so every path a tool card
        attributes to a call is absolute (``files_written_by``), while every
        ``Repo`` method takes a repo-relative one and rejects an absolute
        path outright. The browser had no way to convert between the two,
        which made a click on a tool card's file chip a guaranteed
        ``Absolute paths not accepted``. The root is not a secret — it is in
        the window title's repo name and in every exec dialog's working
        directory — and sending the whole thing once is cheaper than
        relativising every path in every payload that carries one.
        """
        return {
            "messages": await self._current_messages(),
            "denied_read_files": self.get_denied_read_files(),
            "session_id": self.session.session_id,
            "repo_name": self._repo_root.name,
            "repo_root": str(self._repo_root),
            "init_complete": True,
            "engine_ready": self.session.ready,
            "streaming_active": self.session.streaming_active,
            "active_streams": self.session.active_streams(),
            # None unless the engine is compacting right now. A browser
            # refreshed during the pause used to reconnect into a session that
            # looked idle while the engine was still summarising — tens of
            # seconds of apparently hung UI, which is the failure the
            # indicator exists to prevent.
            "compaction": self.session.compaction_state,
            # The last rate-limit record, for the HUD's Rate limits section.
            # None until the CLI sends one, which it does on a status change
            # rather than per turn — so unlike `compaction` above, this is
            # usually the *only* way a browser learns the figure at all.
            "rate_limit": self.session.rate_limit,
            "permission_mode": self.session.permission_mode,
            "model": self.session.model,
            "pending_permissions": self.permissions.pending(),
            **self.doc_builder.status(),
            "review_state": self.review.state(),
            "engine_health": self.get_engine_health(),
            "doc_convert_available": _doc_convert_available(),
            "disk_warning": await self._disk_warning(),
        }

    async def _current_messages(self) -> list[dict[str, Any]]:
        """The rendered transcript of the session the browser is looking at.

        Underscored: ``ExposeClass`` publishes every public method, and a
        browser that wants a specific session's messages asks
        ``history_load`` for it by ID.

        An unreadable transcript renders as an empty conversation rather
        than a failed snapshot. Every other field here — health, review
        state, pending permissions — is still worth painting, and a browser
        that cannot get a snapshot cannot show the user anything at all.
        """
        session_id = await self._visible_session_id()
        if not session_id:
            return []
        loaded = await self.history_load(session_id)
        if isinstance(loaded, dict):
            logger.warning(
                "Could not render session %s for the state snapshot: %s",
                session_id,
                loaded.get("error"),
            )
            return []
        return loaded

    async def _visible_session_id(self) -> str | None:
        """Which session's transcript the browser should be shown.

        The one we are attached to, or — before the engine has ever
        connected — the one the next connect will attach to. Those are the
        same conversation, so showing the second before it is live is not a
        guess; ``_resume_attachment`` makes the same choice from the same
        three sources.

        ``None`` after ``new_session``, which is the one case where the next
        turn starts a conversation that does not exist yet.
        """
        if self.session.session_id:
            return self.session.session_id
        if self._resume_request is not None:
            return self._resume_request[0]
        if not self._auto_resume:
            return None
        return await self._most_recent_session_id()

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    async def chat_streaming(
        self,
        request_id: str,
        message: str,
        images: list[str] | None = None,
        viewer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a turn. Returns as soon as the engine has accepted it.
        **Localhost only.**

        The turn runs in a background task whose lifetime is independent of
        any WebSocket, so a client that disconnects mid-turn re-attaches to
        a turn that kept running.

        Only localhost clients can send prompts, even a promoted
        non-localhost host (``specs5/4-features/collaboration.md`` § Roles).
        A prompt is the one input that makes the agent write files, and the
        permission gate cannot substitute for the restriction: a remote
        participant who cannot answer a dialog can still queue work whose
        dialogs the host then clicks through.

        The gate goes ahead of the slash-command reply so a participant
        cannot use ``/context`` as a probe for whether they are restricted.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted

        slash = self._slash_response(message)
        if slash is not None:
            return slash

        if not self.session.ready:
            # Lazy connect: the first turn is what starts the CLI.
            outcome = await self.connect_engine()
            if "error" in outcome:
                return {"error": outcome["error"], "reason": outcome["reason"]}

        turn = Turn(
            request_id=request_id,
            message=message,
            images=list(images or []),
            # The browser may send the viewer with the turn; when it does
            # not, the last `set_viewer_state` push stands in. Same fact,
            # two arrival paths — and the push is the one that keeps
            # working when the turn comes from somewhere else.
            viewer=ViewerFraming.from_dict(
                viewer if viewer is not None else self._viewer_state
            ),
        )

        try:
            self.session.admit(request_id)
        except TurnInProgressError as exc:
            return {"error": str(exc), "reason": "turn_in_progress"}
        except EngineNotReadyError as exc:
            return {"error": str(exc), "reason": "not_ready"}
        except SessionLostError as exc:
            return {"error": str(exc), "reason": "session_lost"}
        except ValueError as exc:
            return {"error": str(exc), "reason": "bad_request"}

        # Broadcast before the turn starts so a collaborator's transcript
        # shows the message even if the turn then fails. `image_refs` is
        # empty *here* and filled in by the `userMessageImages` follow-up:
        # a pointer needs the entry uuid, and the entry the CLI writes does
        # not exist yet. Data URIs are never broadcast either way, because a
        # handful of screenshots would be megabytes per client
        # (``specs5/4-features/images.md`` § Engine Service Integration).
        await self._broadcast(
            Event(
                "userMessage",
                {
                    "content": message,
                    "request_id": request_id,
                    "image_refs": [],
                },
                turn_scoped=False,
            )
        )

        task = asyncio.create_task(self._run_turn(turn), name=f"cc-turn-{request_id}")
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return {"status": "started"}

    async def cancel_streaming(self, request_id: str) -> dict[str, Any]:
        """Interrupt the turn in flight. **Localhost only.**

        The pump keeps running to the result message; this only asks the
        engine to stop. See ``specs5/3-engine/session.md`` § Cancellation.

        Gated because interrupting someone else's turn mid-edit is a way to
        leave the tree half-written, and because a participant who can cancel
        every turn can deny the host the tool entirely.

        Pending permissions are denied *first*, before the interrupt reaches
        the CLI. An unanswered request is what the CLI is blocked on, so
        releasing it is what makes the interrupt actionable at all — and it
        keeps ``_watch_drain`` from expiring and losing the session over a
        dialog nobody answered. Stop is therefore the way out of a dialog
        you do not want to answer, which is what lets the request itself
        wait indefinitely (``permissions.py`` § Deadline).
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.permissions.cancel_for_turn(
                request_id, reason=DENY_CANCELLED_REASON
            )
            return await self.session.interrupt(request_id)
        except Exception as exc:
            logger.exception("cancel_streaming failed for %s", request_id)
            return {"error": f"Could not cancel the turn: {exc}"}

    async def _run_turn(self, turn: Turn) -> None:
        """Drive one turn to completion, then do post-turn housekeeping."""
        emit = self._emitter(turn.request_id)
        try:
            await self.session.run_turn(turn, emit)
        except asyncio.CancelledError:
            raise
        except Exception:
            # run_turn emits its own synthetic streamComplete for engine
            # failures; reaching here means the failure was ours.
            logger.exception("Turn %s failed outside the pump", turn.request_id)
            await emit(
                Event(
                    "streamComplete",
                    {
                        "session_id": self.session.session_id,
                        "response": "",
                        "subtype": "error_during_execution",
                        "terminal_reason": "engine_error",
                        "is_error": True,
                        "num_turns": 0,
                        "duration_ms": 0,
                        "duration_api_ms": 0,
                        "usage": None,
                        "model_usage": None,
                        "total_cost_usd": None,
                        # The engine sent no footer, so nothing here can be
                        # differenced against the session's running total.
                        # "unpriced", not zero: the turn may have spent
                        # plenty before we lost track of it.
                        "turn_cost_usd": None,
                        "turn_cost_basis": UNPRICED,
                        "turn_model_usage": None,
                        "tool_calls": 0,
                        "permission_prompts": 0,
                        "files_modified": [],
                        "cancelled": False,
                        "mirror_gap": False,
                        "user_message_id": None,
                    },
                )
            )
        finally:
            # The backstop for the invariant that no dialog outlives its
            # turn. `cancel_streaming` normally gets here first; this covers
            # every other way a turn can end with a request still open — a
            # lost session, an engine crash, a drain that timed out.
            #
            # `spare_subagents` because a background subagent outlives this
            # turn: the SDK keeps stdin open with tasks in flight past the
            # result message, so a request it is blocked on is live work,
            # not a leftover dialog. `cancel_for_agent` closes those when the
            # subagent ends (`permissions.py` § spare_subagents).
            try:
                await self.permissions.cancel_for_turn(
                    turn.request_id, spare_subagents=True
                )
            except Exception:
                logger.exception(
                    "Could not sweep pending permissions for turn %s",
                    turn.request_id,
                )
            await self._post_response(turn.request_id)

    async def _post_response(self, request_id: str) -> None:
        """Post-turn housekeeping, then ``postResponseComplete``.

        Always fires after ``streamComplete`` for the same turn, and always
        fires — the Context tab and the file tree wait on it for consistent
        derived state, so a skipped event leaves them stale indefinitely.

        The re-index is flushed first so ``files_reindexed`` is the whole
        turn's list rather than whatever the debounce happened to have
        finished by the time the last message arrived.
        """
        try:
            await self.reindexer.flush()
        except Exception as exc:
            # A stale index is not a reason to withhold the turn footer.
            logger.debug("Post-turn re-index flush failed: %s", exc)
        files_reindexed = self.reindexer.take_reindexed()

        context_usage: dict[str, Any] | None = None
        if self.session.ready:
            try:
                context_usage = await self.session.get_context_usage()
            except Exception as exc:
                # A failed refetch is a stale tab, not a failed turn.
                logger.debug("Could not refresh context usage: %s", exc)
        await self._dispatch(
            Event(
                "postResponseComplete",
                {
                    "files_reindexed": files_reindexed,
                    "context_usage": context_usage,
                    "disk_warning": await self._disk_warning(),
                },
            ),
            request_id,
        )

    async def _disk_warning(self) -> str | None:
        """The mirrored transcripts' size warning, once per server lifetime.

        ``None`` every time but one: the first check that finds
        ``.aic-dc/sessions/`` over the configured threshold returns the
        sentence and every later check returns nothing, whether it came from
        a turn ending or a browser asking for its first paint.

        A transcript is the one thing under ``.aic-dc/`` that does not
        rebuild, so this is a warning rather than a cleanup: nothing is
        deleted, nothing is refused, and the user decides. Pasted images are
        usually the reason — they sit in the entries as base64, verbatim
        (``specs5/3-engine/history.md`` § `SessionStore`).

        The measurement is a directory walk, so it runs in the executor and
        a failure is silent: a size we could not read is not worth failing a
        completed turn over.
        """
        if self._disk_warned or self.session_store is None:
            return None
        try:
            loop = asyncio.get_running_loop()
            total = await loop.run_in_executor(None, self.session_store.total_bytes)
        except Exception as exc:
            logger.debug("Could not measure the session directory: %s", exc)
            return None
        if total < self._history_config().get(
            "session_dir_warning_bytes", DISK_WARNING_BYTES
        ):
            return None
        self._disk_warned = True
        gib = total / (1024 * 1024 * 1024)
        logger.warning("Mirrored session transcripts are using %.1f GiB", gib)
        return (
            f"Mirrored session transcripts are using {gib:.1f} GiB in "
            f"`.aic-dc/sessions/`. Pasted images are stored in the transcript "
            "itself, so a few image-heavy sessions account for most of it. "
            "Deleting old sessions from the history browser reclaims the "
            "space; nothing here needs doing now."
        )

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def resolve_permission(
        self, permission_id: str, decision: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a permission request. **Localhost only.**

        This is the method that authorises arbitrary ``Bash``, which makes
        it the highest-stakes application of the restriction policy: a
        remote participant able to call it would turn collaboration mode
        into a remote-code-execution grant. Non-localhost attempts are
        rejected *and logged*, per the invariant in
        ``specs5/3-engine/permissions.md``.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            logger.warning(
                "Rejected a non-localhost attempt to resolve permission %s "
                "(decision=%r)",
                permission_id,
                (decision or {}).get("action"),
            )
            return restricted
        return await self.permissions.resolve(
            permission_id, decision or {}, resolved_by=self._caller_label()
        )

    def get_denied_read_files(self) -> list[str]:
        """Paths the user has excluded from the agent's reads."""
        return read_denied_read_files(self._repo_root)

    def set_denied_read_files(self, files: list[str] | None = None) -> dict[str, Any]:
        """Replace the ``Read`` deny rules with ``files``. **Localhost only.**

        The rules go into ``.claude/settings.local.json``, which is
        git-ignored and one of our settings sources — so the exclusion is
        per-user, visible, and revocable by editing a file, rather than an
        invisible in-memory filter.

        The honest caveat is in the return value: the CLI reads its
        settings sources itself, and a rule written mid-session applies
        from its next read of them. We cannot push a rule into a running
        session — the SDK's only path for that is ``updated_permissions``
        on a ``can_use_tool`` result, which needs a permission request to
        ride along with.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            applied = write_denied_read_files(self._repo_root, list(files or []))
        except ValueError as exc:
            return {"error": str(exc)}
        logger.info("Denied-read rules now cover %d path(s)", len(applied))
        return {
            "denied_read_files": applied,
            "settings_file": str(self._repo_root / ".claude" / "settings.local.json"),
            "takes_effect": "on the CLI's next read of its settings sources",
        }

    def _note_permission_prompt(self, tool_use_id: str | None) -> str | None:
        """Bridge from the broker to the turn in flight."""
        note = getattr(self.session, "note_permission_prompt", None)
        if note is None:
            return None
        return note(tool_use_id)

    async def _note_permission_mode(self, mode: str) -> None:
        """A permission decision moved the mode; keep everyone in step.

        The CLI has already applied it — the update travelled back on the
        permission result — so this does not call ``set_permission_mode``.
        It updates what the session reports and broadcasts the same event
        the mode selector already listens for, attributed to the dialog so
        the mode does not appear to have changed itself.

        Archived with ``source: "engine"``, which is the whole reason the
        field exists: "accept edits from now on" checked in a permission
        dialog changes the posture for every later tool call, and a history
        that showed only the user's own switches would leave the change
        that mattered unexplained.
        """
        previous = self.session.permission_mode
        note = getattr(self.session, "note_permission_mode", None)
        if note is not None:
            note(mode)
        await self._record_event(
            "permission_mode",
            permission_mode_content(mode),
            payload={"from": previous, "to": mode, "source": "engine"},
        )
        await self._broadcast(
            Event(
                "permissionModeChanged",
                {"mode": mode, "by": "permission dialog"},
                turn_scoped=False,
            )
        )

    def _localhost_available(self) -> bool:
        """Whether any connected client could answer a permission request.

        Without collab there is one client and it is us. With collab, a
        session whose only participants are remote cannot answer anything —
        the broker uses that to fail fast rather than leaving a turn stalled
        on a dialog nobody can see. It is also the whole of what decides
        whether a request has a deadline at all: sampled repeatedly for the
        life of each one, so a host that leaves arms the clock and one that
        comes back cancels it (permissions.py § Deadline).
        """
        collab = self._collab
        if collab is None:
            return True
        try:
            clients = collab.get_connected_clients()
        except Exception as exc:
            logger.warning("Could not list connected clients: %s", exc)
            return False
        return any(bool(client.get("is_localhost")) for client in clients or [])

    def _caller_label(self) -> str:
        """A short attribution for whoever resolved a request."""
        collab = self._collab
        if collab is None:
            return "localhost"
        try:
            role = collab.get_collab_role() or {}
        except Exception:
            return "localhost"
        if not isinstance(role, dict) or role.get("error"):
            return "localhost"
        return str(role.get("client_id") or role.get("role") or "localhost")

    def _check_localhost_only(self) -> dict[str, Any] | None:
        """The standard restricted shape for a non-localhost caller.

        Deliberately a local copy rather than an import from
        ``aic_dc.llm._rpc_lifecycle``: this package has no import edge to
        the native engine, so phase 3's rip-out cannot break it.

        Fails closed — an exception from the collab check itself is a
        denial, not a silent allow.
        """
        collab = self._collab
        if collab is None:
            return None
        try:
            is_local = collab.is_caller_localhost()
        except Exception as exc:
            logger.warning("Collab localhost check raised: %s; denying", exc)
            return {
                "error": "restricted",
                "reason": "Internal error checking caller identity",
            }
        if is_local:
            return None
        return {
            "error": "restricted",
            "reason": "Participants cannot perform this action",
        }

    # ------------------------------------------------------------------
    # Live controls
    # ------------------------------------------------------------------

    async def set_permission_mode(self, mode: str) -> dict[str, Any]:
        """Switch the safety posture. **Localhost only.**

        No reconnect, no turn interruption.

        The restriction is not about who owns the setting — it is that
        ``bypassPermissions`` turns the gate off for *every* subsequent tool
        call in the session, including the host's. A remote participant able
        to set it would reach the same authority ``resolve_permission``
        withholds from them, just one step further back and without a prompt
        anyone would see. Rejected attempts are logged for the same reason
        they are there: an escalation attempt is worth a line in the log.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            logger.warning(
                "Rejected a non-localhost attempt to set permission mode to %r",
                mode,
            )
            return restricted
        # Read before the switch: the record says what the posture moved
        # *from*, and afterwards the session only knows where it landed.
        previous = self.session.permission_mode
        try:
            applied = await self.session.set_permission_mode(mode)
        except ValueError as exc:
            return {"error": str(exc), "valid_modes": list(PERMISSION_MODES)}
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, f"set_permission_mode({mode!r})")
            return {"error": f"Could not change the permission mode: {exc}"}
        await self._record_event(
            "permission_mode",
            permission_mode_content(applied),
            payload={"from": previous, "to": applied, "source": "user"},
        )
        await self._broadcast(
            Event("permissionModeChanged", {"mode": applied, "by": "user"}, turn_scoped=False)
        )
        return {"mode": applied}

    async def get_model(self) -> dict[str, Any]:
        """The model in force, and the models this engine offers.

        One call rather than two, because the answer is one fact in two halves:
        the alias the session runs under, and the CLI's own mapping from alias
        to model id. Splitting them would have the browser fetch
        ``get_current_state`` — which carries the whole rendered transcript —
        for one string.

        ``model`` is the *alias*, and it is ``None`` when nothing named one:
        ``engine.json`` may omit it, in which case no ``--model`` was passed and
        the CLI used its own default. That is worth reporting as null rather
        than as the string ``"default"``, because "nothing is pinned" and
        "``default`` is pinned" are the same destination today and not
        necessarily the same one after a CLI upgrade.

        ``resolved`` is what the CLI says the alias resolves to, and ``None``
        when the engine has not connected or does not list that alias. Never
        derived here: this repo does not resolve aliases, and a guessed model id
        is one the browser would quote back to someone deciding what to spend.

        ``models`` is empty before the first turn, which is not an error — the
        engine connects lazily, so there is no handshake to read yet. The
        surface says so rather than showing an empty menu.
        """
        model = self.session.model
        models: list[dict[str, Any]] = []
        try:
            info = await self.session.get_server_info()
        except Exception:
            # No engine yet is the ordinary pre-first-turn state, and this call
            # exists partly to be made in it. Debug, not exception: a warning
            # per Settings-tab open would be noise on every cold start.
            logger.debug("get_model: no server info to read yet", exc_info=True)
            info = None
        if isinstance(info, dict):
            advertised = info.get("models")
            if isinstance(advertised, list):
                models = [m for m in advertised if isinstance(m, dict)]
        resolved: str | None = None
        for entry in models:
            if entry.get("value") == model:
                candidate = entry.get("resolvedModel")
                resolved = candidate if isinstance(candidate, str) else None
                break
        return {"model": model, "resolved": resolved, "models": models}

    async def set_model(self, model: str | None = None) -> dict[str, Any]:
        """Switch models mid-session. ``None`` restores the CLI default.
        **Localhost only.**

        Models differ in what a turn costs the host, and the host is the one
        paying — under a subscription, in rate-limit headroom.

        Broadcast for the same reason ``set_permission_mode`` is: the model in
        force is session state, not this client's state, and a second window
        left showing the model the session *started* on would be naming a model
        that is no longer answering. Unlike the permission mode, the reply is
        also authoritative on its own — ``Session.set_model`` records the new
        alias only after the control request came back — so a caller may flip
        its own control on the reply and does not have to wait for this event.
        The broadcast is what the *other* windows have instead.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            applied = await self.session.set_model(model)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, f"set_model({model!r})")
            return {"error": f"Could not change the model: {exc}"}
        await self._broadcast(
            Event("modelChanged", {"model": applied, "by": "user"}, turn_scoped=False)
        )
        return {"model": applied}

    async def rewind_files(self, user_message_id: str) -> dict[str, Any]:
        """Undo file changes back to a user message's checkpoint.
        **Localhost only.**

        It writes to the host's working tree — a destructive write, and the
        one restriction the collaboration spec would be most obviously wrong
        to omit.

        ``restored`` is empty because the SDK's ``rewind_files()`` returns
        nothing — the reference spec's ``{restored: [...]}`` cannot be
        satisfied from this call alone. The frontend should refresh the
        file tree on success rather than trusting the list.

        Refused outright while the transcript is mirrored, which is every
        run with a repo: the SDK will not enable checkpointing alongside a
        session store, so there is nothing to rewind *to*. Answered here
        rather than left to the SDK because the SDK's version of this
        answer is a ``ValueError`` about local-disk divergence, which tells
        the user nothing about what to do instead.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not self.session.file_checkpointing:
            return {
                "error": "File checkpoints are unavailable in this session, "
                "because the transcript is mirrored into the repo and the "
                "engine will not do both. Use git to undo file changes."
            }
        try:
            await self.session.rewind_files(user_message_id)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, f"rewind_files({user_message_id!r})")
            return {"error": f"Could not rewind: {exc}"}
        return {"restored": [], "user_message_id": user_message_id}

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """Kill one subagent. It reports back as ``status="killed"``.
        **Localhost only.**

        Killing a subagent mid-write is a way to leave the tree in a state
        nobody asked for. It does *not* interrupt the host's turn: an earlier
        version of this docstring claimed it did, and nothing in the SDK
        supports that. ``stop_task`` is its own control subtype
        (``SDKControlStopTaskRequest``, a sibling of the interrupt request),
        and the CLI answers it in the *message stream* — a
        ``task_notification`` of status ``"stopped"``, or a ``task_updated``
        patch of ``"killed"`` with no notification at all. Hence the
        ``"stopping"`` below: the terminal word arrives as an event, not as
        this return value.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.session.stop_task(task_id)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, f"stop_task({task_id!r})")
            return {"error": f"Could not stop the task: {exc}"}
        return {"status": "stopping", "task_id": task_id}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def get_context_usage(self) -> dict[str, Any]:
        """The live context breakdown, plus when we fetched it.

        ``reason`` names *which* failure this was, because the two read
        the same to a browser holding one error string and mean opposite
        things to the reader. There being no engine is a state that ends
        when a session starts; a control request that went out to a live
        engine and failed is a request worth retrying now. The viewer
        told everyone to wait for a session either way, which sent
        readers off to fix a session that was already connected.
        """
        try:
            usage = await self.session.get_context_usage()
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc), "reason": "no-engine"}
        except Exception as exc:
            _log_control_failure(exc, "get_context_usage")
            return {
                "error": f"Could not read context usage: {exc}",
                "reason": "failed",
            }
        # The memory-file list crosses the wire exactly as the engine
        # reported it, absolute paths and all. This used to enrich each
        # entry with a ``relPath`` — the repo-relative name, added only
        # for files inside the root, which told the browser which rows
        # were clickable and what to label them. Both of those are
        # questions about *rendering*, and the browser already answers
        # them with ``repo-path.js``'s ``toRepoPath`` for every other
        # path it is handed. Two answers to one question is what
        # ``specs5/next.md`` § C3 exists to remove, and the enrichment
        # was the copy that had to go, because a name is not a fact
        # about the repo. What genuinely needs a server-side answer is
        # which repo file a tool *wrote*, since the index it keys is
        # here and no browser is involved: that is
        # ``Reindexer._relative`` (``hooks.py``), and it stays.
        return {"usage": usage, "fetched_at": _now()}

    async def get_mcp_status(self) -> dict[str, Any]:
        try:
            return await self.session.get_mcp_status()
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, "get_mcp_status")
            return {"error": f"Could not read MCP status: {exc}"}

    async def reconnect_mcp_server(self, name: str) -> dict[str, Any]:
        """Re-dial one MCP server. **Localhost only.**"""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.session.reconnect_mcp_server(name)
        except Exception as exc:
            logger.warning("reconnect_mcp_server(%r) failed: %s", name, exc)
            return {"error": f"Could not reconnect {name}: {exc}"}
        return {"status": "reconnecting", "name": name}

    async def toggle_mcp_server(self, name: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable one MCP server. **Localhost only.**

        Enabling a server hands the agent a new set of tools; the host is
        the one who decides which tools exist.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.session.toggle_mcp_server(name, bool(enabled))
        except Exception as exc:
            logger.warning("toggle_mcp_server(%r, %r) failed: %s", name, enabled, exc)
            return {"error": f"Could not toggle {name}: {exc}"}
        return {"status": "ok", "name": name, "enabled": bool(enabled)}

    async def get_server_info(self) -> dict[str, Any]:
        """Advertised commands, tools, and output styles from initialize."""
        try:
            info = await self.session.get_server_info()
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, "get_server_info")
            return {"error": f"Could not read server info: {exc}"}
        return info or {}

    async def list_commands(self) -> dict[str, Any]:
        """The `/` palette's list: what the live CLI advertises, minus dead ends.

        Read from the initialize handshake rather than a table in this file,
        which is the only way skills, plugin commands and
        ``.claude/commands/`` entries can appear without this module being
        told they exist. The CLI's own filtering has already run by then, so
        what arrives is what it will actually dispatch.

        Two edits on top of that list. Denied commands are dropped, because
        offering one and then refusing it is worse than never showing it.
        Routed commands are added if the CLI does not advertise them —
        ``/permissions`` and ``/resume`` are not in its list at all, and
        their whole purpose here is to open an AIC⚡DC surface.

        ``action`` is what selecting the entry does: ``route`` opens a
        surface, ``send`` puts the command in the composer for the engine.
        ``target`` names the surface for a routed entry and is empty
        otherwise, so the webapp never needs its own copy of the mapping.
        ``during_turn`` is whether the row stays actionable while a turn
        streams — carried per entry so the palette holds no copy of the
        mid-turn rule either, and can grey a row out with a reason instead
        of dropping it from a list that would then appear to shrink.

        **A disconnected engine answers with the routed commands and
        ``partial: True``, not an error.** The engine connects on the first
        turn, so before it there is no handshake to read — and that is
        precisely when the palette is most wanted, since the user is
        composing that first turn. Two of the routes are ``/resume`` and
        ``/clear``, which is what somebody who has not started yet is
        reaching for. Connecting here instead would spend a 295 MB
        subprocess on a keystroke, and this method is one a remote
        participant may call.
        """
        try:
            info = await self.session.get_server_info()
        except EngineNotReadyError:
            return {"commands": self._routed_commands(), "partial": True}
        except SessionLostError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            _log_control_failure(exc, "list_commands")
            return {"error": f"Could not read the command list: {exc}"}
        commands: list[dict[str, Any]] = []
        for entry in (info or {}).get("commands") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            # Leading underscore is the CLI's marker for a command that is
            # session-plumbing rather than something a person invokes.
            if name.startswith("_") or name in SLASH_DENIED:
                continue
            route = SLASH_ROUTES.get(name)
            commands.append(
                {
                    "name": name,
                    "aliases": [
                        alias
                        for alias in (entry.get("aliases") or [])
                        if isinstance(alias, str)
                    ],
                    "argument_hint": entry.get("argumentHint") or "",
                    # A routed row's description is ours, even when the CLI
                    # advertises one. The CLI describes what *its* version
                    # does, and selecting this row does not do that — it
                    # opens a surface. "Show total cost and duration of the
                    # current session" beside an "opens UI" badge is the row
                    # contradicting its own badge.
                    "description": (
                        route["palette"] if route else entry.get("description") or ""
                    ),
                    "action": "route" if route else "send",
                    "target": route["target"] if route else "",
                    # A `send` entry is a turn, and the concurrency guard
                    # allows one — so everything the CLI answers is False
                    # here regardless of how cheaply it answers it.
                    "during_turn": bool(route["during_turn"]) if route else False,
                }
            )
        advertised = {command["name"] for command in commands}
        commands.extend(
            entry
            for entry in self._routed_commands()
            if entry["name"] not in advertised
        )
        commands.sort(key=lambda command: command["name"])
        return {"commands": commands}

    @staticmethod
    def _routed_commands() -> list[dict[str, Any]]:
        """`SLASH_ROUTES` as palette entries, for the pre-connect list.

        These are this deployment's own, not the CLI's to supply:
        ``/permissions`` and ``/resume`` it does not advertise at all, and
        the rest it describes in terms of what its own version does rather
        than the surface that opens here. So where it *does* advertise one,
        only the aliases and argument hint are taken from it — the
        description stays ours, because the row's action is ours.

        This list is also the whole answer before the engine connects, which
        is why it is complete rather than only the unadvertised pair.
        """
        return sorted(
            (
                {
                    "name": name,
                    "aliases": [],
                    "argument_hint": "",
                    "description": route["palette"],
                    "action": "route",
                    "target": route["target"],
                    "during_turn": bool(route["during_turn"]),
                }
                for name, route in SLASH_ROUTES.items()
            ),
            key=lambda command: command["name"],
        )

    async def get_sdk_surface(self) -> dict[str, Any]:
        """What the installed SDK offers versus what AIC⚡DC reaches for.

        Never answers ``{"error": ...}``, unlike its neighbours here. The
        static half of the report — options, hook events, message types,
        client methods, beta gates — is pure reflection over the installed
        wheel and this package's own source, so it is exactly as available
        with the engine down as up. Refusing the whole report because the
        CLI is not running would withhold the part that still holds at the
        moment somebody is most likely to be reading it.

        The live half degrades on its own instead: a failed
        ``get_server_info`` leaves ``cli.available`` false and the static
        sections intact. That call is a control request against the
        subprocess, so it fails for the ordinary reasons — no session yet,
        session lost mid-read — and none of them are worth an error banner
        over a diagnostic tab.
        """
        server_info: Any = None
        try:
            server_info = await self.session.get_server_info()
        except (EngineNotReadyError, SessionLostError) as exc:
            logger.debug("SDK surface: no live CLI to probe (%s)", exc)
        except Exception:  # noqa: BLE001 - a diagnostic, never a control path
            logger.warning(
                "SDK surface: could not read server info; reporting the "
                "static surface only",
                exc_info=True,
            )
        return sdk_surface.surface_report(server_info)

    # ------------------------------------------------------------------
    # Indexing — AIC-DC's own, not the engine's
    # ------------------------------------------------------------------

    def _attach_symbol_index(self, symbol_index: Any) -> None:
        """Hand over the built symbol index. Called once, from startup.

        Underscored deliberately: ``add_service`` publishes every public
        method, and a browser calling this with a JSON value would replace
        the index with something that has no ``lsp_get_hover`` — breaking
        hovers for every client until a restart. ``main.py``'s deferred init
        is the only legitimate caller, and it is in-process.
        """
        self.symbol_index = symbol_index
        self.review.symbol_index = symbol_index
        self._symbol_index_failed = symbol_index is None

    def _mark_symbol_index_ready(self) -> None:
        """The repo walk finished; the map now describes the whole repo.

        Separate from :meth:`_attach_symbol_index` because the gap between
        the two is minutes on a large repo, and Monaco wants the index as
        soon as it exists — a hover that resolves for half the repo is
        useful, a *map* that covers half the repo is misleading.
        """
        self._symbol_index_ready = True
        self._symbol_index_failed = False

    def _mark_symbol_index_failed(self) -> None:
        """The walk did not finish. Report unavailable rather than partial.

        Also clears ready, so a failure part-way through a *rebuild* stops
        the tools claiming completeness they had a moment ago.
        """
        self._symbol_index_ready = False
        self._symbol_index_failed = True

    def set_viewer_state(
        self,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """Record what the caller has open in their viewer.
        **Localhost only.**

        Feeds the turn framing and the ``ui_state`` tool. A falsy ``path``
        clears it, which is what closing the pane should mean rather than
        leaving the agent pointed at a file nobody is looking at.

        Gated because it is an input to the prompt: a non-localhost
        participant could otherwise put a path of their choosing in front
        of the model on somebody else's turn. It is a small lever, and it
        is still a lever on what the agent reads.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not path or not isinstance(path, str):
            self._viewer_state = None
            return {"status": "cleared"}
        state: dict[str, Any] = {"path": path}
        if isinstance(start_line, int):
            state["start_line"] = start_line
        if isinstance(end_line, int):
            state["end_line"] = end_line
        self._viewer_state = state
        return {"status": "ok", **state}

    def _schedule_doc_index_build(self) -> None:
        """Start the doc-index build in the background. Idempotent.

        Underscored for the same reason as :meth:`_attach_symbol_index` —
        a startup hook, not something a browser has business triggering.
        """
        self.doc_builder.schedule()

    def lsp_get_hover(self, path: str, line: int, col: int) -> dict[str, Any] | None:
        """Hover text for a position, from the symbol index.

        ``None`` before the index is built — Monaco reads that as "no hover
        here", which is the truth at that moment.
        """
        if self.symbol_index is None:
            return None
        return self.symbol_index.lsp_get_hover(path, line, col)

    def lsp_get_definition(
        self, path: str, line: int, col: int
    ) -> dict[str, Any] | None:
        """Definition site for the symbol at a position."""
        if self.symbol_index is None:
            return None
        return self.symbol_index.lsp_get_definition(path, line, col)

    def lsp_get_references(
        self, path: str, line: int, col: int
    ) -> list[dict[str, Any]]:
        """Every reference to the symbol at a position."""
        if self.symbol_index is None:
            return []
        return self.symbol_index.lsp_get_references(path, line, col)

    def lsp_get_completions(
        self, path: str, line: int, col: int, prefix: str = ""
    ) -> list[dict[str, Any]]:
        """Completion candidates for a position and prefix."""
        if self.symbol_index is None:
            return []
        return self.symbol_index.lsp_get_completions(path, line, col, prefix)

    def navigate_file(self, path: str) -> dict[str, Any]:
        """Ask every client to open ``path``.

        A broadcast, not a local action: this is how one participant points
        the others at a file (``specs5/4-features/collaboration.md``).
        Unrestricted for that reason — showing someone a file changes
        nothing on disk.
        """
        self._broadcast_soon(
            Event("navigateFile", {"path": path}, turn_scoped=False)
        )
        return {"status": "ok", "path": path}

    # ------------------------------------------------------------------
    # Sessions — new, resume, fork
    # ------------------------------------------------------------------

    async def new_session(self) -> dict[str, Any]:
        """Abandon the current conversation and start a blank one.
        **Localhost only.**

        Gated: this discards the context every client is looking at, and
        spends the host's engine. Refused while a turn is running rather
        than interrupting one — the user can cancel first, and pulling the
        session out from under a live turn loses its tail.

        The engine is *not* reconnected here. The next turn connects with
        no resume, which is exactly what a new session is, and a launch
        that never chats never pays for a CLI subprocess (the same
        lazy-connect bargain the rest of the service makes).

        ``session_id`` is **null**, and cannot be otherwise: the CLI mints
        the ID and only reports it in the init message of the first turn.
        The spec's `{session_id: str}` described the native engine, which
        minted its own. The browser learns the real ID from the
        ``sessionStarted`` event that turn emits.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if self.session.streaming_active:
            return {
                "error": "A turn is still running",
                "reason": "turn_in_progress",
            }

        await self.permissions.cancel_all()
        await self.session.reset()
        # No target, and no store lookup either: the user asked for blank.
        self._resume_request = None
        self._auto_resume = False
        await self._broadcast(
            Event(
                "sessionChanged",
                {"session_id": None, "messages": [], "action": "new"},
                turn_scoped=False,
            )
        )
        return {"session_id": None, "status": "new"}

    async def resume_session(
        self, session_id: str, fork: bool = False
    ) -> dict[str, Any]:
        """Attach the engine to a past session, or to a copy of one.
        **Localhost only.**

        Resumption is never a replay: the mirrored transcript is handed to
        the CLI, which rebuilds its own context from it. AIC-DC does not read
        it back into a prompt — that is the mechanism that used to produce
        sessions looking right in the UI while the model's view had
        diverged (``specs5/3-engine/history.md`` § Resume, Fork, and New).
        What we render is a *record* of the session; what the model gets is
        the session.

        ``fork=True`` copies it: the original is left untouched, which makes
        forking the safe way to revisit an old conversation. The new ID is
        minted by the CLI, so the response carries ``forked_from`` and a
        null ``session_id`` until the first turn reports the real one.

        Gated for the reason ``connect_engine`` is: a participant choosing
        which conversation the host's engine attaches to would be deciding
        for everyone.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not session_id:
            return {"error": "A session ID is required", "reason": "no_session_id"}
        if self.session.streaming_active:
            return {
                "error": "A turn is still running",
                "reason": "turn_in_progress",
            }

        messages = await self.history_load(session_id)
        if isinstance(messages, dict):
            # Browsable but not resumable: deleted, unreadable, or from
            # before the conversion. Reported rather than attempted — the
            # CLI would fail the connect and the user would be looking at an
            # engine that will not start.
            return {
                "error": messages.get("error", "That session cannot be read"),
                "reason": "not_resumable",
            }

        await self.permissions.cancel_all()
        self._resume_request = (session_id, bool(fork))
        await self.session.reset()
        outcome = await self.connect_engine()
        if "error" in outcome:
            return {"error": outcome["error"], "reason": outcome["reason"]}

        action = "forked" if fork else "resumed"
        resumed_id = self.session.session_id
        payload: dict[str, Any] = {"action": action, "session_id": resumed_id}
        if fork:
            payload["forked_from"] = session_id
        # Filed against the session named, not the live one. For a resume
        # they are the same session. For a fork the live one has no ID yet,
        # so the record goes where the user actually was: the origin's
        # transcript, which now shows that a branch was taken from here.
        await self._record_event(
            "session_switch",
            session_switch_content(action, session_id),
            payload=payload,
            session_id=session_id,
        )
        await self._broadcast(
            Event(
                "sessionChanged",
                {
                    "session_id": resumed_id,
                    "messages": messages,
                    "action": action,
                    "forked_from": session_id if fork else None,
                },
                turn_scoped=False,
            )
        )
        answer: dict[str, Any] = {"session_id": resumed_id}
        if fork:
            answer["forked_from"] = session_id
        return answer

    async def restart_session(self) -> dict[str, Any]:
        """Replace the CLI subprocess, on ``engine.json`` as it is on disk.
        **Localhost only.**

        The only thing that applies a saved ``effort``, ``cli_path``,
        ``thinking_display``, ``max_budget_usd`` or ``max_buffer_size``:
        ``ClaudeAgentOptions`` is assembled once per connect, so a save that
        touched any of them changes the next session and nothing else
        (``specs5/5-webapp/settings.md`` § The Applies Column Is Load-Bearing).
        This is that next session, without waiting for a relaunch.

        The conversation comes with it. The current session ID is resumed,
        not abandoned, so the transcript and the model's own context survive
        — what does not survive is the CLI's warm state, including the cost
        totals, which start fresh on a resumed session by the CLI's own
        rule.

        **The file wins over live overrides**, one rule rather than a
        per-field carve-out: a model or mode set by hand this session goes
        back to what ``engine.json`` says. See
        :meth:`EngineSession.adopt_config` for why keeping them would be
        worse than reverting them.

        Three refusals, and one shortcut:

        - a turn in flight — the same rule as ``new_session``; pulling the
          subprocess out from under a live turn loses its tail
        - an active review — review holds the session in ``plan`` mode and
          restores the entry mode when it ends, so a restart would drop it
          into ``engine.json``'s mode while the UI still says review
        - a cold engine takes the **shortcut**: the config is adopted and
          nothing is connected. It reaches the same place — the next turn
          starts on the new file — without spending a subprocess, and
          without the reload being a no-op, which it would be if this
          returned early. ``engine.json`` is read at startup, so a cold
          session still holds the old config until something replaces it.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if self.session.streaming_active:
            return {
                "error": "A turn is still running",
                "reason": "turn_in_progress",
            }
        if self.review.active:
            return {
                "error": (
                    "End the review first. A restart would start the session "
                    "in engine.json's permission mode, leaving review mode "
                    "on screen without the posture behind it."
                ),
                "reason": "review_active",
            }

        reloaded = EngineConfig.load(getattr(self._config, "config_dir", None))
        if not self.session.connected:
            self.engine_config = reloaded
            self.session.adopt_config(reloaded)
            return {
                "status": "adopted",
                "session_id": await self._visible_session_id(),
                "permission_mode": self.session.permission_mode,
                "model": self.session.model,
            }

        # Read before the reset, which forgets the ID, and before the config
        # swap, which is allowed to move the other two. The ID is what makes
        # this restart continue the conversation rather than start a blank one.
        resume_target = self.session.session_id
        was_mode = self.session.permission_mode
        was_model = self.session.model
        await self.permissions.cancel_all()
        await self.session.reset()
        self.engine_config = reloaded
        self.session.adopt_config(reloaded)
        if resume_target:
            self._resume_request = (resume_target, False)
        outcome = await self.connect_engine()
        if "error" in outcome:
            return {"error": outcome["error"], "reason": outcome["reason"]}

        session_id = self.session.session_id
        if session_id:
            # Skipped when there is none: a session that connected and never
            # took a turn has no transcript for the record to appear in.
            await self._record_event(
                "session_switch",
                session_switch_content("restarted", session_id),
                payload={"action": "restarted", "session_id": session_id},
                session_id=session_id,
            )
        # No ``sessionChanged``: the session on screen is still this one, and
        # that event replaces the message list wholesale in every client.
        # What did change are the two options the file is allowed to take
        # back, and they are announced the way their own setters announce
        # them — only when they moved, so a restart that changed neither is
        # silent on both rather than telling every window to redraw.
        mode = self.session.permission_mode
        if mode != was_mode:
            await self._record_event(
                "permission_mode",
                permission_mode_content(mode),
                payload={"from": was_mode, "to": mode, "source": "restart"},
            )
            await self._broadcast(
                Event(
                    "permissionModeChanged",
                    {"mode": mode, "by": "restart"},
                    turn_scoped=False,
                )
            )
        model = self.session.model
        if model != was_model:
            await self._broadcast(
                Event("modelChanged", {"model": model, "by": "restart"}, turn_scoped=False)
            )
        return {
            "status": "restarted",
            "session_id": session_id,
            "permission_mode": mode,
            "model": model,
        }

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def history_list(
        self, limit: int = 50
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Past sessions for this repo, most recently modified first.

        A bare list on success and ``{"error": ...}`` on failure, the union
        the RPC table specifies (``specs-reference/3-engine/history.md``
        § RPC surface). An empty list would conflate "no history yet" with
        "could not read it", and only the second is worth a user's time.

        Unrestricted. Reading what the agent did is the reviewing half of
        collaboration, and a participant who cannot see the history cannot
        review the work they were invited to look at
        (``specs5/4-features/collaboration.md`` § Read-Only).

        ``limit`` bounds the real work: the listing itself is one batch read
        of the summary sidecars, but an exact ``message_count`` costs one
        parse per session listed.
        """
        if self.session_store is None:
            return []

        from aic_dc.claude_code import history

        try:
            return await history.list_sessions(
                self.session_store,
                str(self._repo_root),
                limit=max(0, int(limit)),
                index=self.history_index,
            )
        except Exception as exc:
            logger.exception("history_list failed")
            return {"error": f"Could not read the session history: {exc}"}

    async def history_load(
        self, session_id: str
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """One past session's messages, rendered for the browser.

        **Read-only, and deliberately not a resume.** This reads a
        transcript; ``resume_session`` is what puts the engine back into
        one. Keeping them apart means browsing history cannot disturb a
        turn that is running.

        Rendered here, at read time, from the stored entries — so a
        rendering fix reaches every session anyone has ever had, rather
        than only the ones recorded after it shipped.
        """
        if not session_id:
            return {"error": "A session ID is required"}
        if self.session_store is None:
            return {"error": "No session history: this run has no repo directory"}

        from aic_dc.claude_code import history

        events: list[dict[str, Any]] = []
        if self.events_log is not None:
            try:
                events = await self.events_log.load(session_id)
            except Exception:
                # The conversation is the point; the operational lines are
                # a garnish on it. Losing them must not lose the session.
                logger.exception("Could not read events for %s", session_id)

        try:
            messages = await history.load_session(
                self.session_store,
                session_id,
                str(self._repo_root),
                events=events,
            )
        except Exception as exc:
            logger.exception("history_load failed for %s", session_id)
            return {"error": f"Could not read session {session_id}: {exc}"}

        if not messages:
            # An empty transcript is never stored, so nothing to render
            # means the session is gone or unparseable — not that it
            # happened and said nothing. An empty list would render as the
            # latter, which is a lie the user cannot act on.
            return {"error": f"Session {session_id} has no readable transcript"}
        return messages

    async def history_search(
        self, query: str, role: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Substring search across every stored session.

        Newest first, capped — the native engine's ordering and its default
        of 50, kept so a habit formed before the conversion still works.

        ``role`` narrows to ``"user"``, ``"assistant"`` or ``"tool"``. That
        third value is new: a tool call is neither the user's words nor the
        model's prose, and the searches this serves best — for a path, a
        command, a pattern the agent used — are all tool calls. Tool
        *results* are not searched at all; that is what ``Grep`` is for.

        Served from the derived index when it is warm and by scanning the
        transcripts when it is not, and both answer the same rows: the index
        narrows which sessions to read, and every hit is confirmed against
        the transcript text.
        """
        if not query:
            # Not an error: an empty search box is a user who has not
            # searched yet, and an error toast for typing nothing would be
            # noise.
            return []
        if self.session_store is None:
            return []

        from aic_dc.claude_code.history_index import ROLES, SEARCH_LIMIT, search

        if role and role not in ROLES:
            return {"error": f"Unknown role {role!r}; expected one of {sorted(ROLES)}"}

        try:
            return await search(
                self.session_store,
                str(self._repo_root),
                query,
                index=self.history_index,
                role=role or None,
                limit=max(1, int(limit)) if limit else SEARCH_LIMIT,
            )
        except Exception as exc:
            logger.exception("history_search failed")
            return {"error": f"Could not search the session history: {exc}"}

    async def history_delete(self, session_id: str) -> dict[str, Any]:
        """Delete one past session and everything derived from it.
        **Localhost only.**

        Three files, one operation. The store takes the transcript with its
        summary sidecar and its subagent transcripts — and with them the
        pasted images, which live in the entries and nowhere else. The
        events log drops that session's records, because an archived commit
        that outlived the session it describes would render in the browser
        as history for a session that no longer exists. The derived index
        forgets it, which costs nothing to be wrong about but would
        otherwise keep answering searches with a session ID that resolves
        to nothing.

        In that order, so that a crash between steps leaves the smallest
        lie: what survives a half-done delete is unreachable (no listing
        offers it) or self-healing (the next index refresh purges a session
        the store no longer lists). Deleting the transcript last would
        instead leave a browsable session whose events had silently gone.

        Gated because this destroys history every client can see, and
        because a participant who could delete the record of a turn could
        delete the evidence of what they were invited to review.

        The session on screen is refused rather than deleted. The store is
        a *live* mirror: the CLI keeps appending to the session it is
        attached to, so the transcript would come straight back — and the
        next connect would resume an ID with nothing behind it. Starting a
        new session first makes it deletable, which is one click and is
        honest about what is happening.

        Missing is not an error. A row deleted twice, or deleted by another
        client first, is a browser that already has what it asked for.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not session_id:
            return {"error": "A session ID is required", "reason": "no_session_id"}
        if self.session_store is None:
            return {"error": "No session history: this run has no repo directory"}
        if session_id == await self._visible_session_id():
            return {
                "error": "That is the current conversation. Start a new session first.",
                "reason": "session_live",
            }

        from aic_dc.claude_code import history

        try:
            await history.delete_session(
                self.session_store, session_id, str(self._repo_root)
            )
            if self.events_log is not None:
                await self.events_log.delete_session(session_id)
            if self.history_index is not None:
                await self.history_index.forget(session_id)
        except Exception as exc:
            logger.exception("history_delete failed for %s", session_id)
            return {"error": f"Could not delete session {session_id}: {exc}"}

        # No `events.jsonl` record for the deletion: it would be filed
        # against the session that was just deleted, which is the one thing
        # `EventsLog.delete_session` exists to prevent.
        await self._broadcast(
            Event("sessionDeleted", {"session_id": session_id}, turn_scoped=False)
        )
        return {"session_id": session_id, "status": "deleted"}

    async def get_session_storage(self) -> dict[str, Any]:
        """How much disk ``.aic-dc/sessions/`` is using.

        The readable half of a measurement that until now existed only as a
        turn-time warning. ``_disk_warning`` walks the same directory and
        compares it to the same threshold, and this method is deliberately
        **not** routed through it: that one is latched to fire once per server
        lifetime, so borrowing it would mean opening the Settings tab silently
        spends the warning the user has not seen yet. Two callers, one
        measurement, one latch — and the latch belongs to the caller that
        interrupts rather than the one that was asked.

        ``over_warning`` is the verdict, not the threshold, matching how
        ``EngineHealth`` hands the browser a mirror-gap verdict rather than
        ``history.mirror_gap_tolerance``. The comparison is configured here,
        in one place, and a tab that re-derived it would be a second copy of a
        number the user can edit.

        Unrestricted, for ``history_list``'s reason: this is the size of the
        history a read-only participant is already allowed to read, and the
        deletion it argues for is gated where deletion happens.

        A failed walk is reported rather than swallowed. ``_disk_warning``
        swallows one because a size it could not read is not worth failing a
        completed turn over; here the size *is* the answer, so silence would
        leave the card showing nothing with no account of why.
        """
        if self.session_store is None:
            return {
                "error": "No session history: this run has no repo directory",
                "reason": "no_repo",
            }
        try:
            loop = asyncio.get_running_loop()
            total = await loop.run_in_executor(None, self.session_store.total_bytes)
        except Exception as exc:
            logger.exception("Could not measure the session directory")
            return {"error": f"Could not measure the session directory: {exc}"}
        threshold = self._history_config().get(
            "session_dir_warning_bytes", DISK_WARNING_BYTES
        )
        return {"bytes": int(total), "over_warning": int(total) >= int(threshold)}

    async def history_image(
        self, session_id: str, entry_uuid: str, block: int
    ) -> dict[str, Any]:
        """One image out of a past prompt, as a data URI.

        The other half of the pointers ``history_load`` renders in place of
        image bytes. Pulling them one at a time is what keeps opening a
        screenshot-heavy session from costing megabytes per client per
        reconnect (``specs5/3-engine/history.md`` § What the Browser Reads).

        Unrestricted, like the rest of reading history: an image the agent
        was shown is part of what a reviewer needs to see.
        """
        if not session_id:
            return {"error": "A session ID is required"}
        if not entry_uuid:
            return {"error": "A message ID is required"}
        if self.session_store is None:
            return {"error": "No session history: this run has no repo directory"}

        from aic_dc.claude_code import history

        try:
            data_uri = await history.load_image(
                self.session_store,
                session_id,
                str(self._repo_root),
                entry_uuid=entry_uuid,
                block=int(block),
            )
        except history.ImageUnavailable as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("history_image failed for %s", session_id)
            return {"error": f"Could not read that image: {exc}"}
        return {"data_uri": data_uri}

    async def list_subagent_transcripts(
        self, session_id: str | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """The subagents a session spawned, one row per transcript.

        Defaults to the session on screen, so the common call — "what did
        this conversation delegate?" — needs no argument and cannot name a
        different session than the one being read.

        Keyed by the CLI's agent ID throughout. The native engine's
        positional ``agent_idx`` has no successor: nothing in the storage
        layout or the live protocol is positional, and inventing an index
        here would need an ordering to be maintained somewhere.
        """
        target = session_id or await self._visible_session_id()
        if not target:
            return []
        if self.session_store is None:
            return []

        from aic_dc.claude_code import history

        try:
            return await history.list_subagents(
                self.session_store, target, str(self._repo_root)
            )
        except Exception as exc:
            logger.exception("list_subagent_transcripts failed for %s", target)
            return {"error": f"Could not read the subagent transcripts: {exc}"}

    async def get_subagent_transcript(
        self, agent_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """One subagent's conversation, rendered like any other.

        The reference table says this returns raw ``SessionStoreEntry``
        values; it returns rendered messages instead, because a subagent tab
        draws through the same panel code as the main transcript and raw
        entries would put the CLI's internal union in the frontend. Recorded
        as a divergence in ``specs5/plan/delivery.md``.
        """
        if not agent_id:
            return {"error": "An agent ID is required"}
        target = session_id or await self._visible_session_id()
        if not target:
            return {"error": "No session to read subagents from"}
        if self.session_store is None:
            return {"error": "No session history: this run has no repo directory"}

        from aic_dc.claude_code import history

        try:
            messages = await history.load_subagent(
                self.session_store, target, str(self._repo_root), agent_id=agent_id
            )
        except Exception as exc:
            logger.exception("get_subagent_transcript failed for %s", agent_id)
            return {"error": f"Could not read subagent {agent_id}: {exc}"}

        if not messages:
            # Same reasoning as ``history_load``: a subagent that ran wrote
            # entries, so nothing to render means the transcript is gone or
            # was never mirrored, which is worth saying rather than drawing
            # as an empty conversation.
            return {"error": f"Subagent {agent_id} has no readable transcript"}
        return messages

    # ------------------------------------------------------------------
    # Git — commit, reset, review
    # ------------------------------------------------------------------

    async def commit_all(self) -> dict[str, Any]:
        """Stage everything and commit with a generated message.
        **Localhost only.**"""
        from aic_dc.claude_code.commit import commit_all

        return await commit_all(self)

    async def reset_to_head(self) -> dict[str, Any]:
        """Discard every uncommitted change. **Localhost only.**

        A coroutine since phase 5: it records the files it destroyed before
        destroying them, and that record is the only trace they leave.
        """
        from aic_dc.claude_code.commit import reset_to_head

        return await reset_to_head(self)

    def check_review_ready(self) -> dict[str, Any]:
        """Whether the tree is clean enough to enter a review."""
        return self.review.check_ready()

    def get_commit_graph(
        self, limit: int = 100, offset: int = 0, include_remote: bool = False
    ) -> dict[str, Any]:
        """Commits and branches for the review selector's graph."""
        return self.review.commit_graph(
            limit=limit, offset=offset, include_remote=include_remote
        )

    async def start_review(self, branch: str, base_commit: str) -> dict[str, Any]:
        """Enter review mode for ``branch`` from ``base_commit``.
        **Localhost only.**

        Async because entry switches the engine's permission posture to
        ``plan``, which is a control request to the CLI.

        The history record is made here rather than inside ``ReviewMode``:
        that class owns the git arrangement and knows nothing about
        sessions, and giving it a second collaborator to reach the events
        log through would be plumbing for one line.
        """
        result = await self.review.start(branch, base_commit)
        if "error" not in result:
            await self._record_event(
                "review_start",
                review_start_content(base_commit, branch),
                payload={
                    "base": base_commit,
                    "head": branch,
                    "files": _review_file_paths(result.get("changed_files")),
                },
            )
        return result

    async def end_review(self) -> dict[str, Any]:
        """Leave review mode, restoring git state and posture.
        **Localhost only.**

        The state is read before the exit, because exiting clears it and the
        record is about the review that just ended.
        """
        before = self.review.state()
        result = await self.review.end()
        if "error" not in result:
            await self._record_event(
                "review_end",
                review_end_content(),
                payload={
                    "base": before.get("base_commit"),
                    "head": before.get("branch"),
                    "files": _review_file_paths(before.get("changed_files")),
                },
            )
        return result

    def get_review_state(self) -> dict[str, Any]:
        """The current review, or the inactive shape."""
        return self.review.state()

    def get_review_file_diff(self, path: str) -> dict[str, Any]:
        """The forward diff for one file in the active review."""
        return self.review.file_diff(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _set_review_permission_mode(self, mode: str) -> str | None:
        """Apply a posture on review entry or exit, connected or not.

        Returns the mode now in force, or ``None`` when it could not be
        applied at all — which the review reports to the user rather than
        swallowing, because the difference is whether the agent can write.

        A cold engine is the ordinary case here, not an edge one: nothing
        connects the CLI until the first turn, and starting a review before
        chatting is a normal way to work. So the un-connected path records
        the posture for the connect to come instead of failing — see
        :meth:`EngineSession.prefer_permission_mode`.
        """
        try:
            if self.session.ready:
                applied = await self.session.set_permission_mode(mode)
            else:
                applied = self.session.prefer_permission_mode(mode)
        except (EngineNotReadyError, SessionLostError) as exc:
            # The session went while we were asking. Nothing can run in the
            # old posture — there is no session to run it — so recording the
            # request for the next connect is both safe and honest.
            logger.warning(
                "Session unavailable while setting the review posture (%s); "
                "recording %r for the next connect",
                exc,
                mode,
            )
            applied = self.session.prefer_permission_mode(mode)
        except ValueError:
            logger.error("Review asked for an unknown permission mode: %r", mode)
            return None
        await self._broadcast(
            Event(
                "permissionModeChanged",
                {"mode": applied, "by": "review mode"},
                turn_scoped=False,
            )
        )
        return applied

    async def _send_startup_progress(
        self, stage: str, message: str, percent: int
    ) -> None:
        """Forward one indexing progress event to the shell.

        Not an :class:`Event`: ``startupProgress`` takes three positional
        arguments rather than a payload object, and it predates this
        service — the startup orchestrator sends the same shape.
        """
        if self._event_callback is None:
            return
        try:
            await self._event_callback("startupProgress", stage, message, percent)
        except Exception as exc:
            logger.debug("startupProgress(%s) failed: %s", stage, exc)

    def _broadcast_soon(self, event: Event) -> None:
        """Fire-and-forget a broadcast from synchronous code.

        For the handful of RPCs that have nothing to await — the event is
        the whole point of the call, and making them coroutines just to
        reach ``_broadcast`` would change their wire signatures for nothing.
        Off-loop callers lose the event and get a log line; every real
        caller here is an RPC handler, which is always on the loop.
        """
        self._dispatch_soon(event, None)

    def _dispatch_soon(self, event: Event, request_id: str | None) -> None:
        """:meth:`_broadcast_soon` for an event that belongs to a turn."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop; dropped %s broadcast", event.name)
            return
        task = loop.create_task(self._dispatch(event, request_id))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    def _on_entries_mirrored(self, key: Any, entries: list[Any]) -> None:
        """Announce image pointers for a user entry the mirror just took.

        The second half of the ``userMessage`` broadcast. That one goes out
        before the turn starts, when the pasted images have no addresses yet
        — a pointer is ``(session_id, entry_uuid, block)`` and the entry is
        written by the CLI, mid-turn, some time later. So the pointers follow
        as their own event rather than the broadcast waiting for them: a
        collaborator seeing the text immediately and the thumbnails a moment
        later is right, and seeing neither until the CLI has flushed is not.

        Only the main transcript, and only inside a turn. A subagent's
        prompt is not a user message in anybody's chat, and entries mirrored
        with no turn in flight are a resume or a re-import replaying history
        that every client already reads through ``history_load``.

        Called from the store on the loop thread, so this stays synchronous
        and hands the actual send to a task.
        """
        if not isinstance(key, dict) or key.get("subpath") is not None:
            return
        session_id = key.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        request_id = self.session.active_request_id
        if not request_id:
            return

        from aic_dc.claude_code import history

        refs: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            content = (entry.get("message") or {}).get("content")
            uuid = entry.get("uuid")
            refs.extend(
                history.image_refs_for_entry(
                    content, session_id, uuid if isinstance(uuid, str) else ""
                )
            )
        if not refs:
            # The overwhelmingly common case — every turn's entries pass
            # through here and almost none of them carry an image.
            return

        self._dispatch_soon(
            Event("userMessageImages", {"image_refs": refs}),
            request_id,
        )

    def _slash_response(self, message: str) -> dict[str, Any] | None:
        """Intercept a leading `/command` that must not reach the engine.

        Returns ``None`` — meaning "send it" — for everything the CLI can
        answer itself, which is nearly everything. Only the two tables at
        the top of this module are held back: a routed command, whose job an
        AIC⚡DC surface does better, and a denied one, which would reach for
        something this deployment does not have.

        A typo is deliberately *not* caught here. It goes to the CLI, which
        knows the full command list — including skills and
        ``.claude/commands/`` — and answers ``Unknown command: /xyz`` for
        free. Guessing on its behalf is how the old table came to refuse
        `/doctor`, which by then had shipped as a working skill.
        """
        text = (message or "").strip()
        if not text.startswith("/") or len(text) < 2:
            return None
        parts = text[1:].split(None, 1)
        command = parts[0].lower()
        route = SLASH_ROUTES.get(command)
        if route is not None:
            reply = f"/{command} is {route['surface']} here."
            # Anything typed after the command does not travel. Routing opens a
            # surface; there is no argument for a surface to take. Said out loud
            # because `/model sonnet` is a reasonable thing to type — the CLI
            # takes it — and dropping the word silently would leave the user
            # believing they had switched models.
            argument = parts[1].strip() if len(parts) > 1 else ""
            if argument:
                reply += (
                    f" What follows it — {argument!r} — was not applied:"
                    " routing opens a surface rather than running a command."
                )
            return {
                "status": "routed",
                "command": command,
                "target": route["target"],
                "message": reply,
            }
        denial = SLASH_DENIED.get(command)
        if denial is not None:
            return {
                "status": "unsupported",
                "command": command,
                "message": f"/{command} does not work here: {denial}.",
            }
        return None

    def _emitter(self, request_id: str) -> Callable[[Event], Awaitable[None]]:
        """A per-turn emit callback bound to ``request_id``.

        The request ID is closed over rather than read back off the session
        because it must still be correct for events emitted *after* the
        turn has finished — ``postResponseComplete`` most of all.
        """

        async def emit(event: Event) -> None:
            await self._dispatch(event, request_id)

        return emit

    async def _broadcast(self, event: Event) -> None:
        """Dispatch a session-wide event, with no turn to attribute it to."""
        await self._dispatch(event, None)

    async def _dispatch(self, event: Event, request_id: str | None) -> None:
        """Call ``AcApp.<name>`` on every connected browser.

        Turn-scoped events take the request ID as their first argument; the
        rest take only the payload.
        """
        if self._event_callback is not None:
            args: tuple[Any, ...] = (
                (event.payload,)
                if not event.turn_scoped
                else (request_id, event.payload)
            )
            try:
                await self._event_callback(event.name, *args)
            except Exception as exc:
                logger.warning("Broadcast of %s failed: %s", event.name, exc)

        # After the browsers have the event rather than before, and outside
        # the callback check because it is engine state, not display: a
        # subagent that ends with a dialog still open produces two events,
        # and the terminal status is what explains the denial that follows.
        if event.name == "subagentEvent":
            await self._sweep_ended_subagent(event.payload)
        elif event.name == "streamComplete" and request_id:
            await self._post_response_for_background(event.payload, request_id)

    async def _post_response_for_background(
        self, payload: Any, request_id: str
    ) -> None:
        """Run the post-turn housekeeping again when background work ends.

        ``_post_response`` runs in ``_run_turn``'s ``finally``, which is
        reached at the turn's *first* result. A background subagent outlives
        that: the drain follows the stream past it (``session.py`` §
        ``_drain_background``) and only the last of its continuations reports
        the run as over. Until this, the Context tab and the file tree kept
        describing the session as it was before the background work — the
        turn's own footer revised itself, but the derived state around it did
        not, and only the *next* turn moved it on.

        Keyed on the drain's ``background_finished`` flag rather than on
        ``continuation``, because a turn that spawns several subagents
        produces a continuation per result and only one of them ends the run.
        """
        if not isinstance(payload, dict) or not payload.get("background_finished"):
            return
        await self._post_response(request_id)

    async def _sweep_ended_subagent(self, payload: Any) -> None:
        """Close any permission dialog a subagent left open when it ended.

        The other half of ``cancel_for_turn(spare_subagents=True)``. Sparing
        a subagent's request at turn end is only safe because something else
        closes it when the subagent stops working, and this is that
        something: any terminal task status — ``killed`` from ``stop_task()``
        most of all — denies what that subagent was still waiting on.
        """
        if not isinstance(payload, dict) or not payload.get("terminal"):
            return
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return
        try:
            cancelled = await self.permissions.cancel_for_agent(agent_id)
        except Exception:
            logger.exception(
                "Could not sweep pending permissions for subagent %s", agent_id
            )
            return
        if cancelled:
            logger.info(
                "Subagent %s ended with %d permission request(s) unanswered; "
                "they were denied",
                agent_id,
                cancelled,
            )
