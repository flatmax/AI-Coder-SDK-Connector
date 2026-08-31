"""Tests for aic_dc.antigravity.bridge — the consultant as Claude tools.

The load-bearing assertion in this file is the server *name*.

``permissions.can_use_tool`` early-returns an allow — no dialog, no
broadcast — for anything matching ``mcp__aic-dc__*``, because
``specs5/3-engine/permissions.md`` puts the index tools in the read-only
row. ``generate_image`` writes a file. Mounting it under that prefix
would route a file write around the permission dialog, which AG-5 calls
non-negotiable, and it would do so silently: the tool would work, the
file would appear, and nothing would look wrong.

So :class:`TestItIsNotOnTheUngatedServer` checks the name against
``permissions.AIC_DC_MCP_SERVER`` directly rather than against a literal,
which means a rename on either side fails here instead of quietly
re-opening the hole.

The rest is the contract the calling model relies on: every failure comes
back as readable text rather than as an exception, because the caller
cannot act on a stack trace.
"""

from __future__ import annotations

import pytest

from aic_dc.antigravity.bridge import SERVER_NAME, ConsultantBridge
from aic_dc.antigravity.consultant import ConsultationError, ImageResult
from aic_dc.antigravity.credentials import MissingCredentialsError


class FakeConsultant:
    """Records calls; raises whatever a test asked it to."""

    def __init__(self, *, answer="", image=None, raises=None, available=True):
        self._answer = answer
        self._image = image
        self._raises = raises
        self.available = available
        self.calls: list[tuple] = []

    async def second_opinion(self, question, context=""):
        self.calls.append(("second_opinion", question, context))
        if self._raises:
            raise self._raises
        return self._answer

    async def generate_image(self, prompt, output_name="", aspect_ratio=""):
        self.calls.append(("generate_image", prompt, output_name, aspect_ratio))
        if self._raises:
            raise self._raises
        return self._image


def body(result) -> str:
    return result["content"][0]["text"]


# ----------------------------------------------------------------------
# The one that matters
# ----------------------------------------------------------------------


class TestItIsNotOnTheUngatedServer:
    """A file writer must not inherit the index tools' dialog-free allow."""

    def test_the_name_differs_from_the_ungated_prefix(self):
        from aic_dc.claude_code.permissions import AIC_DC_MCP_SERVER

        assert SERVER_NAME != AIC_DC_MCP_SERVER, (
            "These tools would be allowed with no permission dialog. "
            "generate_image writes a file; second_opinion bills a separate "
            "account. Neither is 'the same class of consequence as Read'."
        )

    def test_the_permission_gate_does_not_ungate_it(self):
        """Checked through the real classifier, not by reading the name.

        The early return keys on ``mcp_server_name(tool)``, so this is
        the actual predicate rather than a restatement of it.
        """
        from aic_dc.claude_code.permissions import (
            AIC_DC_MCP_SERVER,
            classify_tool,
            mcp_server_name,
        )

        for tool_name in (
            f"mcp__{SERVER_NAME}__generate_image",
            f"mcp__{SERVER_NAME}__second_opinion",
        ):
            assert mcp_server_name(tool_name) != AIC_DC_MCP_SERVER
            assert classify_tool(tool_name) == "mcp", (
                "an 'mcp' classification is what sends this to the dialog"
            )

    def test_generate_image_is_not_advertised_as_read_only(self):
        """``readOnlyHint`` on a file writer is a false statement to the CLI."""
        bridge = ConsultantBridge(FakeConsultant())
        tools = {t.name: t for t in bridge.build_tools()}
        for definition in tools.values():
            hint = getattr(definition, "annotations", None)
            assert not getattr(hint, "readOnlyHint", False), definition.name


# ----------------------------------------------------------------------
# Tool definitions
# ----------------------------------------------------------------------


class TestToolDefinitions:
    def test_both_tools_are_defined(self):
        names = {t.name for t in ConsultantBridge(FakeConsultant()).build_tools()}
        assert names == {"second_opinion", "generate_image"}

    def test_required_arguments(self):
        tools = {t.name: t for t in ConsultantBridge(FakeConsultant()).build_tools()}
        assert tools["second_opinion"].input_schema["required"] == ["question"]
        assert tools["generate_image"].input_schema["required"] == ["prompt"]

    def test_the_description_says_it_cannot_read_the_repo(self):
        """The failure mode if it does not: an agent asks for a second
        opinion on a file it never pasted, and gets a confident answer
        about nothing."""
        tools = {t.name: t for t in ConsultantBridge(FakeConsultant()).build_tools()}
        assert "no repository access" in tools["second_opinion"].description

    def test_availability_is_readable_without_calling(self):
        """AG-9 applied to a tool definition: two tools that always answer
        "no credentials" cost context every turn and buy nothing."""
        assert ConsultantBridge(FakeConsultant(available=True)).available is True
        assert ConsultantBridge(FakeConsultant(available=False)).available is False


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------


class TestSecondOpinion:
    @pytest.mark.asyncio
    async def test_the_answer_is_passed_through_verbatim(self):
        """Summarising it would put this module's judgement between two
        models that are supposed to disagree in front of the user."""
        fake = FakeConsultant(answer="This diff leaks a file handle.")
        result = await ConsultantBridge(fake).second_opinion("Safe?", "code")
        assert "This diff leaks a file handle." in body(result)

    @pytest.mark.asyncio
    async def test_it_is_framed_as_evidence_not_a_verdict(self):
        result = await ConsultantBridge(FakeConsultant(answer="No.")).second_opinion(
            "Safe?"
        )
        assert "not as a verdict" in body(result)

    @pytest.mark.asyncio
    async def test_context_reaches_the_consultant(self):
        fake = FakeConsultant(answer="ok")
        await ConsultantBridge(fake).second_opinion("Why?", "def f(): pass")
        assert fake.calls == [("second_opinion", "Why?", "def f(): pass")]

    @pytest.mark.asyncio
    async def test_a_failure_is_text_not_an_exception(self):
        """The caller is a model. It cannot act on a stack trace."""
        fake = FakeConsultant(raises=ConsultationError("the model refused"))
        result = await ConsultantBridge(fake).second_opinion("Why?")
        assert "the model refused" in body(result)

    @pytest.mark.asyncio
    async def test_a_missing_credential_explains_itself(self):
        fake = FakeConsultant(
            raises=MissingCredentialsError("no GEMINI_API_KEY; agy's login cannot be used")
        )
        result = await ConsultantBridge(fake).second_opinion("Why?")
        assert "agy's login cannot be used" in body(result)


class TestGenerateImage:
    IMAGE = ImageResult(
        path="docs/architecture.png",
        absolute_path="/repo/docs/architecture.png",
        bytes_written=20481,
        contained=True,
        summary="A layered diagram.",
    )

    @pytest.mark.asyncio
    async def test_it_reports_the_repo_relative_path(self):
        """Which is the only form the agent can reference from markdown."""
        result = await ConsultantBridge(
            FakeConsultant(image=self.IMAGE)
        ).generate_image("a diagram")
        assert "docs/architecture.png" in body(result)
        assert "20,481 bytes" in body(result)

    @pytest.mark.asyncio
    async def test_the_absolute_path_is_not_what_the_agent_is_handed(self):
        result = await ConsultantBridge(
            FakeConsultant(image=self.IMAGE)
        ).generate_image("a diagram")
        assert "/repo/docs" not in body(result)

    @pytest.mark.asyncio
    async def test_arguments_reach_the_consultant(self):
        fake = FakeConsultant(image=self.IMAGE)
        await ConsultantBridge(fake).generate_image("a duck", "duck.png", "16:9")
        assert fake.calls == [("generate_image", "a duck", "duck.png", "16:9")]

    @pytest.mark.asyncio
    async def test_a_diverted_write_reaches_the_agent_as_a_failure(self):
        """AG-R-3. The agent must not go on to reference a file that is
        not there, which is what a swallowed error would produce."""
        fake = FakeConsultant(
            raises=ConsultationError("wrote to /tmp/x.png, outside the repository")
        )
        result = await ConsultantBridge(fake).generate_image("a duck")
        assert "could not be generated" in body(result)
        assert "outside the repository" in body(result)
