"""Antigravity as a one-shot consultant, called from a Claude Code turn.

``specs5/plan-ag/decisions.md`` AG-7: the first thing built is Antigravity
as a *consultant under Claude Code* — a single tool call — not a second
master. It delivers the owner's own worked example (image generation via
Google's model, which Anthropic does not offer) in the smallest increment
that commits to nothing, and it forces the credential question to be
answered with a real bill rather than an assumption.

The boundary this module must not cross
---------------------------------------
AG-R-9 names the failure precisely: an engine adapter grown out of a
consultant is shaped by the consultant's needs — one turn, no resume, no
permissions, no history — which are exactly the four things an engine is
mostly made of. So the extension is all cost and no reuse while *looking*
like reuse.

Its tripwire is a list of names, and this module does not contain any of
them: no ``receive_steps``, no ``cancel``, no ``conversation_id``, no hook
registration. Every call here is a self-contained ``async with
Agent(config)`` that starts a harness, asks one question, and shuts it
down. Streaming, resume and the permission dialog belong to phases 3–5 and
are written against ``Conversation`` directly.

What legitimately survives into phase 3 is narrow and named in AG-R-9:
config construction, credential resolution, and the probe.

Containment
-----------
Neither call gets a general-purpose agent pointed at the repository.

``second_opinion`` enables **no tools at all** beyond ``FINISH``. It is a
question and an answer; it has no business reading the tree, and the
context it needs is passed in by the caller who already has it.

``generate_image`` enables ``GENERATE_IMAGE`` and ``FINISH``. That is a
*write* tool — it saves a file and reports the path — so
``Agent.__aenter__`` requires either a policy or a decide hook before it
will start (``agent.py:93-103``).

Both calls therefore carry the same minimal static allowlist: ``deny_all()``
plus one ``allow`` per enabled tool, and nothing else.

**Leaving ``policies`` unset is not "no policy".** ``LocalAgentConfig``
defaults it to ``policy.confirm_run_command()`` — deny ``run_command``,
**approve everything else** — which is the blanket-bypass posture AG-5
says must never reach a shipped path, arriving as a default nobody chose.
With only ``FINISH`` enabled that default is inert today, and relying on
that is exactly the layered assumption that stops being true the first
time somebody adds a tool. So the allowlist is set on every call, not
only where a write tool is.

That is a deliberate narrowing of AG-5, agreed with the owner rather than
assumed. AG-5 declines the policy DSL **as the permission gate**, because
``policy.ask_user`` returns a bare ``bool`` and gives away both the
message the model reads and the ability to amend a tool call. None of
that is at stake here: there is no dialog, no user in the loop, and
nothing to amend — a one-shot call whose entire purpose is one tool. A
static allowlist in that position is a capability restriction, not a
permission decision. ``policy.allow_all()`` remains a probe-only posture
and does not appear here or anywhere shipped.

Trusting nothing the tool says about where it wrote
---------------------------------------------------
``generate_image`` returns a ``GenerateImageResult`` with an
``output_path``, and AG-R-3 is the recorded case of an Antigravity
product reporting a successful write to a path that was not the one asked
for: ``agy`` diverted a file into a scratch directory under ``~/.gemini/``
and reported success with a ``file://`` link, because the workspace was
untrusted. Whether the SDK's ``workspaces`` is subject to the same list is
a phase-0 unknown this module is what closes.

So :meth:`Consultant.generate_image` stats the file at the absolute path
it was given and checks that path is inside the repository, and reports
``contained`` as a fact it verified rather than one it was told. A
diverted image is a hard failure with the real path in the message, not a
success with an empty file tree.

Governing spec: ``specs5/plan-ag/`` — AG-7, AG-10, AG-R-3, AG-R-9.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

from aic_dc.antigravity.credentials import Credentials
from aic_dc.antigravity.credentials import resolve as resolve_credentials
from aic_dc.antigravity.session import stop_reason_of, turn_usage_of
from aic_dc.antigravity.steps import _name

logger = logging.getLogger(__name__)

#: The text model a consultation runs on. Pinned rather than inherited:
#: the SDK's own default moves between 0.1.x releases, and a second
#: opinion whose model changed under us is not a second opinion.
DEFAULT_TEXT_MODEL = "gemini-3.7-flash"

#: The image model. A separate ``ModelTarget`` with ``ModelType.IMAGE``,
#: which is how ``generate_image`` picks a model distinct from the one
#: holding the conversation.
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-lite-image"

#: How long a single consultation may take before it is abandoned. A
#: consultant that hangs blocks the Claude Code turn that called it, and
#: the caller has no cancel path by construction.
DEFAULT_TIMEOUT_SECONDS = 180.0


class ConsultationError(RuntimeError):
    """A consultation could not be completed.

    Carries prose the calling agent can act on, because the only place
    this surfaces is as the text of an MCP tool result.
    """


@dataclasses.dataclass(frozen=True)
class _Turn:
    """One drained consultation.

    Values rather than the SDK's ``ChatResponse``, because that object is
    a live cursor over a stream and is worthless once its connection has
    closed. Draining into this inside the ``async with`` is what stops a
    caller from accidentally reading a dead stream — see :meth:`
    Consultant._chat`, where doing exactly that hung until killed.
    """

    text: str
    chunks: list[Any]
    stop_reason: str
    #: ``Conversation.last_turn_usage`` — tokens, never a cost (AG-6).
    #: No step carries it, which phase 3's live run found the hard way.
    usage: Any = None


@dataclasses.dataclass(frozen=True)
class ImageResult:
    """Where an image landed, verified rather than reported.

    ``path`` is repo-relative when the write was contained, which is what
    the file tree and the Monaco viewer address files by.
    """

    path: str
    absolute_path: str
    bytes_written: int
    contained: bool
    summary: str


class Consultant:
    """One-shot Antigravity calls, from inside a Claude Code turn.

    Every method starts a harness, asks one thing, and shuts it down. The
    cost of a process per call is accepted deliberately: a long-lived
    connection is a session, a session wants resume and cancellation, and
    that is the engine (AG-R-9).

    Parameters
    ----------
    repo_root:
        The single workspace root, per AG-10. Not a list and not
        configurable — cwd is what the diff viewer, the file tree and
        every tool path resolve against.
    credentials:
        Resolved once at construction so the failure is reported when the
        service starts rather than mid-turn. ``None`` resolves from the
        environment.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        credentials: Credentials | None = None,
        text_model: str = DEFAULT_TEXT_MODEL,
        image_model: str = DEFAULT_IMAGE_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._credentials = credentials or resolve_credentials()
        self._text_model = text_model
        self._image_model = image_model
        self._timeout = timeout_seconds
        #: The live conversation, while a consultation is running. Held so
        #: :meth:`cancel` has something to halt (AG-13).
        self._conversation: Any = None

    @property
    def credentials(self) -> Credentials:
        """What this consultant will authenticate with. Carries no secret
        into :meth:`Credentials.report`, which is what the browser reads."""
        return self._credentials

    @property
    def available(self) -> bool:
        """Whether a consultation can be attempted at all.

        False is a normal state with a good explanation attached, not an
        error: AG-R-8's whole point is that the most likely first
        experience of this engine is a credential that does not exist yet.
        """
        return self._credentials.available

    # ------------------------------------------------------------------
    # Consultations
    # ------------------------------------------------------------------

    async def second_opinion(
        self, question: str, context: str = "", observer: Any = None
    ) -> str:
        """Ask Antigravity one question and return its answer as text.

        No tools, no workspace, no file access — the ``context`` argument
        is how the caller supplies what it already read. Two independent
        agents disagreeing about a diff is information; one agent given a
        second chance to browse the repository is not.
        """
        question = (question or "").strip()
        if not question:
            raise ConsultationError("A second opinion needs a question to answer.")

        prompt = question if not context.strip() else f"{question}\n\n{context.strip()}"
        turn = await self._chat(
            prompt, model=self._text_model, builtin_tools=(), observer=observer
        )
        if not turn.text:
            raise ConsultationError(
                f"Antigravity returned an empty answer (stop reason: {turn.stop_reason})."
            )
        return turn.text

    async def generate_image(
        self,
        prompt: str,
        output_name: str = "",
        aspect_ratio: str = "",
        observer: Any = None,
    ) -> ImageResult:
        """Generate an image into the repository, and verify where it went.

        The capability AG-1 exists for: Google offers image generation and
        Anthropic does not, so this is a thing one engine can do rather
        than a preference between models.

        Returns a verified :class:`ImageResult`. Raises
        :exc:`ConsultationError` when the model produced no file, or when
        it produced one outside the repository — which is AG-R-3's
        diversion, and is a failure however cheerfully it is reported.
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

        turn = await self._chat(
            " ".join(instruction),
            model=self._image_model,
            builtin_tools=("GENERATE_IMAGE",),
            observer=observer,
        )
        return self._verify_image(turn.chunks, turn.text)

    # ------------------------------------------------------------------
    # The single SDK call site
    # ------------------------------------------------------------------

    async def _drive(
        self, agent: Any, prompt: str, observer: Any = None
    ) -> _Turn:
        """Send one prompt and drain its steps, inside the agent's lifetime.

        **Streaming, which AG-7 originally forbade and AG-13 reversed.**
        The reversal is narrow and the reasoning is in
        ``risks.md`` AG-R-9: the risk was the consultant *inventing*
        session machinery ahead of the engine and so shaping the engine
        around a one-shot call. Phase 3 built that machinery properly, so
        this now *consumes* it — ``receive_steps()``, and the caller's
        observer feeding the existing ``StepTranslator``. There is no
        second translator and no second event vocabulary here, which is
        what the redrawn tripwire watches for.

        ``chat()`` is still not called, and that is the other half of the
        boundary. It returns a lazy ``ChatResponse`` — a cursor over a
        stream nothing has pulled — and reading one after
        ``Agent.__aexit__`` hung until killed in phase 1. Driving the
        conversation directly keeps the work inside the lifetime that owns
        the connection.

        The conversation is held for the duration so :meth:`cancel` has
        something to halt, and dropped in ``finally`` so a stopped
        consultation cannot be stopped twice.
        """
        conversation = agent.conversation
        self._conversation = conversation
        steps: list[Any] = []
        try:
            await conversation.send(prompt)
            async for step in conversation.receive_steps():
                steps.append(step)
                if observer is not None:
                    observer(step)
        finally:
            self._conversation = None

        return _Turn(
            text=(conversation.last_response or "").strip(),
            chunks=steps,
            stop_reason=_name(stop_reason_of(conversation)),
            usage=turn_usage_of(conversation),
        )

    async def cancel(self) -> bool:
        """Halt a running consultation. Safe when there is none.

        AG-13's ⏹ Stop. The subagent tab offers one on every row, and
        without this it would be decorative — which is worse than absent,
        because a button that does nothing reads as a hung engine rather
        than as a missing feature.
        """
        conversation = self._conversation
        if conversation is None:
            return False
        try:
            await conversation.cancel()
        except Exception:  # noqa: BLE001 - a cancel that fails is not fatal
            logger.exception("Cancelling the consultation failed")
            return False
        return True

    async def _chat(
        self,
        prompt: str,
        *,
        model: str,
        builtin_tools: tuple[str, ...],
        observer: Any = None,
    ) -> _Turn:
        """Start a harness, send one prompt, drain the answer, shut down.

        **Drained inside the ``async with``, which is not optional.**
        ``agent.chat()`` returns a lazy ``ChatResponse`` — a cursor over a
        stream that has not been pulled yet — so nothing has happened when
        it returns. An earlier version of this method handed that object
        back and let the callers await ``.text()`` on it, which read the
        stream *after* ``Agent.__aexit__`` had torn the connection down.
        It hung until killed, and the timeout below did not fire because
        the timeout covered starting the agent rather than the model work.
        Returning a :class:`_Turn` of already-drained values is what makes
        the context manager's scope mean what it looks like it means.

        The whole SDK surface this module touches, in one function, so the
        AG-R-9 boundary is checkable by reading twenty lines rather than
        the file. Deliberately *not* a reusable session factory: phase 3
        wants ``Conversation`` and a step pump, and would inherit a shape
        built for a call pattern it does not have.

        The import is inside the function because the SDK spawns a bundled
        119 MB binary, and this module has to stay importable where
        ``google-antigravity`` is not installed (AG-R-10).
        """
        import asyncio

        from google.antigravity import Agent, LocalAgentConfig, types
        from google.antigravity.hooks import policy

        self._credentials.require()

        enabled = [types.BuiltinTools.FINISH]
        enabled += [getattr(types.BuiltinTools, name) for name in builtin_tools]

        logger.debug("Antigravity consultation starting on %s", model)
        config = LocalAgentConfig(
            model=model,
            # AG-10: one root, and only one. The SDK would default to
            # os.getcwd(), which is the same value today and is not a
            # promise.
            workspaces=[str(self._repo_root)],
            capabilities=types.CapabilitiesConfig(enabled_tools=enabled),
            # The minimal static allowlist, set on *every* call rather than
            # only where a write tool is enabled. Leaving it unset is not
            # "no policy": LocalAgentConfig defaults to
            # ``policy.confirm_run_command()``, which is deny run_command
            # and **approve everything else** — the blanket-bypass posture
            # AG-5 says must never ship, arriving as a default. Enabling
            # only FINISH would make that inert today, which is exactly
            # the layered assumption that stops being true the moment
            # somebody adds a tool. See this module's docstring for why a
            # static allowlist here is not the AG-5 refusal reversed.
            policies=[policy.deny_all(), *(policy.allow(t.value) for t in enabled)],
            **self._credentials.config_kwargs(),
        )
        try:
            async with asyncio.timeout(self._timeout):
                async with Agent(config) as agent:
                    return await self._drive(agent, prompt, observer)
        except TimeoutError as exc:
            raise ConsultationError(
                f"Antigravity did not answer within {self._timeout:.0f}s. "
                "The consultation was abandoned; nothing was written."
            ) from exc
        except (
            types.AntigravityConnectionError,
            types.AntigravityExecutionError,
            types.AntigravityValidationError,
        ) as exc:
            # The SDK's four error types share no base class, so they are
            # named. AntigravityCancelledError is deliberately absent: it
            # derives from asyncio.CancelledError, which is a
            # BaseException, and swallowing it would turn a cancelled turn
            # into a stuck one.
            #
            # Wrapped rather than propagated because the only caller is an
            # MCP tool handler whose contract is "every failure comes back
            # as prose the model can act on". An unwrapped SDK error
            # escapes it and reaches the model as a stack trace.
            raise ConsultationError(_explain(exc)) from exc

    # ------------------------------------------------------------------
    # AG-R-3: believe the filesystem, not the tool
    # ------------------------------------------------------------------

    def _verify_image(self, chunks: list[Any], summary: str) -> ImageResult:
        """Find the written file and check it is where it claims to be.

        The tool's own success report is not evidence. ``agy`` reported a
        successful write, with a ``file://`` link, for a file it had
        diverted into ``~/.gemini/`` — so the check that matters is a
        ``stat`` at the absolute path, and a containment test against the
        repository root.
        """
        reported = _first_output_path(chunks)
        if not reported:
            raise ConsultationError(
                "Antigravity generated no image file. "
                + (f"It said: {summary}" if summary else "It gave no reason.")
            )

        absolute = Path(reported).expanduser()
        if not absolute.is_absolute():
            # A relative path is resolved against the workspace, which is
            # the only root there is.
            absolute = self._repo_root / absolute
        absolute = absolute.resolve()

        contained = absolute.is_relative_to(self._repo_root)
        if not contained:
            raise ConsultationError(
                f"Antigravity wrote the image to {absolute}, which is outside "
                f"the repository at {self._repo_root}. This is AG-R-3: the "
                "write was diverted and reported as a success. The file tree "
                "and the viewer cannot reach it."
            )
        if not absolute.is_file():
            raise ConsultationError(
                f"Antigravity reported writing {absolute} but there is no file "
                "there. The path is inside the repository, so this is not a "
                "workspace diversion — treat it as a failed generation."
            )

        size = absolute.stat().st_size
        if size == 0:
            raise ConsultationError(
                f"Antigravity wrote an empty file at {absolute}. "
                "An empty image is a failure that looks like a success."
            )
        return ImageResult(
            path=str(absolute.relative_to(self._repo_root)),
            absolute_path=str(absolute),
            bytes_written=size,
            contained=True,
            summary=summary,
        )


#: How much of an SDK error to keep. Quota failures arrive as the same
#: paragraph repeated three or four times with an embedded Go map dump,
#: several kilobytes of it, and the useful part is the first sentence.
_MAX_ERROR_CHARS = 400


def _explain(exc: Exception) -> str:
    """An SDK error as one line a model can act on.

    Quota gets its own branch, and the distinction it draws is the one the
    provider's own message obscures. A 429 saying *"retry in 57s"* while
    also saying **``limit: 0``** is not a throttle — the free tier's
    allowance for that model is zero and no amount of waiting changes it.
    Reporting it as "try again shortly" produces an agent that retries
    forever, which is the failure this function exists to prevent.
    """
    text = " ".join(str(exc).split())
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        if "limit: 0" in text:
            return (
                "Google refused the call: this API key's plan has no quota "
                "for that model at all (limit: 0), so retrying will not "
                "help. It needs a billing account linked to the project. "
                f"Google said: {text[:_MAX_ERROR_CHARS]}"
            )
        return (
            "Google rate-limited the call. An agent turn is many model "
            "calls, so the free tier's per-minute allowance is reached "
            f"mid-turn. Retry shortly. Google said: {text[:_MAX_ERROR_CHARS]}"
        )
    return f"Antigravity failed: {text[:_MAX_ERROR_CHARS]}"


def _first_output_path(chunks: list[Any]) -> str:
    """Where ``generate_image`` says it wrote, or ``""``.

    Read by attribute rather than by type, and defensively: this is an
    alpha SDK and the result object is a pydantic model the harness fills
    in. A shape that moved should read as "no image" — which raises a
    clear error — rather than as an ``AttributeError`` from inside a tool
    call.

    **Two shapes, because AG-13 changed what is passed here.** Until the
    consultation streamed, this read ``ChatResponse.resolve()`` chunks,
    where the path hangs off ``chunk.result.output_path``. It now receives
    ``Step`` objects, where it is a *tool-call argument*:
    ``StepUpdate.generate_image`` carries ``prompt`` and ``output_path`` in
    one sub-message, and the SDK copies the whole thing into
    ``ToolCall.args`` (``sdk-surface.md`` § The step stream).

    Both are tried, chunk shape first. Not defensiveness for its own sake:
    dropping the old one would have been a silent regression of exactly
    the kind phase 3's live run found three of — the tests pass either
    way, and only a real image would have shown it.
    """
    for chunk in chunks:
        result = getattr(chunk, "result", None)
        path = getattr(result, "output_path", None)
        if isinstance(path, str) and path:
            return path
        for call in getattr(chunk, "tool_calls", None) or []:
            args = getattr(call, "args", None) or {}
            path = args.get("output_path") if isinstance(args, dict) else None
            if isinstance(path, str) and path:
                return path
    return ""
