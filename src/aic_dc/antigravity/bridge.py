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

import logging
from typing import Any

from aic_dc.antigravity.consultant import Consultant, ConsultationError
from aic_dc.antigravity.credentials import MissingCredentialsError

logger = logging.getLogger(__name__)

#: The server name the CLI prefixes onto every tool:
#: ``mcp__aic-dc-antigravity__second_opinion``. Deliberately *not*
#: ``aic-dc``: that prefix is what ``permissions.AIC_DC_MCP_SERVER``
#: ungates, and these two tools must reach the dialog.
SERVER_NAME = "aic-dc-antigravity"


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

    def __init__(self, consultant: Consultant) -> None:
        self._consultant = consultant

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
            answer = await self._consultant.second_opinion(question, context)
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
            result = await self._consultant.generate_image(
                prompt, output_name=output_name, aspect_ratio=aspect_ratio
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
