"""The Antigravity engine adapter — the 31 methods the router mounts.

The second half of [AG-3](../../specs5/plan-ag/decisions.md): the router
gives both engines one RPC namespace, and this is what it routes to when
Antigravity is master. It is the object ``build_router(…,
engine=ANTIGRAVITY)`` refuses to mount without.

Why 31 and not 48
=================
The capability descriptor makes 17 of the Claude adapter's methods
optional on this engine — the five ``history_*``, the three MCP ones,
``get_context_usage``, ``get_account_usage``, ``list_commands``,
``get_session_storage``, ``resume_session``, the two subagent-transcript
readers, ``stop_task`` and ``rewind_files``. The router generates a
refusal for each rather than requiring an implementation here, so this
module contains **no stubs that return an empty dict**. Anything absent is
absent by declaration in ``capabilities.py``, where the reason is
recorded; nothing is absent by having been skipped.

Two halves, and only one of them is about the engine
====================================================
Roughly a third of the surface is not engine work at all. ``lsp_get_hover``
reads the symbol index. ``get_commit_graph`` reads git. ``start_review``
arranges branches. Those touch the repository and the indexes, which both
engines share, and the failure mode to avoid is the second engine quietly
growing its own copy of the file tree.

So this adapter **reuses the same modules and the same objects**:

- ``symbol_index`` is injected — the one instance ``main.py`` built and
  handed to the other adapter, not a second index over the same tree.
- ``review`` is a real :class:`~aic_dc.claude_code.review.ReviewMode`,
  constructed here with *this* engine's ``set_permission_mode``. Its
  collaborators are injectable precisely so a second engine can own its
  own posture while sharing the git arrangement.
- ``commit_all`` and ``reset_to_head`` call
  ``aic_dc.claude_code.commit`` directly. That module takes the service
  as its argument and reaches for ``_check_localhost_only``, ``_repo``,
  ``review``, ``_committing``, ``_turn_tasks`` and ``_broadcast`` — so
  this class provides exactly that contract rather than reimplementing
  two hundred lines of git handling.

Importing ``claude_code.review`` and ``claude_code.commit`` from here
reads oddly. It is the same trade AG-3 makes about the class name: those
modules are engine-agnostic in everything but their package, and moving
them is a mechanical change that can happen later. Copying them would not
be mechanical, and the copy is what drifts.

The conversation half
=====================
Built on what phases 3 and 4 landed and adding nothing new to it:
:class:`~aic_dc.antigravity.session.AntigravitySession` for the lifecycle
and the turn, :class:`~aic_dc.antigravity.steps.StepTranslator` for the
event vocabulary, and
:class:`~aic_dc.antigravity.permissions.AntigravityPermissionGate` for
AG-5's dialog — which drives the *shared* ``PermissionBroker``, so
``resolve_permission`` here answers the same queue the Claude adapter's
does.

Governing spec: ``specs5/plan-ag/`` — AG-3, AG-5, AG-6, AG-9, AG-10.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aic_dc import capabilities
from aic_dc.antigravity import options
from aic_dc.antigravity.credentials import Credentials
from aic_dc.antigravity.credentials import resolve as resolve_credentials
from aic_dc.antigravity.permissions import AntigravityPermissionGate
from aic_dc.antigravity.session import AntigravitySession
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

#: Permission postures this engine understands.
#:
#: Deliberately fewer than Claude's. ``plan`` is real — ``AgentBehavior``
#: has a planning mode and ``BuiltinSlashCommandName.PLAN`` is the one
#: slash command that exists — and ``default`` is the dialog on every
#: mutating call. What is *not* here is ``acceptEdits`` and
#: ``bypassPermissions``: AG-5 makes the dialog a requirement of this
#: engine rather than a feature, and a posture that skips it is the
#: blanket bypass that decision says must never ship.
PERMISSION_MODES = ("default", "plan")


class AntigravityService:
    """The Antigravity engine behind the shared RPC surface.

    Parameters
    ----------
    config:
        The ``ConfigManager``. Read for ``repo_root`` only.
    repo:
        The shared ``Repo`` service — the same instance the other adapter
        holds. Git is not per-engine.
    event_callback:
        ``async (event_name, *args) -> None``, the server-push dispatcher
        ``main.py`` wires once the RPC server is up.
    symbol_index:
        The shared index, injected after the background build finishes.
        ``None`` until then, which every reader here treats as "no answer
        yet" rather than "nothing here" — the same contract the Claude
        adapter's LSP methods have.
    """

    def __init__(
        self,
        config: Any,
        *,
        repo: Any = None,
        event_callback: Any = None,
        symbol_index: Any = None,
        credentials: Credentials | None = None,
        model: str = options.DEFAULT_MODEL,
    ) -> None:
        self._config = config
        self._repo = repo
        self._event_callback = event_callback
        self._collab: Any = None
        self.symbol_index = symbol_index

        repo_root = getattr(config, "repo_root", None) or Path.cwd()
        self._repo_root = Path(repo_root)
        self._model = model
        self._credentials = credentials or resolve_credentials()

        # The contract `claude_code.commit` reaches for. Named here rather
        # than discovered by a failing call: that module takes the service
        # as its argument, so these attributes *are* its interface.
        self._committing = False
        self._turn_tasks: set[asyncio.Task] = set()

        self._session: AntigravitySession | None = None
        self._gate: AntigravityPermissionGate | None = None
        self._permission_mode = "default"
        self._denied_read_files: list[str] = []
        self._viewer: dict[str, Any] = {}
        self._turns: dict[str, StepTranslator] = {}
        self._errors: list[dict[str, Any]] = []

        from aic_dc.claude_code.review import ReviewMode

        # This engine's own ReviewMode. Same git arrangement, this
        # engine's posture — which is why its collaborators are injectable
        # rather than reached for.
        self.review = ReviewMode(
            repo=repo,
            broadcast=self._broadcast,
            set_permission_mode=self._apply_permission_mode,
            current_permission_mode=lambda: self._permission_mode,
            restricted=self._check_localhost_only,
        )

    def _attach_symbol_index(self, symbol_index: Any) -> None:
        """Hand over the built symbol index. Called once, from startup.

        The same name and the same underscore as the Claude adapter's,
        and both matter. The name is what lets ``main.py``'s deferred init
        hand the *one* index to every mounted adapter in a loop rather
        than naming each engine — which is how the second engine would
        otherwise end up either without an index or with its own copy of
        one. The underscore keeps it off the RPC surface: ``add_service``
        publishes every public method, and a browser calling this with a
        JSON value would replace the index with something that has no
        ``lsp_get_hover``.

        There is no ``_mark_symbol_index_ready`` counterpart. That flag
        feeds the Claude adapter's symbol-map surface, which this engine
        does not serve; adding a field nothing reads would be the stub
        this class is written to avoid.
        """
        self.symbol_index = symbol_index
        self.review.symbol_index = symbol_index

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    async def connect_engine(self, resume: str | None = None) -> dict[str, Any]:
        """Start the harness. **Localhost only.**

        ``resume`` is accepted and ignored, and that is a declaration
        rather than an oversight: resuming by ``conversation_id`` is phase
        5, and the descriptor reports ``session_mirror`` as unbuilt on
        this engine. Silently starting a *fresh* session when the caller
        asked to resume one would be the wrong kind of success.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if resume:
            return {
                "error": "unsupported",
                "message": (
                    "Resuming an Antigravity conversation is not built yet "
                    "(phase 5). Starting a fresh session instead would "
                    "silently lose the context you asked for."
                ),
            }
        if not self._credentials.available:
            return {
                "error": "no_credentials",
                "message": (
                    "Antigravity needs a Gemini API key or a Vertex project; "
                    "the agy OAuth login cannot supply one. See AG-R-8."
                ),
                "credentials": self._credentials.report(),
            }
        try:
            await self._ensure_session()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return self._record_error("connect", exc)
        return {"status": "connected", "model": self._model}

    async def _ensure_session(self) -> AntigravitySession:
        if self._session is not None and self._session.started:
            return self._session
        self._gate = AntigravityPermissionGate(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            localhost_available=self._localhost_available,
        )
        session = AntigravitySession(
            self._repo_root,
            credentials=self._credentials,
            model=self._model,
            decide_hook=self._gate.as_hook(),
        )
        await session.start()
        self._session = session
        return session

    async def new_session(self) -> dict[str, Any]:
        """Discard the conversation and start a fresh one. **Localhost only.**"""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        await self._close_session()
        self._turns.clear()
        await self._broadcast(
            Event("sessionReset", {"engine": "antigravity"}, turn_scoped=False)
        )
        return {"status": "ok"}

    async def restart_session(self) -> dict[str, Any]:
        """Tear the harness down and bring it back. **Localhost only.**"""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        await self._close_session()
        try:
            await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            return self._record_error("restart", exc)
        return {"status": "ok", "model": self._model}

    async def shutdown(self) -> dict[str, Any] | None:
        """Stop the harness. Never raises — teardown that raises orphans it."""
        await self._close_session()
        return {"status": "ok"}

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        self._gate = None
        if session is not None:
            await session.close()

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    async def chat_streaming(
        self,
        request_id: str,
        message: str,
        images: list[str] | None = None,
        viewer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one turn, streaming its events. **Localhost only.**

        ``images`` is declined rather than dropped. The SDK accepts image
        input, so this is unbuilt rather than impossible — but a turn that
        silently discarded the screenshot a user attached would answer the
        wrong question convincingly, which is worse than refusing.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if images:
            return {
                "error": "unsupported",
                "message": (
                    "Image input is not wired for the Antigravity engine "
                    "yet. The turn was not sent, rather than sent without "
                    "the images."
                ),
            }
        if viewer:
            self._viewer = dict(viewer)

        try:
            session = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            return self._record_error("connect", exc)

        translator = StepTranslator(request_id)
        self._turns[request_id] = translator
        try:
            async for event in session.stream_turn(message, translator=translator):
                await self._dispatch(event, request_id)
        except Exception as exc:  # noqa: BLE001 - a turn failure is an event
            self._record_error("turn", exc)
            await self._dispatch(
                Event(
                    "systemEvent",
                    {"subtype": "engine_error", "data": {"message": str(exc)}},
                ),
                request_id,
            )
            return {"status": "error", "message": str(exc)}
        finally:
            self._turns.pop(request_id, None)
        return {
            "status": "ok",
            "response": translator.response_text(),
            "usage": translator.turn_usage(),
        }

    async def cancel_streaming(self, request_id: str) -> dict[str, Any]:
        """Halt the running turn. **Localhost only.**

        ``request_id`` is checked rather than ignored: a stale cancel from
        a reconnecting browser must not stop the turn that replaced the
        one it was looking at.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if request_id not in self._turns:
            return {"status": "not_running", "request_id": request_id}
        if self._session is not None:
            await self._session.cancel()
        return {"status": "ok", "request_id": request_id}

    async def resolve_permission(
        self, permission_id: str, decision: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a permission dialog. **Localhost only.**

        Forwarded to the *shared* broker, which is the whole of AG-5's
        "one ask path": this resolves the same queue, with the same
        first-one-wins rule, that the Claude adapter's does.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if self._gate is None:
            return {"error": "unknown", "reason": "No engine session is running."}
        return await self._gate.broker.resolve(
            permission_id, decision or {}, resolved_by="localhost"
        )

    def _note_permission_prompt(self, tool_use_id: str | None) -> str | None:
        """Attribute a dialog to the turn that caused it.

        One turn at a time on this engine — ``stream_turn`` raises rather
        than queueing a second — so the running request is unambiguous.
        """
        for request_id, translator in self._turns.items():
            translator.stats.permission_prompts += 1
            return request_id
        return None

    # ------------------------------------------------------------------
    # State the UI reads
    # ------------------------------------------------------------------

    async def get_current_state(self) -> dict[str, Any]:
        """Everything the app needs to render itself after a reconnect."""
        session = self._session
        return {
            "connected": bool(session and session.started),
            "model": self._model,
            "permission_mode": self._permission_mode,
            "read_only": session.read_only if session else True,
            "streaming": bool(self._turns),
            "request_id": next(iter(self._turns), None),
            "blocks": [
                block
                for translator in self._turns.values()
                for block in translator.rendered_blocks()
            ],
            "pending_permissions": (
                self._gate.broker.pending() if self._gate is not None else []
            ),
            "review": self.review.state(),
            "credentials": self._credentials.report(),
            "denied_read_files": list(self._denied_read_files),
            # No `cost`. AG-6: there is no USD figure anywhere on this
            # engine, and a zero would be a measurement.
        }

    async def get_model(self) -> dict[str, Any]:
        return {"model": self._model, "models": [self._model]}

    async def set_model(self, model: str | None = None) -> dict[str, Any]:
        """Change the model. **Localhost only.**

        Takes effect on the next session: the harness is started with its
        model fixed, and restarting one mid-conversation would silently
        drop the context the user is talking to.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not model:
            return {"model": self._model}
        self._model = model
        return {
            "model": self._model,
            "applies": "next_session" if self._session else "now",
        }

    async def set_permission_mode(self, mode: str) -> dict[str, Any]:
        """Set the posture. **Localhost only.**

        Refuses anything outside :data:`PERMISSION_MODES`, and the two it
        refuses are the point: ``acceptEdits`` and ``bypassPermissions``
        would skip the dialog AG-5 makes non-negotiable for this engine.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        applied = await self._apply_permission_mode(mode)
        if applied is None:
            return {
                "error": "unsupported",
                "message": (
                    f"{mode!r} is not a posture this engine offers. It has "
                    f"{', '.join(PERMISSION_MODES)} — and deliberately no "
                    "mode that skips the permission dialog (AG-5)."
                ),
            }
        return {"permission_mode": applied}

    async def _apply_permission_mode(self, mode: str) -> str | None:
        if mode not in PERMISSION_MODES:
            return None
        self._permission_mode = mode
        await self._broadcast(
            Event("permissionMode", {"mode": mode}, turn_scoped=False)
        )
        return mode

    def get_denied_read_files(self) -> list[str]:
        return list(self._denied_read_files)

    def set_denied_read_files(self, files: list[str] | None = None) -> dict[str, Any]:
        """**Localhost only.** A list of paths the agent may not read."""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        self._denied_read_files = [str(f) for f in (files or [])]
        return {"status": "ok", "count": len(self._denied_read_files)}

    def set_viewer_state(
        self,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """**Localhost only.** What the user is looking at, for turn framing."""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        self._viewer = (
            {"path": path, "start_line": start_line, "end_line": end_line}
            if path
            else {}
        )
        return {"status": "ok"}

    def navigate_file(self, path: str) -> dict[str, Any]:
        """Point every client at a file. Unrestricted — it changes nothing."""
        self._broadcast_soon(
            Event("navigateFile", {"path": path}, turn_scoped=False)
        )
        return {"status": "ok", "path": path}

    # ------------------------------------------------------------------
    # Health and diagnostics
    # ------------------------------------------------------------------

    def get_engine_health(self) -> dict[str, Any]:
        """What is wrong, if anything, before a turn is attempted.

        The credential is the first thing checked because AG-R-8 makes it
        the most likely failure and it is knowable without the network.
        Workspace containment is the second (AG-10): a diverted write is
        reported as a success, so it has to be a startup check rather than
        something noticed later.
        """
        session = self._session
        return {
            "engine": "antigravity",
            "model": self._model,
            "connected": bool(session and session.started),
            "credentials": self._credentials.report(),
            "workspace": str(self._repo_root),
            "read_only": session.read_only if session else True,
            "errors": len(self._errors),
        }

    async def get_engine_errors(self, limit: int | None = None) -> dict[str, Any]:
        errors = self._errors[-limit:] if limit else list(self._errors)
        return {"errors": errors, "total": len(self._errors)}

    async def get_sdk_surface(self) -> dict[str, Any]:
        """The AG-8 drift probe's report, for the diagnostics tab."""
        from aic_dc.antigravity.surface import surface_report

        return surface_report()

    async def get_server_info(self) -> dict[str, Any]:
        return {
            "engine": "antigravity",
            "model": self._model,
            "repo_root": str(self._repo_root),
            "capabilities": capabilities.descriptor(capabilities.ANTIGRAVITY),
        }

    def _record_error(self, phase: str, exc: Exception) -> dict[str, Any]:
        """Keep a failure where the diagnostics tab can find it.

        The Claude engine writes these to ``engine-errors.jsonl``; this
        holds them in memory, because a second on-disk log wants a path
        convention and there is no reader for one yet. Recorded as a gap
        rather than pretended away.
        """
        message = " ".join(str(exc).split())[:500]
        entry = {"phase": phase, "error": message, "type": type(exc).__name__}
        self._errors.append(entry)
        logger.warning("Antigravity engine error during %s: %s", phase, message)
        return {"error": "engine", "phase": phase, "message": message}

    # ------------------------------------------------------------------
    # Repository and indexes — shared objects, not a second copy
    # ------------------------------------------------------------------

    def lsp_get_hover(self, path: str, line: int, col: int) -> dict[str, Any] | None:
        if self.symbol_index is None:
            return None
        return self.symbol_index.lsp_get_hover(path, line, col)

    def lsp_get_definition(
        self, path: str, line: int, col: int
    ) -> dict[str, Any] | None:
        if self.symbol_index is None:
            return None
        return self.symbol_index.lsp_get_definition(path, line, col)

    def lsp_get_references(
        self, path: str, line: int, col: int
    ) -> list[dict[str, Any]]:
        if self.symbol_index is None:
            return []
        return self.symbol_index.lsp_get_references(path, line, col)

    def lsp_get_completions(
        self, path: str, line: int, col: int, prefix: str = ""
    ) -> list[dict[str, Any]]:
        if self.symbol_index is None:
            return []
        return self.symbol_index.lsp_get_completions(path, line, col, prefix)

    async def commit_all(self) -> dict[str, Any]:
        """**Localhost only.** The shared implementation, not a second one."""
        from aic_dc.claude_code.commit import commit_all

        return await commit_all(self)

    async def reset_to_head(self) -> dict[str, Any]:
        """**Localhost only.**"""
        from aic_dc.claude_code.commit import reset_to_head

        return await reset_to_head(self)

    def get_commit_graph(
        self, limit: int = 100, offset: int = 0, include_remote: bool = False
    ) -> dict[str, Any]:
        return self.review.commit_graph(
            limit=limit, offset=offset, include_remote=include_remote
        )

    def check_review_ready(self) -> dict[str, Any]:
        return self.review.check_ready()

    async def start_review(self, branch: str, base_commit: str) -> dict[str, Any]:
        """**Localhost only.**"""
        return await self.review.start(branch, base_commit)

    async def end_review(self) -> dict[str, Any]:
        """**Localhost only.**"""
        return await self.review.end()

    def get_review_state(self) -> dict[str, Any]:
        return self.review.state()

    def get_review_file_diff(self, path: str) -> dict[str, Any]:
        return self.review.file_diff(path)

    # ------------------------------------------------------------------
    # Plumbing the shared modules reach for
    # ------------------------------------------------------------------

    def _check_localhost_only(self) -> dict[str, Any] | None:
        """The restricted shape for a non-localhost caller.

        Fails closed: an exception from the collab check is a denial, not
        a silent allow. Same rule as the Claude adapter's, and it has to
        be — ``permissions.md``'s localhost-only property is a property of
        the product, not of an engine.
        """
        collab = self._collab
        if collab is None:
            return None
        try:
            is_local = collab.is_caller_localhost()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Collab localhost check raised: %s; denying", exc)
            return {"error": "restricted", "reason": "localhost_check_failed"}
        if is_local:
            return None
        return {"error": "restricted", "reason": "localhost_only"}

    def _localhost_available(self) -> bool:
        """Whether anyone who *could* answer a dialog is connected."""
        collab = self._collab
        if collab is None:
            return True
        try:
            return bool(collab.has_localhost_client())
        except Exception:  # noqa: BLE001 - absence is not a denial here
            return True

    async def _broadcast(self, event: Event) -> None:
        await self._dispatch(event, None)

    def _broadcast_soon(self, event: Event) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No loop for %s; dropped", event.name)
            return
        task = loop.create_task(self._dispatch(event, None))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _dispatch(self, event: Event, request_id: str | None) -> None:
        """Call ``AcApp.<name>`` on every browser. Never raises.

        Turn-scoped events take the request ID first, exactly as the
        Claude pump's do — the browser has one handler per event name and
        it must not have to know which engine sent it (AG-R-4).
        """
        if self._event_callback is None:
            return
        args: tuple[Any, ...] = (
            (request_id, event.payload) if event.turn_scoped else (event.payload,)
        )
        try:
            await self._event_callback(event.name, *args)
        except Exception:  # noqa: BLE001 - a dead client is not a turn failure
            logger.exception("Dropping %s: the event callback failed", event.name)
