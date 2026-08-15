"""``ClaudeCodeService`` — the browser-facing RPC surface for the engine.

The RPC namespace is the class name: ``server.add_service(instance)``
derives it from ``type(instance).__name__``, so every method here is
reachable as ``ClaudeCodeService.<method>`` from the browser. **The class
name is interface, not implementation detail** — renaming it renames every
RPC and breaks every frontend call site
(``specs-reference/3-engine/session.md`` § Dependency quirks).

This service owns the *outward* half of a turn: admitting it, persisting
and broadcasting the user message, spawning the pump, and turning
:class:`~ac_dc.claude_code.messages.Event` objects into ``AcApp.*`` calls
on every connected browser. :class:`~ac_dc.claude_code.session.
EngineSession` owns the engine.

Since phase 3 it also owns the AC-DC subsystems that outlived the native
engine and had nowhere else to live once ``LLMService`` was deleted: the
symbol index's LSP surface, the doc index's background build, review mode,
and the two git writes the user performs by hand. They are grouped in their
own sections below and share nothing with the turn path except this class.

Since phase 4 those indexes face the agent as well as the browser. This
class owns both halves of that: the :class:`~ac_dc.claude_code.mcp_server.
McpBridge` the session is handed as an MCP server, and the
:class:`~ac_dc.claude_code.hooks.Reindexer` behind the ``PostToolUse``
hook that keeps it honest after the agent writes.

Since phase 5 it owns history too: the transcript mirror, the events log
the transcript could never hold, reading a past session back for the
browser — its images and its subagents included — and choosing which
session the engine attaches to. Still absent, landing later in the same
phase: search, delete and the derived index.

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

from ac_dc.claude_code.engine_config import PERMISSION_MODES, EngineConfig
from ac_dc.claude_code.events_log import session_switch_content
from ac_dc.claude_code.health import EngineStartupError
from ac_dc.claude_code.hooks import Reindexer, build_hook_matchers
from ac_dc.claude_code.mcp_server import SERVER_NAME, McpBridge
from ac_dc.claude_code.messages import Event
from ac_dc.claude_code.permissions import (
    PermissionBroker,
    read_denied_read_files,
    write_denied_read_files,
)
from ac_dc.claude_code.review import ReviewMode
from ac_dc.claude_code.session import (
    EngineNotReadyError,
    EngineSession,
    SessionLostError,
    Turn,
    TurnInProgressError,
    ViewerFraming,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[..., Awaitable[Any]]


# Claude Code's built-in slash commands are terminal interface, not SDK
# features. Each maps to the AC-DC affordance that does the same job. An
# unmapped `/command` is answered explicitly and is **never** forwarded to
# the model as prose, which would silently turn a typo into a question
# (specs5/3-engine/session.md § Slash Command Equivalents).
SLASH_EQUIVALENTS: dict[str, str | None] = {
    "context": "the Context tab, which is live rather than a one-shot print",
    "clear": "New Session",
    "model": "the model picker in the chat panel, or the Settings tab",
    "cost": "the usage HUD",
    "rewind": "the undo affordance on your message",
    "permissions": "the Settings tab's permission-mode control and rules list",
    "mcp": "MCP server health in the Context tab",
    "agents": "the subagent inventory in the Context tab",
    "resume": "the history browser",
    "compact": None,
    "login": None,
    "logout": None,
    "doctor": None,
    "bug": None,
    "help": None,
    "vim": None,
    "terminal-setup": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_convert_available() -> bool:
    """Whether document conversion can run — i.e. ``markitdown`` imports.

    An optional extra (``pip install 'ac-dc[docs]'``), so the import is
    probed rather than assumed, and any failure means "not available"
    rather than a broken snapshot.
    """
    try:
        from ac_dc.doc_convert import DocConvert

        return bool(DocConvert._probe_import("markitdown"))
    except Exception:
        return False


class ClaudeCodeService:
    """Browser → engine RPC. One instance per process.

    Parameters
    ----------
    config:
        The AC-DC :class:`~ac_dc.config.Config`. Supplies the repo root
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
        # The events the transcript never holds — commits, resets, mode
        # switches. Built the same way as the store and for the same reason:
        # a path derived from config, not a collaborator injected by the
        # startup path. `None` without a repo, where there is nowhere to
        # write and nothing to browse.
        self.events_log = self._build_events_log()
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
        # AC-DC's own indexes. These are not engine state and did not come
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
        hooks, mcp_servers = self._build_bridge_wiring()

        self.session = EngineSession(
            self._repo_root,
            self.engine_config,
            can_use_tool=self.permissions.can_use_tool,
            hooks=hooks,
            mcp_servers=mcp_servers,
            session_store=self.session_store,
        )

        self._selected_files: list[str] = []
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

        self.review = ReviewMode(
            repo=repo,
            broadcast=self._broadcast,
            set_permission_mode=self._set_review_permission_mode,
            current_permission_mode=lambda: self.session.permission_mode,
            restricted=self._check_localhost_only,
            on_selection_cleared=self._clear_selection,
        )
        self.review.doc_builder = self.doc_builder

    def _build_session_store(self) -> Any:
        """The repo-local transcript mirror, or ``None`` without a repo.

        No repo means no ``.ac-dc4/`` to mirror into, and the engine runs
        fine without a store — the CLI keeps its own transcript under
        ``~/.claude/projects/`` either way. What is lost is survival past
        the CLI's retention window and the history browser with it.
        """
        ac_dc_dir = getattr(self._config, "ac_dc_dir", None)
        if ac_dc_dir is None:
            logger.info(
                "No repo directory, so sessions are not mirrored; history "
                "will only last as long as the CLI keeps its own transcript."
            )
            return None
        from ac_dc.claude_code.session_store import RepoSessionStore

        return RepoSessionStore(Path(ac_dc_dir) / "sessions")

    def _build_events_log(self) -> Any:
        """``.ac-dc4/events.jsonl``, or ``None`` without a repo.

        Nothing derives this file, so nothing can rebuild it — but its
        absence costs only the operational lines in a browsed transcript,
        never a session. So a repoless run logs a note and carries on
        rather than failing to start.
        """
        ac_dc_dir = getattr(self._config, "ac_dc_dir", None)
        if ac_dc_dir is None:
            return None
        from ac_dc.claude_code.events_log import EventsLog

        return EventsLog(Path(ac_dc_dir) / "events.jsonl")

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
        from ac_dc.doc_index.background import DocIndexBuilder
        from ac_dc.doc_index.index import DocIndex
        from ac_dc.doc_index.keyword_enricher import (
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
            repo=self._repo,
            progress=self._send_startup_progress,
        )

    def _build_bridge_wiring(self) -> tuple[Any, Any]:
        """The ``hooks`` and ``mcp_servers`` the session is built with.

        Degrades to ``(None, None)``: without the bridge the agent loses
        the symbol map and the outline tools and keeps every built-in, and
        without the hook the file tree needs a manual refresh. Both are
        worth losing to keep a session that starts. Refusing to construct
        would trade a missing feature for a dead editor.
        """
        try:
            hooks = build_hook_matchers(self.reindexer, self._broadcast)
        except Exception as exc:
            logger.warning(
                "Post-write re-index hook unavailable; the file tree and the "
                "indexes will not follow the agent's writes: %s",
                exc,
            )
            hooks = None
        try:
            mcp_servers = {SERVER_NAME: self.mcp_bridge.build_server()}
        except Exception as exc:
            logger.warning(
                "The ac-dc MCP bridge failed to build; the agent will fall "
                "back to Glob/Grep/Read for repo structure: %s",
                exc,
            )
            mcp_servers = None
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
        """
        return {
            "selected_files": list(self._selected_files),
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

        Pending permission requests are denied first. A ``can_use_tool``
        callback still waiting on a browser would otherwise be cancelled
        without ever answering the CLI's control request.

        Gated because ``add_service`` exposes every public method, which
        makes process teardown reachable from a browser: without the check
        a participant could kill the host's engine mid-turn, which is a
        broader denial than ``cancel_streaming``. The gate does not get in
        the way of the real caller — ``is_caller_localhost`` trusts a call
        with no RPC caller behind it, so an in-process teardown hook passes.
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
        AC-DC's own subsystems, which this service now owns. The shell reads
        them on first paint to decide whether to show the doc-index progress
        overlay and the review banner.

        ``doc_convert_available`` is not engine state — it is a server
        capability probe that the shell has nowhere else to read. Document
        conversion survives the conversion untouched
        (``specs5/plan/inventory.md`` § Frontend — KEEP unchanged), and this
        snapshot is the only one the shell fetches once the chat path moves
        off ``LLMService``, so the probe comes with it.
        """
        return {
            "messages": await self._current_messages(),
            "selected_files": list(self._selected_files),
            "denied_read_files": self.get_denied_read_files(),
            "session_id": self.session.session_id,
            "repo_name": self._repo_root.name,
            "init_complete": True,
            "engine_ready": self.session.ready,
            "streaming_active": self.session.streaming_active,
            "active_streams": self.session.active_streams(),
            "permission_mode": self.session.permission_mode,
            "model": self.session.model,
            "pending_permissions": self.permissions.pending(),
            **self.doc_builder.status(),
            "review_state": self.review.state(),
            "engine_health": self.get_engine_health(),
            "doc_convert_available": _doc_convert_available(),
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

    def get_selected_files(self) -> list[str]:
        return list(self._selected_files)

    async def set_selected_files(
        self, files: list[str] | None
    ) -> list[str] | dict[str, Any]:
        """Record the picker's selection, dropping paths that do not exist.
        **Localhost only.**

        The selection is a *hint* about what the user is pointing at, not a
        context contract — the agent reads whatever it needs with its own
        tools (``specs5/plan/decisions.md`` CC-14). Filtering here keeps a
        stale selection from framing a turn with a path that was deleted.

        Gated and broadcast for the reasons
        ``specs5/4-features/collaboration.md`` § File Selection gives: only
        localhost clients can change the selection, and everyone sees the
        result immediately. The broadcast is what makes a participant's
        picker agree with the host's rather than drift silently.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        resolved: list[str] = []
        for entry in files or []:
            if not isinstance(entry, str) or not entry:
                continue
            path = Path(entry)
            absolute = path if path.is_absolute() else self._repo_root / path
            if absolute.exists():
                resolved.append(entry)
            else:
                logger.debug("Dropping selected file that does not exist: %s", entry)
        self._selected_files = resolved
        await self._broadcast(
            Event("filesChanged", list(resolved), turn_scoped=False)
        )
        return list(resolved)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    async def chat_streaming(
        self,
        request_id: str,
        message: str,
        files: list[str] | None = None,
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
            files=list(files) if files is not None else list(self._selected_files),
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
        # shows the message even if the turn then fails. image_refs stays
        # empty until image persistence lands in phase 3 — data URIs are
        # never broadcast, because a handful of screenshots would be
        # megabytes per client.
        await self._broadcast(
            Event(
                "userMessage",
                {
                    "content": message,
                    "request_id": request_id,
                    "files": list(turn.files),
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
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
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
                    "disk_warning": None,
                },
            ),
            request_id,
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
        """
        note = getattr(self.session, "note_permission_mode", None)
        if note is not None:
            note(mode)
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
        the broker uses that to fail fast rather than stalling a turn for
        five minutes.
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
        ``ac_dc.llm._rpc_lifecycle``: this package has no import edge to
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
        try:
            applied = await self.session.set_permission_mode(mode)
        except ValueError as exc:
            return {"error": str(exc), "valid_modes": list(PERMISSION_MODES)}
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("set_permission_mode(%r) failed", mode)
            return {"error": f"Could not change the permission mode: {exc}"}
        await self._broadcast(
            Event("permissionModeChanged", {"mode": applied, "by": "user"}, turn_scoped=False)
        )
        return {"mode": applied}

    async def set_model(self, model: str | None = None) -> dict[str, Any]:
        """Switch models mid-session. ``None`` restores the CLI default.
        **Localhost only.**

        Models differ in what a turn costs the host, and the host is the one
        paying — under a subscription, in rate-limit headroom.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            applied = await self.session.set_model(model)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("set_model(%r) failed", model)
            return {"error": f"Could not change the model: {exc}"}
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
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.session.rewind_files(user_message_id)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("rewind_files(%r) failed", user_message_id)
            return {"error": f"Could not rewind: {exc}"}
        return {"restored": [], "user_message_id": user_message_id}

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """Kill one subagent. It reports back as ``status="killed"``.
        **Localhost only.**

        Killing a subagent mid-write is a way to leave the tree in a state
        nobody asked for, and it interrupts the host's turn.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            await self.session.stop_task(task_id)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("stop_task(%r) failed", task_id)
            return {"error": f"Could not stop the task: {exc}"}
        return {"status": "stopping", "task_id": task_id}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def get_context_usage(self) -> dict[str, Any]:
        """The live context breakdown, plus when we fetched it."""
        try:
            usage = await self.session.get_context_usage()
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("get_context_usage failed")
            return {"error": f"Could not read context usage: {exc}"}
        return {"usage": usage, "fetched_at": _now()}

    async def get_mcp_status(self) -> dict[str, Any]:
        try:
            return await self.session.get_mcp_status()
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("get_mcp_status failed")
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
            logger.exception("get_server_info failed")
            return {"error": f"Could not read server info: {exc}"}
        return info or {}

    # ------------------------------------------------------------------
    # Indexing — AC-DC's own, not the engine's
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

    def get_snippets(self) -> list[dict[str, str]]:
        """The prompt snippets for the current situation.

        Two sets now, not three: ``review`` while a review is active and
        ``code`` otherwise. The ``doc`` set went with the modes — there is
        no longer a state in which documents are the only thing the agent
        can see, so a document-specific snippet list has nothing to key off.
        """
        if self.review.active:
            return self._config.get_snippets("review")
        return self._config.get_snippets("code")

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
        the CLI, which rebuilds its own context from it. AC-DC does not read
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

        from ac_dc.claude_code import history

        try:
            return await history.list_sessions(
                self.session_store, str(self._repo_root), limit=max(0, int(limit))
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

        from ac_dc.claude_code import history

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

        from ac_dc.claude_code import history

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

        from ac_dc.claude_code import history

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

        from ac_dc.claude_code import history

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
        from ac_dc.claude_code.commit import commit_all

        return await commit_all(self)

    def reset_to_head(self) -> dict[str, Any]:
        """Discard every uncommitted change. **Localhost only.**"""
        from ac_dc.claude_code.commit import reset_to_head

        return reset_to_head(self)

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
        """
        return await self.review.start(branch, base_commit)

    async def end_review(self) -> dict[str, Any]:
        """Leave review mode, restoring git state and posture.
        **Localhost only.**"""
        return await self.review.end()

    def get_review_state(self) -> dict[str, Any]:
        """The current review, or the inactive shape."""
        return self.review.state()

    def get_review_file_diff(self, path: str) -> dict[str, Any]:
        """The forward diff for one file in the active review."""
        return self.review.file_diff(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _clear_selection(self) -> None:
        """Drop the file selection and tell every client.

        Review entry's use of this is the point: the selection described
        the branch you were on, and the tree has just moved.
        """
        self._selected_files = []
        await self._broadcast(Event("filesChanged", [], turn_scoped=False))

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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop; dropped %s broadcast", event.name)
            return
        task = loop.create_task(self._broadcast(event))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    def _slash_response(self, message: str) -> dict[str, Any] | None:
        """Answer a built-in slash command without involving the model.

        Returns ``None`` for anything that should reach the engine —
        including custom commands from ``.claude/commands/``, which the CLI
        resolves itself.
        """
        text = (message or "").strip()
        if not text.startswith("/") or len(text) < 2:
            return None
        command = text[1:].split(None, 1)[0].lower()
        if command not in SLASH_EQUIVALENTS:
            # Either a custom command from the project's settings sources
            # or a typo. The CLI knows which; we do not.
            return None
        equivalent = SLASH_EQUIVALENTS[command]
        if equivalent is None:
            return {
                "status": "unsupported",
                "command": command,
                "message": (
                    f"/{command} is a Claude Code terminal command and has no "
                    f"equivalent here."
                ),
            }
        return {
            "status": "unsupported",
            "command": command,
            "message": f"/{command} is available as {equivalent}.",
            "equivalent": equivalent,
        }

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
        if self._event_callback is None:
            return
        args: tuple[Any, ...] = (
            (event.payload,) if not event.turn_scoped else (request_id, event.payload)
        )
        try:
            await self._event_callback(event.name, *args)
        except Exception as exc:
            logger.warning("Broadcast of %s failed: %s", event.name, exc)
