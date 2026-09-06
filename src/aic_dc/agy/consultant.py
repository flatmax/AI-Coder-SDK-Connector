"""Antigravity as a consultant, over the transport that reaches the paid account.

The ``agy`` counterpart of :mod:`aic_dc.antigravity.consultant`, and
[AG-16](../../../specs5/plan-ag/decisions.md#ag-16) is why it exists at
all. Both drive the same product and answer the same two questions —
``second_opinion`` and ``generate_image`` — through the same
:class:`~aic_dc.antigravity.bridge.ConsultantBridge`. What differs is
which account pays, and that turned out to decide whether the feature
works.

Why a second consultant rather than a better first one
======================================================
The SDK consultant authenticates with a Gemini API key, and on the free
tier of one of those:

- **every image model reports ``limit: 0``** — not a throttle, an
  allowance of zero, so ``generate_image`` has never once returned an
  image since it was built ([AG-12](../../../specs5/plan-ag/decisions.md#ag-12));
- **agent requests are capped at 20 per model per day**, which an
  afternoon of verification reaches;
- and what is left arrives *slowly*, because free-tier traffic is queued
  behind paid rather than refused.

``agy`` authenticates by OAuth against the owner's Google account, which
carries a paid subscription. It is the same product, the same model
family and the same ``generate_image`` tool — reached over a pipe instead
of a wheel, and billed to a plan that has an allowance. So this is not a
second implementation of a working feature; it is the first
implementation that can run.

``claude_code/service.py`` said, in as many words, *"There is no agy
equivalent — the CLI has no one-shot consultation mode"*. That was
written when nothing here drove ``agy`` at all. It is wrong twice over:
``agy`` runs headlessly per prompt, and phase 8 already built the
process, the stream reader and the gate this module composes.

Containment, which is the whole of the design
=============================================
The SDK consultant contains itself by **enabling** only the tools a
consultation needs — ``FINISH``, plus ``GENERATE_IMAGE`` for an image —
so the agent is not merely refused the rest, it never has them.

That option does not exist here. ``agy``'s tool set is the binary's: 57
tools, including ``run_command``, the browser suite and ``invoke_subagent``,
and the flag that would let AIC⚡DC review them
(``--dangerously-skip-permissions`` plus our hook) is on rather than off,
because ``agy``'s own headless posture auto-denies instead of asking.

So the restriction moves to the gate, as a
:class:`~aic_dc.agy.gate_server.StaticPolicy`: the consultation's
conversation is claimed in the registry exactly as a session's is, and
every call it makes is answered from a fixed allowlist with **no dialog**.
A second opinion is allowed nothing; an image generation is allowed
``generate_image``. Everything else is denied with a sentence the model
reads, which is the mechanism that turns a refusal into "answer the
question" rather than into a search for another route (AG-R-11, used the
useful way round).

**The claim is mandatory, and this refuses to run without the hook
installed.** An unclaimed conversation is passed straight through by the
hook — correctly, since that is how the user's own ``agy`` sessions stay
untouched — and an unclaimed consultation is therefore an agent with 57
tools, ``--dangerously-skip-permissions``, and the repository as its
working directory. That is not a degraded consultation; it is the thing
AG-5 exists to prevent, so :attr:`AgyConsultant.available` is false
without the gate and the tools are not offered at all.

What it borrows, and what it must not
=====================================
AG-R-9 warned that a consultant grown into an engine adapter is all cost
and no reuse. This is the same relationship in the opposite direction and
the risk does not apply: the engine was built first, and this *consumes*
it — :class:`~aic_dc.agy.session.AgySession` for the process and the
frames, :class:`~aic_dc.agy.steps.AgyTranslator` for the rendering,
:func:`~aic_dc.claude_code.messages.files_written_by` for which file a
call wrote, and
:func:`~aic_dc.antigravity.consultant.verify_image_write` for whether to
believe it. Nothing here is a second copy of any of those.

The one thing it deliberately does *not* borrow is the turn's close.
``AgySession.stream_turn`` ends by emitting ``streamComplete``, which
carries a request id and tells the browser a turn is over — and the turn
that is open is the *Claude* turn holding this tool call. So this reads
:meth:`~aic_dc.agy.session.AgySession.stream_frames` and lets the bridge
settle the tab, which is the one event a consultation is entitled to end.

Governing spec: ``specs5/plan-ag/`` — AG-16, AG-7, AG-13, AG-5, AG-R-3.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from aic_dc.agy import install
from aic_dc.agy.gate_server import AgyGateServer, StaticPolicy
from aic_dc.agy.session import (
    AgyNotInstalledError,
    AgySession,
    PromptNotSentError,
)
from aic_dc.agy.steps import AgyTranslator, unwrap
from aic_dc.antigravity.consultant import (
    ConsultationError,
    ImageResult,
    verify_image_write,
)
from aic_dc.antigravity.credentials import Credentials, agy_credentials
from aic_dc.claude_code.messages import files_written_by

logger = logging.getLogger(__name__)

#: How long a second opinion may take before the process is killed.
#:
#: Higher than the SDK consultant's 120s, and for a reason that is not
#: "be generous": that number was chosen against a free-tier queue where
#: the whole wait lands before the first token, and this transport does
#: not queue. What it has instead is an *agent loop* — ``agy`` is a CLI
#: that thinks, may reach for a tool, is refused, and reasons about the
#: refusal — so the budget has to cover a conversation rather than a
#: model call.
#:
#: It is still a bound rather than a preference. A Claude turn is blocked
#: on this call for its whole duration, and a second opinion that takes
#: four minutes has already stopped being worth waiting for.
DEFAULT_TIMEOUT_SECONDS = 180.0

#: The same, for an image.
#:
#: Longer because generation is a tool call *inside* that loop and the
#: whole loop has to fit: the model decides on a prompt, calls
#: ``generate_image``, waits for the picture, and then reports where it
#: put it. The SDK path never measured this end to end on a funded key,
#: so this is a first bound to be corrected by the first real run rather
#: than a figure with evidence behind it.
DEFAULT_IMAGE_TIMEOUT_SECONDS = 300.0

#: Tool names allowed under every consultation policy.
#:
#: ``finish`` is how the model says it is done. It is control rather than
#: capability — the SDK's own vocabulary classes it ``read``, and it
#: touches neither the tree nor the machine — and denying it would refuse
#: the agent its way of stopping, which is a refusal that reads as a hang.
#: Allowing a name the CLI may not have costs nothing; denying one it does
#: costs the consultation.
CONTROL_TOOLS = frozenset({"finish"})

#: What the model is told when it reaches for a tool a consultation does
#: not have. Prose rather than a code, because it is read by a model
#: deciding what to do next, and the thing we want it to do next is
#: *answer*.
_NO_TOOLS_REASON = (
    "This is a one-shot consultation inside another agent's session, not "
    "a session of your own. You have no tools here and no repository "
    "access. Answer from the question and the context you were given, in "
    "prose, and do not look for another way to read or change files."
)

_IMAGE_ONLY_REASON = (
    "This is a one-shot image generation inside another agent's session. "
    "The only tool you may use is generate_image. Generate the image, "
    "report the absolute path you wrote it to, and do nothing else — do "
    "not read, search, or run commands."
)

#: A second opinion: prose in, prose out, nothing else permitted.
SECOND_OPINION_POLICY = StaticPolicy.of(CONTROL_TOOLS, _NO_TOOLS_REASON)

#: An image: one tool, which is the capability the call exists for.
IMAGE_POLICY = StaticPolicy.of({"generate_image", *CONTROL_TOOLS}, _IMAGE_ONLY_REASON)


class AgyConsultant:
    """One-shot ``agy`` calls, from inside a Claude Code turn.

    The same four-method contract
    :class:`~aic_dc.antigravity.consultant.Consultant` has, because
    :class:`~aic_dc.antigravity.bridge.ConsultantBridge` holds one or the
    other and must not know which.

    Parameters
    ----------
    repo_root:
        The single workspace root (AG-10). Passed to ``agy`` as
        ``--add-dir`` by the session, which is what puts the agent *in*
        the repository rather than in ``agy``'s own scratch directory —
        the actual cause of AG-R-3, and the reason a generated image can
        land somewhere the file tree can see.
    config_dir:
        Where the gate's socket and the conversation registry live. The
        same directory the engine's gate uses, because the hook reads the
        registry from one place.
    model:
        Left unset by default, which is the opposite of the SDK
        consultant's pinned model and is deliberate. That pin exists
        because the SDK's default moves between 0.1.x releases *and*
        because the newest models were unusable on a free key. Here the
        default is the one the account holder chose in ``agy``'s own
        settings, on a plan that can run it — so inheriting it is
        inheriting a decision rather than an accident.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        config_dir: Path | str | None = None,
        model: str | None = None,
        executable: str = "agy",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        image_timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._config_dir = Path(
            config_dir or Path.home() / ".config" / "aic-dc"
        )
        self._model = model
        self._executable = executable
        self._timeout = timeout_seconds
        self._image_timeout = image_timeout_seconds
        #: The live session, while a consultation is running. Held so
        #: :meth:`cancel` has something to stop.
        self._session: AgySession | None = None
        self._cancelled = False
        self._counter = 0

    # ------------------------------------------------------------------
    # What the bridge asks before it offers the tools
    # ------------------------------------------------------------------

    @property
    def credentials(self) -> Credentials:
        """Who pays, in the shape the browser already renders.

        Not a Gemini key and not a Vertex project: ``agy`` holds its own
        OAuth, which is the entire reason this transport exists (AG-14).
        Reported through the same object so the Context tab and the log
        line naming the credential source need no branch.
        """
        return agy_credentials(shutil.which(self._executable) is not None)

    @property
    def gate_status(self) -> dict[str, Any]:
        """The hook's installation state, from the one authority on it."""
        report = dict(install.status(self._config_dir))
        report["agy_present"] = shutil.which(self._executable) is not None
        return report

    @property
    def available(self) -> bool:
        """Whether a consultation can be attempted at all.

        **Two conditions, and the second is not a nicety.** Without the
        hook installed in the user's ``agy`` configuration, our claim on
        the conversation means nothing: the gate would never be asked,
        and a consultation would run as an unreviewed agent with the
        binary's whole tool set and the repository as its cwd. AG-9's
        "hidden rather than stubbed" is the mild reason to answer false
        here; AG-5 is the real one.
        """
        return (
            shutil.which(self._executable) is not None
            and self.gate_status.get("state") == "current"
        )

    def make_translator(
        self, request_id: str, agent_id: str | None = None
    ) -> AgyTranslator:
        """The pump this transport's raw frames must go through.

        The bridge builds the tab and needs a translator for it, and the
        translator that can read these frames is this one — so it asks
        the consultant rather than naming a class. That is the whole of
        what makes :class:`~aic_dc.antigravity.bridge.ConsultantBridge`
        transport-agnostic: one event vocabulary reaching the browser,
        two readers producing it, and no branch in between.
        """
        return AgyTranslator(request_id, agent_id=agent_id)

    # ------------------------------------------------------------------
    # Consultations
    # ------------------------------------------------------------------

    async def second_opinion(
        self, question: str, context: str = "", observer: Any = None
    ) -> str:
        """Ask Antigravity one question and return its answer as text.

        No tools and no repository access — ``context`` is how the caller
        supplies what it already read. Two independent agents disagreeing
        about a diff is information; one agent given a second chance to
        browse the repository is not.
        """
        question = (question or "").strip()
        if not question:
            raise ConsultationError("A second opinion needs a question to answer.")

        prompt = question if not context.strip() else f"{question}\n\n{context.strip()}"
        translator, _ = await self._run(
            prompt,
            policy=SECOND_OPINION_POLICY,
            observer=observer,
            timeout=self._timeout,
        )
        answer = translator.response_text().strip()
        if not answer:
            raise ConsultationError(
                "Antigravity returned an empty answer. The consultation ran "
                "and produced no prose, which usually means the model spent "
                "the turn reaching for tools it does not have here."
            )
        return answer

    async def generate_image(
        self,
        prompt: str,
        output_name: str = "",
        aspect_ratio: str = "",
        observer: Any = None,
    ) -> ImageResult:
        """Generate an image into the repository, and verify where it went.

        The capability AG-1 exists for, on the account that can pay for
        it. The verification is
        :func:`~aic_dc.antigravity.consultant.verify_image_write` — shared
        with the SDK path rather than restated, because "the tool said it
        succeeded" is not evidence on *either* transport and AG-R-3 was
        measured on this one.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ConsultationError("Image generation needs a prompt.")

        instruction = [prompt]
        if output_name.strip():
            instruction.append(f"Save the image as {output_name.strip()}.")
        if aspect_ratio.strip():
            instruction.append(f"Use aspect ratio {aspect_ratio.strip()}.")
        instruction.append(
            f"Write it inside {self._repo_root} and report the absolute path."
        )

        translator, frames = await self._run(
            " ".join(instruction),
            policy=IMAGE_POLICY,
            observer=observer,
            timeout=self._image_timeout,
        )
        return verify_image_write(
            _image_path_from(frames), self._repo_root, translator.response_text().strip()
        )

    async def cancel(self) -> bool:
        """Stop a running consultation. Safe when there is none.

        **This kills the process, where the engine's ⏹ deliberately does
        not.** ``AgySession.cancel`` starves a turn by refusing its tool
        calls, because ending the process there would end a *session* the
        user is still holding — and it states its own limit: a turn
        producing only prose asks permission for nothing and cannot be
        starved.

        A consultation is one process for one call, so that trade is not
        the one in front of us: there is no session to lose, and starving
        would be exactly useless here since a second opinion is prose by
        construction and is already allowed nothing. Killing is the only
        thing that stops it, and it stops it completely.
        """
        session = self._session
        if session is None:
            return False
        self._cancelled = True
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - a cancel that fails is not fatal
            logger.exception("Stopping the agy consultation failed")
            return False
        return True

    # ------------------------------------------------------------------
    # The single call site
    # ------------------------------------------------------------------

    async def _run(
        self,
        prompt: str,
        *,
        policy: StaticPolicy,
        observer: Any,
        timeout: float,
    ) -> tuple[AgyTranslator, list[dict[str, Any]]]:
        """Spawn, ask one thing, drain it, and shut down.

        The whole subprocess surface this module touches, in one method,
        so the boundary is checkable by reading it rather than the file.

        The translator is the *bridge's* when there is a browser
        attached, so the frames are translated exactly once and the tab
        and the answer are two readings of one pass. Without a browser
        there is nobody to push to and one is made here, because the
        answer still has to be assembled from the same frames.
        """
        if not self.available:
            raise ConsultationError(self._unavailable_reason())

        self._cancelled = False
        translator = (
            observer.translator
            if observer is not None and getattr(observer, "translator", None) is not None
            else self.make_translator("")
        )
        frames: list[dict[str, Any]] = []

        gate = AgyGateServer(
            self._socket_path(), policy=policy, config_dir=self._config_dir
        )
        session = AgySession(
            self._repo_root,
            gate=gate,
            model=self._model,
            executable=self._executable,
        )
        self._session = session
        stream: Any = None
        try:
            async with asyncio.timeout(timeout):
                await session.start()
                stream = session.stream_frames(prompt)
                async for frame in stream:
                    frames.append(frame)
                    if observer is not None:
                        # Translates through the same translator and
                        # pushes each event into the tab.
                        observer(frame)
                    else:
                        translator.translate(frame)
        except TimeoutError as exc:
            raise ConsultationError(
                f"Antigravity did not answer within {timeout:.0f}s. The "
                "consultation was abandoned and its process stopped."
            ) from exc
        except AgyNotInstalledError as exc:
            raise ConsultationError(str(exc)) from exc
        except PromptNotSentError as exc:
            raise ConsultationError(
                f"The consultation could not be started: {exc}"
            ) from exc
        except ConsultationError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as prose, always
            if self._cancelled:
                raise ConsultationError(
                    "The consultation was stopped before it answered."
                ) from exc
            raise ConsultationError(
                f"The consultation failed: {' '.join(str(exc).split())[:400]}"
            ) from exc
        finally:
            self._session = None
            if stream is not None:
                # A timeout abandons the loop mid-frame, and an async
                # generator left suspended runs its `finally` whenever the
                # collector gets to it. Closed here so the turn latch is
                # released before the process is.
                await stream.aclose()
            # Closes the process *and* the gate, which releases the
            # registry claim. A consultation that left its claim standing
            # would make the hook deny the user's own next `agy` session
            # on a socket nobody is listening to.
            await session.close()

        if self._cancelled:
            raise ConsultationError("The consultation was stopped before it answered.")
        return translator, frames

    def _socket_path(self) -> Path:
        """A socket of its own, per consultation, and a *short* one.

        Not under ``config_dir`` beside the engine's
        ``agy-sessions/gate.sock``, and the reason is a hard limit rather
        than a preference: a unix socket path is bounded at around 107
        bytes by ``sockaddr_un``, and exceeding it fails at ``bind`` with
        ``AF_UNIX path too long`` — which surfaces here as a consultation
        that failed for no reason the user can act on. The engine's path
        is short because ``~/.config/aic-dc`` is short; nothing guarantees
        that, since ``config_dir`` is configurable, and a consultation
        adds two more path segments on top of it.

        So it lives in the system temp directory under a name built from
        the process, the object and a counter — unique enough for two
        bridges in one process, and short whatever the user's
        configuration looks like. Nothing persists: the socket exists for
        one call and :meth:`AgyGateServer.stop` unlinks it. The registry
        entry carries this path to the hook, so where it lives is
        immaterial as long as both processes can reach it.

        Found by a test whose ``tmp_path`` was long enough to trip the
        limit, which is the cheapest possible way to have learned it.
        """
        self._counter += 1
        return Path(tempfile.gettempdir()) / (
            f"aic-dc-c{os.getpid()}-{id(self):x}-{self._counter}.sock"
        )

    def _unavailable_reason(self) -> str:
        """Why this could not run, in the words the model should relay."""
        if shutil.which(self._executable) is None:
            return (
                f"{self._executable!r} is not on PATH, so the Antigravity "
                "consultant cannot run over the CLI transport."
            )
        state = self.gate_status.get("state")
        return (
            "The AIC-DC permission gate is not installed in the agy "
            f"configuration (it reports {state!r}), so a consultation could "
            "not be reviewed before it ran. Install it from Settings."
        )


def choose_consultant(
    repo_root: Path | str,
    *,
    config_dir: Path | str | None = None,
    transport: str = "auto",
    enabled: Iterable[str] | None = None,
) -> tuple[Any, str]:
    """The consultant to mount, and one sentence saying why.

    Returns ``(None, reason)`` when neither transport can run, and the
    reason is written to be *logged at the user*: the first version of the
    SDK mount told a user who already had a key to go and set one, which
    is a diagnostic that sends someone to fix the wrong thing.

    **Why this lives in the ``agy`` package** rather than beside the SDK
    consultant it also constructs: ``agy`` already imports ``antigravity``
    and the reverse would be a cycle. The choice belongs above both, and
    of the two packages only this one is above the other.

    ``auto`` prefers ``agy`` whenever it can run, because the alternative
    is a metered key whose free tier cannot generate an image at all.
    It falls through to the SDK when the CLI is absent or its gate is not
    installed — a fallback rather than a refusal, since a second opinion
    on a metered key is still a second opinion.

    ``enabled`` is AG-17's engine policy, and the two arguments compose by
    **intersection**: the policy decides *whether* this install may reach
    Antigravity at all, ``transport`` decides *which* way given that it
    may. So naming a transport the policy excludes yields no consultant
    rather than an override — a preference cannot widen a policy, which is
    the only ordering that makes the policy one.

    Consulting it here rather than at the mount point is deliberate. This
    function is where "can we reach Antigravity" is already answered, and
    a second place asking the same question is a second place that can
    forget to.
    """
    from aic_dc import capabilities
    from aic_dc.antigravity.consultant import Consultant

    permitted = (
        set(capabilities.ENGINES) if enabled is None else {str(n) for n in enabled}
    )
    if not permitted & {capabilities.AGY, capabilities.ANTIGRAVITY}:
        return None, (
            "app.json's engines.enabled does not permit any Antigravity "
            "engine, so this install offers no second opinion and no image "
            "generation. This is a configured policy rather than a missing "
            "credential (AG-17)."
        )

    agy_consultant = AgyConsultant(repo_root, config_dir=config_dir)
    sdk_consultant = Consultant(repo_root)
    if capabilities.AGY not in permitted:
        # Reported as unusable for the same reason a missing binary is,
        # and through the same channel: every caller below already knows
        # how to say why a transport is not available, and a policy is
        # one more why.
        agy_consultant = _Excluded(
            "app.json's engines.enabled does not permit the agy transport"
        )
    if capabilities.ANTIGRAVITY not in permitted:
        sdk_consultant = _Excluded(
            "app.json's engines.enabled does not permit the Antigravity SDK "
            "transport"
        )

    if transport == "agy":
        if agy_consultant.available:
            return agy_consultant, "the agy CLI, named by app.json"
        return None, (
            "app.json names the agy consultant transport, and "
            f"{agy_consultant._unavailable_reason()}"
        )
    if transport == "sdk":
        if sdk_consultant.available:
            return sdk_consultant, "the Antigravity SDK, named by app.json"
        return None, (
            "app.json names the SDK consultant transport, and "
            f"{_sdk_reason(sdk_consultant)}"
        )

    if agy_consultant.available:
        return agy_consultant, (
            "the agy CLI, on the account's own subscription — preferred "
            "over an API key because a free-tier key cannot generate "
            "images at all (AG-12)"
        )
    if sdk_consultant.available:
        return sdk_consultant, (
            "the Antigravity SDK on a Gemini key; the agy transport was "
            f"not usable because {agy_consultant._unavailable_reason()}"
        )
    return None, (
        "neither transport is usable: "
        f"{agy_consultant._unavailable_reason()} And "
        f"{_sdk_reason(sdk_consultant)}"
    )


def _sdk_reason(consultant: Any) -> str:
    """Why the SDK consultant cannot run, in one clause.

    The SDK consultant has no ``_unavailable_reason`` of its own — its
    two absences are AG-R-8's missing credential and AG-R-10's missing
    wheel, and it reports them through ``credentials.source`` and
    ``sdk_installed()`` rather than as a sentence. Composed here rather
    than added there, because this is the only caller that has to
    explain a *choice* between transports.
    """
    excluded = getattr(consultant, "reason", None)
    if excluded:
        return f"{excluded}."
    return (
        "the SDK transport has no Gemini API key or Vertex project, or no "
        "google-antigravity wheel to run one with (AG-R-8, AG-R-10)."
    )


class _Excluded:
    """A transport the engine policy does not permit (AG-17).

    Stands where a consultant would, answering the two questions the
    chooser asks of one — ``available`` and why not — so a policy
    exclusion travels the same path as a missing binary instead of
    needing a branch at every comparison. It has no other methods,
    deliberately: anything that got hold of one of these and tried to
    consult with it should fail loudly rather than silently do nothing.
    """

    available = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _unavailable_reason(self) -> str:
        return self.reason


def _image_path_from(frames: list[dict[str, Any]]) -> str:
    """Where a ``generate_image`` call said it put the file.

    Read from the tool frame's own parameters through
    :func:`~aic_dc.claude_code.messages.files_written_by`, which is the
    one table mapping a tool name to its path argument for all three
    vocabularies — and which already knows ``generate_image`` under both
    of Antigravity's spellings, ``output_path`` and ``OutputPath``. A
    second reader here would be a fourth vocabulary to keep in step.

    Returns ``""`` when no such call appears, which
    :func:`~aic_dc.antigravity.consultant.verify_image_write` turns into
    "generated no image file" — the honest answer, since a turn that
    never called the tool did not produce a picture whatever its prose
    says.
    """
    for frame in frames:
        step = unwrap(frame, "step_update")
        if step is None or step.get("step_type") != "tool":
            continue
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        name = str(step.get("tool_name") or info.get("name") or "")
        params = info.get("parameters")
        params = dict(params) if isinstance(params, dict) else {}
        written = files_written_by(name, params)
        if written:
            return written[0]
    return ""
