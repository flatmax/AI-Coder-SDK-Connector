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

import asyncio
import time

import pytest

from aic_dc.antigravity.bridge import SERVER_NAME, ConsultantBridge, Event
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
        #: The observer each call was handed. Never ``None`` from the
        #: bridge, including with no browser attached — see
        #: :class:`TestNoSinkIsLoadBearing`. The parameter keeps its
        #: ``None`` default because a direct caller may still omit it.
        self.observers: list = []

    async def second_opinion(self, question, context="", observer=None):
        self.calls.append(("second_opinion", question, context))
        self.observers.append(observer)
        if self._raises:
            raise self._raises
        return self._answer

    async def generate_image(
        self, prompt, output_name="", aspect_ratio="", observer=None
    ):
        self.calls.append(("generate_image", prompt, output_name, aspect_ratio))
        self.observers.append(observer)
        if self._raises:
            raise self._raises
        return self._image


def _step(text):
    """A minimal Step the pump will translate into a chunk."""
    return type("S", (), {
        "id": "t:1", "type": "TEXT_RESPONSE", "source": "MODEL",
        "target": "USER", "status": "ACTIVE", "content": text,
        "content_delta": text, "depth": 0,
    })()


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


# ----------------------------------------------------------------------
# AG-13 — the consultation as its own agent tab
# ----------------------------------------------------------------------


class TestTheConsultationGetsATab:
    """The contract read off ``subagent-tabs.js``, asserted from this side.

    The webapp needs no change for this to work, and that claim is only
    true if the server gets four things exactly right: a turn-scoped
    event, an identity, blocks carrying the *same* identity as their
    ``agent_id``, and a terminal event. Each is checked here, because each
    fails silently — a mismatch drops the event and the tab simply never
    appears.
    """

    def bridge_with_emit(self, consultant=None, request_id="req-1"):
        seen = []

        async def emit(event, rid):
            seen.append((event, rid))

        return (
            ConsultantBridge(
                consultant or FakeConsultant(answer="ok"),
                emit=emit,
                request_id=lambda: request_id,
            ),
            seen,
        )

    def events(self, seen, name):
        return [e for e, _ in seen if e.name == name]

    @pytest.mark.asyncio
    async def test_a_consultation_announces_itself(self):
        bridge, seen = self.bridge_with_emit()
        await bridge.second_opinion("Well?")
        rows = self.events(seen, "subagentEvent")
        assert rows, "no subagentEvent — the tab strip never hears about it"
        assert rows[0].payload["subagent_type"] == "Antigravity"

    @pytest.mark.asyncio
    async def test_the_event_is_turn_scoped_to_the_live_request(self):
        """``onSubagentEvent`` drops anything whose owner tab is not live."""
        bridge, seen = self.bridge_with_emit(request_id="req-42")
        await bridge.second_opinion("Well?")
        assert all(rid == "req-42" for _, rid in seen)

    @pytest.mark.asyncio
    async def test_it_settles_so_the_tab_stops_streaming(self):
        """``state.streaming = !row.terminal`` — without this it spins."""
        bridge, seen = self.bridge_with_emit()
        await bridge.second_opinion("Well?")
        rows = self.events(seen, "subagentEvent")
        assert rows[-1].payload["terminal"] is True
        assert rows[-1].payload["status"] == "completed"

    @pytest.mark.asyncio
    async def test_it_settles_even_when_the_consultation_fails(self):
        """A refusal must not leave a tab spinning for the session."""
        bridge, seen = self.bridge_with_emit(
            FakeConsultant(raises=ConsultationError("no quota"))
        )
        await bridge.second_opinion("Well?")
        assert self.events(seen, "subagentEvent")[-1].payload["terminal"] is True

    @pytest.mark.asyncio
    async def test_one_identity_joins_the_row_to_its_blocks(self):
        """``row.tool_use_id`` is what picks the blocks to mirror."""
        bridge, seen = self.bridge_with_emit()
        await bridge.second_opinion("Well?")
        row = self.events(seen, "subagentEvent")[0].payload
        assert row["tool_use_id"] == row["agent_id"] == row["task_id"]

    @pytest.mark.asyncio
    async def test_each_consultation_gets_its_own_identity(self):
        """Two in one turn are two tabs, not one that overwrites itself."""
        bridge, seen = self.bridge_with_emit()
        await bridge.second_opinion("First?")
        await bridge.second_opinion("Second?")
        ids = {e.payload["agent_id"] for e in self.events(seen, "subagentEvent")}
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_nothing_is_emitted_with_no_live_turn(self):
        """The bridge outlives any one turn; a tab needs one to attach to."""
        bridge, seen = self.bridge_with_emit(request_id=None)
        await bridge.second_opinion("Well?")
        assert seen == []

    @pytest.mark.asyncio
    async def test_the_consultant_is_given_an_observer(self):
        """Which is what makes it stream rather than answer all at once."""
        consultant = FakeConsultant(answer="ok")
        bridge, _ = self.bridge_with_emit(consultant)
        await bridge.second_opinion("Well?")
        assert consultant.observers[-1] is not None

    @pytest.mark.asyncio
    async def test_an_observer_is_given_even_with_nothing_listening(self):
        """The replacement for ``test_no_observer_when_nothing_is_listening``.

        That test asserted the coupling this file now argues against: the
        bridge used to yield ``None`` with no browser attached, and
        ``AgyConsultant._run`` read that as *translate the frames
        yourself*. Whether a tab existed therefore decided which code
        assembled the reply, and the two assemblers could disagree — which
        is the shipped defect, not a hypothetical.

        An observer with nothing to emit to still translates, so there is
        one assembler in both cases. What a missing browser switches off is
        the tab machinery, which :class:`TestNoSinkIsLoadBearing` checks
        stays off.
        """
        consultant = FakeConsultant(answer="ok")
        await ConsultantBridge(consultant).second_opinion("Well?")
        assert consultant.observers[-1] is not None, (
            "with no observer the consultant needs a second way to build the "
            "answer, and two ways to build one answer is how they disagree"
        )

    @pytest.mark.asyncio
    async def test_stop_reaches_the_consultation(self):
        """AG-13's ⏹ is real, not decorative."""
        consultant = FakeConsultant(answer="ok")
        consultant.cancelled = False

        async def cancel():
            consultant.cancelled = True
            return True

        consultant.cancel = cancel
        assert await ConsultantBridge(consultant).cancel() is True
        assert consultant.cancelled

    @pytest.mark.asyncio
    async def test_a_failed_consultation_settles_as_failed_not_completed(self):
        """The bug the first live browser run found.

        The status goes straight to a colour: ``subagent-tabs.js`` maps
        ``completed`` to a **green** LED and ``failed`` to red. An earlier
        version announced ``completed`` unconditionally from a ``finally``,
        so a consultation that timed out after 180s — Google never
        answered — settled green. A failure rendered as a success is worth
        less than a spinner, and it is the manufactured-consent shape AG-5
        and AG-R-3 are both written against.

        Note the 26 tests that already existed all passed against the
        broken version: every one of them asserted ``terminal`` was true,
        and none asserted *what* the status said.
        """
        bridge, seen = self.bridge_with_emit(
            FakeConsultant(raises=ConsultationError("timed out after 180s"))
        )
        await bridge.second_opinion("Well?")
        last = self.events(seen, "subagentEvent")[-1].payload
        assert last["terminal"] is True
        assert last["status"] == "failed", (
            "a failed consultation reported itself completed, which the "
            "webapp renders as a green LED"
        )

    @pytest.mark.asyncio
    async def test_a_successful_consultation_still_settles_as_completed(self):
        """The other half — `failed` must not become the blanket answer."""
        bridge, seen = self.bridge_with_emit()
        await bridge.second_opinion("Well?")
        assert self.events(seen, "subagentEvent")[-1].payload["status"] == "completed"

    @pytest.mark.asyncio
    async def test_the_status_is_one_the_webapp_has_a_colour_for(self):
        """An unrecognised status lands on amber, which says nothing."""
        known = {"completed", "failed", "stopped", "killed"}
        for raises in (None, ConsultationError("no")):
            bridge, seen = self.bridge_with_emit(
                FakeConsultant(answer="ok", raises=raises)
            )
            await bridge.second_opinion("Well?")
            assert self.events(seen, "subagentEvent")[-1].payload["status"] in known

    @pytest.mark.asyncio
    async def test_a_failure_says_why_in_the_tab(self):
        """Red is not a reason.

        Before this the explanation went to the *model*, as the tool's
        text result, which the person watching the tab never reads. The
        tab settled red and said nothing.
        """
        bridge, seen = self.bridge_with_emit(
            FakeConsultant(raises=ConsultationError("did not answer within 120s"))
        )
        await bridge.second_opinion("Well?")
        notices = [
            e.payload for e in self.events(seen, "systemEvent")
            if e.payload.get("subtype") == "engine_error"
        ]
        assert notices, "the tab was given no reason for the failure"
        assert "120s" in notices[0]["data"]["message"]

    @pytest.mark.asyncio
    async def test_the_reason_is_attributed_to_the_consultation(self):
        """Or it renders in Main, where it reads as the master failing."""
        bridge, seen = self.bridge_with_emit(
            FakeConsultant(raises=ConsultationError("boom"))
        )
        await bridge.second_opinion("Well?")
        row = self.events(seen, "subagentEvent")[0].payload
        notice = next(
            e.payload for e in self.events(seen, "systemEvent")
            if e.payload.get("subtype") == "engine_error"
        )
        assert notice["agent_id"] == row["agent_id"]

    @pytest.mark.asyncio
    async def test_the_heartbeat_reports_waiting_and_then_stops(self, monkeypatch):
        """Silence was the problem; a growing number is the smallest fix.

        The heartbeat must also *stop* — a background task that outlives
        the consultation it reports on is how "harmless" tasks accumulate.
        """
        import aic_dc.antigravity.bridge as mod

        monkeypatch.setattr(mod, "HEARTBEAT_SECONDS", 0.01)

        class Slow(FakeConsultant):
            async def second_opinion(self, question, context="", observer=None):
                await asyncio.sleep(0.05)
                return "late"

        bridge, seen = self.bridge_with_emit(Slow(answer="late"))
        await bridge.second_opinion("Well?")
        beats = [
            e.payload for e in self.events(seen, "systemEvent")
            if "so far" in str(e.payload.get("data", {}).get("message", ""))
        ]
        assert beats, "no heartbeat while the consultation was running"
        before = len(beats)
        await asyncio.sleep(0.05)
        after = len([
            e for e in self.events(seen, "systemEvent")
            if "so far" in str(e.payload.get("data", {}).get("message", ""))
        ])
        assert after == before, "the heartbeat outlived its consultation"

    def test_the_timeout_default_is_not_three_minutes(self):
        """The number that produced two silent 180s waits."""
        from aic_dc.antigravity.consultant import DEFAULT_TIMEOUT_SECONDS

        assert DEFAULT_TIMEOUT_SECONDS < 180

    @pytest.mark.asyncio
    async def test_the_heartbeat_names_the_queue_before_the_first_step(
        self, monkeypatch
    ):
        """Google confirmed the whole wait lands before the first token.

        So "no step yet" means *queued behind paid traffic*, which is
        something a reader can act on, and it is a different state from a
        model that is thinking. Same spinner, opposite meanings.
        """
        import aic_dc.antigravity.bridge as mod

        monkeypatch.setattr(mod, "HEARTBEAT_SECONDS", 0.01)

        class Stalled(FakeConsultant):
            async def second_opinion(self, question, context="", observer=None):
                await asyncio.sleep(0.05)  # never calls the observer
                return "late"

        bridge, seen = self.bridge_with_emit(Stalled(answer="late"))
        await bridge.second_opinion("Well?")
        messages = [
            str(e.payload.get("data", {}).get("message", ""))
            for e in self.events(seen, "systemEvent")
        ]
        assert any("queued behind paid traffic" in m for m in messages), (
            "a stalled consultation did not say it was queued"
        )

    @pytest.mark.asyncio
    async def test_the_heartbeat_stops_naming_the_queue_once_it_starts(
        self, monkeypatch
    ):
        """Once a step has arrived the request has cleared the queue.

        Still saying "queued" then would be wrong, and the wrong kind of
        wrong: it would blame the provider for a model that is simply
        taking its time.
        """
        import aic_dc.antigravity.bridge as mod

        monkeypatch.setattr(mod, "HEARTBEAT_SECONDS", 0.01)

        class Streaming(FakeConsultant):
            async def second_opinion(self, question, context="", observer=None):
                if observer:
                    observer(_step("hello"))
                await asyncio.sleep(0.05)
                return "done"

        bridge, seen = self.bridge_with_emit(Streaming(answer="done"))
        await bridge.second_opinion("Well?")
        messages = [
            str(e.payload.get("data", {}).get("message", ""))
            for e in self.events(seen, "systemEvent")
        ]
        beats = [m for m in messages if "so far" in m]
        assert beats, "no heartbeat at all"
        assert not any("queued behind paid traffic" in m for m in beats)
        assert any("Antigravity is working" in m for m in beats)


class TestTheTabAndTheAnswerAgree:
    """One stream, two consumers, and they used to be able to disagree.

    A consultation's text reaches the browser as ``streamChunk`` deltas
    and reaches the *calling model* through ``response_text()``, which on
    the ``agy`` transport prefers the prose assembled in the ``result``
    frame. Nothing tied those two together, so a complete answer beside an
    empty tab was a reachable state — and it shipped
    (``specs5/known-issues.md`` § *An empty consultation tab*). The first
    two tests fail against the bridge as it shipped; the third passes there
    and guards the fix against over-reaching.
    """

    class Recording:
        """A translator that says what it was asked, and holds an answer.

        Stands in for both real translators, which agree on the three
        methods the bridge uses: ``translate``, ``turn_usage``,
        ``response_text``.
        """

        def __init__(self, answer="", events=()):
            self.answer = answer
            self.seen: list = []
            self._events = list(events)

        def translate(self, step):
            self.seen.append(step)
            return list(self._events)

        def turn_usage(self):
            return {}

        def response_text(self):
            return self.answer

    def bridge_with(self, translator, *, request_id="req-1", body=None):
        """A bridge whose consultant hands back ``translator``.

        ``request_id`` is read as a callable on every emit, so a test can
        close the gate part-way through a consultation — which is the state
        the first test is about, and is not reachable by passing ``None``
        up front (``_tab`` then never opens at all).
        """
        seen = []
        live = {"id": request_id}

        async def emit(event, rid):
            seen.append((event, rid))

        class Consultant(FakeConsultant):
            def make_translator(self, request_id, agent_id):
                return translator

            async def second_opinion(self, question, context="", observer=None):
                if body is not None:
                    body(observer, live)
                return translator.answer or "ok"

        bridge = ConsultantBridge(
            Consultant(answer="ok"),
            emit=emit,
            request_id=lambda: live["id"],
        )
        return bridge, seen

    @pytest.mark.asyncio
    async def test_a_step_is_translated_even_with_no_tab_to_stream_to(self):
        """Translation is how the answer is *built*, not how it is shown.

        ``AgyConsultant._run`` calls ``observer(frame)`` **instead of**
        ``translator.translate(frame)`` whenever a browser is attached, so
        the observer holds the only pass over the stream. The old guard
        returned before translating when there was no live turn to emit to,
        which meant a window of ``request_id is None`` shortened the reply
        the model received — a rendering condition silently corrupting the
        answer.
        """
        translator = self.Recording(answer="the answer")

        def stream(observer, live):
            live["id"] = None  # the tab is open; the turn is now gone
            observer(_step("hello"))

        bridge, seen = self.bridge_with(translator, body=stream)
        await bridge.second_opinion("Well?")

        assert translator.seen, (
            "the step was never translated, so response_text() could not "
            "have seen it — the answer, not just the tab, loses text"
        )

    @pytest.mark.asyncio
    async def test_a_turn_that_streamed_no_text_seeds_the_tab_from_its_answer(self):
        """No deltas is not the same as nothing to say.

        Seeded from the translator the answer came out of, so the tab and
        the tool result cannot disagree about what was said.
        """
        translator = self.Recording(answer="the whole answer", events=())

        def stream(observer, live):
            observer(_step("ignored — translate() yields nothing"))

        bridge, seen = self.bridge_with(translator, body=stream)
        await bridge.second_opinion("Well?")

        chunks = [e for e, _ in seen if e.name == "streamChunk"]
        assert chunks, "a consultation with prose left an empty tab"
        assert chunks[-1].payload["content"] == "the whole answer"
        assert chunks[-1].payload["done"] is True
        # Same identity as the tab, or the block lands nowhere.
        announced = [e for e, _ in seen if e.name == "subagentEvent"]
        assert chunks[-1].payload["agent_id"] == announced[0].payload["agent_id"]

    @pytest.mark.asyncio
    async def test_a_turn_that_did_stream_is_not_seeded_on_top_of_itself(self):
        """The seed is a fallback, not a footer.

        A turn whose deltas arrived already has its text in the tab;
        appending the assembled prose would render the answer twice.
        """
        from aic_dc.antigravity.bridge import Event

        chunk = Event(
            "streamChunk",
            {"block_id": "b", "seq": 1, "content": "streamed", "done": True},
        )
        translator = self.Recording(answer="assembled", events=[chunk])

        def stream(observer, live):
            observer(_step("hello"))

        bridge, seen = self.bridge_with(translator, body=stream)
        await bridge.second_opinion("Well?")

        contents = [
            e.payload.get("content") for e, _ in seen if e.name == "streamChunk"
        ]
        assert contents == ["streamed"], (
            f"the assembled prose was appended to a tab that already had "
            f"text: {contents}"
        )


class TestNoSinkIsLoadBearing:
    """The invariant, stated as three ways a consumer can fail.

    ``specs5/7-future/blank-sheet-architecture.md``: *no sink may be
    load-bearing; every sink is an observer, and the result never depends
    on one.* An Invocation's result must be independent of the presence,
    count, latency, failure or absence of any attached EventSink.

    Each test below is one of those words, and each was reachable before:
    **failure** because four emits were awaited inline with only the send
    guarded, **latency** because the end-of-tab drain was unbounded, and
    **absence** because it changed which code assembled the answer. The
    consultant here builds its reply *out of the frames it feeds*, which is
    ``AgyConsultant._run``'s shape and the reason absence mattered — a fake
    that returns a canned string would pass all three without testing
    anything.
    """

    class Accumulating:
        """A translator that is also the accumulator, as the real one is."""

        def __init__(self):
            self._text: list[str] = []

        def translate(self, step):
            self._text.append(step.content)
            return [
                Event(
                    "streamChunk",
                    {"block_id": "b", "seq": len(self._text), "content": step.content},
                )
            ]

        def turn_usage(self):
            return {}

        def response_text(self):
            return "".join(self._text)

    class Assembling(FakeConsultant):
        """Answers with what the observer accumulated, or fails to answer.

        The fallback for a missing observer is deliberately *not* a second
        accumulator: this consultant cannot answer at all without one, so a
        bridge that goes back to yielding ``None`` fails these tests loudly
        instead of quietly returning a shorter reply.
        """

        FRAMES = ("Three ", "problems ", "with this.")

        def make_translator(self, request_id, agent_id=""):
            return TestNoSinkIsLoadBearing.Accumulating()

        async def second_opinion(self, question, context="", observer=None):
            self.observers.append(observer)
            translator = getattr(observer, "translator", None)
            assert translator is not None, "no observer, no accumulator"
            for text in self.FRAMES:
                observer(_step(text))
            return translator.response_text()

    ANSWER = "Three problems with this."

    @pytest.mark.asyncio
    async def test_the_answer_survives_a_sink_that_raises_on_every_frame(self):
        """Failure. A broken consumer costs frames, never the reply.

        **This one passes against the bridge as it shipped**, and is here as
        a guard rather than as a fix: every emit was already wrapped, and a
        raise was the one sink failure the old code did handle. It is the
        *silent* failures either side of it — a sink that stalls instead of
        raising, and a sink that is simply absent — that the next two tests
        cover and that shipped broken.
        """
        attempts = []

        async def emit(event, rid):
            attempts.append(event.name)
            raise RuntimeError("this websocket is closed")

        bridge = ConsultantBridge(
            self.Assembling(), emit=emit, request_id=lambda: "req-1"
        )
        result = await bridge.second_opinion("Well?")

        assert self.ANSWER in body(result)
        assert attempts, "nothing was even attempted, so the raise proved nothing"

    @pytest.mark.asyncio
    async def test_the_answer_does_not_wait_for_a_sink_that_stops_reading(
        self, monkeypatch
    ):
        """Latency. The drain is an ordering courtesy with a deadline.

        A paused browser tab or a TCP window that has stopped opening holds
        every task scheduled against it. ``_tab`` closes inside the tool
        call, so an unbounded drain there hands a stalled consumer a hold on
        the answer — the model waits on a rendering detail it cannot see.
        """
        monkeypatch.setattr("aic_dc.antigravity.bridge.DRAIN_SECONDS", 0.05)
        started = asyncio.Event()

        async def emit(event, rid):
            started.set()
            await asyncio.sleep(30)  # never returns within this test

        bridge = ConsultantBridge(
            self.Assembling(), emit=emit, request_id=lambda: "req-1"
        )
        began = time.monotonic()
        result = await asyncio.wait_for(bridge.second_opinion("Well?"), timeout=5)
        elapsed = time.monotonic() - began

        assert self.ANSWER in body(result)
        assert started.is_set(), "the sink was never called, so it never stalled"
        assert elapsed < 3, (
            f"the consultation took {elapsed:.1f}s waiting on a sink that "
            "sleeps for 30 — the answer is being held by a rendering wait"
        )
        # The stalled sends are still pending, on purpose: a push cancelled
        # mid-await is a half-written frame on a shared socket. Cleared here
        # so this test does not leave them for the next one.
        for task in list(bridge._tasks):
            task.cancel()

    @pytest.mark.asyncio
    async def test_the_full_answer_comes_back_with_no_sink_at_all(self):
        """Absence. The case that shipped the defect, and the cheapest one.

        No emit, no request id, no browser: the ordinary state of a
        headless server. The reply must be the whole reply, not the part
        that happened to be rendered.
        """
        consultant = self.Assembling()
        result = await ConsultantBridge(consultant).second_opinion("Well?")

        assert body(result).endswith(self.ANSWER), (
            f"a browserless consultation returned {body(result)!r}"
        )

    @pytest.mark.asyncio
    async def test_absence_still_costs_nothing(self):
        """The other half of absence: no tab machinery for a headless run.

        The observer is always built; the announce, the heartbeat and the
        seed are not. Asserted through the consultant's own timing rather
        than by reading the method — a consultation that started a heartbeat
        it never cancelled would be a task left running here.
        """
        before = len(asyncio.all_tasks())
        bridge = ConsultantBridge(self.Assembling())
        await bridge.second_opinion("Well?")

        assert bridge._tasks == set(), (
            "a browserless consultation scheduled emits, which means it "
            "found something to push to that it should not have"
        )
        assert len(asyncio.all_tasks()) <= before, "a heartbeat outlived its tab"
