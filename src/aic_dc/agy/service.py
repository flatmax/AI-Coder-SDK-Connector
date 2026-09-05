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
import logging
import shutil
from pathlib import Path
from typing import Any

from aic_dc.agy import install
from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.agy.session import AgyNotInstalledError, AgySession
from aic_dc.agy.steps import AgyTranslator
from aic_dc.antigravity.permissions import AntigravityPermissionGate
from aic_dc.antigravity.service import AntigravityService
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)


class AgyService(AntigravityService):
    """Antigravity, driven through the CLI on the owner's own subscription."""

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
        self._model = model

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
        """Whether the gate is installed in the user's `agy` configuration.

        Public because it is the settings surface's whole question, and
        because a user is entitled to ask it without starting a session.
        """
        report = install.status(self._config_dir)
        report["agy_present"] = shutil.which(self._executable) is not None
        return report

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
        return {"model": self._model}

    async def connect_engine(self, resume: str | None = None) -> dict[str, Any]:
        """Start ``agy``. **Localhost only.**

        Two refusals before anything spawns, and both are deliberate:

        ``resume`` is declined for the same reason the SDK transport
        declines it — starting a fresh conversation when the caller asked
        to continue one is the wrong kind of success.

        **A session will not start without the gate installed.** Running
        one anyway would mean `agy` executing with
        ``--dangerously-skip-permissions`` and nothing intercepting it,
        which is not a degraded experience but an ungated agent editing the
        user's tree. AG-5 makes the dialog a requirement of this engine.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if resume:
            return {
                "error": "unsupported",
                "message": (
                    "Resuming an agy conversation is not built yet. Starting "
                    "a fresh session instead would silently lose the context "
                    "you asked for."
                ),
            }
        if shutil.which(self._executable) is None:
            return {
                "error": "not_installed",
                "message": (
                    f"{self._executable!r} is not on PATH, so the agy "
                    "transport cannot run. Install the Antigravity CLI, or "
                    "use the SDK transport with a Gemini API key."
                ),
            }
        gate = self.gate_status()
        if gate["state"] != "current":
            return {
                "error": "gate_not_installed",
                "reason": gate["state"],
                "message": (
                    "The AIC-DC permission gate is not installed in "
                    f"{gate['path']}, so a turn could not be reviewed before "
                    "it wrote to your files. Install it from Settings — it "
                    "stays installed until you remove it there."
                ),
                "gate": gate,
            }
        try:
            await self._ensure_session()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return self._record_error("connect", exc)
        return {"status": "connected", "model": self._model}

    async def _ensure_session(self) -> Any:
        if self._session is not None and self._session.started:
            return self._session

        # The same gate object the SDK transport builds, so the dialog, the
        # queue and `resolve_permission` are literally shared. Only what
        # carries a call *to* it differs.
        self._gate = AntigravityPermissionGate(
            self._repo_root,
            broadcast=self._broadcast,
            note_prompt=self._note_permission_prompt,
            localhost_available=self._localhost_available,
            denied_reads=self.get_denied_read_files,
        )
        self._agy_gate = AgyGateServer(
            self._config_dir / "agy-sessions" / "gate.sock",
            gate=self._gate,
            config_dir=self._config_dir,
        )
        session = AgySession(
            self._repo_root,
            gate=self._agy_gate,
            model=self._model,
            executable=self._executable,
        )
        try:
            await session.start()
        except AgyNotInstalledError:
            await self._agy_gate.stop()
            raise
        self._session = session
        return session

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        self._agy_gate = None
        self._gate = None
        if session is None:
            return
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("The agy session did not close cleanly")

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

        translator = AgyTranslator(request_id)
        self._turns[request_id] = translator
        import asyncio

        task = asyncio.create_task(
            self._run_agy_turn(session, translator, request_id, message),
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
        return {"status": "ok", "request_id": request_id}
