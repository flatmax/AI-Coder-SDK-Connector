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

Scope note — this is conversion phase 1 (``specs5/plan/README.md``). The
engine is registered alongside ``LLMService`` and reachable, but not yet
wired to the UI. Deliberately absent, each landing in a later phase with
the subsystem it belongs to:

- ``get_denied_read_files`` / ``set_denied_read_files`` — phase 2. They
  write ``Read(path)`` deny rules through a ``PermissionUpdate``, which
  needs the permission layer. A stub that filtered in memory would report
  success while the CLI happily read the file.
- ``resolve_permission`` and the ``can_use_tool`` gate — phase 2.
- Transcript mirroring, ``history_*``, image persistence — phase 3.
- ``files_reindexed`` in ``postResponseComplete`` — phase 3, with the MCP
  bridge. Reported empty until then rather than omitted, so the frontend
  contract does not change when it starts being populated.

**The engine connects lazily**, on the first turn or an explicit
``connect_engine()`` call. Phase 1 must not add a second ``claude``
subprocess to every app startup while the native engine is still the one
serving the UI.

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
        self.session = EngineSession(self._repo_root, self.engine_config)

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

        Idempotent, and safe to call concurrently. Returns rather than
        raises on failure: the caller is a browser, and an RPC exception
        would surface as a generic transport error instead of the
        actionable message the failure carries.
        """
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

    async def shutdown(self) -> None:
        """Disconnect the engine as part of graceful shutdown."""
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
        3; the key is present so the frontend contract does not change
        when it starts being populated.
        """
        return {
            "messages": [],
            "selected_files": list(self._selected_files),
            "denied_read_files": [],
            "session_id": self.session.session_id,
            "repo_name": self._repo_root.name,
            "init_complete": True,
            "engine_ready": self.session.ready,
            "streaming_active": self.session.streaming_active,
            "active_streams": self.session.active_streams(),
            "permission_mode": self.session.permission_mode,
            "model": self.session.model,
            "pending_permissions": [],
            "doc_index_ready": False,
            "doc_index_enriched": False,
            "review_state": {"active": False},
            "engine_health": self.get_engine_health(),
        }

    def get_selected_files(self) -> list[str]:
        return list(self._selected_files)

    def set_selected_files(self, files: list[str] | None) -> list[str]:
        """Record the picker's selection, dropping paths that do not exist.

        The selection is a *hint* about what the user is pointing at, not a
        context contract — the agent reads whatever it needs with its own
        tools (``specs5/plan/decisions.md`` CC-14). Filtering here keeps a
        stale selection from framing a turn with a path that was deleted.
        """
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

        The turn runs in a background task whose lifetime is independent of
        any WebSocket, so a client that disconnects mid-turn re-attaches to
        a turn that kept running.
        """
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
        """Interrupt the turn in flight.

        The pump keeps running to the result message; this only asks the
        engine to stop. See ``specs5/3-engine/session.md`` § Cancellation.
        """
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
    # Live controls
    # ------------------------------------------------------------------

    async def set_permission_mode(self, mode: str) -> dict[str, Any]:
        """Switch the safety posture. No reconnect, no turn interruption."""
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
        """Switch models mid-session. ``None`` restores the CLI default."""
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

        ``restored`` is empty because the SDK's ``rewind_files()`` returns
        nothing — the reference spec's ``{restored: [...]}`` cannot be
        satisfied from this call alone. The frontend should refresh the
        file tree on success rather than trusting the list.
        """
        try:
            await self.session.rewind_files(user_message_id)
        except (EngineNotReadyError, SessionLostError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("rewind_files(%r) failed", user_message_id)
            return {"error": f"Could not rewind: {exc}"}
        return {"restored": [], "user_message_id": user_message_id}

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """Kill one subagent. It reports back as ``status="killed"``."""
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
        try:
            await self.session.reconnect_mcp_server(name)
        except Exception as exc:
            logger.warning("reconnect_mcp_server(%r) failed: %s", name, exc)
            return {"error": f"Could not reconnect {name}: {exc}"}
        return {"status": "reconnecting", "name": name}

    async def toggle_mcp_server(self, name: str, enabled: bool) -> dict[str, Any]:
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
