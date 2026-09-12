"""The ``agy`` transport behind the shared RPC surface.

[AG-14](../../../specs5/plan-ag/decisions.md#ag-14) makes this a second
*transport* for the same engine rather than a third engine, and the class
says so by inheriting: :class:`~aic_dc.antigravity.service.AntigravityService`
already implements the two-thirds of the surface that is repository, index
and review work, and none of that is transport-specific. What is overridden
is exactly the part that differs — how a session starts, how a turn is
pumped, and what stop does.

Inheriting rather than composing, for once
==========================================
Everywhere else in this project a shared concern is *delegated* — the
Antigravity adapter holds a real ``ReviewMode`` and calls ``commit.py``
rather than subclassing the Claude adapter. That is right there, because
those are two different engines and a shared base class would invite each
to reach into the other's lifecycle.

Here it is one engine reached two ways. `agy` and the SDK talk to the same
product, share a tool-argument vocabulary, and drive the *same*
``PermissionBroker`` through the *same* dialog. A parallel class would
duplicate 31 method bodies whose only content is ``return
self._repo.something()``, and the copy is what drifts — which is the
argument this file would otherwise be making against itself.

What actually differs
=====================
- **No Gemini key.** `agy` authenticates from the OS keyring against the
  owner's Google account and reaches the Code Assist backend, which is the
  whole reason this transport exists (AG-14). ``AG-R-8``'s credential wall
  is the *SDK's*, and does not apply.
- **The gate is a socket, not a hook object.** The SDK passes a
  ``PreToolCallDecideHook`` into its config; here `agy` runs a separate
  process that connects back to :class:`AgyGateServer`. The
  ``AntigravityPermissionGate`` underneath is the same one, which is why
  ``resolve_permission`` is inherited unchanged.
- **Stop starves rather than halts.** There is no halt frame in the
  stream-json protocol, so ⏹ refuses subsequent tool calls. See
  :mod:`aic_dc.agy.session`.
- **The hook must be installed** in the user's global `agy` configuration
  before a turn can be gated, and this class refuses to start a session
  without it rather than running one ungated. See :mod:`aic_dc.agy.install`.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5, AG-3.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from aic_dc.agy import install, roots
from aic_dc.agy import tools as agy_tools
from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.agy.session import AgyNotInstalledError, AgySession
from aic_dc.agy.steps import AgyTranslator
from aic_dc.antigravity.permissions import AntigravityPermissionGate
from aic_dc.antigravity.service import AntigravityService
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)


class AgyService(AntigravityService):
    """Antigravity, driven through the CLI on the owner's own subscription."""

    #: This transport's own transcript mirror (AG-1).
    #:
    #: Separate from the SDK transport's although both reach the same
    #: product, because a conversation id is only meaningful to the
    #: harness that minted it: `agy` keeps its conversations in the CLI's
    #: store and `localharness` keeps its trajectories in the SDK's, and
    #: neither can resume the other's. Sharing a root would put every
    #: conversation in one list and let the user pick one this transport
    #: would then fail — or worse, silently open a new one under an id
    #: that already had a transcript.
    MIRROR_DIR = "agy-sessions"

    def __init__(
        self,
        *args: Any,
        executable: str = "agy",
        model: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._executable = executable
        self._agy_gate: AgyGateServer | None = None
        #: `agy models`, read once. None until asked.
        self._models: list[dict[str, Any]] | None = None
        # **Not the SDK's default.** The two Antigravity surfaces do not
        # agree on model names: the SDK takes `gemini-3.7-flash` plus a
        # separate `ThinkingLevel`, while `agy` bakes the effort into the
        # name and rejects the bare form —
        # ``--model gemini-3.7-flash requires --effort (available: low,
        # medium, high)``. Inheriting the SDK's default therefore made
        # every session exit before its init frame, surfacing as
        # "Error: engine" with the real message discarded.
        #
        # `sdk-surface.md` § *What `agy` models returns* recorded this
        # disagreement on 2026-08-30 and the code did it anyway, which is
        # the argument for the assertion in `test_agy_service.py` rather
        # than another paragraph.
        #
        # None means "agy's own default", which is the only value this
        # side can be sure it accepts.
        #
        # An explicit argument wins; otherwise a model the user chose in a
        # previous session is read back from `engines.agy_model`, which is
        # the half `set_model` was missing until 2026-09-09.
        self._model = model or getattr(self._config, "agy_model", None)
        # AG-22's other half. The listener is the server this app runs so
        # that `agy` can consult Claude; the token and the spawn id are how
        # one `agy` child is told apart from the next on it. All three are
        # None between spawns, which is also what "no consultant is
        # reachable" looks like.
        self._listener: Any = None
        self._consult_token: str | None = None
        self._consult_spawn_id: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # `_config_dir` was defined here as a property until AG-15 gave the SDK
    # transport a rule store that needs the same value. It is now set once
    # in `AntigravityService.__init__` and inherited — which it had to be,
    # not merely ought to be: a property on this subclass shadows the base
    # class's instance attribute, so the base's assignment would have raised
    # `AttributeError: property has no setter` on every `AgyService`.

    def gate_status(self) -> dict[str, Any]:
        """Whether the gate is installed in **this app's** `agy` root.

        Public because it is the settings surface's whole question, and
        because a user is entitled to ask it without starting a session.

        **The file it reads moved with AG-21.** It used to be
        ``~/.gemini/config/hooks.json`` — the user's own, shared with every
        `agy` they run by hand. It is now the master root this app owns, so
        the answer is about our sessions and only ours.
        """
        report = install.status(
            self._config_dir,
            path=roots.hooks_file(roots.master_root(self._config_dir)),
        )
        report["agy_present"] = shutil.which(self._executable) is not None
        return report

    def _ensure_gate(self) -> tuple[Path, dict[str, Any]]:
        """Provision the master root and put the gate inside it.

        Returns the root and the install report.

        **This is not asked about, and that is the point of AG-21.**
        Installing a hook into the user's own configuration was a decision
        with consequences outside this app — every `agy` they ran by hand
        went through our socket — so it had a button and no default. A hook
        inside a directory this app created, for a process this app spawns,
        is not a decision at all. Nothing outside ``<config_dir>/agy-roots``
        is written.

        **And the old one comes out here**, because "the user's own `agy`
        is untouched" is false on an upgraded machine until it does, and
        waiting for somebody to find the Settings button would mean the
        claim is true of new installs and quietly false of everyone else.

        Raises ``RuntimeError`` if the command will not run: `install`
        probes before it writes, and a gate that cannot be installed must
        stop the session rather than be skipped, because `agy` is spawned
        with ``--dangerously-skip-permissions`` and the hook is the only
        thing between the model and the tree.
        """
        config_root = roots.prepare(
            roots.master_root(self._config_dir), self._config_dir
        )
        if install.retire_global():
            logger.info(
                "Removed the pre-AG-21 gate from %s: this root replaces it",
                install.GLOBAL_HOOKS,
            )
        report = install.install(
            self._config_dir, path=roots.hooks_file(config_root)
        )
        report["agy_present"] = shutil.which(self._executable) is not None
        return config_root, report

    async def _list_models(self) -> list[dict[str, Any]]:
        """The models ``agy`` will accept, from ``agy models``.

        Cached for the life of the adapter. It is a subprocess and the
        answer is a property of the account rather than of the session, so
        running it per request would spend ~1s of the user's time to
        re-learn something that has not changed.

        **Entries are objects, because that is ``get_model``'s contract on
        every engine.** This previously returned bare id strings on the
        stated grounds that "the shape is a list of names", and it is not:
        the Claude adapter returns the CLI's own ``{value, displayName,
        resolvedModel, description}`` dicts, and the browser's
        ``modelEntries`` skips anything that is not an object. Fourteen
        names therefore arrived in the browser and rendered as *nothing* —
        an empty, disabled select under the note that says the engine has
        not connected yet, which is the one sentence guaranteed to send a
        reader looking at the transport instead of at the shape. Returning
        objects is the smaller change and the honest one, and it lets the
        display labels `agy models` already prints be kept rather than
        thrown away.

        An empty list on any failure, and every caller treats that as
        "unknown" rather than "none": a model picker that went blank
        because a subprocess timed out would look exactly like this
        transport having no models, which is the confusion the whole
        surface exists to avoid.
        """
        if self._models is not None:
            return self._models
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:  # noqa: BLE001 - a picker must not break a session
            logger.warning("Could not read the model list from %s", self._executable)
            return []
        names: list[dict[str, Any]] = []
        for line in out.decode("utf-8", "replace").splitlines():
            # `id<TAB>Display Name`. The id is what --model takes and is
            # the `value`; the label is what the user reads. Keeping it
            # does not make the picker engine-aware (AG-R-4) — it is the
            # same `displayName` key the Claude CLI's handshake fills in,
            # so the browser renders both engines through one code path.
            value, _, label = line.partition("\t")
            value = value.strip()
            if not value or value.startswith("#"):
                continue
            names.append({"value": value, "displayName": label.strip() or value})
        self._models = names
        return names

    async def get_model(self) -> dict[str, Any]:
        """The current model and the ones this account can use.

        ``None`` for the current model is honest rather than a gap: no
        ``--model`` is passed unless the user picks one, so ``agy`` is
        using its own default and this side does not know its name. The
        ``init`` frame carried a ``model`` field at 1.1.22 and does not at
        1.1.25, so there is nowhere to read it from.
        """
        return {"model": self._model, "models": await self._list_models()}

    async def set_model(self, model: str | None = None) -> dict[str, Any]:
        """Choose a model. **Localhost only.**

        **Validated against ``agy models``, and that is the point.** An
        unrecognised name does not fail at selection — it fails when the
        next session starts, as `agy` exiting before its init frame, which
        surfaced as a bare "Error: engine" and cost a day to diagnose. The
        SDK's own default is exactly such a name, so this is not a
        hypothetical class of mistake.

        Takes effect on the next session, matching the SDK transport:
        restarting mid-conversation would drop the context the user is
        talking to.

        **Persisted since 2026-09-09.** This assigned ``self._model`` and
        nothing else, so a model chosen in the picker was silently
        forgotten at the next server start and the account's own default
        came back — a control that looks like it worked, for one session.
        It now writes ``engines.agy_model`` to ``app.json`` as well, and a
        write failure costs the persistence rather than the selection: the
        session the user is configuring still gets the model they asked
        for, and the log says it will not survive a restart.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not model:
            return {"model": self._model}
        known = await self._list_models()
        if known and model not in {entry["value"] for entry in known}:
            return {
                "error": "unknown_model",
                "message": (
                    f"{model!r} is not a model this Antigravity account "
                    f"offers. `agy` rejects an unknown name by exiting "
                    f"before the session starts, so it is refused here "
                    f"instead."
                ),
                "models": known,
            }
        self._model = model
        persisted = True
        try:
            self._config.set_engine_option("agy_model", model)
        except Exception:  # noqa: BLE001 - a config write must not lose a selection
            logger.warning(
                "Chose %s for this session, but could not write it to "
                "app.json; it will not survive a restart.",
                model,
            )
            persisted = False
        return {"model": self._model, "persisted": persisted}

    async def connect_engine(self, resume: str | None = None) -> dict[str, Any]:
        """Start ``agy``. **Localhost only.**

        ``resume`` becomes ``agy --conversation <id>``; absent, this
        continues whatever conversation the mirror says was last active,
        which is what makes a server restart invisible (phase 5). `agy`
        restores the context from its own store — nothing here replays our
        transcript into a prompt.

        **A session will not start without the gate installed.** Running
        one anyway would mean `agy` executing with
        ``--dangerously-skip-permissions`` and nothing intercepting it,
        which is not a degraded experience but an ungated agent editing the
        user's tree. AG-5 makes the dialog a requirement of this engine.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if shutil.which(self._executable) is None:
            return {
                "error": "not_installed",
                "message": (
                    f"{self._executable!r} is not on PATH, so the agy "
                    "transport cannot run. Install the Antigravity CLI, or "
                    "use the SDK transport with a Gemini API key."
                ),
            }
        try:
            _, gate = self._ensure_gate()
        except RuntimeError as exc:
            gate = self.gate_status()
            gate["detail"] = str(exc)
        if gate["state"] != "current":
            return {
                "error": "gate_not_installed",
                "reason": gate["state"],
                "message": (
                    "The AIC-DC permission gate could not be installed in "
                    f"{gate['path']}, so a turn could not be reviewed before "
                    "it wrote to your files, and the session was not started."
                    + (f" {gate['detail']}" if gate.get("detail") else "")
                ),
                "gate": gate,
            }
        try:
            await self._ensure_session(resume=resume)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return self._record_error("connect", exc)
        return {"status": "connected", "model": self._model}

    async def _ensure_session(self, resume: str | None = None) -> Any:
        if self._session is not None and self._session.started:
            return self._session
        target = await self._resume_target(resume)

        # The same gate object the SDK transport builds, so the dialog, the
        # queue and `resolve_permission` are literally shared. Only what
        # carries a call *to* it differs.
        self._gate = AntigravityPermissionGate(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            localhost_available=self._localhost_available,
            denied_reads=self.get_denied_read_files,
            # The session's posture, read live: `acceptEdits` lets an
            # in-repo file write through without a dialog, and the
            # user flips it from the action bar mid-session.
            permission_mode=lambda: self._permission_mode,
            # AG-15's standing rules, in the app's own configuration
            # directory rather than the default one. Omitted here until
            # 2026-09-12, which was a real fault and not only an
            # inconsistency with the SDK transport: a grant the user made
            # on this transport went to ``~/.config/aic-dc`` while
            # everything else this service owns went to ``config_dir``, so
            # a host handed a directory kept its permission state
            # somewhere else entirely. It surfaced as a probe that hung
            # for eleven minutes on a dialog nobody could answer, because
            # the rule it had been given was seeded in the directory the
            # gate was not reading.
            config_dir=self._config_dir,
        )
        self._agy_gate = AgyGateServer(
            self._config_dir / "agy-sessions" / "gate.sock",
            gate=self._gate,
            config_dir=self._config_dir,
        )
        # **The master's own config root** (AG-21), stable so that resume
        # keeps working across restarts, and private so that the hook this
        # app installs is not in the user's file. The hook goes in with it:
        # the gate is the only thing between the model and the tree on this
        # transport, because `agy` runs with `--dangerously-skip-permissions`.
        config_root, report = self._ensure_gate()
        if report.get("state") != "current":
            await self._agy_gate.stop()
            raise RuntimeError(
                "The agy permission gate could not be installed into this "
                "session's config root, so the session would run ungated: "
                + str(report.get("detail") or report.get("state"))
            )
        # **Before `exec`, because `agy` reads `mcp_config.json` during
        # startup** — a file that appears afterwards is a file it never
        # sees, and the failure is silent: the tool is simply absent and
        # the model explains that it has no way to ask anyone.
        await self._offer_consultant(config_root)
        session = AgySession(
            self._repo_root,
            gate=self._agy_gate,
            model=self._model,
            executable=self._executable,
            resume=target,
            config_root=config_root,
        )
        try:
            await session.start()
        except AgyNotInstalledError:
            await self._agy_gate.stop()
            raise
        except Exception:
            # Anything else — including a resume `agy` answered with a
            # different conversation — leaves a gate socket listening for a
            # session that never started. Reaped here rather than at the
            # next connect, which would find the socket in use.
            await self._agy_gate.stop()
            raise
        self._session = session
        await self._sync_mirror()
        return session

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        self._agy_gate = None
        self._gate = None
        # First, so that a consultation still running for a child that is
        # about to be buried is cancelled rather than left spending the
        # subscription on an answer nobody will read. Unconditional,
        # because a session that failed to start can still have had a
        # token minted for it.
        await self._retire_consultant()
        if session is None:
            return
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("The agy session did not close cleanly")

    # ------------------------------------------------------------------
    # The consultant `agy` can reach (AG-22)
    # ------------------------------------------------------------------

    async def _offer_consultant(self, config_root: Path) -> None:
        """Start the listener, mint this spawn a bearer, write the config.

        **This is what closes AG-1's asymmetry.** `agy` could already be
        consulted by Claude; until this ran, Claude could not be consulted
        by `agy`, because the listener existed and was reachable by
        nothing.

        The token is per spawn and lives in a file only this app writes,
        under a root only this app owns. Three things about that file are
        deliberate and stated where they are enforced, in
        :func:`aic_dc.agy.roots.write_mcp_config`: atomic, ``0600`` inside
        a ``0700`` directory, and rewritten unconditionally rather than
        blanked on exit.

        **A failure here does not fail the session.** Every path leaves no
        config file rather than a stale one, and `agy` then runs with no
        consultant — which is what it did before this existed. The
        alternative is refusing to start an engine because an optional
        second opinion could not be offered.

        What this does *not* do is make the bearer a security boundary.
        Any code already running as the user can read that file and call
        the listener as the master, including by omitting the ``_meta``
        key the subagent refusal reads — that check is a policy against
        `agy`'s own dispatcher, not a boundary against local code. The same
        code already holds the user's Claude credentials, so the bearer
        grants strictly less than what an attacker in that position has;
        see AG-R-26.
        """
        # Retired first, so that one spawn is one listener holding one
        # token. A second mint onto a live listener would be legal — it
        # holds many — but nothing here wants two, and a token whose child
        # is gone is a token nothing will ever revoke.
        await self._retire_consultant()

        from aic_dc.claude_code.consult_listener import ConsultationListener
        from aic_dc.claude_code.consultant import ClaudeConsultant

        if not ClaudeConsultant(self._config_dir).available():
            # Not a warning: an install with no Claude CLI is a supported
            # install, and `agy` is the transport that survives one. Said
            # once per spawn rather than silently, because "the tool is
            # missing" is otherwise indistinguishable from "the tool is
            # broken" when the model reports it.
            logger.info(
                "No Claude second opinion is offered to agy: this install "
                "has no Claude CLI or SDK to run one"
            )
            roots.clear_mcp_config(config_root)
            return

        spawn_id = uuid.uuid4().hex
        try:
            listener = ConsultationListener(lambda: ClaudeConsultant(self._config_dir))
            await listener.start()
            token = listener.mint(repo_root=self._repo_root, session_id=spawn_id)
            roots.write_mcp_config(config_root, listener.config_entry(token))
        except Exception as exc:  # noqa: BLE001 - an optional tool, not the session
            logger.warning(
                "The Claude consultation listener could not be offered to "
                "this agy session, which will run without it: %s",
                exc,
            )
            with contextlib.suppress(Exception):
                roots.clear_mcp_config(config_root)
            self._listener = None
            with contextlib.suppress(Exception):
                await listener.aclose()
            return
        self._listener = listener
        self._consult_token = token
        self._consult_spawn_id = spawn_id
        logger.info(
            "agy may consult Claude on 127.0.0.1:%s for this session",
            listener.port,
        )

    async def _retire_consultant(self) -> None:
        """Revoke this spawn's bearer and stop listening. Never raises.

        Bound to the subprocess and to nothing else: the token dies with
        the child that was given it, and anything still in flight is
        cancelled rather than left to finish into a session that has gone.

        The config file goes too. Nothing depends on that — the token it
        names has just been revoked and buys nothing — but a file shaped
        like a credential outliving the thing it authenticated is an
        invitation to reason about it later as though it still worked.
        Tidiness on the clean path only; the *correctness* is the
        unconditional overwrite at the next start, which a crash cannot
        skip.
        """
        listener, self._listener = self._listener, None
        token, self._consult_token = self._consult_token, None
        self._consult_spawn_id = None
        if listener is None:
            return
        with contextlib.suppress(Exception):
            roots.clear_mcp_config(roots.master_root(self._config_dir))
        try:
            if token:
                await listener.revoke(token)
            await listener.aclose()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("The consultation listener did not close cleanly")

    # ------------------------------------------------------------------
    # A turn
    # ------------------------------------------------------------------

    async def chat_streaming(
        self,
        request_id: str,
        message: str,
        images: list[str] | None = None,
        viewer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a turn. Returns as soon as ``agy`` has accepted it.

        The same contract as every other adapter here, and for the reason
        the SDK transport learned on 2026-09-03: a reply that waits for the
        turn is a reply the browser's 75s deadline kills, after which the
        agent keeps working and the transcript says it failed.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if images:
            return {
                "error": "unsupported",
                "message": (
                    "Image input is not wired for the agy transport yet. The "
                    "turn was not sent, rather than sent without the images."
                ),
            }
        if viewer:
            self._viewer = dict(viewer)
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

        # The conversation id is read here rather than inside the pump
        # because `init` is consumed by the session, before a translator
        # for this turn exists. It is what names the directory `agy` puts
        # a generated image in, and the repo root is where that image has
        # to end up — neither is anywhere in the turn's own frames.
        translator = AgyTranslator(
            request_id,
            repo_root=self._repo_root,
            conversation_id=session.conversation_id,
            # AG-R-18: the brain and scratch directories are properties of
            # the root this session's `agy` was spawned against, not of
            # this server's own `HOME`.
            config_root=roots.master_root(self._config_dir),
        )
        self._turns[request_id] = translator
        import asyncio

        # Framed here rather than in `_run_agy_turn`, so the *framed* text
        # is what the mirror records: `agy_tools.WRITE_GUIDANCE` explains
        # why the guidance is needed at all, and the transcript's job is to
        # say what the model was actually sent. `history.strip_framing`
        # removes the block again at read time, so the user sees what they
        # typed.
        task = asyncio.create_task(
            self._run_agy_turn(
                session,
                translator,
                request_id,
                agy_tools.WRITE_GUIDANCE + message,
            ),
            name=f"agy-turn-{request_id}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return {"status": "started"}

    async def _run_agy_turn(
        self,
        session: AgySession,
        translator: AgyTranslator,
        request_id: str,
        message: str,
    ) -> None:
        """Drive one turn, dispatching as it goes.

        ``stream_turn`` closes the turn out itself on every path, so the
        failure branch here only has to report *why* — never to invent a
        terminal event, which would emit two.
        """
        await self._open_mirrored_turn(request_id, message)
        # **Unconditional, and named after this turn.** The consultation
        # budget is per turn and nothing on an incoming MCP request carries
        # a turn id, so this push is the only place the identity exists.
        # Opening it here rather than at the first consultation means a
        # turn always starts with a full budget, including one that opened
        # while the previous turn's `finally` had not yet run.
        if self._listener is not None and self._consult_token:
            self._listener.begin_turn(self._consult_token, request_id)
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
            for event in translator.stream_complete():
                await self._dispatch(event, request_id)
        finally:
            self._turns.pop(request_id, None)
            # In a `finally` because between turns is the closed state: a
            # consultation that arrives after the host considers the turn
            # over would otherwise spend the *next* turn's budget before it
            # opens. Named, so that a slow turn one closing after turn two
            # has opened closes its own turn and not the live one.
            if self._listener is not None and self._consult_token:
                self._listener.end_turn(self._consult_token, request_id)

    # ------------------------------------------------------------------
    # Subagent transcripts
    # ------------------------------------------------------------------
    #
    # The ``subagent_transcripts`` surface, which reads ``agy``'s own
    # conversation store rather than our mirror — see
    # :mod:`aic_dc.agy.subagents` for why there is nothing in the mirror to
    # read. Both methods are **synchronous file work on the executor**, like
    # every other history read here: a listing is one file per subagent and
    # a transcript is one file, but they are on the user's disk and the event
    # loop is serving a live turn.

    async def list_subagent_transcripts(
        self, session_id: str | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """The subagents a conversation delegated to, one row per tab.

        Defaults to the session on screen, matching the Claude adapter, so
        the common call needs no argument and cannot name a session other
        than the one being read.

        A bare list on success and ``{"error": …}`` on failure, which is
        the union the RPC table specifies and the distinction the history
        browser draws differently: a session that delegated nothing and a
        listing that could not be read want opposite reactions from the
        user.

        **A session we do not own answers an empty list, not an error.**
        The id would have to come from somewhere other than our own
        session list to get here, and the honest answer to "what did that
        conversation delegate?" from this service is that it has no such
        conversation — see :meth:`_mirrors_session`.
        """
        target = session_id or await self._visible_session_id()
        if not target:
            return []
        if not await self._mirrors_session(target):
            logger.warning(
                "Refused a subagent listing for %s, which is not a session "
                "this repository mirrors",
                target,
            )
            return []

        from aic_dc.agy import subagents

        try:
            loop = asyncio.get_running_loop()
            brain = roots.brain_dir(roots.master_root(self._config_dir))
            return await loop.run_in_executor(
                None, functools.partial(subagents.rows, target, brain_dir=brain)
            )
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("list_subagent_transcripts failed for %s", target)
            return {"error": f"Could not read the subagent transcripts: {exc}"}

    async def get_subagent_transcript(
        self, agent_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """One subagent's conversation, rendered like any other.

        Rendered messages rather than raw records, for the reason the
        Claude adapter states: a subagent tab draws through the same panel
        code as the main transcript.

        **The id is checked against what this session announced**, and
        that check is the reason this method is not a hole. ``agent_id`` is
        a conversation id in ``agy``'s own store, so an unchecked read here
        would be "open any Antigravity conversation on this machine by id"
        — including the ones the user had with the IDE, which AIC⚡DC never
        owned and has no business showing. Two gates, because either alone
        leaks: the session has to be one we mirror, and the agent has to be
        reachable by announcement from it (AG-R-12's family).

        A refusal reads as an unreadable transcript rather than as a
        permission error, and deliberately: the browser renders the reason
        inside the tab, and "not this session's subagent" is a sentence
        about the request, which is what happened.
        """
        if not agent_id:
            return {"error": "An agent ID is required"}
        target = session_id or await self._visible_session_id()
        if not target:
            return {"error": "No session to read subagents from"}
        if not await self._mirrors_session(target):
            return {"error": f"{target} is not a session in this repository"}

        from aic_dc.agy import subagents

        try:
            loop = asyncio.get_running_loop()
            brain = roots.brain_dir(roots.master_root(self._config_dir))
            owned = await loop.run_in_executor(
                None, functools.partial(subagents.descendants, target, brain_dir=brain)
            )
            if agent_id not in owned:
                logger.warning(
                    "Refused subagent %s: not announced by session %s",
                    agent_id,
                    target,
                )
                return {
                    "error": (
                        f"{agent_id} is not a subagent of this conversation, "
                        f"so there is no transcript here to read."
                    )
                }
            messages = await loop.run_in_executor(
                None, functools.partial(subagents.load, agent_id, brain_dir=brain)
            )
        except Exception as exc:  # noqa: BLE001 - answered, not raised
            logger.exception("get_subagent_transcript failed for %s", agent_id)
            return {"error": f"Could not read subagent {agent_id}: {exc}"}

        if not messages:
            # A subagent that ran wrote records, so nothing to render means
            # the conversation was pruned out of `agy`'s store — worth
            # saying rather than drawing as an empty conversation. Same
            # reasoning as `history_load`'s.
            return {"error": f"Subagent {agent_id} has no readable transcript"}
        return messages

    async def _mirrors_session(self, session_id: str) -> bool:
        """Whether this repository's mirror holds ``session_id``.

        The ownership half of the containment check. ``agy``'s store holds
        every conversation the user has ever had with this CLI, in this
        repository or anywhere else; our mirror holds the ones AIC⚡DC ran
        here. Only the second set is this service's to hand out.

        The store's own ``list_sessions`` rather than
        ``history.list_sessions``: this needs the ids, and that one parses
        every transcript to count its messages, which is a session-list
        page's worth of work to answer a yes/no.
        """
        if self.session_store is None:
            return False
        from claude_agent_sdk import project_key_for_directory

        try:
            rows = await self.session_store.list_sessions(
                project_key_for_directory(str(self._repo_root))
            )
        except Exception:  # noqa: BLE001 - a failed check refuses, never allows
            logger.exception("Could not list mirrored sessions to check ownership")
            return False
        return any(
            isinstance(row, dict) and row.get("session_id") == session_id
            for row in rows
        )

    #: What a stopped subagent is told, and it is written to be read by a
    #: model rather than logged: the second sentence is AG-R-11's, because
    #: an agent refused one way has been measured reaching for another.
    STOP_SUBAGENT_REASON = (
        "The user stopped this subagent in AIC-DC. Stop what you are doing, "
        "do not continue, and do not try another way of making this change."
    )

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        """⏹ **one subagent**, leaving the rest of the turn running.

        There is no halt frame on this transport, so this is the same
        starvation ``cancel_streaming`` performs, aimed at one
        conversation: the gate refuses every later call from it with a
        reason the model reads, and the parent turn is untouched. The id is
        the subagent's own ``agy`` conversation, which is what the row
        carries and what the gate already claims as the announcement
        arrives.

        **``stopping``, never ``stopped``.** The row keeps rendering as
        live until the *stream* reports the subagent terminal, and that is
        the contract ``chat-panel/index.js::_stopSubagent`` is written
        against: a row that greyed out on the request would claim a
        subagent had stopped while its tools were still running. What this
        returns is a fact about the gate — the refusal is recorded — and
        nothing about the agent.

        **The id is checked against what this turn announced.** An
        unchecked one would let a caller aim a refusal at any conversation
        on this machine, which is the containment
        :mod:`aic_dc.agy.subagents` states for the reading half of the
        same id.

        Three limits, stated in ``capabilities.py`` before this was built
        and unchanged by building it: a subagent producing only prose never
        asks for a tool and runs to its end; a subagent's own subagent has
        an id of its own that this does not match; and what ``agy`` reports
        for a starved subagent decides the row's LED, which is the stream's
        answer rather than this method's.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if not task_id:
            return {"error": "A task ID is required"}
        if self._agy_gate is None:
            return {
                "error": "This session has no permission gate, so there is "
                "nothing to refuse a subagent's calls through."
            }
        stopped_on = [
            translator
            for translator in self._turns.values()
            if task_id in translator.subagents
        ]
        if not stopped_on:
            # Not an error: a subagent whose turn has ended is a stale
            # button rather than a bad request, and the browser draws Stop
            # only while a row is live.
            logger.info(
                "stop_task for %s, which no live turn announced", task_id
            )
            return {"status": "not_running", "task_id": task_id}
        self._agy_gate.refuse_conversation(task_id, self.STOP_SUBAGENT_REASON)
        # The row's terminal word is ours, because `agy` reports a starved
        # subagent as DONE and a green LED over a stop is a lie the user
        # would have to read twice. See `AgyTranslator.mark_stopped`.
        for translator in stopped_on:
            translator.mark_stopped(task_id)
        logger.info("Stopped subagent %s by refusing its calls", task_id)
        return {"status": "stopping", "task_id": task_id}

    async def cancel_streaming(self, request_id: str) -> dict[str, Any]:
        """⏹ — starve the turn. **Localhost only.**

        There is no halt frame on this transport, so this refuses every
        subsequent tool call rather than interrupting. A turn producing
        only prose cannot be stopped this way and runs to its end; that
        limit is stated in :mod:`aic_dc.agy.session` rather than papered
        over with a process kill, which would end the session too.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if request_id not in self._turns:
            return {"status": "not_running", "request_id": request_id}
        if self._session is not None:
            await self._session.cancel()
        # **The gate cannot reach a consultation, which is why this is
        # here.** The starvation above refuses *subsequent* tool calls, and
        # around an MCP call the gate is blind from `PreToolUse` until the
        # call returns — so a stop landing mid-consultation would be queued
        # behind the very thing it is stopping, for up to `agy`'s own three
        # minutes. This cancels the consultant's process directly.
        if self._listener is not None and self._consult_spawn_id:
            with contextlib.suppress(Exception):
                # Logged rather than discarded. `cancel_session` returns
                # whether it found anything to stop, and that boolean is the
                # only evidence anywhere that the stop reached the
                # consultation rather than merely the master — the hooks are
                # silent for the whole call, so nothing else in the record
                # distinguishes "stopped it" from "it finished on its own".
                if await self._listener.cancel_session(self._consult_spawn_id):
                    logger.info("The stop ended a consultation that was in flight")
        return {"status": "ok", "request_id": request_id}
