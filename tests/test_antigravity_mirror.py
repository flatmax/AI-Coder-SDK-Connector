"""Tests for aic_dc.antigravity.mirror — phase 5's repo-local transcript.

Everything here is a **round trip**, and that is the whole design of the
file. The mirror's job is not "write plausible JSONL"; it is "write
something the existing reader renders", and the reader is somebody else's
parser on an SDK that moves. So almost nothing asserts on the entries
directly: events go in one end, ``history.load_session`` comes out the
other, and the assertion is about the rendered message list.

That matters because of how this failed while it was being built. Four
guesses at the entry shape all parsed to **zero messages**, including a
verbatim entry copied out of a real mirror, and nothing raised — an empty
list is exactly what the parser answers for a session that does not
exist. A test asserting on stored fields would have passed on all four.

Two measured facts are pinned here rather than left in a comment, because
both fail silently and in the "looks fine, does nothing" direction:

- **The session id must be a UUID.** ``get_session_messages_from_store``
  validates it and returns ``[]`` otherwise.
- **``parentUuid`` links the chain.** The reader finds the terminal entry
  and walks *back*; with no links every entry is its own terminal and the
  conversation is one message long. A single entry parses either way,
  which is why the minimal-shape bisect did not surface this.

Nothing here starts a harness or touches the network.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid

import pytest

from aic_dc.antigravity.mirror import SessionMirror
from aic_dc.claude_code import history
from aic_dc.claude_code.messages import Event
from aic_dc.claude_code.session_store import RepoSessionStore

CONVERSATION = "cd4edb7f-6de3-468f-9815-e76b310a920a"


def build(tmp_path, *, model="gemini-3.7-flash", run=0):
    """A mirror over a real store, with a deterministic clock and ids.

    ``run`` shifts the id space. The store dedups on ``uuid``, so two
    mirrors sharing a counter would have the second one's entries silently
    swallowed as duplicates — an artefact of making the ids predictable,
    not something real UUID4s do.
    """
    store = RepoSessionStore(tmp_path / "antigravity-sessions")
    ticks = itertools.count(1_700_000_000.0 + run * 1000, 1.0)
    ids = itertools.count(run * 1000 + 1)
    return store, SessionMirror(
        store,
        tmp_path,
        model=lambda: model,
        clock=lambda: next(ticks),
        new_uuid=lambda: f"00000000-0000-4000-8000-{next(ids):012d}",
    )


def load(store, tmp_path, session_id=CONVERSATION):
    return asyncio.run(history.load_session(store, session_id, str(tmp_path)))


def chunk(block_id, content, *, done=False):
    return Event(
        "streamChunk",
        {"block_id": block_id, "seq": 0, "content": content, "done": done,
         "agent_id": None},
    )


async def drive(mirror, events):
    for event in events:
        await mirror.observe(event)


# ----------------------------------------------------------------------
# The round trip
# ----------------------------------------------------------------------


class TestATurnComesBackRendered:
    def test_a_prompt_and_an_answer_render_as_a_conversation(self, tmp_path):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("add a docstring", request_id="r1")
            await drive(mirror, [
                chunk("s1:text", "I will "),
                chunk("s1:text", "I will add one."),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        messages = load(store, tmp_path)

        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "add a docstring"
        # The accumulated content, once — not one entry per chunk. Both
        # pumps send the running total rather than a delta.
        assert messages[1]["content"] == "I will add one."

    def test_a_tool_call_comes_back_as_a_resolved_card(self, tmp_path):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("what is in here?", request_id="r1")
            await drive(mirror, [
                Event("toolUse", {
                    "tool_use_id": "t1",
                    "name": "list_directory",
                    "input": {"directory_path": "."},
                    "status": "pending",
                }),
                Event("toolResult", {
                    "tool_use_id": "t1",
                    "status": "ok",
                    "preview": "README.md\nsrc/",
                }),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        turn = load(store, tmp_path)[1]

        cards = [b for b in turn["blocks"] if b["kind"] == "tool"]
        assert len(cards) == 1
        card = cards[0]
        assert card["tool"]["name"] == "list_directory"
        assert card["done"] is True
        assert card["result"]["status"] == "ok"
        assert card["result"]["preview"] == "README.md\nsrc/"

    def test_thinking_and_prose_stay_separate_blocks(self, tmp_path):
        """A thinking block rendered as prose puts the reasoning on screen."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("think about it", request_id="r1")
            await drive(mirror, [
                Event("thinkingChunk", {
                    "block_id": "s1:thinking", "seq": 0,
                    "content": "the file is small", "done": False,
                }),
                chunk("s1:text", "It is small."),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        turn = load(store, tmp_path)[1]

        assert [b["kind"] for b in turn["blocks"]] == ["thinking", "text"]
        assert turn["blocks"][0]["content"] == "the file is small"
        # `content` is the prose only. Thinking in it would be the model's
        # reasoning quoted back as its answer.
        assert turn["content"] == "It is small."

    def test_a_turn_counts_as_one_engine_turn(self, tmp_path):
        """Not one per block, which is what a per-entry message id gives."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("go", request_id="r1")
            await drive(mirror, [
                chunk("s1:text", "one"),
                chunk("s2:text", "two"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        turn = load(store, tmp_path)[1]
        assert turn["turn"]["num_turns"] == 1

    def test_two_prompts_render_as_two_turns(self, tmp_path):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            for n in ("first", "second"):
                await mirror.note_prompt(n, request_id=f"r-{n}")
                await drive(mirror, [
                    chunk(f"{n}:text", f"answering {n}"),
                    Event("streamComplete", {"request_id": f"r-{n}", "usage": {}}),
                ])

        asyncio.run(run())
        messages = load(store, tmp_path)
        assert [m["role"] for m in messages] == [
            "user", "assistant", "user", "assistant"
        ]
        assert messages[2]["content"] == "second"


# ----------------------------------------------------------------------
# The two facts that fail silently
# ----------------------------------------------------------------------


class TestTheTwoSilentFailures:
    def test_a_non_uuid_conversation_is_not_mirrored_at_all(self, tmp_path):
        """The reader answers ``[]`` for one, which reads as "no session".

        So a mirror keyed on ``agy-1`` would write perfectly good
        transcripts that the history browser reported as missing, with no
        error anywhere. Refused at the door instead, and loudly in the log.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach("antigravity-session-3")
            await mirror.note_prompt("this must not be filed", request_id="r1")

        asyncio.run(run())
        assert mirror.session_id is None
        assert not list((tmp_path / "antigravity-sessions").rglob("*.jsonl"))

    def test_the_chain_is_linked_or_only_the_last_message_survives(self, tmp_path):
        """``parentUuid`` is what makes a conversation more than one message.

        The reader walks back from the terminal entry, so entries with no
        parent are each their own terminal and only the newest is
        rendered. This asserts the linking by asserting the *outcome*: a
        four-entry conversation comes back as four messages.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("one", request_id="r1")
            await drive(mirror, [
                chunk("a:text", "answer one"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])
            await mirror.note_prompt("two", request_id="r2")
            await drive(mirror, [
                chunk("b:text", "answer two"),
                Event("streamComplete", {"request_id": "r2", "usage": {}}),
            ])

        asyncio.run(run())
        assert len(load(store, tmp_path)) == 4

        entries = asyncio.run(
            store.load({
                "project_key": _project_key(tmp_path),
                "session_id": CONVERSATION,
            })
        )
        assert entries[0]["parentUuid"] is None
        assert all(
            e["parentUuid"] == entries[i]["uuid"]
            for i, e in enumerate(entries[1:])
        )


def _project_key(directory):
    from claude_agent_sdk import project_key_for_directory

    return project_key_for_directory(str(directory))


# ----------------------------------------------------------------------
# One observer, two payload vocabularies
# ----------------------------------------------------------------------


class TestBothPumpsFeedOneObserver:
    """The event *names* agree and the payloads do not.

    Phase 8 made both translators emit ``toolResult``; it did not make
    them emit the same one. ``StepTranslator`` says
    ``{status: "ok", preview: …}`` and ``AgyTranslator`` says
    ``{status: "success", content: …}``. A reader that knew one spelling
    would silently mark every successful call on the other transport as an
    error, and store an empty result body for it — visible only as tool
    cards that render red with no output.

    Driven from the **real** translators rather than from hand-written
    payloads, so a pump that changes its field names fails here.
    """

    def _card(self, tmp_path, events):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("do it", request_id="r1")
            await drive(mirror, events)
            await mirror.observe(
                Event("streamComplete", {"request_id": "r1", "usage": {}})
            )

        asyncio.run(run())
        turn = load(store, tmp_path)[1]
        cards = [b for b in turn["blocks"] if b["kind"] == "tool"]
        assert len(cards) == 1
        return cards[0]

    def test_the_agy_pumps_own_events_resolve_a_card(self, tmp_path):
        from aic_dc.agy.steps import AgyTranslator

        translator = AgyTranslator("r1")
        events = []
        for state in ("RUNNING", "DONE"):
            events += translator.translate({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 3,
                    "state": state,
                    "tool_name": "view_file",
                    "tool_info": {
                        "parameters": {"AbsolutePath": "/tmp/x/README.md"},
                        "output": "hello",
                    },
                },
            })

        card = self._card(tmp_path, events)
        assert card["result"]["status"] == "ok", (
            "agy reports a successful call as 'success' and the SDK pump "
            "as 'ok'. One of the two spellings has been forgotten."
        )
        assert card["result"]["preview"] == "hello"

    def test_the_sdk_pumps_own_events_resolve_a_card(self, tmp_path):
        from aic_dc.antigravity.steps import StepTranslator

        translator = StepTranslator("r1")
        events = []
        for status in ("ACTIVE", "DONE"):
            events += translator.translate(
                _step(status, {"combined_output": "ok\n", "exit_code": 0})
                if status == "DONE"
                else _step(status, {})
            )

        card = self._card(tmp_path, events)
        assert card["result"]["status"] == "ok"
        assert card["result"]["preview"] == "ok\n"

    def test_a_failure_is_recorded_as_one(self, tmp_path):
        card = self._card(tmp_path, [
            Event("toolUse", {
                "tool_use_id": "t1", "name": "run_command", "input": {},
            }),
            Event("toolResult", {
                "tool_use_id": "t1", "status": "error", "preview": "boom",
            }),
        ])
        assert card["result"]["status"] == "error"

    def test_an_unrecognised_status_reads_as_a_failure(self, tmp_path):
        """The safe direction: a green card over a failure is the worse lie."""
        card = self._card(tmp_path, [
            Event("toolUse", {"tool_use_id": "t1", "name": "x", "input": {}}),
            Event("toolResult", {"tool_use_id": "t1", "status": "fine"}),
        ])
        assert card["result"]["status"] == "error"


class _Call:
    def __init__(self, args):
        self.id = "t1"
        self.name = "run_command"
        self.args = {"command_line": "ls", **args}
        self.server_name = None


class _Step:
    id = "traj:3"
    depth = 0
    trajectory_id = "traj"
    step_index = 3
    content = ""
    content_delta = ""
    thinking = ""
    thinking_delta = ""
    error = ""
    usage_metadata = None
    source = "MODEL"
    target = "USER"
    type = "TOOL_CALL"


def _step(status, result_args):
    step = _Step()
    step.status = status
    step.tool_calls = [_Call(result_args)]
    return step


# ----------------------------------------------------------------------
# Files the turn touched
# ----------------------------------------------------------------------


class TestFileAttributionSurvivesTheRoundTrip:
    """The turn footer lists the same files after a reload as before it.

    ``files_written_by`` is the one home for the tool → path-key table,
    and it has to know all three vocabularies or a browsed Antigravity
    turn attributes nothing. That failure is quiet: no error, the list is
    just shorter.
    """

    @pytest.mark.parametrize(
        "name,tool_input,expected",
        [
            ("edit_file", {"file_path": "/repo/a.py"}, "/repo/a.py"),
            ("create_file", {"file_path": "/repo/b.py"}, "/repo/b.py"),
            ("replace_file_content", {"TargetFile": "/repo/c.py"}, "/repo/c.py"),
            ("write_to_file", {"TargetFile": "/repo/d.py"}, "/repo/d.py"),
            ("generate_image", {"output_path": "/repo/e.png"}, "/repo/e.png"),
            ("generate_image", {"OutputPath": "/repo/f.png"}, "/repo/f.png"),
        ],
    )
    def test_a_write_is_attributed_after_a_reload(
        self, tmp_path, name, tool_input, expected
    ):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("write it", request_id="r1")
            await drive(mirror, [
                Event("toolUse", {
                    "tool_use_id": "t1", "name": name, "input": tool_input,
                }),
                Event("toolResult", {
                    "tool_use_id": "t1", "status": "ok", "preview": "done",
                }),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        turn = load(store, tmp_path)[1]
        assert turn["files"] == [expected]

    def test_the_live_pump_and_the_reader_agree(self, tmp_path):
        """One table, asserted from both sides rather than by inspection."""
        from aic_dc.antigravity.steps import _files_written
        from aic_dc.claude_code.messages import files_written_by

        args = {"file_path": "/repo/a.py"}
        assert _files_written("edit_file", args) == files_written_by(
            "edit_file", args
        )

    def test_a_failed_write_attributes_nothing(self, tmp_path):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("write it", request_id="r1")
            await drive(mirror, [
                Event("toolUse", {
                    "tool_use_id": "t1",
                    "name": "edit_file",
                    "input": {"file_path": "/repo/a.py"},
                }),
                Event("toolResult", {
                    "tool_use_id": "t1", "status": "error", "preview": "no",
                }),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        assert load(store, tmp_path)[1]["files"] == []


# ----------------------------------------------------------------------
# Attaching, deferring, resuming
# ----------------------------------------------------------------------


class TestAttaching:
    def test_a_prompt_sent_before_the_id_exists_is_held_not_dropped(
        self, tmp_path
    ):
        """The ordinary first turn on the SDK transport.

        ``Conversation.conversation_id`` is derived from the event
        processor's main trajectory, so it is empty until the first step
        lands — which is *after* the prompt was sent. Dropping it would
        lose the first prompt of every conversation, and writing it late
        would put it after the answer it produced.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.note_prompt("the very first thing", request_id="r1")
            await mirror.attach(CONVERSATION)
            await drive(mirror, [
                chunk("s1:text", "answering"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        messages = load(store, tmp_path)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "the very first thing"

    def test_attaching_to_the_same_id_twice_changes_nothing(self, tmp_path):
        """It is called on every event of a turn, so it must be a no-op."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("hello", request_id="r1")
            for _ in range(5):
                await mirror.attach(CONVERSATION)
            await drive(mirror, [
                chunk("s1:text", "hi"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        assert len(load(store, tmp_path)) == 2

    def test_a_resumed_conversation_continues_its_chain(self, tmp_path):
        """A second process appending must not start a second chain.

        The reader picks one terminal entry and walks back from it, so an
        unparented entry appended to an existing transcript makes
        everything before it stop rendering — a resume that looked like it
        had lost the conversation.
        """
        store, first = build(tmp_path)

        async def session_one():
            await first.attach(CONVERSATION)
            await first.note_prompt("before the restart", request_id="r1")
            await drive(first, [
                chunk("a:text", "answered before"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(session_one())

        _, second = build(tmp_path, run=1)

        async def session_two():
            await second.attach(CONVERSATION)
            await second.note_prompt("after the restart", request_id="r2")
            await drive(second, [
                chunk("b:text", "answered after"),
                Event("streamComplete", {"request_id": "r2", "usage": {}}),
            ])

        asyncio.run(session_two())

        messages = load(store, tmp_path)
        assert [m["content"] for m in messages if m["role"] == "user"] == [
            "before the restart",
            "after the restart",
        ]

    def test_detaching_stops_the_mirror(self, tmp_path):
        """``new_session``: the conversation the user discarded gets nothing."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("kept", request_id="r1")
            await mirror.observe(
                Event("streamComplete", {"request_id": "r1", "usage": {}})
            )
            mirror.detach()
            await mirror.note_prompt("discarded", request_id="r2")

        asyncio.run(run())
        assert [m["content"] for m in load(store, tmp_path)] == ["kept"]


# ----------------------------------------------------------------------
# The listing
# ----------------------------------------------------------------------


class TestTheHistoryBrowserSeesIt:
    def test_a_mirrored_conversation_is_listed_with_its_prompt(self, tmp_path):
        """A session with no extractable prompt is *dropped* by the lister.

        Not errored — dropped. So a mirror whose user entries the SDK's
        head scan could not read would produce a history browser that was
        simply empty, with every transcript on disk.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("rename the widget", request_id="r1")
            await drive(mirror, [
                chunk("s1:text", "renamed"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        rows = asyncio.run(history.list_sessions(store, str(tmp_path)))
        assert [r["session_id"] for r in rows] == [CONVERSATION]
        assert "rename the widget" in rows[0]["preview"]

    def test_a_deleted_conversation_stops_being_listed(self, tmp_path):
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("temporary", request_id="r1")
            await mirror.observe(
                Event("streamComplete", {"request_id": "r1", "usage": {}})
            )
            await history.delete_session(store, CONVERSATION, str(tmp_path))

        asyncio.run(run())
        assert asyncio.run(history.list_sessions(store, str(tmp_path))) == []


# ----------------------------------------------------------------------
# What is deliberately not written
# ----------------------------------------------------------------------


class TestWhatIsLeftOut:
    def test_a_turn_that_produced_nothing_writes_no_empty_answer(self, tmp_path):
        """A turn killed before its first token has no assistant message.

        An empty assistant entry would render as the model having answered
        with silence, which is a different thing from a turn that failed.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("go", request_id="r1")
            await mirror.observe(
                Event("streamComplete", {"request_id": "r1", "usage": {}})
            )

        asyncio.run(run())
        assert [m["role"] for m in load(store, tmp_path)] == ["user"]

    def test_engine_errors_are_not_written_as_transcript_entries(self, tmp_path):
        """There is no entry type that renders one; a line nobody reads
        is worse than an honest absence. Their home is the events log."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("go", request_id="r1")
            await drive(mirror, [
                Event("systemEvent", {
                    "subtype": "engine_error",
                    "data": {"message": "429"},
                }),
                chunk("s1:text", "recovered"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        turn = load(store, tmp_path)[1]
        assert [b["kind"] for b in turn["blocks"]] == ["text"]

    def test_a_turn_writes_no_entry_of_its_own_to_carry_tokens(self, tmp_path):
        """Found in a browser: the history row said "3 msgs" for two.

        The first cut wrote a closing assistant entry to hold the turn's
        token counters. It rendered nothing — an assistant entry with no
        content blocks folds into the turn card and adds no block — but
        ``message_count`` counts *parsed messages*, so every turn inflated
        the row by one. It bought no figure either, since this engine's
        counter names are not the four the reader sums.

        So the entries are exactly the conversation, and each assistant
        one carries an empty ``usage`` under the turn's shared id, which
        is what still makes it one engine turn rather than one per block.
        """
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("go", request_id="r1")
            await drive(mirror, [
                chunk("s1:text", "done"),
                Event("streamComplete", {
                    "request_id": "r1",
                    "usage": {"prompt_token_count": 13873},
                }),
            ])

        asyncio.run(run())
        entries = asyncio.run(
            store.load({
                "project_key": _project_key(tmp_path),
                "session_id": CONVERSATION,
            })
        )
        assert [e["type"] for e in entries] == ["user", "assistant"]
        assert all(e["message"]["usage"] == {} for e in entries[1:])
        turn = load(store, tmp_path)[1]["turn"]
        assert turn["num_turns"] == 1
        # An empty per-model entry, which `modelUsageLines` skips — no
        # chip, rather than a chip reading zero.
        assert turn["turn_model_usage"] == {"gemini-3.7-flash": {}}

    def test_a_broken_event_does_not_kill_the_turn(self, tmp_path):
        """A mirror that raised would trade a transcript for a conversation."""
        store, mirror = build(tmp_path)

        async def run():
            await mirror.attach(CONVERSATION)
            await mirror.note_prompt("go", request_id="r1")
            await mirror.observe(Event("toolUse", None))
            await drive(mirror, [
                chunk("s1:text", "still here"),
                Event("streamComplete", {"request_id": "r1", "usage": {}}),
            ])

        asyncio.run(run())
        assert load(store, tmp_path)[1]["content"] == "still here"


def test_the_conversation_id_both_transports_produce_is_a_uuid():
    """The fact the whole keying rests on, stated as an assertion.

    ``agy``'s ``init`` frame carries this exact id, captured in
    ``sdk-surface.md``, and the SDK's ``conversation_id`` is the same
    shape. If either ever stops being one, the mirror stops recording and
    says so in the log rather than filing under a key the reader rejects.
    """
    assert uuid.UUID(CONVERSATION)


def test_the_message_count_is_the_conversation(tmp_path):
    """The browser-found wart, pinned where it was found: the row count.

    ``summarise_session`` counts parsed messages, which is what the
    history browser renders as "N msgs". A prompt and an answer is two.
    """
    store, mirror = build(tmp_path)

    async def run():
        await mirror.attach(CONVERSATION)
        await mirror.note_prompt("say ok", request_id="r1")
        await drive(mirror, [
            chunk("s1:text", "ok"),
            Event("streamComplete", {"request_id": "r1", "usage": {}}),
        ])

    asyncio.run(run())
    rows = asyncio.run(history.list_sessions(store, str(tmp_path)))
    assert rows[0]["message_count"] == 2
