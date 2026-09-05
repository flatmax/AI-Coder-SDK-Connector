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
from aic_dc.claude_code.service import doc_convert_available

logger = logging.getLogger(__name__)

#: Permission postures this engine understands.
#:
#: Deliberately fewer than Claude's six. ``plan`` is real —
#: ``AgentBehavior`` has a planning mode and
#: ``BuiltinSlashCommandName.PLAN`` is the one slash command that exists —
#: and ``default`` is the dialog on every mutating call.
#:
#: ``acceptEdits`` was added on 2026-09-05, and it **amends AG-5 rather
#: than overturning it**. That decision makes the dialog a requirement of
#: this engine, and what it was protecting is the execution path:
#: AG-R-11 measured the agent, refused an edit, reaching for ``sed -i``,
#: then inline ``python3``, then ``list_dir`` — three routes to one write,
#: every one of them through the shell. So this posture lets through a
#: *file write to a file inside the repository* and nothing else;
#: ``run_command`` and the subagent spawners keep their dialog. That is
#: the same line ``agy``'s own accept-edits draws, and the same one
#: Claude's does.
#:
#: What is still *not* here is ``bypassPermissions``. A blanket bypass is
#: what AG-5 says must never ship on this engine, and it is the one
#: posture that would hand AG-R-11's shell route an ungated agent.
PERMISSION_MODES = ("default", "plan", "acceptEdits")


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

    #: Where this transport's transcript mirror lives, under ``.aic-dc/``.
    #:
    #: **Its own root, and that is AG-1 rather than tidiness.** A session
    #: record does not say which engine wrote it, and ``resume_session``
    #: hands an id to whichever engine is master — so a record written by
    #: one would be offered to a harness that has never heard of it, and
    #: the honest failure ("no such session") is indistinguishable from a
    #: bug. A separate root makes a foreign record unreachable by
    #: construction rather than by a check somebody has to remember to
    #: write. Overridden by the ``agy`` transport, which reaches the same
    #: *product* through a different harness with a different session
    #: store, so its ids are no more resumable here than Claude's are.
    MIRROR_DIR = "antigravity-sessions"

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
        # Lifted from `AgyService`, which defined the identical property,
        # once the SDK transport needed it too for AG-15's rule store. One
        # definition rather than two that must agree — the same reasoning as
        # merging the tool tables, and the same failure if they drift.
        self._config_dir = Path(
            getattr(config, "config_dir", None) or Path.home() / ".config" / "aic-dc"
        )
        self._credentials = credentials or resolve_credentials()

        # The contract `claude_code.commit` reaches for. Named here rather
        # than discovered by a failing call: that module takes the service
        # as its argument, so these attributes *are* its interface.
        self._committing = False
        self._turn_tasks: set[asyncio.Task] = set()

        # Phase 5. The mirror and the store it writes into, both `None`
        # without a repo — there is no `.aic-dc/` to mirror into, and the
        # engine runs fine without one. What is lost is the history
        # browser and the ability to resume after a restart, never a turn.
        self.session_store = self._build_session_store()
        self._mirror = self._build_mirror()
        # Whether a connect nobody gave a target for should continue the
        # previous conversation. True at startup, because a server restart
        # is meant to be invisible — that is phase 5's exit criterion in
        # one field. `new_session` is the one thing that clears it, and it
        # is the Claude adapter's arrangement deliberately: two engines
        # that answered "which conversation am I in" differently would
        # make a switch feel like a bug.
        self._auto_resume = True
        # A conversation `resume_session` asked for, held across the
        # connect it triggers so that a concurrent first turn cannot
        # connect around it and get a blank session.
        self._resume_request: str | None = None

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

    def _build_session_store(self) -> Any:
        """The repo-local transcript mirror's store, or ``None``.

        ``MIRROR_DIR`` is read off ``type(self)`` rather than off the
        module, so the ``agy`` transport's override reaches this without
        it having to reimplement the method. The two transports reach the
        same product but not the same *conversation store*: an ``agy``
        conversation id means nothing to the SDK's harness and the other
        way about, so a shared root would offer every session to an engine
        that could resume half of them.
        """
        aic_dc_dir = getattr(self._config, "aic_dc_dir", None)
        if aic_dc_dir is None:
            logger.info(
                "No repo directory, so Antigravity sessions are not "
                "mirrored; there will be no history and no resume."
            )
            return None
        from aic_dc.claude_code.session_store import RepoSessionStore

        return RepoSessionStore(Path(aic_dc_dir) / type(self).MIRROR_DIR)

    def _build_mirror(self) -> Any:
        if self.session_store is None:
            return None
        from aic_dc.antigravity.mirror import SessionMirror

        return SessionMirror(
            self.session_store,
            self._repo_root,
            # Read live rather than copied in, so a mid-session `set_model`
            # is what a browsed turn reports rather than what the session
            # started on.
            model=lambda: self._model,
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

        ``resume`` names the conversation to continue. Absent, the engine
        continues the one it was already in, or — on the first connect of
        a fresh process — the most recent one in this engine's own mirror.
        **That default is what makes a server restart invisible**, which
        is phase 5's exit criterion; ``new_session`` is what turns it off.

        Resuming is a handshake argument, never a replay. The harness
        rebuilds its own context from its own trajectory store; nothing
        here reads our mirror back into a prompt, because a transcript
        that looked right while the model's view had diverged is the exact
        failure ``specs5/3-engine/history.md`` records for the native
        engine.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
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
            await self._ensure_session(resume=resume)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return self._record_error("connect", exc)
        return {"status": "connected", "model": self._model}

    async def _ensure_session(self, resume: str | None = None) -> AntigravitySession:
        if self._session is not None and self._session.started:
            return self._session
        target = await self._resume_target(resume)
        self._gate = AntigravityPermissionGate(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            localhost_available=self._localhost_available,
            # Read live rather than copied in, so a shift-click on the file
            # tree takes effect on the next call rather than the next
            # session. This list had no reader at all until 2026-09-03: the
            # service stored it and answered `get_denied_read_files` with
            # it, and nothing enforced it — survivable only because every
            # read was raising a dialog the user could refuse by hand. The
            # gate now allows reads without asking, so wiring this is part
            # of that change rather than a separate improvement.
            denied_reads=self.get_denied_read_files,
            # The session's posture, read live: `acceptEdits` lets an
            # in-repo file write through without a dialog, and the
            # user flips it from the action bar mid-session.
            permission_mode=lambda: self._permission_mode,
            # AG-15's standing rules. Passed explicitly rather than left to
            # the default so that a host given a config directory keeps all
            # of its state in one place, and so a test that forgets it
            # cannot silently write grants into the developer's own config.
            config_dir=self._config_dir,
        )
        session = AntigravitySession(
            self._repo_root,
            credentials=self._credentials,
            model=self._model,
            decide_hook=self._gate.as_hook(),
            resume=target,
        )
        await session.start()
        self._session = session
        await self._sync_mirror()
        return session

    async def _resume_target(self, requested: str | None) -> str | None:
        """Which conversation the imminent connect attaches to.

        Three cases, in order of how explicit the ask was: an id passed to
        ``connect_engine``; a pending ``resume_session``; and otherwise
        :meth:`_visible_session_id` — the session we are already in, or
        the most recent one in the mirror, or nothing after
        ``new_session``.

        The last two are the same method deliberately, and for the reason
        the Claude adapter gives: the session the engine attaches to and
        the session the browser is shown have to be the same one, and two
        functions answering that separately is how they come to disagree.
        """
        if requested:
            return requested
        if self._resume_request:
            return self._resume_request
        return await self._visible_session_id()

    async def _visible_session_id(self) -> str | None:
        """Which conversation's transcript the browser should be shown."""
        session = self._session
        if session is not None and session.conversation_id:
            return session.conversation_id
        if self._resume_request:
            return self._resume_request
        if not self._auto_resume:
            return None
        return await self._most_recent_session_id()

    async def _most_recent_session_id(self) -> str | None:
        """The newest conversation in this engine's mirror, or ``None``.

        No stored pointer to the "current" one: the store already sorts by
        ``last_modified``, and a pointer file is one more thing that can
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
            logger.exception("Could not work out which conversation to resume")
            return None
        return recent[0].session_id if recent else None

    async def _sync_mirror(self) -> None:
        """Point the mirror at whatever conversation the engine is in now.

        Called after a connect and again on **every** event of a turn,
        because on the SDK transport the id arrives mid-turn: the SDK
        derives it from the event processor's main trajectory, so
        ``Conversation.conversation_id`` is empty until the first step
        lands. :meth:`SessionMirror.attach` is a comparison when nothing
        has changed, which is what makes calling it that often free.
        """
        if self._mirror is None or self._session is None:
            return
        await self._mirror.attach(self._session.conversation_id)

    async def new_session(self) -> dict[str, Any]:
        """Discard the conversation and start a fresh one. **Localhost only.**"""
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        await self._close_session()
        self._turns.clear()
        # The user asked for blank, so neither the pending target nor the
        # store lookup applies. Nothing is deleted: the conversation left
        # behind stays in the mirror and stays loadable.
        self._resume_request = None
        self._auto_resume = False
        if self._mirror is not None:
            self._mirror.detach()
        await self._broadcast(
            Event("sessionReset", {"engine": "antigravity"}, turn_scoped=False)
        )
        await self._broadcast(
            Event(
                "sessionChanged",
                {"session_id": None, "messages": [], "action": "new"},
                turn_scoped=False,
            )
        )
        return {"status": "ok"}

    def _start_blank_session(self) -> None:
        """Do not continue where this engine left off. Called by the router.

        The switch itself is the router's, and it has already told every
        browser the panel is empty; this is the server agreeing with that.
        Underscored so it stays off the RPC surface — it is not a thing a
        browser asks for, and ``new_session`` is the public version of the
        same intent, with the broadcasts a user-initiated reset owes.

        Nothing is deleted. The conversation left behind stays in the
        mirror and stays loadable from the history browser, which is the
        whole reason a switch can afford to be a boundary.
        """
        self._resume_request = None
        self._auto_resume = False
        if self._mirror is not None:
            self._mirror.detach()

    async def restart_session(self) -> dict[str, Any]:
        """Tear the harness down and bring it back. **Localhost only.**

        The conversation comes with it. ``_close_session`` drops the
        session object but not the id it was in, so the reconnect resumes
        rather than starting blank — the same bargain the Claude adapter
        makes, and the reason a restart is a way to apply a setting rather
        than a way to lose your context.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        previous = await self._visible_session_id()
        await self._close_session()
        try:
            await self._ensure_session(resume=previous)
        except Exception as exc:  # noqa: BLE001
            return self._record_error("restart", exc)
        return {"status": "ok", "model": self._model}

    async def resume_session(
        self, session_id: str, fork: bool = False
    ) -> dict[str, Any]:
        """Attach the engine to a past conversation. **Localhost only.**

        Never a replay: the id goes to the harness, which rebuilds its own
        context from its own trajectory store. What we render is a
        *record* of the conversation; what the model gets is the
        conversation.

        ``fork`` is refused rather than approximated. Claude forks by
        copying a transcript the CLI will rebuild from, and Antigravity
        has no counterpart at any layer — the trajectory store is the
        harness's and is opaque (``sdk-surface.md`` § *What does not
        translate*). Copying our mirror would fork the *record* and leave
        both branches pointed at one engine conversation, so the second
        turn on either would append to the same context. That is not a
        fork; it is two transcripts of one session.

        Gated for ``connect_engine``'s reason: a participant choosing
        which conversation the host's engine attaches to would be deciding
        for everyone.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not session_id:
            return {"error": "A session ID is required", "reason": "no_session_id"}
        if fork:
            return {
                "error": (
                    "Forking is not available on this engine: the harness "
                    "owns the conversation store and there is no way to "
                    "copy one. Resume it instead, or start a new session."
                ),
                "reason": "unsupported",
            }
        if self._turns:
            return {"error": "A turn is still running", "reason": "turn_in_progress"}
        if self.session_store is None:
            return {
                "error": "No session history: this run has no repo directory",
                "reason": "no_repo",
            }

        messages = await self.history_load(session_id)
        if isinstance(messages, dict):
            # Browsable but not resumable — deleted, unreadable, or from
            # before the mirror existed. Reported rather than attempted:
            # the harness would refuse the handshake and the user would be
            # looking at an engine that will not start.
            return {
                "error": messages.get("error", "That session cannot be read"),
                "reason": "not_resumable",
            }

        await self._close_session()
        self._resume_request = session_id
        self._auto_resume = True
        outcome = await self.connect_engine(resume=session_id)
        if "error" in outcome:
            self._resume_request = None
            return {
                "error": outcome["error"],
                "reason": outcome.get("reason", "connect_failed"),
            }
        self._resume_request = None
        resumed = self._session.conversation_id if self._session else session_id
        await self._broadcast(
            Event(
                "sessionChanged",
                {
                    "session_id": resumed,
                    "messages": messages,
                    "action": "resumed",
                    "forked_from": None,
                },
                turn_scoped=False,
            )
        )
        return {"session_id": resumed}

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
        """Start a turn. Returns as soon as the engine has accepted it.
        **Localhost only.**

        ``images`` is declined rather than dropped. The SDK accepts image
        input, so this is unbuilt rather than impossible — but a turn that
        silently discarded the screenshot a user attached would answer the
        wrong question convincingly, which is worse than refusing.

        **The turn runs in a background task, and this returning early is
        the whole point of it.** Until 2026-09-03 this method awaited
        ``stream_turn`` to exhaustion, so the browser's JRPC call stayed
        open for the length of the turn — against a client deadline of 75s
        (``webapp/src/app-shell/index.js``). The first live phase-4
        conversation found what that costs: the deadline fired mid-turn,
        ``input.js`` took the transport's contentless *"Timed out waiting
        for response"* down its ``catch`` branch, and
        ``rollbackUnstartedTurn`` erased the tool cards it had already
        drawn — while the turn ran happily on and edited the file three
        minutes later. **The app said the turn failed and then wrote to
        the user's tree**, which is the worst arrangement of those two
        facts available.

        A permission dialog does not merely risk that overrun, it
        guarantees it: the user's thinking time is inside the same budget,
        and ``permissions.py`` deliberately lets a request wait
        indefinitely. So a gated engine cannot hold the RPC open for a
        turn, and the fix is not a longer deadline.

        The shape is the Claude adapter's, deliberately and to the letter
        (``claude_code.service.chat_streaming``): admit, spawn, return
        ``{"status": "started"}``, and let every later fact arrive as a
        server-push event keyed on ``request_id``. The browser already
        expects exactly this — it reads ``error`` / ``routed`` /
        ``unsupported`` from the reply and nothing else — so no webapp
        change goes with it. It also buys the property the Claude
        docstring names: the task's lifetime is independent of any
        WebSocket, so a client that reloads mid-turn re-attaches to a turn
        that kept running rather than losing it.
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

        # Decided here rather than inside the generator. ``stream_turn``
        # raises TurnInProgressError, but it is an async generator, so the
        # raise lands on first iteration — which is now inside the
        # background task, where a synchronous refusal cannot be made out
        # of it. Claude answers the same question in `session.admit`
        # before it spawns, for the same reason.
        if self._turns:
            return {
                "error": (
                    "A turn is already running on this session. Stop it "
                    "before sending another."
                ),
                "reason": "turn_in_progress",
            }

        try:
            session = await self._ensure_session()
        except Exception as exc:  # noqa: BLE001
            return self._record_error("connect", exc)

        translator = StepTranslator(request_id)
        # Registered before the task is spawned, not inside it: `cancel_
        # streaming` and `_note_permission_prompt` both key off `_turns`,
        # and a browser is free to press ⏹ on the reply to this call.
        self._turns[request_id] = translator
        task = asyncio.create_task(
            self._run_turn(session, translator, request_id, message),
            name=f"ag-turn-{request_id}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return {"status": "started"}

    async def _run_turn(
        self,
        session: AntigravitySession,
        translator: StepTranslator,
        request_id: str,
        message: str,
    ) -> None:
        """Drive one turn to completion, dispatching as it goes.

        Returns nothing, and that is the constraint the error path is
        written to: with no RPC reply left to carry a failure, **the event
        stream is the only channel there is**, so every exit from here has
        to leave the browser settled. A path that emits no terminal event
        is a spinner that never stops.

        That was survivable while the caller awaited the turn and could
        answer with a status — barely, since the browser ignored the
        status anyway — and it is not survivable now.
        """
        await self._open_mirrored_turn(request_id, message)
        try:
            async for event in session.stream_turn(message, translator=translator):
                await self._sync_mirror()
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
            # The stream raised part-way, so `stream_turn`'s own closing
            # events never ran. Emitted here from the translator's own
            # state — a half-finished turn still has blocks, tokens and
            # tool calls worth reporting, and `streamComplete` is what
            # ends the spinner and settles the tab.
            for event in translator.stream_complete():
                await self._dispatch(event, request_id)
        finally:
            self._turns.pop(request_id, None)

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

    def _rule_store(self) -> Any:
        """The standing-rule store, without needing a session.

        Read from the gate when there is one so that a rule granted this
        session is listed immediately, and constructed directly otherwise:
        the rules outlive sessions, and a settings panel opened before the
        first turn must still show what the user has granted. The two point
        at the same file.
        """
        from aic_dc.antigravity.rules import RuleStore

        if self._gate is not None:
            return self._gate.rules
        return RuleStore(self._repo_root, config_dir=self._config_dir)

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
        """Everything a freshly connected browser needs to render.

        **The browser's field names are the contract here, not this
        adapter's.** AG-3 mounts both engines under one RPC name, and the
        app shell reads this single snapshot for facts that have nothing
        to do with which engine is master: the repo it is looking at,
        whether startup finished, whether Doc Convert is installed.

        The first draft of this method answered with the fields this
        adapter found interesting — ``blocks``, ``request_id``, ``review``
        — and the shell read none of them. Nothing failed: every key was
        present, every value was true, and the 94 offline tests passed,
        because they asserted this method's own shape rather than the
        reader's. What it cost was the whole engine. ``init_complete`` is
        the key that dismisses the startup overlay, it was not here, and
        the first live run as master sat behind "Connecting…" forever
        (``delivery.md`` § phase 4). A snapshot is only correct against
        the thing that reads it.

        The keys below are therefore ``app-shell/state-fetch.js``'s and
        ``chat-panel/events.js``'s, and the ones this engine cannot feed
        are **absent rather than zeroed**, per AG-9: no ``cost``, no
        ``rate_limit``, no ``session_usage``, no ``compaction``. The
        descriptor hides each of those surfaces and every reader of them
        is guarded, so absence renders as a hidden panel while a zero
        would render as a measurement.

        ``messages`` is the mirrored transcript of the conversation this
        browser is about to be shown — which since phase 5 is the
        conversation the next connect will resume, not necessarily one
        that is running. Those are the same conversation, so showing it
        before the harness is up is not a guess; it is what makes a
        reloaded page look like the one the user left.
        """
        session = self._session
        return {
            # What the app shell reads (app-shell/state-fetch.js).
            "init_complete": True,
            "repo_name": self._repo_root.name,
            "repo_root": str(self._repo_root),
            "review_state": self.review.state(),
            "doc_convert_available": doc_convert_available(),
            # What the chat panel and the dialog read
            # (chat-panel/events.js § onStateLoaded, permission-dialog).
            "messages": await self._current_messages(),
            "active_streams": self._active_streams(),
            "pending_permissions": (
                self._gate.broker.pending() if self._gate is not None else []
            ),
            # This engine's own posture.
            "session_id": session.conversation_id if session else None,
            "connected": bool(session and session.started),
            "streaming": bool(self._turns),
            "model": self._model,
            "permission_mode": self._permission_mode,
            # Two, where Claude reports six. The selector renders what the
            # engine says it accepts, so the four this one refuses stop
            # being offerable — until now the dropdown was Claude's list
            # hardcoded in the webapp, and picking "Accept edits" here got
            # an `unsupported` error. A control that is reachable and
            # refuses is the same defect phase 5 found in the history
            # browser's Fork button, and AG-9's rule is the same: hide it
            # on what the engine reports, never on its name.
            "permission_modes": list(PERMISSION_MODES),
            "read_only": session.read_only if session else True,
            "credentials": self._credentials.report(),
            "denied_read_files": list(self._denied_read_files),
            # No `cost`. AG-6: there is no USD figure anywhere on this
            # engine, and a zero would be a measurement.
        }

    async def _current_messages(self) -> list[dict[str, Any]]:
        """The rendered transcript the browser should be looking at.

        Underscored: ``add_service`` publishes every public method, and a
        browser that wants a particular conversation asks ``history_load``
        for it by id.

        An unreadable transcript renders as an empty conversation rather
        than a failed snapshot. Every other field here is still worth
        painting, and a browser that cannot get a snapshot cannot show the
        user anything at all.
        """
        session_id = await self._visible_session_id()
        if not session_id:
            return []
        loaded = await self.history_load(session_id)
        if isinstance(loaded, dict):
            logger.warning(
                "Could not render conversation %s for the state snapshot: %s",
                session_id,
                loaded.get("error"),
            )
            return []
        return loaded

    # ------------------------------------------------------------------
    # History — seven delegations, no second implementation
    # ------------------------------------------------------------------
    #
    # Every one of these forwards to `claude_code.history` with *this*
    # engine's store. That module is engine-agnostic by construction —
    # `load_session(store, session_id, directory, …)` takes the store as
    # an argument, and `RepoSessionStore(root)` takes its root as one — so
    # phase 5 needed no sibling of it and no `SessionStore` protocol
    # implementation. Importing from `claude_code` here reads oddly and is
    # the same trade AG-3 makes about the class name: those modules are
    # engine-agnostic in everything but their package, and the copy is
    # what would drift.

    async def history_list(
        self, limit: int = 50
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Past conversations on this engine, most recently modified first.

        A bare list on success and ``{"error": …}`` on failure, the union
        the RPC table specifies. An empty list would conflate "no history
        yet" with "could not read it", and only the second is worth a
        user's time.

        No derived index is passed. The index caches a *finished row* keyed
        by transcript mtime and is a performance feature; a cold index is a
        slower listing, never a wrong one, and building a second one for
        this engine before anybody has felt the cost would be a file to
        keep in agreement for no measured gain.
        """
        if self.session_store is None:
            return []

        from aic_dc.claude_code import history

        try:
            return await history.list_sessions(
                self.session_store,
                str(self._repo_root),
                limit=max(0, int(limit)),
            )
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("history_list failed")
            return {"error": f"Could not read the session history: {exc}"}

    async def history_load(
        self, session_id: str
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """One past conversation's messages, rendered for the browser.

        **Read-only, and deliberately not a resume.** This reads a
        transcript; ``resume_session`` is what puts the engine back into
        one. Keeping them apart means browsing history cannot disturb a
        turn that is running.

        No events are interleaved: this engine has no events log, because
        ``EventsLog``'s ``event`` domain is closed on purpose and none of
        its members is a thing this engine reports. A browsed conversation
        therefore carries the model's work and not the operational lines
        around it, which is stated here rather than left to be discovered
        from an empty list.
        """
        if not session_id:
            return {"error": "A session ID is required"}
        if self.session_store is None:
            return {"error": "No session history: this run has no repo directory"}

        from aic_dc.claude_code import history

        try:
            messages = await history.load_session(
                self.session_store, session_id, str(self._repo_root)
            )
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("history_load failed for %s", session_id)
            return {"error": f"Could not read session {session_id}: {exc}"}

        if not messages:
            # An empty transcript is never stored, so nothing to render
            # means the session is gone or unparseable — not that it
            # happened and said nothing.
            return {"error": f"Session {session_id} has no readable transcript"}
        return messages

    async def history_search(
        self, query: str, role: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Substring search across every stored conversation, newest first."""
        if not query:
            # Not an error: an empty search box is a user who has not
            # searched yet, and an error toast for typing nothing is noise.
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
                role=role or None,
                limit=max(1, int(limit)) if limit else SEARCH_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("history_search failed")
            return {"error": f"Could not search the session history: {exc}"}

    async def history_delete(self, session_id: str) -> dict[str, Any]:
        """Delete one past conversation's transcript. **Localhost only.**

        Gated because this destroys history every client can see, and
        because a participant who could delete the record of a turn could
        delete the evidence of what they were invited to review.

        The conversation on screen is refused rather than deleted: the
        mirror is *live*, so the transcript would come straight back and
        the next connect would resume an id with nothing behind it.
        Starting a new session first makes it deletable, which is one
        click and is honest about what is happening.

        Missing is not an error. A row deleted twice, or deleted by
        another client first, is a browser that already has what it asked
        for.
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
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("history_delete failed for %s", session_id)
            return {"error": f"Could not delete session {session_id}: {exc}"}

        await self._broadcast(
            Event("sessionDeleted", {"session_id": session_id}, turn_scoped=False)
        )
        return {"session_id": session_id, "status": "deleted"}

    async def history_image(
        self, session_id: str, entry_uuid: str, block: int
    ) -> dict[str, Any]:
        """One image out of a past prompt, as a data URI.

        Mounted although this engine declines image *input* today
        (``chat_streaming`` refuses rather than dropping them), because
        the surface is the transcript's rather than the turn's: a
        conversation mirrored by a later build that does accept images
        must be readable by this one, and a reader that 404s on it would
        be a hole that only appears once the feature lands.
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
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("history_image failed for %s", session_id)
            return {"error": f"Could not read that image: {exc}"}
        return {"data_uri": data_uri}

    async def get_session_storage(self) -> dict[str, Any]:
        """How much disk this engine's mirror is using.

        Measured over ``MIRROR_DIR`` and not over the whole of
        ``.aic-dc/``, because that is the directory the answer argues for
        deleting from — a figure that included the other engine's
        transcripts would invite a user to delete rows that would not
        shrink it.

        The threshold comes from ``app.json`` through the same key the
        Claude adapter reads, so the number a user edited applies to both
        engines rather than to whichever one they were on when they
        edited it.
        """
        if self.session_store is None:
            return {
                "error": "No session history: this run has no repo directory",
                "reason": "no_repo",
            }
        from aic_dc.claude_code.session_store import DISK_WARNING_BYTES

        try:
            loop = asyncio.get_running_loop()
            total = await loop.run_in_executor(None, self.session_store.total_bytes)
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("Could not measure the session directory")
            return {"error": f"Could not measure the session directory: {exc}"}
        history_config = getattr(self._config, "history_config", None)
        section = history_config if isinstance(history_config, dict) else {}
        threshold = section.get("session_dir_warning_bytes", DISK_WARNING_BYTES)
        return {"bytes": int(total), "over_warning": int(total) >= int(threshold)}

    def _active_streams(self) -> list[dict[str, Any]]:
        """The turn in flight, in the replay shape a reconnect resumes from.

        Same keys as ``ActiveTurn.to_dict()`` on the Claude side, because
        ``resumeStreamBlocks`` reads them by name: a ``request_id`` it can
        route by, the ``blocks`` rendered so far, a ``started_at`` in epoch
        seconds so the elapsed counter does not restart from the reconnect,
        and the token counters, which are a whole assistant message away
        from their next push.

        No ``subagents`` key. This engine's subagent trajectories do not
        yet produce their own tabs outside a consultation (AG-13), and
        ``rehydrateSubagentTabs`` treats a missing list as none — which is
        the fact — rather than as an empty one it has to explain.
        """
        return [
            {
                "request_id": request_id,
                "session_id": (
                    self._session.conversation_id if self._session else None
                ),
                "started_at": translator.started_at,
                "blocks": translator.rendered_blocks(),
                "usage": {"turn_model_usage": translator.turn_usage()},
            }
            for request_id, translator in self._turns.items()
        ]

    async def get_model(self) -> dict[str, Any]:
        """The model in force, and the one-entry menu it makes.

        The SDK has no "list what this key may use" call, so the only name
        this side can vouch for is the one it is configured with. That is a
        menu of one rather than nothing: the user cannot choose here, but
        they are entitled to read what is answering.

        ``models`` entries are **objects**, matching the Claude adapter and
        the browser's ``modelEntries``, which drops anything that is not
        one. A bare string here rendered as an empty picker rather than as
        a single row — the same defect the `agy` transport shipped with.
        """
        if not self._model:
            return {"model": self._model, "models": []}
        return {"model": self._model, "models": [{"value": self._model}]}

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

    async def _open_mirrored_turn(self, request_id: str, message: str) -> None:
        """Record the prompt that opens this turn.

        Called by both transports' turn runners rather than by
        ``chat_streaming``, because the two adapters implement that method
        separately and the runner is the one place both funnel through
        before the first event.

        The prompt may land *after* this returns: on the SDK transport the
        conversation has no id yet, so the mirror holds the text and files
        it the moment :meth:`_sync_mirror` learns one. Ordering is
        preserved either way — the mirror writes the held prompt before
        anything the turn produced.
        """
        if self._mirror is None:
            return
        await self._sync_mirror()
        await self._mirror.note_prompt(message, request_id=request_id)

    async def _note_disk_writes(self, event: Event) -> None:
        """Tell every browser the tree changed, when a call wrote to it.

        The file picker reloads on a ``filesModified`` push and on nothing
        else. On the Claude engine that push comes from a ``PostToolUse``
        hook (``claude_code/hooks.py``); this engine has no such hook, and
        for a while it had no push either — so an approved write landed on
        disk and the tree went on showing the repository as it was before.
        Reported by a user watching the picker not move after a write they
        had just allowed.

        Derived from the event rather than from a second walk of the
        filesystem: a completed tool result already names the files it
        wrote, from the same shared table
        (:func:`~aic_dc.claude_code.messages.files_written_by`) the turn
        footer and the browsed transcript use. One source, three readers.

        **Session-wide, never turn-scoped.** The tree is the same tree for
        every watching browser, including ones that did not send this
        turn — the same reasoning the Claude hook records.

        What this deliberately does *not* do is re-index the symbol table.
        The Claude hook does both, and the second half needs a
        ``Reindexer`` this engine has never had; a stale symbol index
        degrades autocomplete, where a stale tree hides the agent's work.
        Named here so the gap is a known one rather than an oversight.
        """
        if event.name != "toolResult" or self._event_callback is None:
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        paths = payload.get("files_modified")
        if not isinstance(paths, list) or not paths:
            return
        await self._dispatch(
            Event("filesModified", list(paths), turn_scoped=False), None
        )

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

        **The mirror observes here**, because this is the one point every
        event of both transports passes through — ``AgyService`` inherits
        this method and overrides only what produces the events. Observing
        in the two turn runners instead would be two call sites for one
        job, which is how one of them comes to be forgotten.
        """
        if self._mirror is not None:
            await self._mirror.observe(event)
        await self._note_disk_writes(event)
        if self._event_callback is None:
            return
        args: tuple[Any, ...] = (
            (request_id, event.payload) if event.turn_scoped else (event.payload,)
        )
        try:
            await self._event_callback(event.name, *args)
        except Exception:  # noqa: BLE001 - a dead client is not a turn failure
            logger.exception("Dropping %s: the event callback failed", event.name)
