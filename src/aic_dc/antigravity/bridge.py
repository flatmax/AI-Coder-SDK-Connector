"""The consultant's two tools, packaged for a Claude Code turn.

AG-7's deliverable as the master engine sees it: ``second_opinion`` and
``generate_image``, reachable from a Claude Code turn as MCP tools.

Why this is a *second* MCP server
---------------------------------
``aic_dc.claude_code.mcp_server`` already exposes an in-process server,
and adding two tools to it would have been one line shorter and wrong.

That server's tools are ungated by construction:
``permissions.can_use_tool`` early-returns an allow for anything matching
``mcp__aic-dc__*``, with no dialog and no broadcast, because
``specs5/3-engine/permissions.md`` puts them in the read-only row —
"displayed, not gated". The bridge's own docstring states the invariant
that earns it: *"Read-only, all six. Nothing here mutates the repository,
the engine or the UI."*

``generate_image`` writes a file. ``second_opinion`` spends money on a
different provider's account. Neither belongs in a row whose entry
condition is "the same class of consequence as Read", and putting them
there would not merely stretch a category — it would route a file write
around the permission dialog entirely, which is the thing AG-5 calls
non-negotiable.

So they mount under their own name. Nothing special-cases it, which means
they classify as ``mcp`` and reach the dialog by the ordinary path. The
cost is one more server in the Context tab's inventory; the alternative
was an ungated write.

Why it lives in this package
----------------------------
AG-4's observation is that the ``@tool``-decorated wrappers are
Claude-specific *packaging* around functions that are not. The packaging
is here rather than in ``claude_code`` because AG-1 requires the mirror —
Claude reachable as a consultant *from* Antigravity — and both directions
of that symmetry want to sit next to each other rather than one per
engine package.

Governing spec: ``specs5/plan-ag/`` — AG-7, and AG-5 for why this is not
on the ungated server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aic_dc.antigravity.consultant import Consultant, ConsultationError
from aic_dc.antigravity.credentials import MissingCredentialsError
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

#: The server name the CLI prefixes onto every tool:
#: ``mcp__aic-dc-antigravity__second_opinion``. Deliberately *not*
#: ``aic-dc``: that prefix is what ``permissions.AIC_DC_MCP_SERVER``
#: ungates, and these two tools must reach the dialog.
SERVER_NAME = "aic-dc-antigravity"

#: How often a running consultation says it is still running.
#:
#: Two live browser consultations on 2026-09-02 showed a spinner and
#: nothing else for the full timeout, because Google had accepted the
#: request and never answered. The tab could not distinguish that from a
#: fast model thinking hard, and neither could the person watching it.
#:
#: A heartbeat is the smallest honest fix: it claims nothing about
#: progress — there is none to report, the harness is blocked on a socket
#: — and only says how long the wait has been. That is exactly the
#: distinction the UI was missing, and it is deliberately *not* dressed up
#: as progress, because a bar that moves while nothing happens is worse
#: than a number that grows.
#:
#: **It says two different things, and the difference is diagnostic.**
#: Google confirmed (2026-09-02) that free-tier requests are queued behind
#: paid traffic rather than refused, and that *"the capacity queueing
#: occurs at the routing layer before a model is allocated … the entire
#: wait time is absorbed into your Time to First Token"*. So a
#: consultation that has produced no step yet is queued, and one that has
#: is merely thinking — two states with the same spinner and completely
#: different meanings. Before the first step the heartbeat names the
#: queue, because "your free tier is waiting behind paying customers" is
#: something a reader can act on and "still working" is not.
HEARTBEAT_SECONDS = 20.0


def _text(body: str) -> dict[str, Any]:
    """One text block, the shape every handler returns."""
    return {"content": [{"type": "text", "text": body}]}


class ConsultantBridge:
    """The consultant, as tools a Claude Code turn can call.

    Holds a :class:`~aic_dc.antigravity.consultant.Consultant` and turns
    its exceptions into prose. Every failure comes back as *text* rather
    than as a raised error, because the caller is a model: an exception
    ends the tool call with a stack trace it cannot act on, while a
    sentence saying what went wrong lets it choose to try something else
    or to tell the user.
    """

    def __init__(
        self,
        consultant: Consultant,
        *,
        emit: Any = None,
        request_id: Any = None,
    ) -> None:
        self._consultant = consultant
        self._emit = emit
        self._request_id = request_id
        self._counter = 0
        self._tasks: set[Any] = set()

    # ------------------------------------------------------------------
    # AG-13 — the consultation as its own agent tab
    # ------------------------------------------------------------------

    def _new_agent_id(self) -> str:
        """A fresh identity for one consultation.

        **Minted, not borrowed, and the reason is a measured limitation.**
        An in-process MCP tool handler receives only its own ``args``
        dict — no ``tool_use_id``, no context object
        (``claude_agent_sdk.tool``, verified 2026-09-01) — so a
        consultation cannot learn the id of the tool card that invoked it.
        Correlating against the most recent
        ``mcp__aic-dc-antigravity__*`` card in the pump would be a race
        whose failure mode is attaching output to the *wrong* card, which
        is worse than not attaching it.

        The cost, accepted in AG-13: the row does not nest inside its
        spawning tool card the way a ``Task`` subagent's does. It gets its
        own row and its own tab.
        """
        self._counter += 1
        return f"consultation-{id(self):x}-{self._counter}"

    def _turn(self) -> str | None:
        """The request id the tab must be attributed to.

        ``subagentEvent`` is turn-scoped, and ``onSubagentEvent`` drops any
        event whose request id does not match a *live* Main tab. That is
        satisfied by construction here — a consultation only runs inside a
        Claude turn — and it is the requirement that fails silently if the
        bridge is ever driven from outside one.
        """
        source = self._request_id
        try:
            return source() if callable(source) else source
        except Exception:  # noqa: BLE001 - no tab is better than no answer
            logger.exception("Could not read the active request id")
            return None

    async def _announce(self, agent_id: str, label: str, **fields: Any) -> None:
        """One ``subagentEvent``, in the shape the tab strip already reads.

        No new event name and no webapp change: ``subagent-tabs.js`` joins
        on identifiers alone, and ``subagent_type``/``description`` are
        labels. ``terminal`` is what stops the tab streaming forever.
        """
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        payload = {
            "task_id": agent_id,
            "agent_id": agent_id,
            "tool_use_id": agent_id,
            "description": label,
            "task_type": "consultation",
            "subagent_type": "Antigravity",
            "status": "running",
            "terminal": False,
            **fields,
        }
        try:
            await self._emit(Event("subagentEvent", payload), request_id)
        except Exception:  # noqa: BLE001 - a dead client is not a failed call
            logger.exception("Could not announce the consultation")

    def _observer(
        self, agent_id: str, translator: Any, progress: dict | None = None
    ) -> Any:
        """Feed each step through the shared pump, tagged with our id.

        The translator is ``steps.StepTranslator`` — imported, not
        reimplemented, which is AG-R-9's redrawn tripwire. Its
        ``agent_id`` makes every block it produces carry that scope, and
        that is the whole of what puts the text in the tab.
        """

        def observe(step: Any) -> None:
            if progress is not None:
                progress["steps"] += 1
            request_id = self._turn()
            if self._emit is None or request_id is None:
                return
            for event in translator.translate(step):
                # Fire-and-forget: an observer is called from inside the
                # step loop and must not block it, and a dropped chunk
                # costs a frame of text rather than the consultation.
                task = asyncio.ensure_future(self._push(event, request_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

        return observe

    async def _push(self, event: Any, request_id: str) -> None:
        try:
            await self._emit(event, request_id)
        except Exception:  # noqa: BLE001
            logger.exception("Dropping consultation event %s", event.name)

    @contextlib.asynccontextmanager
    async def _tab(self, label: str) -> Any:
        """Open a tab for one consultation, and settle it however it ends.

        A context manager because the *settling* is the part that must not
        be forgotten: ``state.streaming = !row.terminal`` in the webapp, so
        a consultation that raised without a terminal event leaves a tab
        spinning for the rest of the session. ``finally`` is the only
        placement that survives a refusal, a timeout and a cancel alike.

        Yields the observer to hand to the consultant, or ``None`` when
        there is nothing to emit to — which keeps the whole feature off
        the critical path of a session with no browser attached.

        **The status is earned, not assumed.** An earlier version reported
        ``completed`` unconditionally from the ``finally``, and the first
        live run through the browser showed what that costs: Google never
        answered, the consultation timed out after 180s, and the row
        settled to a **green** "completed" LED — because the webapp maps
        that status straight to green (``subagent-tabs.js`` ``_TERMINAL_LED``).
        A failure rendered as a success is the one outcome worth more than
        a spinner, and it is exactly the manufactured-consent shape AG-5
        and AG-R-3 are both written against.

        So the caller must let the exception propagate *through* this
        manager rather than catching it inside: ``else`` is what earns
        ``completed``, and a ``return`` from inside the ``with`` block
        would look like success here no matter what it returned.
        """
        if self._emit is None or self._turn() is None:
            yield None
            return

        agent_id = self._new_agent_id()
        translator = StepTranslator(self._turn() or "", agent_id=agent_id)
        await self._announce(agent_id, label)
        status = "failed"
        # Mutable, shared with the heartbeat: it is the only way the
        # heartbeat can tell a queued consultation from a working one, and
        # that distinction is the whole of what it has to say.
        progress = {"steps": 0}
        heartbeat = asyncio.ensure_future(self._heartbeat(agent_id, progress))
        try:
            yield self._observer(agent_id, translator, progress)
        except BaseException as exc:
            # The reason, into the tab. Until now a failed consultation
            # settled red and said nothing about why; the explanation went
            # to the model as tool text, which the user does not read.
            await self._push_reason(agent_id, exc)
            # Including cancellation: a consultation the user stopped did
            # not complete, and saying so is the point of the ⏹ button.
            raise
            raise
        else:
            status = "completed"
        finally:
            heartbeat.cancel()
            # Drain what the observer scheduled before saying the tab is
            # done, or the terminal event can arrive ahead of the text it
            # is meant to be terminating.
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await self._announce(
                agent_id,
                label,
                status=status,
                terminal=True,
                usage=translator.turn_usage() or None,
            )

    async def _heartbeat(self, agent_id: str, progress: dict | None = None) -> None:
        """Say how long the wait has been, until cancelled.

        Cancelled in the ``finally`` of :meth:`_tab`, so it cannot outlive
        the consultation it is reporting on. ``CancelledError`` is allowed
        to propagate — swallowing it is how a "harmless" background task
        becomes one that never stops.
        """
        waited = 0.0
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            waited += HEARTBEAT_SECONDS
            request_id = self._turn()
            if self._emit is None or request_id is None:
                return
            started = bool(progress and progress.get("steps"))
            message = (
                f"Antigravity is working — {waited:.0f}s so far."
                if started
                else (
                    f"Waiting for Google to start — {waited:.0f}s so far, and "
                    "nothing has arrived yet. On a free-tier key requests are "
                    "queued behind paid traffic rather than refused, and the "
                    "whole wait lands before the first token."
                )
            )
            await self._push(
                Event(
                    "systemEvent",
                    {
                        "subtype": "engine_notice",
                        "data": {"message": message},
                        "agent_id": agent_id,
                    },
                ),
                request_id,
            )

    async def _push_reason(self, agent_id: str, exc: BaseException) -> None:
        """The failure's own words, into the tab that is about to go red."""
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        await self._push(
            Event(
                "systemEvent",
                {
                    "subtype": "engine_error",
                    "data": {"message": " ".join(str(exc).split())[:600]},
                    "agent_id": agent_id,
                },
            ),
            request_id,
        )

    async def cancel(self) -> bool:
        """Stop a running consultation. AG-13's ⏹, and it is real.

        Reached from ``stop_task``, which is why that method belongs to
        the ``subagent_tabs`` surface. A button that did nothing would
        read as a hung engine rather than as a missing feature.
        """
        return await self._consultant.cancel()

    @property
    def available(self) -> bool:
        """Whether the tools can do anything.

        Read by the caller deciding whether to register them at all —
        AG-9's "hidden rather than stubbed", applied to a tool definition:
        two tools that always answer "no credentials" cost context on
        every turn and buy nothing.
        """
        return self._consultant.available

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def second_opinion(self, question: str, context: str = "") -> dict[str, Any]:
        """Ask Antigravity, and hand the answer back verbatim.

        Verbatim on purpose. The value of a second opinion is that it was
        not produced by the agent reading it, so summarising it here — or
        framing it as "Antigravity agrees" — would put this module's
        judgement between two models that are supposed to disagree in
        front of the user.
        """
        try:
            # The catch is *outside* the tab, so a failure propagates
            # through it and settles the row as failed rather than as a
            # green "completed". See `_tab`.
            async with self._tab("Second opinion") as observer:
                answer = await self._consultant.second_opinion(
                    question, context, observer=observer
                )
        except (ConsultationError, MissingCredentialsError) as exc:
            return _text(f"The second opinion could not be obtained: {exc}")
        return _text(
            "A second opinion from Google Antigravity (a different model, "
            "reasoning independently — treat it as evidence, not as a "
            f"verdict):\n\n{answer}"
        )

    async def generate_image(
        self,
        prompt: str,
        output_name: str = "",
        aspect_ratio: str = "",
    ) -> dict[str, Any]:
        """Generate an image into the repository and report where it went.

        The reported path is repo-relative and verified against the
        filesystem (AG-R-3), so it is directly usable in a markdown or
        HTML reference — which is the only reason the agent asked.
        """
        try:
            # Outside the tab, for the same reason as second_opinion.
            async with self._tab("Generate image") as observer:
                result = await self._consultant.generate_image(
                    prompt,
                    output_name=output_name,
                    aspect_ratio=aspect_ratio,
                    observer=observer,
                )
        except (ConsultationError, MissingCredentialsError) as exc:
            return _text(f"The image could not be generated: {exc}")
        return _text(
            f"Image written to {result.path} ({result.bytes_written:,} bytes). "
            "The path is repo-relative and verified on disk; the file tree "
            "and the viewer can open it."
            + (f"\n\nThe model said: {result.summary}" if result.summary else "")
        )

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    def build_tools(self) -> list[Any]:
        """The two ``SdkMcpTool`` definitions, before they are wrapped.

        Separate from :meth:`build_server` for the reason the index
        bridge's equivalent is: ``create_sdk_mcp_server`` folds them into
        an object that does not hand them back, and the descriptions,
        schemas and annotations are exactly what wants checking.

        Neither carries ``readOnlyHint``. ``generate_image`` writes a
        file, and ``second_opinion`` reaches a third-party service and
        bills a different account — a hint that either is read-only would
        be a claim about consequence, not about the repository.
        """
        from claude_agent_sdk import tool
        from mcp.types import ToolAnnotations

        external = ToolAnnotations(openWorldHint=True)

        @tool(
            "second_opinion",
            "Ask Google's Gemini, running as an independent agent, to review a "
            "question you have already formed a view on — a diff, a design "
            "choice, a diagnosis. Two models disagreeing is information; the "
            "same model asked twice is not. It has no repository access, so "
            "pass the code or context it needs in `context`. Costs a call on a "
            "separate Google account.",
            {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to put, stated so it can be "
                        "answered without the surrounding conversation.",
                    },
                    "context": {
                        "type": "string",
                        "description": "The code, diff or facts it needs. It "
                        "cannot read the repository, so anything omitted here "
                        "is unavailable to it.",
                    },
                },
                "required": ["question"],
            },
            external,
        )
        async def second_opinion(args: dict[str, Any]) -> dict[str, Any]:
            return await self.second_opinion(
                question=args.get("question", ""),
                context=args.get("context", ""),
            )

        @tool(
            "generate_image",
            "Generate an image with Google's image model and write it into the "
            "repository. Anthropic's models cannot generate images, so this is "
            "a capability the session otherwise does not have. Returns the "
            "repo-relative path, verified on disk, ready to reference from "
            "markdown or HTML.",
            {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the image should show.",
                    },
                    "output_name": {
                        "type": "string",
                        "description": "Preferred filename, e.g. "
                        "'docs/architecture.png'. The model chooses one if "
                        "this is omitted.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Aspect ratio such as '16:9' or '1:1'.",
                    },
                },
                "required": ["prompt"],
            },
        )
        async def generate_image(args: dict[str, Any]) -> dict[str, Any]:
            return await self.generate_image(
                prompt=args.get("prompt", ""),
                output_name=args.get("output_name", ""),
                aspect_ratio=args.get("aspect_ratio", ""),
            )

        return [second_opinion, generate_image]

    def build_server(self) -> Any:
        """The ``McpSdkServerConfig`` for ``ClaudeAgentOptions.mcp_servers``.

        In-process, like the index bridge — but under its own name, so the
        permission layer treats these two as the third-party calls they
        effectively are.
        """
        from claude_agent_sdk import create_sdk_mcp_server

        return create_sdk_mcp_server(name=SERVER_NAME, tools=self.build_tools())
