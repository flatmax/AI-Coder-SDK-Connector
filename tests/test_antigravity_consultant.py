"""Tests for aic_dc.antigravity.consultant — AG-7's one-shot call.

Three things are worth testing here and one of them is unusual.

**Containment.** Neither consultation gets a general-purpose agent
pointed at the repository, and ``generate_image``'s allowlist is a
narrowing of AG-5 that was agreed rather than assumed. Both are assertions
about the config that reaches the SDK, so the real ``LocalAgentConfig`` is
constructed and only ``Agent`` is faked — a config we build wrong should
fail here rather than at a live turn.

**AG-R-3.** The tool reports where it wrote; ``agy`` was measured
reporting a successful write to a path it had diverted. So every claim
about the file is re-derived from the filesystem, and these tests are
mostly about the diverted, missing and empty cases.

**AG-R-9, as a test rather than a comment.** The risk register's tripwire
for "the consultant became the engine adapter" is a list of four names.
:class:`TestTheBoundaryHolds` reads this module's own syntax tree for
them, so the boundary fails the build instead of being noticed in review.

Offline: no harness process, no credentials, no network.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from aic_dc.antigravity import consultant as consultant_module
from aic_dc.antigravity.consultant import (
    Consultant,
    ConsultationError,
    ImageResult,
)
from aic_dc.antigravity.credentials import GEMINI_API, NONE, Credentials

pytest.importorskip(
    "google.antigravity",
    reason="google-antigravity is an optional extra (AG-R-10)",
)

KEYED = Credentials(mode=GEMINI_API, source="a test", api_key="k")
KEYLESS = Credentials(mode=NONE, source="no credential, on purpose")


# ----------------------------------------------------------------------
# A harness that records rather than runs
# ----------------------------------------------------------------------


class FakeResponse:
    """What ``Agent.chat`` returns, in the two shapes we read."""

    def __init__(self, text: str = "", chunks: list | None = None):
        self._text = text
        self._chunks = chunks or []
        self.stop_reason = type("Stop", (), {"name": "UNSPECIFIED"})()

    async def text(self) -> str:
        return self._text

    async def resolve(self) -> list:
        return self._chunks


class FakeAgent:
    """Records the config it was given; never starts anything.

    ``configs`` is a class attribute so a test can read what the
    consultant built without threading a fixture through the call.
    """

    configs: list = []
    responses: list = []

    def __init__(self, config):
        FakeAgent.configs.append(config)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, prompt):
        FakeAgent.prompts.append(prompt)
        return FakeAgent.responses.pop(0) if FakeAgent.responses else FakeResponse("ok")


FakeAgent.prompts = []


class ImageChunk:
    """A ``ToolResult`` carrying a ``GenerateImageResult``, duck-typed.

    Read by attribute in the module under test, so a stand-in with the
    same two attributes exercises the real path.
    """

    def __init__(self, output_path: str):
        self.result = type("R", (), {"output_path": output_path})()


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    """Replace ``Agent`` only. ``LocalAgentConfig`` stays real.

    That is the point: the config assembly is what these tests are about,
    and a fake config class would validate nothing.
    """
    FakeAgent.configs = []
    FakeAgent.prompts = []
    FakeAgent.responses = []
    monkeypatch.setattr("google.antigravity.Agent", FakeAgent)
    return FakeAgent


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture
def consultant(repo):
    return Consultant(repo, credentials=KEYED)


# ----------------------------------------------------------------------
# AG-R-9: the boundary, checked rather than described
# ----------------------------------------------------------------------


class TestTheBoundaryHolds:
    """``risks.md`` AG-R-9's tripwire, as a test.

    "``receive_steps``, ``cancel``, ``conversation_id`` or a hook
    registration appearing in the consultant module. Any of them means
    the boundary has moved." A comment saying so would be noticed in
    review at best; this fails the build.
    """

    FORBIDDEN = ("receive_steps", "cancel", "conversation_id")

    def _names(self) -> set[str]:
        source = Path(inspect.getfile(consultant_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ClassDef):
                names.update(
                    b.id for b in node.bases if isinstance(b, ast.Name)
                )
        return names

    @pytest.mark.parametrize("name", FORBIDDEN)
    def test_no_session_machinery(self, name):
        assert name not in self._names(), (
            f"{name} appears in consultant.py. AG-R-9: the consultant has "
            "become the engine adapter, which means phase 3 will inherit a "
            "shape built for a call pattern it does not have."
        )

    def test_no_hook_registration(self):
        """A hook is the other half of the same tripwire."""
        names = self._names()
        hooks = {n for n in names if n.endswith("Hook")}
        assert not hooks, f"hook machinery in the consultant: {sorted(hooks)}"

    def test_it_really_is_one_shot(self):
        """One ``Agent(...)`` construction, in one function.

        The structural claim the module docstring makes. If the SDK is
        entered from more than one place, "one-shot" has stopped being
        checkable by reading twenty lines. Counted from the syntax tree,
        not the text — the docstring names ``Agent(config)`` too, and a
        prose mention is not a second call site.
        """
        source = Path(inspect.getfile(consultant_module)).read_text(encoding="utf-8")
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        ]
        assert len(calls) == 1


# ----------------------------------------------------------------------
# Containment
# ----------------------------------------------------------------------


class TestSecondOpinionIsContained:
    @pytest.mark.asyncio
    async def test_no_tools_beyond_finish(self, consultant, fake_agent):
        """A second opinion has no business reading the tree.

        Two independent agents disagreeing about a diff is information;
        one agent given a second chance to browse the repository is not.
        """
        await consultant.second_opinion("Is this diff safe?")
        config = fake_agent.configs[0]
        enabled = [t.name for t in config.capabilities.enabled_tools]
        assert enabled == ["FINISH"]

    @pytest.mark.asyncio
    async def test_the_allowlist_is_set_even_with_no_write_tools(
        self, consultant, fake_agent
    ):
        """Unset ``policies`` is not "no policy" — it is approve-all.

        ``LocalAgentConfig`` defaults to ``confirm_run_command()``: deny
        ``run_command``, approve **everything else**. That is the blanket
        bypass AG-5 says must never ship, arriving as a default nobody
        chose. Enabling only ``FINISH`` makes it inert today, and this
        test exists so that stays true by construction rather than by
        coincidence the first time a tool is added.
        """
        await consultant.second_opinion("Anything?")
        policies = fake_agent.configs[0].policies
        assert {(p.tool, p.decision.name) for p in policies} == {
            ("*", "DENY"),
            ("finish", "APPROVE"),
        }

    def test_the_sdk_default_really_is_approve_all(self):
        """Pins the finding the test above defends against.

        Read from the SDK rather than asserted from memory, so a release
        that fixes the default turns this red and the argument above can
        be revisited instead of quietly outliving its reason.
        """
        from google.antigravity import LocalAgentConfig

        default = LocalAgentConfig(model="m", api_key="k").policies
        assert {(p.tool, p.decision.name) for p in default} == {
            ("run_command", "DENY"),
            ("*", "APPROVE"),
        }

    @pytest.mark.asyncio
    async def test_context_is_passed_in_not_fetched(self, consultant, fake_agent):
        await consultant.second_opinion("Why?", context="def f(): pass")
        assert "def f(): pass" in fake_agent.prompts[0]

    @pytest.mark.asyncio
    async def test_an_empty_question_is_refused_before_the_network(
        self, consultant, fake_agent
    ):
        with pytest.raises(ConsultationError):
            await consultant.second_opinion("   ")
        assert not fake_agent.configs, "nothing should have been started"

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_an_error_not_an_empty_string(
        self, consultant, fake_agent
    ):
        """An empty second opinion rendered as a tool result reads as
        agreement, which is the one thing it is not."""
        fake_agent.responses = [FakeResponse("   ")]
        with pytest.raises(ConsultationError, match="empty answer"):
            await consultant.second_opinion("Well?")


class TestGenerateImageIsContained:
    @pytest.mark.asyncio
    async def test_only_the_image_tool_is_enabled(self, consultant, fake_agent, repo):
        fake_agent.responses = [
            FakeResponse("done", [ImageChunk(str(repo / "out.png"))])
        ]
        (repo / "out.png").write_bytes(b"\x89PNG\r\n")
        await consultant.generate_image("a duck")
        enabled = {t.name for t in fake_agent.configs[0].capabilities.enabled_tools}
        assert enabled == {"FINISH", "GENERATE_IMAGE"}

    @pytest.mark.asyncio
    async def test_the_allowlist_is_minimal_and_denies_by_default(
        self, consultant, fake_agent, repo
    ):
        """AG-5 narrowed, not reversed.

        ``generate_image`` is a write tool, so ``Agent.__aenter__`` will
        not start without a policy or a decide hook. It gets the smallest
        possible one: deny everything, then allow the single tool the call
        exists to invoke. ``allow_all`` must appear nowhere.
        """
        fake_agent.responses = [
            FakeResponse("done", [ImageChunk(str(repo / "out.png"))])
        ]
        (repo / "out.png").write_bytes(b"\x89PNG\r\n")
        await consultant.generate_image("a duck")

        policies = fake_agent.configs[0].policies
        decisions = {(p.tool, p.decision.name) for p in policies}
        assert decisions == {
            ("*", "DENY"),
            ("finish", "APPROVE"),
            ("generate_image", "APPROVE"),
        }
        assert not any(
            p.decision.name == "APPROVE" and p.tool == "*" for p in policies
        ), "allow_all must never reach a shipped path"

    @pytest.mark.asyncio
    async def test_workspaces_is_the_repo_root_and_nothing_else(
        self, consultant, fake_agent, repo
    ):
        """AG-10. The SDK defaults to ``os.getcwd()``, which happens to be
        the same value and is not a promise."""
        fake_agent.responses = [
            FakeResponse("done", [ImageChunk(str(repo / "out.png"))])
        ]
        (repo / "out.png").write_bytes(b"\x89PNG\r\n")
        await consultant.generate_image("a duck")
        assert fake_agent.configs[0].workspaces == [str(repo.resolve())]

    @pytest.mark.asyncio
    async def test_an_empty_prompt_is_refused_before_the_network(
        self, consultant, fake_agent
    ):
        with pytest.raises(ConsultationError):
            await consultant.generate_image("")
        assert not fake_agent.configs


# ----------------------------------------------------------------------
# AG-R-3: believe the filesystem, not the tool
# ----------------------------------------------------------------------


class TestImageWriteIsVerified:
    """The tool's own success report is not evidence.

    ``agy`` reported a successful write, with a ``file://`` link, for a
    file it had diverted into a scratch directory under ``~/.gemini/``.
    """

    @pytest.mark.asyncio
    async def test_a_contained_write_returns_a_repo_relative_path(
        self, consultant, fake_agent, repo
    ):
        target = repo / "src" / "duck.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        fake_agent.responses = [FakeResponse("saved it", [ImageChunk(str(target))])]

        result = await consultant.generate_image("a duck", output_name="duck.png")
        assert isinstance(result, ImageResult)
        assert result.path == "src/duck.png", "the file tree addresses files this way"
        assert result.absolute_path == str(target)
        assert result.bytes_written == 8
        assert result.contained is True

    @pytest.mark.asyncio
    async def test_a_diverted_write_is_a_hard_failure(
        self, consultant, fake_agent, tmp_path
    ):
        """AG-R-3 itself. The success report is cheerful and wrong.

        A "success" the file tree cannot reach is worse than a failure,
        because the user goes looking for a file that is not there.
        """
        elsewhere = tmp_path.parent / "scratch-duck.png"
        elsewhere.write_bytes(b"\x89PNG")
        fake_agent.responses = [
            FakeResponse("Saved!", [ImageChunk(str(elsewhere))])
        ]
        with pytest.raises(ConsultationError, match="outside the repository"):
            await consultant.generate_image("a duck")

    @pytest.mark.asyncio
    async def test_the_error_names_the_real_path(
        self, consultant, fake_agent, tmp_path
    ):
        """Diagnosable without reading someone else's settings file."""
        elsewhere = tmp_path.parent / "diverted.png"
        elsewhere.write_bytes(b"x")
        fake_agent.responses = [FakeResponse("ok", [ImageChunk(str(elsewhere))])]
        with pytest.raises(ConsultationError) as exc:
            await consultant.generate_image("a duck")
        assert str(elsewhere) in str(exc.value)
        assert "AG-R-3" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_reported_path_with_no_file_is_a_failure(
        self, consultant, fake_agent, repo
    ):
        fake_agent.responses = [
            FakeResponse("ok", [ImageChunk(str(repo / "ghost.png"))])
        ]
        with pytest.raises(ConsultationError, match="no file"):
            await consultant.generate_image("a duck")

    @pytest.mark.asyncio
    async def test_an_empty_file_is_a_failure_that_looks_like_success(
        self, consultant, fake_agent, repo
    ):
        (repo / "empty.png").write_bytes(b"")
        fake_agent.responses = [
            FakeResponse("ok", [ImageChunk(str(repo / "empty.png"))])
        ]
        with pytest.raises(ConsultationError, match="empty"):
            await consultant.generate_image("a duck")

    @pytest.mark.asyncio
    async def test_no_image_at_all_reports_what_the_model_said(
        self, consultant, fake_agent
    ):
        """The model's own refusal is the useful half of this error."""
        fake_agent.responses = [FakeResponse("I can't generate that.", [])]
        with pytest.raises(ConsultationError, match="I can't generate that"):
            await consultant.generate_image("a duck")

    @pytest.mark.asyncio
    async def test_a_relative_path_resolves_against_the_workspace(
        self, consultant, fake_agent, repo
    ):
        """Which is the only root there is, per AG-10."""
        (repo / "rel.png").write_bytes(b"\x89PNG")
        fake_agent.responses = [FakeResponse("ok", [ImageChunk("rel.png")])]
        result = await consultant.generate_image("a duck")
        assert result.path == "rel.png"

    @pytest.mark.asyncio
    async def test_a_moved_result_shape_reads_as_no_image(
        self, consultant, fake_agent
    ):
        """This is an alpha SDK and ``resolve()`` returns a union.

        A result object whose shape changed must produce the clear "no
        image" error, not an AttributeError from inside a tool call.
        """
        fake_agent.responses = [
            FakeResponse("ok", [object(), type("T", (), {"result": None})()])
        ]
        with pytest.raises(ConsultationError, match="no image file"):
            await consultant.generate_image("a duck")


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------


class TestCredentials:
    def test_availability_is_readable_without_calling(self, repo):
        assert Consultant(repo, credentials=KEYED).available is True
        assert Consultant(repo, credentials=KEYLESS).available is False

    def test_the_report_carries_no_secret(self, repo):
        report = Consultant(repo, credentials=KEYED).credentials.report()
        assert "k" not in report["source"]
        assert "api_key" not in report

    @pytest.mark.asyncio
    async def test_a_missing_credential_fails_before_the_harness_starts(
        self, repo, fake_agent
    ):
        """AG-R-8, arriving as our error rather than the SDK's.

        ``validate_endpoint`` would raise on the connect path — the right
        shape, the wrong message, after a 119 MB binary has been spawned.
        """
        from aic_dc.antigravity.credentials import MissingCredentialsError

        target = Consultant(repo, credentials=KEYLESS)
        with pytest.raises(MissingCredentialsError):
            await target.second_opinion("anything?")
        assert not fake_agent.configs, "the harness must not have been started"

    @pytest.mark.asyncio
    async def test_the_key_reaches_the_config(self, repo, fake_agent):
        await Consultant(repo, credentials=KEYED).second_opinion("hi")
        assert fake_agent.configs[0].api_key == "k"


class TestModelPinning:
    @pytest.mark.asyncio
    async def test_text_and_image_use_different_models(
        self, repo, fake_agent
    ):
        """``ModelType.IMAGE`` is a separate target; a shared default
        would silently ask a text model to draw."""
        (repo / "a.png").write_bytes(b"\x89PNG")
        target = Consultant(repo, credentials=KEYED)
        fake_agent.responses = [
            FakeResponse("x"),
            FakeResponse("ok", [ImageChunk(str(repo / "a.png"))]),
        ]
        await target.second_opinion("hi")
        await target.generate_image("a duck")
        text_model, image_model = (c.model for c in fake_agent.configs)
        assert text_model != image_model
        assert "image" in image_model
