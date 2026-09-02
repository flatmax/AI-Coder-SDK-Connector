"""Tests for aic_dc.antigravity.session — the phase-3 engine spike.

The load-bearing assertions are about **posture and teardown**, because
those are the two things a fake harness can check honestly and the two
whose failure modes are expensive.

*Posture*: a session with no decide hook must be structurally unable to
write. AG-5 makes the permission dialog a requirement of this engine and
AG-R-11 showed a refused ``edit_file`` coming back as ``sed -i``, so
"read-only" has to be a property of the config the session builds rather
than a promise in its docstring.

*Teardown*: the harness is a bundled 119 MB Go subprocess. A close that
raises leaves it alive while the caller believes it is gone, and a failed
start that does not unwind leaves one with nobody holding a reference at
all. Both are asserted directly.

The AG-R-9 tripwire is inverted here relative to the consultant's. There,
``receive_steps``/``cancel``/``conversation_id`` appearing is the failure;
here, this is where they are *supposed* to live — and what must not appear
is ``chat()``, the one-shot call pattern whose shape the risk register
warns the engine against inheriting.

Everything runs offline against a fake conversation. No key, no harness,
no network.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from aic_dc.antigravity import options
from aic_dc.antigravity import session as session_module
from aic_dc.antigravity.credentials import GEMINI_API, Credentials
from aic_dc.antigravity.session import (
    AntigravitySession,
    SessionNotStartedError,
    TurnInProgressError,
)
from aic_dc.antigravity.steps import StepTranslator


def credentials() -> Credentials:
    return Credentials(mode=GEMINI_API, api_key="test-key", source="test")


class FakeStep:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        raise AttributeError(name)


def text_step(content):
    return FakeStep(
        id="t:1",
        type="TEXT_RESPONSE",
        source="MODEL",
        target="USER",
        status="DONE",
        content=content,
        content_delta=content,
        depth=0,
    )


class FakeConversation:
    """A conversation that yields a scripted turn and records cancels.

    Steps are yielded from a generator that checks it is still inside the
    session's lifetime, because the phase-1 hang was exactly a stream
    drained after teardown and a fake that answers whenever asked cannot
    catch it.
    """

    def __init__(self, steps=(), *, alive=None, stop_reason=None, usage=None):
        self.steps = list(steps)
        self.sent: list[str] = []
        self.cancels = 0
        # The SDK's own spelling, underscore and all. This fake said
        # `stop_reason` until 2026-09-02 — a name the real Conversation
        # does not have — so every offline test passed while a live turn
        # reported no reason at all. A fake that invents a friendlier
        # shape than the thing it stands in for cannot fail the way the
        # real one does; `test_the_fake_matches_the_sdks_shape` is what
        # now holds it to that.
        self._last_turn_stop_reason = stop_reason
        self.last_turn_usage = usage
        self._alive = alive

    async def send(self, prompt):
        self.sent.append(prompt)

    async def receive_steps(self):
        for step in self.steps:
            if self._alive is not None and not self._alive():
                raise AssertionError(
                    "receive_steps() was drained after the session closed — "
                    "the phase-1 hang, in the engine this time."
                )
            yield step

    async def cancel(self):
        self.cancels += 1


def started_session(conversation, **kw) -> AntigravitySession:
    """A session wired to a fake conversation, with no SDK involved."""
    s = AntigravitySession("/tmp/repo", credentials=credentials(), **kw)
    s._conversation = conversation
    s._exit = None
    return s


# ----------------------------------------------------------------------
# Posture: what this session can and cannot do
# ----------------------------------------------------------------------


class TestPosture:
    def test_a_session_with_no_hook_is_read_only(self):
        s = AntigravitySession("/tmp/repo", credentials=credentials())
        assert s.read_only

    def test_read_only_is_enforced_in_the_config_not_promised(self):
        s = AntigravitySession("/tmp/repo", credentials=credentials())
        enabled = s.config_kwargs()["capabilities"]["enabled_tools"]
        assert not (set(enabled) & options.MUTATING_TOOLS)

    def test_a_hook_makes_it_writable(self):
        s = AntigravitySession(
            "/tmp/repo", credentials=credentials(), decide_hook=object()
        )
        assert not s.read_only
        enabled = s.config_kwargs()["capabilities"]["enabled_tools"]
        assert "run_command" in enabled

    def test_one_workspace_root(self, tmp_path):
        """AG-10, asserted at the layer that builds the config."""
        s = AntigravitySession(tmp_path, credentials=credentials())
        assert s.config_kwargs()["workspaces"] == [str(tmp_path.resolve())]


# ----------------------------------------------------------------------
# The turn
# ----------------------------------------------------------------------


class TestTurn:
    def test_a_prompt_is_sent_and_its_steps_become_events(self):
        conv = FakeConversation([text_step("hello")])
        s = started_session(conv)

        events = asyncio.run(self._drain(s, "hi"))
        assert conv.sent == ["hi"]
        assert [e.name for e in events][:1] == ["streamChunk"]
        assert events[-1].name == "streamComplete"

    async def _drain(self, s, prompt):
        return [e async for e in s.stream_turn(prompt)]

    def test_the_turn_always_closes_with_usage_and_completion(self):
        """Even an empty turn ends the browser's spinner."""
        s = started_session(FakeConversation([]))
        events = asyncio.run(self._drain(s, "hi"))
        assert [e.name for e in events] == ["turnUsage", "streamComplete"]

    def test_a_turn_before_start_is_refused(self):
        s = AntigravitySession("/tmp/repo", credentials=credentials())

        async def go():
            return [e async for e in s.stream_turn("hi")]

        with pytest.raises(SessionNotStartedError):
            asyncio.run(go())

    def test_a_second_turn_during_the_first_is_refused(self):
        """Named rather than queued.

        ``Conversation.send`` would handle it by draining the in-flight
        turn into history, and doing that silently means the user's second
        prompt arrives with the first turn's tool calls half-rendered and
        no event saying why.
        """
        s = started_session(FakeConversation([text_step("a")]))

        async def go():
            async for _ in s.stream_turn("first"):
                async for _ in s.stream_turn("second"):
                    pass

        with pytest.raises(TurnInProgressError):
            asyncio.run(go())

    def test_the_turn_flag_clears_so_a_next_turn_can_run(self):
        s = started_session(FakeConversation([text_step("a")]))
        asyncio.run(self._drain(s, "one"))
        asyncio.run(self._drain(s, "two"))
        assert s._conversation.sent == ["one", "two"]

    def test_a_timeout_is_an_event_rather_than_an_exception(self):
        """The partial transcript is real and the browser is rendering it."""

        class Hanging(FakeConversation):
            async def receive_steps(self):
                await asyncio.sleep(10)
                yield  # pragma: no cover

        s = started_session(Hanging(), turn_timeout=0.01)
        events = asyncio.run(self._drain(s, "hi"))
        assert events[0].payload["subtype"] == "turn_timeout"
        assert events[-1].name == "streamComplete"

    def test_run_turn_emits_and_returns_the_translator(self):
        seen = []

        async def emit(event):
            seen.append(event)

        s = started_session(FakeConversation([text_step("hello")]))
        translator = asyncio.run(s.run_turn("hi", "req-1", emit=emit))
        assert isinstance(translator, StepTranslator)
        assert translator.response_text() == "hello"
        assert [e.name for e in seen][-1] == "streamComplete"

    def test_a_dead_client_does_not_end_the_turn(self):
        """A closed WebSocket must not take the other clients' turn with it."""
        delivered = []

        async def emit(event):
            delivered.append(event.name)
            raise RuntimeError("socket closed")

        s = started_session(FakeConversation([text_step("hello")]))
        asyncio.run(s.run_turn("hi", "req-1", emit=emit))
        assert "streamComplete" in delivered


class TestCancel:
    def test_cancel_reaches_the_conversation(self):
        conv = FakeConversation([text_step("a")])
        s = started_session(conv)

        async def go():
            async for _ in s.stream_turn("hi"):
                await s.cancel()

        asyncio.run(go())
        assert conv.cancels == 1

    def test_cancel_with_no_turn_running_is_a_no_op(self):
        """The caller is a browser button; a spare click is not an error."""
        conv = FakeConversation()
        s = started_session(conv)
        asyncio.run(s.cancel())
        assert conv.cancels == 0

    def test_cancel_before_start_is_a_no_op(self):
        s = AntigravitySession("/tmp/repo", credentials=credentials())
        asyncio.run(s.cancel())

    def test_a_failing_cancel_does_not_raise(self):
        class Stubborn(FakeConversation):
            async def cancel(self):
                raise RuntimeError("no")

        s = started_session(Stubborn([text_step("a")]))

        async def go():
            async for _ in s.stream_turn("hi"):
                await s.cancel()

        asyncio.run(go())


# ----------------------------------------------------------------------
# Teardown: a 119 MB subprocess that must not be orphaned
# ----------------------------------------------------------------------


class FakeStack:
    def __init__(self, *, fails=False):
        self.closed = 0
        self.fails = fails

    async def aclose(self):
        self.closed += 1
        if self.fails:
            raise RuntimeError("harness would not die")


class TestTeardown:
    def test_close_marks_the_session_stopped(self):
        s = started_session(FakeConversation())
        s._exit = FakeStack()
        asyncio.run(s.close())
        assert not s.started

    def test_close_is_idempotent(self):
        s = started_session(FakeConversation())
        stack = s._exit = FakeStack()
        asyncio.run(s.close())
        asyncio.run(s.close())
        assert stack.closed == 1

    def test_a_failing_close_still_marks_it_closed(self):
        """Otherwise the caller retries forever against a dead handle."""
        s = started_session(FakeConversation())
        s._exit = FakeStack(fails=True)
        asyncio.run(s.close())
        assert not s.started

    def test_the_context_manager_closes(self):
        s = AntigravitySession("/tmp/repo", credentials=credentials())
        stack = FakeStack()

        async def fake_start():
            s._conversation = FakeConversation()
            s._exit = stack

        s.start = fake_start

        async def go():
            async with s:
                pass

        asyncio.run(go())
        assert stack.closed == 1

    def test_steps_are_never_drained_after_close(self):
        """The phase-1 hang, guarded in the engine.

        ``chat()`` returned a lazy cursor and reading it after teardown
        hung until killed. The fake refuses to yield once the session says
        it has stopped, so a pump that outlived its connection fails here
        instead of in production.
        """
        s = AntigravitySession("/tmp/repo", credentials=credentials())
        conv = FakeConversation([text_step("a")], alive=lambda: s.started)
        s._conversation = conv
        s._exit = FakeStack()

        async def go():
            events = [e async for e in s.stream_turn("hi")]
            await s.close()
            return events

        assert asyncio.run(go())


# ----------------------------------------------------------------------
# AG-R-9, from the other side
# ----------------------------------------------------------------------


class TestTheBoundaryFromTheEngineSide:
    """The consultant's tripwire, inverted.

    ``receive_steps``, ``cancel`` and ``conversation`` machinery are
    forbidden in ``consultant.py`` and are the whole point of this module.
    What must not cross the other way is ``chat()`` — the one-shot call
    pattern AG-R-9 warns the engine against inheriting, and the specific
    call whose lazy ``ChatResponse`` caused the phase-1 hang.
    """

    def _calls(self) -> set[str]:
        source = Path(inspect.getfile(session_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        return {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_the_engine_does_not_call_chat(self):
        assert "chat" not in self._calls(), (
            "session.py calls chat(). That is the consultant's shape — one "
            "prompt, one lazy response — and AG-R-9 is the record of why an "
            "engine built around it inherits a call pattern it does not have. "
            "Drive send() and receive_steps() instead."
        )

    def test_the_engine_does_use_the_session_machinery(self):
        """The positive half: this is where those names belong."""
        calls = self._calls()
        assert {"send", "receive_steps", "cancel"} <= calls

    def test_the_consultant_is_not_imported(self):
        source = Path(inspect.getfile(session_module)).read_text(encoding="utf-8")
        assert "from aic_dc.antigravity.consultant" not in source


# ----------------------------------------------------------------------
# What the live probe found, 2026-09-02
# ----------------------------------------------------------------------


class FakeUsage:
    """``UsageMetadata``'s fields, by the names the SDK gives them."""

    def __init__(self, prompt=0, cached=0, candidates=0, thoughts=0, total=0):
        self.prompt_token_count = prompt
        self.cached_content_token_count = cached
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.total_token_count = total


async def _drain(session, translator=None):
    from aic_dc.antigravity.steps import StepTranslator

    translator = translator or StepTranslator("smoke")
    return [
        event
        async for event in session.stream_turn("hi", translator=translator)
    ]


class TestTheTurnsFiguresComeFromTheConversation:
    """Both were empty on the first live turn, and neither test caught it.

    ``Step.usage_metadata`` is documented as per-step and was ``None`` on
    all ten steps of a turn that really billed tokens; the figure lives on
    ``Conversation.last_turn_usage``, which the SDK computes as a diff.
    The stop reason lives at ``_last_turn_stop_reason``. Neither is
    reachable from inside ``translate``, so the session hands both over at
    turn close.
    """

    async def test_turn_usage_comes_from_the_conversation(self):
        conversation = FakeConversation(
            [FakeStep()],
            usage=FakeUsage(prompt=13873, cached=13000, candidates=42, total=13915),
        )
        events = await _drain(started_session(conversation))
        usage = next(e for e in events if e.name == "turnUsage")
        assert usage.payload["turn_model_usage"] == {
            "prompt_token_count": 13873,
            "cached_content_token_count": 13000,
            "candidates_token_count": 42,
            "thoughts_token_count": 0,
            "total_token_count": 13915,
        }

    async def test_an_empty_conversation_usage_leaves_the_key_absent(self):
        """AG-9: an absent figure is not a zero.

        The SDK returns ``None`` for a turn whose total came to zero, and
        a `{}` here is what lets the browser hide the figure rather than
        render a measurement nobody took.
        """
        events = await _drain(started_session(FakeConversation([FakeStep()])))
        usage = next(e for e in events if e.name == "turnUsage")
        assert usage.payload["turn_model_usage"] == {}

    async def test_the_stop_reason_is_read_from_the_private_name(self):
        conversation = FakeConversation([FakeStep()], stop_reason="MAX_TURNS_EXCEEDED")
        events = await _drain(started_session(conversation))
        done = next(e for e in events if e.name == "streamComplete")
        assert done.payload["stop_reason"] == "MAX_TURNS_EXCEEDED"

    async def test_a_normal_turn_names_no_terminal_reason(self):
        """`UNSPECIFIED` is the SDK's word for "nothing to report".

        Forwarding it would stamp a red header badge reading
        "UNSPECIFIED" on every clean turn: the browser sends an unmapped
        reason to the header with `severity: 'error'`, deliberately, so an
        engine that always says something must only say it when there is
        something to say.
        """
        conversation = FakeConversation([FakeStep()], stop_reason="UNSPECIFIED")
        events = await _drain(started_session(conversation))
        done = next(e for e in events if e.name == "streamComplete")
        assert done.payload["stop_reason"] == ""

    async def test_a_real_stop_reason_still_gets_through(self):
        """The filter must not swallow the ones that matter.

        `MAX_*_EXCEEDED` naming which cap fired is why AG-6 offers
        `BudgetConfig` in place of a dollar cap.
        """
        conversation = FakeConversation(
            [FakeStep()], stop_reason="QUOTA_EXHAUSTED"
        )
        events = await _drain(started_session(conversation))
        done = next(e for e in events if e.name == "streamComplete")
        assert done.payload["stop_reason"] == "QUOTA_EXHAUSTED"

    async def test_a_usage_read_that_raises_does_not_fail_the_turn(self):
        """The output is already rendered; a missing figure is not a fault."""

        conversation = FakeConversation([FakeStep()])
        # Patched on the instance's type after construction: a property on
        # the subclass would explode in `__init__` instead, which is a
        # different failure from the one under test.
        type(conversation).last_turn_usage = property(
            lambda self: (_ for _ in ()).throw(
                RuntimeError("private surface moved")
            )
        )
        try:
            events = await _drain(started_session(conversation))
        finally:
            del type(conversation).last_turn_usage
        assert any(e.name == "streamComplete" for e in events)

    def test_the_fake_matches_the_sdks_shape(self):
        """The fake may not invent a friendlier surface than the SDK's.

        This is the test that would have caught both bugs, and it is the
        general form of the lesson: an offline double that answers to a
        name the real object does not have cannot fail the way the real
        one does. Skipped where the SDK is not installed, because the
        offline suite must stay runnable without it.
        """
        conversation = pytest.importorskip(
            "google.antigravity.conversation.conversation"
        )
        real = conversation.Conversation
        for name in ("_last_turn_stop_reason", "last_turn_usage"):
            assert hasattr(real, name), (
                f"The SDK no longer has Conversation.{name}. The session "
                f"reads it at turn close; find where it moved to rather "
                f"than letting the figure go quietly empty."
            )
            assert hasattr(FakeConversation(), name), (
                f"FakeConversation is missing {name}, which the real one "
                f"has — the offline suite would pass while a live turn "
                f"reports nothing."
            )

