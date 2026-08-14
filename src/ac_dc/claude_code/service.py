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

Scope note — this is conversion phase 2 (``specs5/plan/README.md``). The
chat path runs on this service and the permission gate is live. Deliberately
absent, each landing in a later phase with the subsystem it belongs to:

- Transcript mirroring, ``history_*``, image persistence — phase 5.
- ``files_reindexed`` in ``postResponseComplete`` — phase 4, with the MCP
  bridge. Reported empty until then rather than omitted, so the frontend
  contract does not change when it starts being populated.

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
from ac_dc.claude_code.health import EngineStartupError
from ac_dc.claude_code.messages import Event
from ac_dc.claude_code.permissions import (
    PermissionBroker,
    read_denied_read_files,
    write_denied_read_files,
)
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
        # The permission gate. Constructed before the session because the
        # session is built *around* its callback: attaching it afterwards
        # would need a reconnect, and a session running without it would
        # write files without asking.
        self.permissions = PermissionBroker(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            localhost_available=self._localhost_available,
        )
        self.session = EngineSession(
            self._repo_root,
            self.engine_config,
            can_use_tool=self.permissions.can_use_tool,
        )

        self._selected_files: list[str] = []
        # Serialises connect attempts from concurrent first turns, so two
        # clients sending at once cannot spawn two CLI subprocesses.
        self._connect_lock = asyncio.Lock()
        self._connect_error: str | None = None
        self._turn_tasks: set[asyncio.Task[Any]] = set()

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
            try:
                await self.session.connect(resume=resume)
            except EngineStartupError as exc:
                self._connect_error = str(exc)
                logger.error("Claude Code engine failed to start: %s", exc)
                await self._broadcast(
                    Event("engineHealth", self.session.health.to_dict(), turn_scoped=False)
                )
                return {"error": str(exc), "reason": "startup_failed"}
            self._connect_error = None
            await self._broadcast(
                Event("engineHealth", self.session.health.to_dict(), turn_scoped=False)
            )
            return {"status": "ready", "health": self.session.health.to_dict()}

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

    def get_current_state(self) -> dict[str, Any]:
        """Everything a freshly connected browser needs to render.

        ``messages`` is empty until the mirrored transcript lands in phase
        5; the key is present so the frontend contract does not change
        when it starts being populated.

        ``pending_permissions`` is how a client that connects while a
        dialog is open gets the dialog: the request was broadcast before it
        was listening, and the callback is still waiting.

        ``doc_convert_available`` is not engine state — it is a server
        capability probe that the shell has nowhere else to read. Document
        conversion survives the conversion untouched
        (``specs5/plan/inventory.md`` § Frontend — KEEP unchanged), and this
        snapshot is the only one the shell fetches once the chat path moves
        off ``LLMService``, so the probe comes with it.
        """
        return {
            "messages": [],
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
            "doc_index_ready": False,
            "doc_index_enriched": False,
            "review_state": {"active": False},
            "engine_health": self.get_engine_health(),
            "doc_convert_available": _doc_convert_available(),
        }

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
            viewer=ViewerFraming.from_dict(viewer),
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
        """
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
                    "files_reindexed": [],
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
    # Internals
    # ------------------------------------------------------------------

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
