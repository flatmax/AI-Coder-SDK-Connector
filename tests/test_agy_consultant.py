"""Tests for the consultant that runs over ``agy`` — AG-16.

Two things carry this file, and neither is "does it return text".

**The containment.** The SDK consultant restricts itself by *enabling*
only the tools a consultation needs; that option does not exist here,
because ``agy``'s tool set belongs to the binary and the flag that lets
AIC⚡DC review it is ``--dangerously-skip-permissions``. So the whole
restriction is a :class:`~aic_dc.agy.gate_server.StaticPolicy` and a
registry claim, and a consultation that ran *unclaimed* would be an agent
with 57 tools and the repository as its working directory. Hence
:class:`TestItWillNotRunUngated`, which is the AG-5 assertion for this
transport.

**The turn it must not end.** A consultation happens inside a Claude turn
that is blocked on the tool call. ``AgySession.stream_turn`` finishes by
emitting ``streamComplete``, which tells the browser a turn is over — so
the consultant reads ``stream_frames`` instead, and
:class:`TestItDoesNotEndTheTurnThatAskedIt` is what stops anybody
"simplifying" it back.

Driven by the same kind of fake ``agy`` as ``test_agy_session.py``: a real
subprocess speaking the real frame shapes, because the handshake's order
and the pipes are what an in-process fake would not exercise.

Offline. No real ``agy``, no network, no Google.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from aic_dc.agy import registry
from aic_dc.agy.consultant import (
    CONTROL_TOOLS,
    IMAGE_POLICY,
    SECOND_OPINION_POLICY,
    AgyConsultant,
    choose_consultant,
)
from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.antigravity.consultant import ConsultationError

CONV = "0d9d1f3a-6c1e-4a51-9b0e-3f5f0f1b2c34"


def _fake_agy(
    *,
    image_path: str | None = None,
    answer: str = "It depends.",
    log: str = "",
) -> str:
    """A fake ``agy``: init, prose, optionally an image tool call, result.

    ``image_path`` is written by the fake itself, which is the point —
    :func:`~aic_dc.antigravity.consultant.verify_image_write` stats the
    file rather than believing the frame, so a fake that only *claimed* to
    write would fail the same way a diverted real one does.

    **The frame this emits was wrong until 2026-09-08**, and wrong in the
    way AG-16 predicted an offline test would be: it carried an
    ``OutputPath`` parameter, because that is the shape the code assumed.
    The first live run showed the real call carries ``ImageName`` and
    ``Prompt`` and no path at all — the harness picks the location. So the
    fake now writes where ``agy`` writes and names the arguments ``agy``
    names, and the collection is what is under test.
    """
    tool = ""
    if image_path is not None:
        tool = textwrap.dedent(
            f'''
            path = {image_path!r}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(b"PNG-ish bytes")
            emit({{"event": "step_update", "step_update": {{
                "step_index": 2, "state": "DONE", "step_type": "tool",
                "conversation_id": conv,
                "tool_name": "generate_image",
                "tool_info": {{"name": "generate_image",
                              "parameters": {{"ImageName": "hero",
                                             "Prompt": "a hero image"}},
                              "output": "done"}}}}}})
            '''
        )
    return textwrap.dedent(
        '''
        import json, sys, os
        conv = "{conv}"
        log = {log!r}
        def emit(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        emit({{"event": "init", "conversation_id": conv,
              "init": {{"cwd": os.getcwd(), "tools": ["generate_image"]}}}})
        for line in sys.stdin:
            if not line.strip():
                continue
            json.loads(line)
            if log:
                open(log, "a").write(line)
            emit({{"event": "step_update", "step_update": {{
                "step_index": 1, "state": "DONE",
                "step_type": "agent_response", "text_delta": {answer!r}}}}})
        {tool}
            emit({{"event": "result", "result": {{
                "conversation_id": conv, "status": "SUCCESS",
                "response": {answer!r}, "usage": {{"total_tokens": 7}}}}}})
        '''
    ).format(
        conv=CONV,
        answer=answer,
        log=log,
        tool=textwrap.indent(tool, "    ").strip("\n"),
    )


def brain_image(tmp_path, name: str = "hero_1788851691210.jpg"):
    """Where ``agy`` would put an image, in the fake brain directory.

    ``brain/<conversation_id>/<ImageName>_<epoch_ms>.jpg`` — measured on
    2026-09-08, and the reason the consultant collects rather than asks.
    """
    return tmp_path / "brain" / CONV / name


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """An :class:`AgyConsultant` wired to a fake binary and a live gate.

    The gate is reported ``current`` rather than actually installed: the
    hook's own installation is ``test_agy_install.py``'s subject, and what
    matters here is that this refuses to run when it is *not*.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "cfg"
    # The image is collected out of agy's own per-conversation directory,
    # so a test that did not move this would read the developer's real one.
    monkeypatch.setattr("aic_dc.agy.consultant.BRAIN_DIR", tmp_path / "brain")

    def build(
        *,
        image_path: str | None = None,
        answer: str = "It depends.",
        log: str = "",
    ):
        fake = tmp_path / "fake_agy.py"
        fake.write_text(
            _fake_agy(image_path=image_path, answer=answer, log=log), "utf-8"
        )
        launcher = tmp_path / "agy"
        launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {fake}\n", "utf-8")
        launcher.chmod(0o755)
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "/fake/hooks.json"},
        )
        monkeypatch.setattr(
            "aic_dc.agy.consultant.shutil.which", lambda _n: str(launcher)
        )
        return AgyConsultant(
            repo, config_dir=config_dir, executable=str(launcher)
        )

    return build, repo, config_dir


class Recorder:
    """The bridge's observer, as the consultant sees it."""

    def __init__(self, translator):
        self.translator = translator
        self.frames: list = []
        self.events: list = []

    def __call__(self, frame):
        self.frames.append(frame)
        self.events.extend(self.translator.translate(frame))


# ----------------------------------------------------------------------
# The one that matters: it cannot run without a gate
# ----------------------------------------------------------------------


class TestItWillNotRunUngated:
    """AG-5, for a transport whose tool set is not ours to restrict."""

    def test_a_missing_gate_makes_it_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "absent", "path": "/fake/hooks.json"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        assert AgyConsultant(tmp_path).available is False

    def test_a_stale_gate_is_not_good_enough(self, tmp_path, monkeypatch):
        """`stale` means another checkout's hook: our calls are not gated."""
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "stale", "path": "/fake/hooks.json"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        assert AgyConsultant(tmp_path).available is False

    def test_it_refuses_to_run_rather_than_running_unreviewed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "absent", "path": "/fake/hooks.json"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant = AgyConsultant(tmp_path)
        with pytest.raises(ConsultationError) as caught:
            asyncio.run(consultant.second_opinion("does this hold?"))
        assert "permission gate is not installed" in str(caught.value)

    def test_a_missing_binary_says_so_rather_than_blaming_the_gate(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        consultant = AgyConsultant(tmp_path)
        assert consultant.available is False
        assert "not on PATH" in consultant._unavailable_reason()


# ----------------------------------------------------------------------
# The static policy — a capability restriction, not a permission decision
# ----------------------------------------------------------------------


class TestTheStaticPolicy:
    def _server(self, tmp_path, policy):
        return AgyGateServer(tmp_path / "c.sock", policy=policy, config_dir=tmp_path)

    def _decide(self, server, tool):
        return asyncio.run(
            server.decide({"conversationId": CONV, "toolCall": {"name": tool}})
        )

    def test_a_second_opinion_is_allowed_no_tools_at_all(self, tmp_path):
        server = self._server(tmp_path, SECOND_OPINION_POLICY)
        for tool in ("view_file", "run_command", "grep_search", "write_to_file"):
            assert self._decide(server, tool)["decision"] == "deny", tool

    def test_reads_are_denied_too_and_that_is_deliberate(self, tmp_path):
        """The SDK consultant enables *no* tools; this is the nearest posture.

        A read looks harmless and is not the point: the value of a second
        opinion is that it reasoned about what it was given, so an agent
        that goes and reads the repository is answering a different
        question from the one that was asked.
        """
        server = self._server(tmp_path, SECOND_OPINION_POLICY)
        assert self._decide(server, "view_file")["decision"] == "deny"

    def test_an_image_consultation_allows_exactly_one_tool(self, tmp_path):
        server = self._server(tmp_path, IMAGE_POLICY)
        assert self._decide(server, "generate_image")["decision"] == "allow"
        for tool in ("run_command", "write_to_file", "view_file"):
            assert self._decide(server, tool)["decision"] == "deny", tool

    def test_finish_is_allowed_because_denying_it_reads_as_a_hang(self, tmp_path):
        for policy in (SECOND_OPINION_POLICY, IMAGE_POLICY):
            server = self._server(tmp_path, policy)
            for tool in CONTROL_TOOLS:
                assert self._decide(server, tool)["decision"] == "allow"

    def test_the_refusal_tells_the_model_to_answer_not_to_reroute(self, tmp_path):
        """AG-R-11's mechanism, used the useful way round."""
        server = self._server(tmp_path, SECOND_OPINION_POLICY)
        reason = self._decide(server, "run_command")["reason"]
        assert "Answer from the question" in reason
        assert "do not look for another way" in reason.lower()

    def test_it_never_reaches_the_broker(self, tmp_path):
        """No dialog: the user answered when they approved the MCP call.

        Asserted by giving the server a gate that explodes if touched —
        the same shape phase 9's exit criterion uses, because "no dialog
        appeared" is also what a call that never happened looks like.
        """

        class Explodes:
            @property
            def broker(self):  # pragma: no cover - reaching it is the failure
                raise AssertionError("a consultation must not raise a dialog")

            def pre_verdict(self, *_a):  # pragma: no cover - same
                raise AssertionError("a consultation must not consult the gate")

        server = AgyGateServer(
            tmp_path / "c.sock", policy=SECOND_OPINION_POLICY, config_dir=tmp_path
        )
        server._gate = Explodes()
        assert self._decide(server, "run_command")["decision"] == "deny"

    def test_a_server_with_neither_posture_is_refused_at_construction(self, tmp_path):
        with pytest.raises(ValueError, match="neither"):
            AgyGateServer(tmp_path / "c.sock")

    def test_a_server_with_both_postures_is_refused_at_construction(self, tmp_path):
        with pytest.raises(ValueError, match="both"):
            AgyGateServer(
                tmp_path / "c.sock",
                gate=object(),
                policy=SECOND_OPINION_POLICY,
            )

    def test_a_stop_still_beats_the_allowlist(self, tmp_path):
        """⏹ is answered before anything else, on both postures."""
        server = self._server(tmp_path, IMAGE_POLICY)
        server.refuse_all("The user stopped this.")
        assert self._decide(server, "generate_image")["decision"] == "deny"

    def test_the_asking_gate_says_it_asks_and_this_one_does_not(self, tmp_path):
        assert self._server(tmp_path, IMAGE_POLICY).asks is False


# ----------------------------------------------------------------------
# A consultation, end to end against a fake binary
# ----------------------------------------------------------------------


class TestASecondOpinion:
    def test_it_returns_the_models_prose(self, gated):
        build, _repo, _cfg = gated
        consultant = build(answer="I would not merge this.")
        assert asyncio.run(consultant.second_opinion("well?")) == (
            "I would not merge this."
        )

    def test_an_empty_question_is_refused_before_a_process_is_spawned(self, gated):
        build, _repo, _cfg = gated
        with pytest.raises(ConsultationError, match="needs a question"):
            asyncio.run(build().second_opinion("   "))

    def test_the_context_is_appended_to_the_question(self, gated):
        """It has no repository access, so what it is given is all it has."""
        build, _repo, _cfg = gated
        consultant = build()
        recorder = Recorder(consultant.make_translator("r1"))
        asyncio.run(consultant.second_opinion("why?", "def f(): pass", recorder))
        sent = [f for f in recorder.frames if f.get("event") == "step_update"]
        assert sent, "the fake echoed nothing, so the prompt never arrived"

    def test_the_claim_is_released_afterwards(self, gated):
        """A consultation that kept its claim would gate the user's own agy."""
        build, _repo, config_dir = gated
        asyncio.run(build().second_opinion("well?"))
        assert registry.lookup(CONV, config_dir=config_dir) is None


class TestItDoesNotEndTheTurnThatAskedIt:
    """``streamComplete`` carries a request id, and the open turn is Claude's."""

    def test_no_stream_complete_reaches_the_tab(self, gated):
        build, _repo, _cfg = gated
        consultant = build()
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))
        assert "streamComplete" not in [e.name for e in recorder.events]

    def test_the_text_still_arrives_in_the_tab(self, gated):
        build, _repo, _cfg = gated
        consultant = build(answer="Two problems.")
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))
        chunks = [e for e in recorder.events if e.name == "streamChunk"]
        assert chunks, "the consultation rendered nothing"
        assert chunks[-1].payload["content"] == "Two problems."

    def test_every_block_is_attributed_to_the_consultation(self, gated):
        """AG-13: this is the whole of what puts the text in its own tab."""
        build, _repo, _cfg = gated
        consultant = build()
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))
        scoped = [e for e in recorder.events if "agent_id" in e.payload]
        assert scoped
        assert all(e.payload["agent_id"] == "consultation-1" for e in scoped)


class TestAnImage:
    """Collected, not requested — the correction the first live run bought.

    ``agy``'s ``generate_image`` takes ``ImageName``, not a path, and writes
    into ``~/.gemini/antigravity-cli/brain/<conversation_id>/``. So the
    subject of these tests is the *collection*: what the consultant does
    after a turn that generated a picture somewhere the file tree cannot
    see.
    """

    def test_it_collects_the_image_into_the_repository(self, gated, tmp_path):
        build, repo, _cfg = gated
        consultant = build(image_path=str(brain_image(tmp_path)))
        result = asyncio.run(
            consultant.generate_image("a hero image", output_name="docs/hero.png")
        )
        assert result.path == "docs/hero.jpg"
        assert result.bytes_written > 0
        assert result.contained is True
        assert (repo / "docs" / "hero.jpg").is_file()

    def test_the_produced_extension_wins_over_the_requested_one(
        self, gated, tmp_path
    ):
        """A ``.png`` holding JPEG bytes is a second lie told to tidy the first."""
        build, repo, _cfg = gated
        consultant = build(image_path=str(brain_image(tmp_path)))
        result = asyncio.run(
            consultant.generate_image("a hero image", output_name="icon.png")
        )
        assert result.path == "icon.jpg"
        assert not (repo / "icon.png").exists()

    def test_an_image_it_cannot_find_is_loud_rather_than_silent(
        self, gated, tmp_path
    ):
        """AG-R-3's shape on this transport, now that the destination is ours.

        The consultant chooses where the file goes, so a *diverted* write
        cannot land outside the repository any more — it fails one step
        earlier, as an image that was generated and is not in the
        conversation's directory. The wording has to distinguish that from
        "no image was generated", because the two send a reader to
        different places.
        """
        build, _repo, _cfg = gated
        diverted = tmp_path / "scratch" / "hero.jpg"
        consultant = build(image_path=str(diverted))
        with pytest.raises(ConsultationError) as caught:
            asyncio.run(consultant.generate_image("a hero image"))
        assert "could not be found" in str(caught.value)
        assert "generated no image file" not in str(caught.value)

    def test_it_does_not_ask_the_model_to_place_the_file(self, gated, tmp_path):
        """The instruction that caused the denied ``run_command``, removed.

        The first live run told the model to *"write it inside <repo> and
        report the absolute path"* — which the tool cannot do, so the model
        reached for ``run_command`` to move the file and the policy denied
        it. Correct, and avoidable: the prompt no longer asks.
        """
        build, _repo, _cfg = gated
        log = tmp_path / "prompt.jsonl"
        consultant = build(image_path=str(brain_image(tmp_path)), log=str(log))
        asyncio.run(consultant.generate_image("a hero image", output_name="icon.png"))
        sent = log.read_text("utf-8")
        assert "do not try to move, copy or save it" in sent.lower()
        assert str(consultant._repo_root) not in sent

    def test_the_shared_table_cannot_answer_this_and_that_is_the_finding(self):
        """Why the collector exists rather than a third spelling in the table.

        ``files_written_by`` maps a tool to its *path argument*.
        ``generate_image`` has none: the real call carries ``ImageName`` and
        ``Prompt``, and ``output_path`` — which the table names — is a
        **result** field that only looks like an argument on the SDK
        transport, because that stream merges results back into ``args`` at
        ``DONE``. Adding ``ImageName`` here would encode the wrong belief:
        it is a name, not a path, and no file exists at it.
        """
        from aic_dc.claude_code.messages import files_written_by

        real = {"ImageName": "hero", "Prompt": "a hero image"}
        assert files_written_by("generate_image", real) == []

    def test_a_turn_that_never_called_the_tool_is_not_an_image(self, gated):
        """Prose claiming success is not a picture."""
        build, _repo, _cfg = gated
        consultant = build(answer="Here is your image!")
        with pytest.raises(ConsultationError, match="generated no image file"):
            asyncio.run(consultant.generate_image("a hero image"))

    def test_an_empty_prompt_is_refused(self, gated):
        build, _repo, _cfg = gated
        with pytest.raises(ConsultationError, match="needs a prompt"):
            asyncio.run(build().generate_image(""))


# ----------------------------------------------------------------------
# Choosing a transport
# ----------------------------------------------------------------------


class _Stub:
    def __init__(self, available):
        self.available = available


class TestChoosingATransport:
    def test_auto_prefers_agy_because_a_free_key_cannot_make_images(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant, why = choose_consultant(tmp_path, config_dir=tmp_path)
        assert isinstance(consultant, AgyConsultant)
        assert "subscription" in why

    def test_it_falls_back_to_the_sdk_when_agy_cannot_run(
        self, tmp_path, monkeypatch
    ):
        from aic_dc.antigravity.consultant import Consultant

        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        monkeypatch.setattr(Consultant, "available", property(lambda _s: True))
        consultant, why = choose_consultant(tmp_path, config_dir=tmp_path)
        assert isinstance(consultant, Consultant)
        assert "not on PATH" in why

    def test_neither_available_names_both_reasons(self, tmp_path, monkeypatch):
        from aic_dc.antigravity.consultant import Consultant

        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        monkeypatch.setattr(Consultant, "available", property(lambda _s: False))
        consultant, why = choose_consultant(tmp_path, config_dir=tmp_path)
        assert consultant is None
        assert "not on PATH" in why
        assert "Gemini API key" in why

    def test_naming_sdk_does_not_silently_use_agy(self, tmp_path, monkeypatch):
        """An explicit choice that cannot run is an explicit failure."""
        from aic_dc.antigravity.consultant import Consultant

        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        monkeypatch.setattr(Consultant, "available", property(lambda _s: False))
        consultant, why = choose_consultant(
            tmp_path, config_dir=tmp_path, transport="sdk"
        )
        assert consultant is None
        assert "app.json names the SDK" in why

    def test_naming_agy_does_not_silently_use_the_sdk(self, tmp_path, monkeypatch):
        from aic_dc.antigravity.consultant import Consultant

        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        monkeypatch.setattr(Consultant, "available", property(lambda _s: True))
        consultant, why = choose_consultant(
            tmp_path, config_dir=tmp_path, transport="agy"
        )
        assert consultant is None
        assert "app.json names the agy" in why


class TestWhoPays:
    def test_it_reports_a_subscription_rather_than_a_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        credentials = AgyConsultant(tmp_path).credentials
        assert credentials.available is True
        assert "sign-in" in credentials.source
        assert credentials.api_key is None

    def test_it_carries_no_secret_into_the_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        report = AgyConsultant(tmp_path).credentials.report()
        assert json.dumps(report)
        assert report["mode"] == "agy-oauth"
