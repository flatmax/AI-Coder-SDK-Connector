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
import re
import sys
import textwrap

import pytest

from aic_dc.agy import consultant as consultant_mod
from aic_dc.agy import registry
from aic_dc.agy.consultant import (
    CONTROL_TOOLS,
    IMAGE_POLICY,
    SECOND_OPINION_POLICY,
    AgyConsultant,
    choose_consultant,
)
from aic_dc.agy.gate_server import AgyGateServer, StaticPolicy
from aic_dc.antigravity.consultant import ConsultationError

CONV = "0d9d1f3a-6c1e-4a51-9b0e-3f5f0f1b2c34"


#: What a real ``agy`` says it has, cut down to the names this file cares
#: about. Measured on 1.2.2: 57 of them, ``finish`` among them, and the
#: inventory arrives in the ``init`` frame before any prompt is sent.
#:
#: ``finish`` is in the default because a fixture that omitted it would
#: describe a binary AG-R-22 is *about* rather than the one users have —
#: and every consultation run against it would carry the renamed-tool note
#: as though it were normal.
ADVERTISED = ("finish", "generate_image", "view_file", "read_url_content")


def _fake_agy(
    *,
    image_path: str | None = None,
    image_in_brain: str | None = None,
    denied: str = "",
    denied_error: str = "",
    answer: str = "It depends.",
    log: str = "",
    hold_until: str = "",
    advertises: tuple[str, ...] | None = ADVERTISED,
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

    **``image_in_brain`` resolves against the fake's own ``HOME``**, which
    is how AG-21 made this test honest. The brain directory used to be a
    module constant the fixture monkeypatched, and a fake pointed at an
    absolute path proved only that the collector could read the path it was
    handed. It is now derived from the config root the consultant spawned
    this process against — so if that plumbing breaks, this writes
    somewhere the collector does not look, and the test fails for the
    reason a user would.

    **``hold_until`` is what lets two consultations be in flight at once.**
    Every other mode here answers the moment the prompt lands, which is why
    nothing in this file ever held two — and a consultant that can only be
    caught running one at a time is one whose ⏹ nobody can aim. A prompt
    containing ``hold`` waits for that path to appear before its result
    frame, so a test decides when each consultation finishes: the one it
    releases answers, and the one it does not is still running when ⏹ is
    pressed.

    **``advertises`` is the binary's own tool inventory** (AG-R-22), and
    ``None`` is not the same as ``()``: ``None`` emits an ``init`` frame
    with no ``tools`` key at all, which is the older binary that says
    nothing about its vocabulary, while ``()`` emits an empty list. Both
    must leave the consultant claiming nothing about what is missing, and
    they are separable here so that a test can prove it of each.
    """
    tool = ""
    if hold_until:
        # Not a tool call: a pause placed exactly where the answer would be.
        # ``time.sleep`` rather than anything cleverer because this is a
        # separate process with one job, and the interval is only how fast
        # the release is noticed.
        tool = textwrap.dedent(
            f'''
            if "hold" in line:
                import time
                while not os.path.exists({hold_until!r}):
                    time.sleep(0.01)
            '''
        )
    elif denied:
        # The gate's own refusal, as `agy` reports it back: an ERROR step
        # whose message is the pre-tool hook's, transcribed from the P20
        # live capture (2026-09-12). `output` is absent, which is the
        # whole of AG-R-22 — the reason lives in `error.message` and a
        # pump that reads only `output` throws it away.
        #
        # **The reason is not written here — it is asked for.** This used
        # to interpolate ``SECOND_OPINION_POLICY.reason`` at generation
        # time, on the reasoning that quoting the policy beat inventing a
        # paraphrase. Then the policy stopped being a constant: it is
        # stamped per consultation with a nonce minted inside ``_run``,
        # and the pump authenticates a refusal by that nonce rather than
        # by the fixed mark, precisely because the mark is a string this
        # repository's own files contain and a model could echo. A fixture
        # that spelled the reason could not know the nonce, and would have
        # had to be handed one — which is testing the pump against a
        # string the test supplied to both sides.
        #
        # So the fake does what ``agy`` does: reads the hooks file from
        # the config root it was spawned against, runs the ``PreToolUse``
        # command, and reports whatever came back on its stdout. The
        # refusal therefore travels the real path — stamped policy, gate
        # server, socket, hook, wire — and the nonce in it is one nothing
        # in this file has seen.
        tool = textwrap.dedent(
            f'''
            message = {denied_error!r}
            if not message:
                import subprocess
                hooks = json.load(open(os.path.join(
                    os.environ["HOME"], ".gemini", "config", "hooks.json")))
                command = hooks["aic-dc-gate"]["PreToolUse"][0]["hooks"][0]["command"]
                asked = subprocess.run(
                    ["sh", "-c", command],
                    input=json.dumps({{"conversationId": conv, "toolCall": {{
                        "name": {denied!r},
                        "args": {{"Url": "https://www.kernel.org/"}}}}}}),
                    capture_output=True, text=True,
                )
                answer = json.loads(asked.stdout or "{{}}")
                if answer.get("decision") == "deny":
                    message = (
                        "tool call denied by pre-tool hook: " + answer["reason"]
                    )
                else:
                    # Allowed, and then failed on its own — which is a real
                    # shape and the one `test_a_failure_of_an_allowed_tool_
                    # is_not_a_refusal` is about. Emitted rather than
                    # raised: a fake that gave up here would leave that
                    # test asserting an absence of notices against a
                    # consultation that never drew a card.
                    message = "the tool ran and failed"
            emit({{"event": "step_update", "step_update": {{
                "step_index": 2, "state": "ERROR", "step_type": "tool",
                "conversation_id": conv,
                "tool_name": {denied!r},
                "tool_info": {{"name": {denied!r},
                              "parameters": {{"Url": "https://www.kernel.org/"}},
                              "error": {{"type": "TOOL_ERROR",
                                        "message": message}}}}}}}})
            '''
        )
    elif image_in_brain is not None:
        tool = textwrap.dedent(
            f'''
            path = os.path.join(
                os.environ["HOME"], ".gemini", "antigravity-cli", "brain",
                conv, {image_in_brain!r},
            )
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
    elif image_path is not None:
        # The diverted write: somewhere that is not this conversation's
        # directory, which is the one AG-R-3 shape still reachable here.
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
        init = {{"cwd": os.getcwd()}}
        if {advertises!r} is not None:
            init["tools"] = list({advertises!r})
        emit({{"event": "init", "conversation_id": conv, "init": init}})
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
        advertises=advertises,
        tool=textwrap.indent(tool, "    ").strip("\n"),
    )


BRAIN_IMAGE = "hero_1788851691210.jpg"
"""What ``agy`` names an image it generated.

``brain/<conversation_id>/<ImageName>_<epoch_ms>.jpg`` — measured on
2026-09-08, and the reason the consultant collects rather than asks. The
directory it lands in is no longer a constant a test can move: AG-21 made
it a property of the config root the consultation was spawned against, so
the fake resolves it from its own ``HOME``.
"""


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """An :class:`AgyConsultant` wired to a fake binary and a live gate.

    The hook command is reported runnable rather than actually probed —
    three interpreter startups per consultation would buy nothing here, and
    whether the command runs is ``test_agy_install.py``'s subject. What is
    *not* faked is the install: ``_run`` writes a real ``hooks.json`` into
    the real ephemeral root, because "an unhooked root is an ungated agent"
    is the invariant this file exists to defend.

    Nothing here monkeypatches a brain directory any more. The consultation
    gets its own config root under ``config_dir``, the fake ``agy`` reads
    ``HOME`` like the real one does, and the collector derives the path
    from the same root — so the plumbing is under test rather than stubbed
    out of the way.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "cfg"

    def build(
        *,
        image_path: str | None = None,
        image_in_brain: str | None = None,
        denied: str = "",
        denied_error: str = "",
        answer: str = "It depends.",
        log: str = "",
        hold_until: str = "",
        advertises: tuple[str, ...] | None = ADVERTISED,
        timeout_seconds: float | None = None,
    ):
        fake = tmp_path / "fake_agy.py"
        fake.write_text(
            _fake_agy(
                image_path=image_path,
                image_in_brain=image_in_brain,
                denied=denied,
                denied_error=denied_error,
                answer=answer,
                log=log,
                hold_until=hold_until,
                advertises=advertises,
            ),
            "utf-8",
        )
        launcher = tmp_path / "agy"
        launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {fake}\n", "utf-8")
        launcher.chmod(0o755)
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.hook_runs", lambda *_a, **_k: ""
        )
        monkeypatch.setattr(
            "aic_dc.agy.consultant.shutil.which", lambda _n: str(launcher)
        )
        extra = (
            {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
        )
        return AgyConsultant(
            repo, config_dir=config_dir, executable=str(launcher), **extra
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

    def test_a_hook_that_cannot_run_makes_it_unavailable(
        self, tmp_path, monkeypatch
    ):
        """AG-21 changed the question from *installed* to *runnable*.

        There is no standing hooks file to be absent from: a consultation
        writes its own into a root that does not exist until it starts. So
        the condition that used to read ``status() == "current"`` reads a
        probe of the command instead, and this is the probe failing.
        """
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.hook_runs",
            lambda *_a, **_k: "exit 127: no such file",
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        assert AgyConsultant(tmp_path).available is False

    def test_one_broken_event_is_enough(self, tmp_path, monkeypatch):
        """Three commands differ by an argument, and an argument is what the
        frozen build got wrong. A consultant that ran because *the gate*
        probed clean would be gated and unstoppable."""
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.hook_runs",
            lambda _c, *, event="PreToolUse", **_k: (
                "exit 2: unrecognized arguments" if event == "Stop" else ""
            ),
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        assert AgyConsultant(tmp_path).available is False

    def test_it_refuses_to_run_rather_than_running_unreviewed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.hook_runs",
            lambda *_a, **_k: "exit 127: no such file",
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant = AgyConsultant(tmp_path)
        with pytest.raises(ConsultationError) as caught:
            asyncio.run(consultant.second_opinion("does this hold?"))
        assert "permission gate cannot run" in str(caught.value)

    def test_a_missing_binary_says_so_rather_than_blaming_the_gate(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        consultant = AgyConsultant(tmp_path)
        assert consultant.available is False
        assert "not on PATH" in consultant._unavailable_reason()

    def test_constructing_one_retires_the_pre_ag21_entry(
        self, tmp_path, monkeypatch
    ):
        """The consultant does it too, and not as a duplicate.

        A user may never connect the `agy` engine and only ever ask for
        second opinions, in which case the service's copy of this never
        runs. Neither path can assume the other one did.
        """
        from aic_dc.agy import install as install_mod

        theirs = tmp_path / "their-hooks.json"
        monkeypatch.setattr(install_mod, "GLOBAL_HOOKS", theirs)
        install_mod.install(tmp_path / "old-cfg", path=theirs)

        AgyConsultant(tmp_path)

        assert not theirs.exists()

    def test_the_pinned_model_is_googles(self, tmp_path):
        """AG-R-15: a second opinion has to be a second *vendor*.

        `agy models` offers `claude-sonnet-4-6` and
        `claude-opus-4-6-thinking` beside the Gemini entries, so an
        unpinned consultant can inherit an account setting that makes
        AG-13's premise false without anything looking broken. This
        asserts the vendor rather than the exact name: the pin may be
        raised as models are released, and it may not be raised out of
        Google's range.
        """
        assert AgyConsultant(tmp_path)._model == consultant_mod.DEFAULT_MODEL
        assert consultant_mod.DEFAULT_MODEL.startswith("gemini-")

    def test_none_still_means_the_accounts_own_default(self, tmp_path):
        """The escape hatch the pin's docstring promises actually exists."""
        assert AgyConsultant(tmp_path, model=None)._model is None


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

    def test_every_refusal_opens_with_the_mark(self, tmp_path):
        """The token the pump reads a refusal back by (AG-R-22).

        At the head of the message rather than anywhere in it, so a
        transport that truncates the reason still leaves it intact, and
        prepended by ``StaticPolicy.of`` rather than by each posture, so a
        policy cannot be written without one. It is addressed to the
        reader too: this is what a denied card shows, in place of the
        vendor's "denied by pre-tool hook" and nothing else.
        """
        for policy in (SECOND_OPINION_POLICY, IMAGE_POLICY):
            assert policy.reason.startswith(StaticPolicy.MARK), policy.reason
            server = self._server(tmp_path, policy)
            assert self._decide(server, "run_command")["reason"].startswith(
                StaticPolicy.MARK
            )

    def test_the_mark_is_not_doubled_on_a_policy_built_from_one(self):
        """``of`` is idempotent — a reason that already carries the mark
        keeps exactly one. Policies are built from other policies'
        reasons in tests and probes, and a doubled mark is the kind of
        thing nobody reads closely enough to catch."""
        once = StaticPolicy.of({"finish"}, "Because.")
        twice = StaticPolicy.of({"finish"}, once.reason)
        assert twice.reason == once.reason
        assert once.reason.count(StaticPolicy.MARK) == 1

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

    def test_the_answer_is_the_same_whether_or_not_a_tab_is_watching(self, gated):
        """The two readings of one pass, asserted as equal.

        ``_run`` used to choose between ``observer(frame)`` and
        ``translator.translate(frame)`` per frame, so a browser's presence
        picked which code assembled the reply — and one of the two was the
        code nobody watched. Now the sink is resolved once and the loop has
        no branch, which is only worth claiming if the *answer* is
        identical either way. Compared through the real binary rather than
        by reading the loop, because the loop is the thing under suspicion.
        """
        build, _repo, _cfg = gated
        watched = build(answer="I would not merge this.")
        recorder = Recorder(watched.make_translator("r1", "consultation-1"))
        with_tab = asyncio.run(watched.second_opinion("well?", "", recorder))

        alone = build(answer="I would not merge this.")
        without = asyncio.run(alone.second_opinion("well?"))

        assert with_tab == without == "I would not merge this."
        assert recorder.frames, "the observer was never fed, so this proves nothing"


class TestTwoConsultationsAreStoppedIndependently:
    """AG-31, on the transport where stopping the wrong one kills a process.

    A Claude turn can hold two consultations at once — a subagent's tools
    are never narrowed, so one can ask for a second opinion while the main
    turn is already waiting on one. The shipped consultant kept a single
    ``_session`` and a single ``_cancelled`` flag for all of them, and both
    were wrong in that state:

    - ⏹ closed whichever process had started most recently, whatever row
      it was pressed on, and the other consultation could not be stopped
      at all;
    - the flag was then read by *every* run, so a consultation that got its
      answer reported itself "stopped before it answered" because somebody
      else's had been.

    The second is the worse one and it is specific to this transport: on
    the SDK side a mis-aimed stop wastes a call, here it kills a
    subprocess and loses prose the user was waiting for.

    Slower than the rest of this file, and deliberately so: two real
    ``agy`` processes, held mid-answer by the fake, is the only state in
    which either claim can be made.
    """

    async def in_flight(self, *recorders, tries: int = 200) -> None:
        """Wait until every consultation has emitted something.

        The fake sends its prose frame and *then* holds, so a recorded
        frame means the process is up, the prompt was sent and ⏹ has
        something to aim at. Spawning a process, installing a hook and
        starting a gate is not instant, hence the patience.
        """
        for _ in range(tries):
            if all(r.frames for r in recorders):
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the consultations never both reached the fake agy")

    def held_pair(self, gated, tmp_path):
        """One consultant, two consultations, both stopped mid-answer."""
        build, _repo, _cfg = gated
        release = tmp_path / "go"
        consultant = build(
            hold_until=str(release), answer="I would not merge this."
        )
        first = Recorder(consultant.make_translator("r1", "consultation-1"))
        second = Recorder(consultant.make_translator("r2", "consultation-2"))
        return consultant, release, first, second

    def test_the_named_consultation_is_the_one_that_dies(self, gated, tmp_path):
        """And the answer the other one was owed still arrives.

        Both halves in one test because they are one event: under the
        single-slot shape this ⏹ killed ``consultation-2``'s process and
        left ``consultation-1`` running, and then the shared flag made
        ``consultation-2`` — had it survived — report itself stopped.
        """
        consultant, release, first, second = self.held_pair(gated, tmp_path)

        async def scenario():
            one = asyncio.create_task(
                consultant.second_opinion(
                    "hold this one?", "", first, "consultation-1"
                )
            )
            two = asyncio.create_task(
                consultant.second_opinion(
                    "hold that one?", "", second, "consultation-2"
                )
            )
            try:
                await self.in_flight(first, second)
                assert await consultant.cancel("consultation-1") is True
                # Bounded rather than awaited outright: the failure this
                # guards against is the *other* task dying, which would
                # leave this one running to the consultation timeout.
                with pytest.raises(ConsultationError, match="stopped before"):
                    await asyncio.wait_for(one, 10)
                assert not two.done(), "the other consultation was killed too"
                release.write_text("go", "utf-8")
                assert await asyncio.wait_for(two, 10) == "I would not merge this."
            finally:
                release.write_text("go", "utf-8")
                await consultant.cancel()
                await asyncio.gather(one, two, return_exceptions=True)

        asyncio.run(scenario())

    def test_an_unknown_id_stops_nothing_and_says_so(self, gated, tmp_path):
        """``False``, which ``stop_task`` renders as ``not_running``.

        The id names a consultation that finished or was never here.
        Killing whatever process happens to be live instead would lose a
        stranger's answer *and* report success for it.
        """
        consultant, release, first, second = self.held_pair(gated, tmp_path)

        async def scenario():
            one = asyncio.create_task(
                consultant.second_opinion(
                    "hold this one?", "", first, "consultation-1"
                )
            )
            two = asyncio.create_task(
                consultant.second_opinion(
                    "hold that one?", "", second, "consultation-2"
                )
            )
            try:
                await self.in_flight(first, second)
                assert await consultant.cancel("consultation-nobody") is False
                assert not one.done() and not two.done()
            finally:
                release.write_text("go", "utf-8")
                await consultant.cancel()
                await asyncio.gather(one, two, return_exceptions=True)

        asyncio.run(scenario())

    def test_stopping_with_no_id_stops_every_one_of_them(self, gated, tmp_path):
        """The caller with no row: a probe, or a shutdown, or a test.

        Written without ``consultation_id`` on purpose, so it is the
        *shipped* call that runs and the failure is behavioural rather than
        a missing parameter: against the single-slot consultant this closes
        the younger process and leaves the elder holding, which is the
        defect stated in the only vocabulary that version has.
        """
        consultant, release, first, second = self.held_pair(gated, tmp_path)

        async def scenario():
            one = asyncio.create_task(
                consultant.second_opinion("hold this one?", "", first)
            )
            two = asyncio.create_task(
                consultant.second_opinion("hold that one?", "", second)
            )
            try:
                await self.in_flight(first, second)
                assert await consultant.cancel() is True
                for task in (one, two):
                    with pytest.raises(ConsultationError, match="stopped before"):
                        await asyncio.wait_for(task, 10)
            finally:
                release.write_text("go", "utf-8")
                await consultant.cancel()
                await asyncio.gather(one, two, return_exceptions=True)

        asyncio.run(scenario())

    def test_a_finished_consultation_cannot_be_stopped(self, gated):
        """The registry empties, so ⏹ on a stale row is honest.

        A row outlives its consultation in the browser — its answer is
        there to read — and an entry left behind would answer ``stopping``
        for a process that has already exited.
        """
        build, _repo, _cfg = gated
        consultant = build()

        async def scenario():
            await consultant.second_opinion(
                "well?", "", None, "consultation-1"
            )
            return await consultant.cancel("consultation-1")

        assert asyncio.run(scenario()) is False


class TestARefusalIsNotAFailure:
    """AG-R-22: the pump is told the policy, so it can tell the two apart.

    ``_run`` hands the translator ``policy.allowed`` because that is the
    only place the policy and the pump are both in scope, and because the
    alternative — reading ``agy``'s error prose for the word "denied" —
    couples this app to a vendor's wording. A tool that is not on the
    allowlist *cannot have run*, so a failed call of one was refused here;
    a failed call of an allowed one is an ordinary failure and must not be
    dressed up as containment.
    """

    def test_a_refused_tool_is_named_as_soon_as_it_is_refused(self, gated):
        build, _repo, _cfg = gated
        consultant = build(denied="read_url_content")
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("what is on kernel.org?", "", recorder))

        assert recorder.translator.ungrounded_tools == ("read_url_content",)
        names = [e.name for e in recorder.events]
        notices = [
            e for e in recorder.events
            if e.name == "systemEvent"
            and e.payload.get("subtype") == "consultation_ungrounded"
        ]
        assert len(notices) == 1, "the reader was told nothing"
        assert notices[0].payload["data"]["tool"] == "read_url_content"
        # The nonce, on the wire, in a string nothing in this file wrote.
        # The fake asked the installed hook for this reason rather than
        # quoting a constant, so the tag below was minted inside `_run`,
        # carried through the gate server, the socket and the hook, and
        # came back as `agy` would carry it. It is what tells this app's
        # own refusal from a copy of its own refusal — the mark beside it
        # is a fixed sentence sitting in this repository's source, which
        # a consultation that read that source could repeat verbatim.
        preview = [
            e.payload["preview"] for e in recorder.events if e.name == "toolResult"
        ][-1]
        assert re.search(r"\[ref [0-9a-f]{16}\]", preview), (
            f"no minted refusal tag reached the pump: {preview!r}"
        )
        # Before the turn ends, not after: a consultation that is stopped
        # or times out never emits a `result` frame, and a sentence under
        # prose the reader has already believed arrives too late.
        assert names.index("systemEvent") == names.index("toolResult") + 1

    def test_the_card_carries_the_reason_the_gate_gave(self, gated):
        """Not merely that it failed. The gate wrote a sentence; show it."""
        build, _repo, _cfg = gated
        consultant = build(denied="read_url_content")
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("what is on kernel.org?", "", recorder))

        results = [e for e in recorder.events if e.name == "toolResult"]
        assert results, "the denied call produced no card at all"
        assert "denied by pre-tool hook" in results[-1].payload["preview"]

    def test_a_call_that_never_reached_the_gate_still_warns(self, gated):
        """Measured live, P24 arm C: told to call `read_url_content` with a
        parameter it does not have, `agy` rejected the call against the
        tool's schema in 203ms and never ran the hook. What came back was
        "invalid arguments: missing properties \'Url\'…" — the vendor's
        words, no mark in them.

        The answer that follows is missing the fetch, so the reader is
        told. What is not said is that this app refused it — this app was
        never asked — and, since a sixth review round, that nothing came
        back from it either. Live measurement can say the call was made
        and the answer is over; only the gate's own mark can say what
        happened in between, and there is none here.
        """
        build, _repo, _cfg = gated
        consultant = build(
            denied="read_url_content",
            denied_error=(
                "invalid arguments:\n- missing properties 'Url', "
                "'toolSummary', 'toolAction'\n- additional properties "
                "'invalid_parameter' not allowed"
            ),
        )
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))

        assert recorder.translator.unverified_tools == ("read_url_content",)
        assert recorder.translator.ungrounded_tools == ()
        assert [
            e for e in recorder.events
            if e.name == "systemEvent"
            and e.payload.get("subtype") == "consultation_unverified"
        ], "the answer lost a retrieval and nothing said so"
        results = [e for e in recorder.events if e.name == "toolResult"]
        assert "invalid arguments" in results[-1].payload["preview"]
        assert StaticPolicy.MARK not in results[-1].payload["preview"], (
            "the card says what came back; our gate never saw this call"
        )

    def test_a_failure_of_an_allowed_tool_is_not_a_refusal(self, gated):
        """``finish`` is on the allowlist, so a failed one ran and failed.

        This is what makes the previous test about the *policy* rather
        than about the word ERROR: mark every failure as containment and
        this one goes red too.
        """
        build, _repo, _cfg = gated
        consultant = build(denied="finish")
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))

        assert "finish" in CONTROL_TOOLS, "this test's premise moved"
        results = [e for e in recorder.events if e.name == "toolResult"]
        assert results, (
            "no card was drawn at all, so the assertions below hold "
            "vacuously — the fake asks the real gate, and a change that "
            "stops it emitting is invisible without this line"
        )
        assert results[-1].payload["status"] == "error"
        assert recorder.translator.ungrounded_tools == ()
        assert recorder.translator.unverified_tools == ()
        assert not [
            e for e in recorder.events
            if e.name == "systemEvent"
            and e.payload.get("subtype") == "consultation_ungrounded"
        ]

    def test_a_consultation_that_reached_for_nothing_says_nothing(self, gated):
        """The ordinary case. A notice on every consultation is noise."""
        build, _repo, _cfg = gated
        consultant = build()
        recorder = Recorder(consultant.make_translator("r1", "consultation-1"))
        asyncio.run(consultant.second_opinion("well?", "", recorder))

        assert recorder.translator.ungrounded_tools == ()
        assert recorder.translator.unverified_tools == ()
        assert not [
            e for e in recorder.events
            if e.name == "systemEvent"
            and e.payload.get("subtype") == "consultation_ungrounded"
        ]

    def test_the_refusal_does_not_cost_the_answer(self, gated):
        """A denied tool is the design working, not the consultation dying."""
        build, _repo, _cfg = gated
        consultant = build(denied="read_url_content", answer="I would not merge this.")
        assert asyncio.run(consultant.second_opinion("well?")) == (
            "I would not merge this."
        )


class TestAnEmptyAnswerStillReportsWhatHappened:
    """The two surfaces have to say the same thing, including on failure.

    P28's malformed-call arm spent the whole consultation on a tool call
    the vendor rejected and wrote no prose at all. The tab was correct —
    `consultation_unverified` — and the asking model was handed
    *"Antigravity returned an empty answer"*, a sentence that reads as a
    transport hiccup and invites a retry. The surface an agent is
    actually blocked on was the one that lost the containment event, and
    that inverts the invariant this whole mechanism rests on.
    """

    def test_an_unverified_call_is_named_in_the_failure(self, gated):
        """Empty prose plus a rejection the app cannot authenticate.

        `denied_error` makes the fake report a failure of its own instead
        of asking the gate, so the message is not this app's and no token
        comes back with it — the wire shape of P28's schema rejection.
        """
        build, _repo, _cfg = gated
        consultant = build(
            answer="",
            denied="read_url_content",
            denied_error=(
                "invalid arguments:\n- additional properties 'url' not allowed"
            ),
        )
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("well?"))
        said = str(raised.value)
        assert "read_url_content" in said, (
            "the tab knew which call it was; the caller was told nothing"
        )
        assert "cannot account for" in said
        assert "empty answer" in said, "and it is still an empty answer"

    def test_a_breach_is_named_rather_than_called_an_empty_answer(self):
        """The same path when the gate did not hold at all.

        Unit rather than end-to-end: the fake `agy` reports every tool
        call with an error in it, and a breach is by definition a call
        that came back without one.
        """

        class Escaped:
            breached_tools = ("run_command",)
            unverified_tools = ()

        said = consultant_mod._empty_answer_reason(Escaped())
        assert "run_command ran despite" in said
        assert "containment failure" in said

    def test_an_ordinary_empty_answer_is_not_dressed_up_as_a_breach(self):
        class Clean:
            breached_tools = ()
            unverified_tools = ()

        said = consultant_mod._empty_answer_reason(Clean())
        assert "empty answer" in said
        assert "containment" not in said
        assert "cannot account for" not in said

    def test_two_unverified_calls_are_both_named(self):
        class Twice:
            breached_tools = ()
            unverified_tools = ("view_file", "read_url_content")

        said = consultant_mod._empty_answer_reason(Twice())
        assert "view_file and read_url_content" in said


class TestARenamedControlToolIsNamedRatherThanTimedOut:
    """AG-R-22's second silence: the allowlist outliving the vocabulary.

    ``finish`` is the only tool a second opinion may call, and it is how
    the model says it is done. If a weekly-releasing CLI renames or
    namespaces it, the allowlist refuses the consultant its own turn-ending
    tool — and the shipped failure was the worst shape available: every
    consultation runs to the bridge timeout, the tokens are spent, and both
    the tab and the answer say only that Antigravity did not answer in
    time. Nothing anywhere names the cause.

    The fix is *detection*, and deliberately nothing more. ``agy``'s
    ``init`` frame lists its tools before any prompt is sent, so the check
    is free; permitting an unknown name because it looks like a control
    tool is exactly the reasoning the allowlist exists to refuse, and
    refusing to launch is ruled out by measurement — AG-R-22's P24 arm D
    answered without making a single tool call, so a missing ``finish``
    makes failure likely rather than certain.
    """

    def test_the_timeout_names_the_tool_the_binary_does_not_have(self, gated):
        """The shipped message for this failure, and what it now adds.

        A fake that never sends its result frame is the renamed-``finish``
        shape end to end: the consultation cannot end its own turn, and the
        only thing that ends it is the clock.
        """
        build, _repo, _cfg = gated
        consultant = build(
            hold_until=str(_cfg / "never-appears"),
            advertises=("generate_image", "view_file"),
            timeout_seconds=2.0,
        )
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("hold, then answer"))
        said = str(raised.value)
        assert "did not answer within 2s" in said, "still says what happened"
        assert "does not advertise finish" in said, (
            "and now says why: the tool it needed to stop with is not there"
        )
        assert "version mismatch in AIC-DC" in said, (
            "which is nobody's question being bad"
        )

    def test_an_empty_answer_names_it_too(self, gated):
        """The other surface, because a reader may only see one of them."""
        build, _repo, _cfg = gated
        consultant = build(answer="", advertises=("generate_image",))
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("well?"))
        said = str(raised.value)
        assert "empty answer" in said
        assert "does not advertise finish" in said

    def test_a_binary_that_has_the_tool_provokes_nothing(self, gated):
        """No false alarm on the ordinary case, which is every real run."""
        build, _repo, _cfg = gated
        consultant = build(answer="")
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("well?"))
        assert "does not advertise" not in str(raised.value)

    def test_a_binary_that_lists_no_tools_makes_no_claim(self, gated):
        """An ``init`` frame with no inventory is silence, not absence.

        The direction this must fail in. Reading an empty advertisement as
        "every permitted tool is missing" would fire the alarm on every
        consultation against an older ``agy`` — teaching a reader to ignore
        the one that matters.
        """
        build, _repo, _cfg = gated
        consultant = build(answer="", advertises=None)
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("well?"))
        assert "does not advertise" not in str(raised.value)

    def test_an_empty_list_is_silence_as_well(self, gated):
        build, _repo, _cfg = gated
        consultant = build(answer="", advertises=())
        with pytest.raises(ConsultationError) as raised:
            asyncio.run(consultant.second_opinion("well?"))
        assert "does not advertise" not in str(raised.value)

    def test_being_advertised_is_not_being_permitted(self, gated):
        """The half of this that must not have moved.

        The binary advertises ``read_url_content``; the policy does not
        permit it. Detection reads the inventory — it must not widen
        anything on the strength of what it read, or AG-R-22's cure is
        worse than the disease.
        """
        build, _repo, _cfg = gated
        consultant = build(denied="read_url_content", advertises=ADVERTISED)
        recorder = Recorder(consultant.make_translator("r1", agent_id="a1"))
        answer = asyncio.run(
            consultant.second_opinion("well?", observer=recorder)
        )
        assert answer == "It depends."
        assert recorder.translator.ungrounded_tools == ("read_url_content",), (
            "an advertised tool the policy does not permit is still refused"
        )

    def test_the_missing_set_is_the_policy_minus_the_inventory(self):
        """The subtraction itself, including the image policy's own tool.

        Generalised over the policy rather than written against
        ``CONTROL_TOOLS``: a renamed ``generate_image`` strands an image
        generation the way a renamed ``finish`` strands a second opinion,
        and there is no reason for one to be diagnosable and the other not.
        """
        assert consultant_mod._missing_from_binary(
            SECOND_OPINION_POLICY, {"view_file"}
        ) == ("finish",)
        assert consultant_mod._missing_from_binary(
            IMAGE_POLICY, {"finish"}
        ) == ("generate_image",)
        assert consultant_mod._missing_from_binary(
            IMAGE_POLICY, {"finish", "generate_image", "view_file"}
        ) == ()

    def test_no_inventory_means_no_finding(self):
        assert consultant_mod._missing_from_binary(SECOND_OPINION_POLICY, ()) == ()

    def test_the_translator_carries_it_without_a_signature_change(self):
        """Recorded on the pump, so both surfaces read one source.

        ``_empty_answer_reason`` takes a translator and nothing else, and
        the timeout reads the same attribute — so the tab and the answer
        cannot come to different conclusions about which tool was missing.
        """
        from aic_dc.agy.steps import AgyTranslator

        translator = AgyTranslator("r1")
        assert translator.unadvertised_tools == ()
        translator.note_unadvertised(("finish",))
        translator.note_unadvertised(("finish",))
        assert translator.unadvertised_tools == ("finish",), "recorded once"
        said = consultant_mod._empty_answer_reason(translator)
        assert "does not advertise finish" in said


class TestTheNoToolsInvariantIsCheckedRatherThanAssumed:
    """A future edit must not be able to make the header false in silence.

    The answer goes back under a sentence in this app's own voice saying
    nothing was read or fetched, which is true of the second-opinion
    policy only because its one entry, `finish`, retrieves nothing.
    Adding a read tool "just for consultations" would leave that sentence
    asserting something false with every test still green: a permitted
    call raises no refusal and no breach, because there is nothing to
    notice.
    """

    def test_a_policy_that_permits_a_read_tool_will_not_launch(self):
        policy = StaticPolicy.of({"view_file", *CONTROL_TOOLS}, "because")
        with pytest.raises(ConsultationError, match="policy permits view_file"):
            consultant_mod._no_tools_or_fail(policy)

    def test_the_real_policy_passes_and_is_handed_back_unchanged(self):
        assert (
            consultant_mod._no_tools_or_fail(SECOND_OPINION_POLICY)
            is SECOND_OPINION_POLICY
        )

    def test_it_is_a_raise_and_not_an_assert(self):
        """`python -O` strips `assert`, and unattended builds run with it.

        Read from the source rather than by running an optimised
        interpreter: the point is that the invariant is not written in a
        statement the compiler is allowed to delete.
        """
        import inspect

        source = inspect.getsource(consultant_mod._no_tools_or_fail)
        assert "raise ConsultationError" in source
        assert "assert " not in source

    def test_a_second_opinion_goes_through_the_check(self, gated):
        """The guard is on the launch path, not merely available to it."""
        build, _repo, _cfg = gated
        seen: list = []
        original = consultant_mod._no_tools_or_fail
        consultant_mod._no_tools_or_fail = lambda policy: (
            seen.append(policy) or original(policy)
        )
        try:
            asyncio.run(build(answer="It depends.").second_opinion("well?"))
        finally:
            consultant_mod._no_tools_or_fail = original
        assert seen == [SECOND_OPINION_POLICY]


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
        consultant = build(image_in_brain=BRAIN_IMAGE)
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
        consultant = build(image_in_brain=BRAIN_IMAGE)
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
        consultant = build(image_in_brain=BRAIN_IMAGE, log=str(log))
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


class TestModelResolution:
    """AG-R-15: the Settings control is live because this reads per call."""

    class _Config:
        def __init__(self, model):
            self.consultant_model = model

    def test_config_wins_over_the_construction_default(self, tmp_path):
        consultant = AgyConsultant(
            tmp_path, config=self._Config("gemini-3.8-flash-low")
        )
        assert consultant._resolve_model() == "gemini-3.8-flash-low"

    def test_it_is_re_read_rather_than_captured(self, tmp_path):
        """app.json is reloadable, so a save reaches the *next* consultation
        without a restart. Capturing at construction would silently make the
        control an app-restart one."""
        config = self._Config("gemini-3.8-flash-low")
        consultant = AgyConsultant(tmp_path, config=config)
        config.consultant_model = "gemini-3.1-pro-high"
        assert consultant._resolve_model() == "gemini-3.1-pro-high"

    def test_no_config_keeps_the_pin(self, tmp_path):
        assert AgyConsultant(tmp_path)._resolve_model() == consultant_mod.DEFAULT_MODEL

    def test_a_broken_config_falls_back_rather_than_losing_the_answer(self, tmp_path):
        class _Angry:
            @property
            def consultant_model(self):
                raise RuntimeError("no config today")

        consultant = AgyConsultant(tmp_path, config=_Angry())
        assert consultant._resolve_model() == consultant_mod.DEFAULT_MODEL
