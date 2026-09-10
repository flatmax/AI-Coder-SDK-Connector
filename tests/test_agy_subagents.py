"""What a subagent did, read out of ``agy``'s conversation store.

Every inference in :mod:`aic_dc.agy.subagents` was drawn from one capture
on 2026-09-10, and each of them had a plausible wrong answer available.
So these tests are mostly *falsifications of the plausible reader*: the one
that trusts the tool's name, the one that pairs a result with the wrong
call, the one that reads ``transcript.jsonl`` because it is the file with
the obvious name, the one that hands a caller any conversation on the
machine because the id looked like a UUID.

The record fixtures below are the shapes that capture actually held —
abbreviated, never invented. Where a field is elided it is because this
reader never reads it, and where one is spelled oddly (``conversationId``
in prose, ``Subagents`` in PascalCase) that is the CLI's spelling and not
a typo.

Offline. No ``agy``, no network; the store is a ``tmp_path`` tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from aic_dc.agy import subagents

# ---------------------------------------------------------------------------
# Fixtures: the capture's shapes, small enough to read
# ---------------------------------------------------------------------------

PARENT = "1106960b-42e8-453c-a510-105f4a29d5bb"
CHILD = "c21acd4d-f093-473e-9df7-d199d10b1329"
GRANDCHILD = "9f0f2ad0-0000-4000-8000-000000000001"


def record(index, kind, **extra):
    """One transcript record. ``status`` and ``source`` default as captured."""
    base = {
        "step_index": index,
        "type": kind,
        "source": {"USER_INPUT": "USER_EXPLICIT", "SYSTEM_MESSAGE": "SYSTEM"}.get(
            kind, "MODEL"
        ),
        "status": "DONE",
        "created_at": "2026-09-10T04:00:00Z",
    }
    base.update(extra)
    return base


def spawn(index, *subagent_args, name="invoke_subagent"):
    """A ``PLANNER_RESPONSE`` whose call delegates."""
    return record(
        index,
        "PLANNER_RESPONSE",
        content="Delegating.",
        tool_calls=[{"name": name, "args": {"Subagents": list(subagent_args)}}],
    )


def subagent_arg(role="Notes Reader", prompt="Please read `notes.txt`."):
    return {
        "Model": "inherit",
        "Prompt": prompt,
        "Role": role,
        "TypeName": "research",
        "Workspace": "inherit",
    }


def announcement(index, *conversation_ids):
    """The ``GENERIC`` result that reports the ids — in prose, as captured."""
    bodies = "\n".join(
        json.dumps(
            {
                "conversationId": cid,
                "logAbsoluteUri": f"file:///brain/{cid}/log.jsonl",
                "workspaceUris": ["file:///tmp/temp/agy-subagent-4t83wchm"],
            },
            indent=2,
        )
        for cid in conversation_ids
    )
    return record(
        index,
        "GENERIC",
        content=(
            "Created At: 2026-09-10T04:00:00Z\n"
            "Created the following subagents:\n"
            f"{bodies}\n"
            "The subagents will send you a message when they have completed "
            "their task."
        ),
    )


def write_transcript(brain_dir, conversation_id, records, name="transcript_full.jsonl"):
    directory = brain_dir / conversation_id / subagents.LOG_SUBPATH
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


def a_delegating_parent(brain_dir, child=CHILD, role="Notes Reader"):
    """The captured turn: one delegation, its announcement, its reply."""
    write_transcript(
        brain_dir,
        PARENT,
        [
            record(0, "USER_INPUT", content="Ask a subagent to read notes.txt."),
            spawn(1, subagent_arg(role=role)),
            announcement(2, child),
            record(
                3,
                "SYSTEM_MESSAGE",
                content=(
                    "The following is a <SYSTEM_MESSAGE> not actually sent by "
                    "the user.\n\n<SYSTEM_MESSAGE>\n[Message] "
                    f"sender={child} content=The word is **PELICAN**.\n"
                    "</SYSTEM_MESSAGE>"
                ),
            ),
        ],
    )


def a_child(brain_dir, conversation_id=CHILD, records=None):
    write_transcript(
        brain_dir,
        conversation_id,
        records
        if records is not None
        else [
            record(
                0,
                "USER_INPUT",
                content=(
                    "<USER_REQUEST>\nPlease read `notes.txt`.\n</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>\nThe current local time is: "
                    "2026-09-10 04:00\n</ADDITIONAL_METADATA>"
                ),
            ),
            record(
                1,
                "PLANNER_RESPONSE",
                content="Reading it.",
                tool_calls=[
                    {
                        "name": "view_file",
                        "args": {"AbsolutePath": "/tmp/temp/x/notes.txt"},
                    }
                ],
                created_at="2026-09-10T04:00:01Z",
            ),
            record(
                2,
                "GENERIC",
                content="PELICAN",
                created_at="2026-09-10T04:00:03Z",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Finding the file
# ---------------------------------------------------------------------------


class TestWhichFileIsRead:
    """The measurement that would otherwise be a rendering bug.

    Both files exist in every capture and they disagree about tool
    arguments: ``transcript.jsonl`` double-encodes each value as a JSON
    string, so every card would render ``"\"/tmp/x\""``. A reader that
    took the obvious filename would look correct in a listing and wrong in
    every tool card.
    """

    def test_the_full_transcript_wins(self, tmp_path):
        write_transcript(tmp_path, CHILD, [record(0, "USER_INPUT", content="a")])
        write_transcript(
            tmp_path,
            CHILD,
            [record(0, "USER_INPUT", content="b")],
            name="transcript.jsonl",
        )
        path = subagents.transcript_path(CHILD, brain_dir=tmp_path)
        assert path is not None
        assert path.name == "transcript_full.jsonl"

    def test_the_lesser_file_is_still_read_when_it_is_the_only_one(self, tmp_path):
        """A release that stopped writing the full file should degrade to a
        shabby tool card, not to no tab."""
        write_transcript(
            tmp_path,
            CHILD,
            [record(0, "USER_INPUT", content="b")],
            name="transcript.jsonl",
        )
        path = subagents.transcript_path(CHILD, brain_dir=tmp_path)
        assert path is not None
        assert path.name == "transcript.jsonl"

    def test_an_unknown_conversation_has_no_path(self, tmp_path):
        assert subagents.transcript_path(CHILD, brain_dir=tmp_path) is None

    def test_a_directory_with_no_transcript_has_no_path(self, tmp_path):
        (tmp_path / CHILD / subagents.LOG_SUBPATH).mkdir(parents=True)
        assert subagents.transcript_path(CHILD, brain_dir=tmp_path) is None

    def test_an_empty_id_has_no_path(self, tmp_path):
        assert subagents.transcript_path("", brain_dir=tmp_path) is None


class TestAnIdIsNotAPath:
    """The check that holds even if a caller forgets the ownership one.

    ``agent_id`` arrives from the browser and is concatenated onto a
    directory. ``descendants`` is the real containment; this is the second
    line, and it is here because the cost of being wrong is reading the
    user's home directory rather than showing an empty tab.
    """

    def test_a_traversal_is_refused_even_though_the_file_exists(self, tmp_path):
        brain = tmp_path / "brain"
        outside = tmp_path / "secrets"
        write_transcript(outside, "private", [record(0, "USER_INPUT", content="x")])
        brain.mkdir()
        escape = str(Path("..") / "secrets" / "private")
        assert subagents.transcript_path(escape, brain_dir=brain) is None
        assert subagents.load(escape, brain_dir=brain) == []

    def test_a_backslash_is_refused_too(self, tmp_path):
        assert subagents.transcript_path("..\\other", brain_dir=tmp_path) is None

    def test_a_bare_parent_reference_is_refused(self, tmp_path):
        assert subagents.transcript_path("..", brain_dir=tmp_path) is None


class TestReadingRecords:
    def test_a_half_written_last_line_costs_only_itself(self, tmp_path):
        """The file belongs to a live process appending to it."""
        path = write_transcript(tmp_path, CHILD, [record(0, "USER_INPUT", content="a")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"step_index": 1, "type": "PLANNER_RES')
        got = subagents.read_records(path)
        assert [r["step_index"] for r in got] == [0]

    def test_a_json_line_that_is_not_an_object_is_skipped(self, tmp_path):
        path = write_transcript(tmp_path, CHILD, [record(0, "USER_INPUT", content="a")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]\n")
        assert len(subagents.read_records(path)) == 1

    def test_records_come_back_in_the_order_the_file_holds_them(self, tmp_path):
        """This sorted by ``step_index`` until a second measurement.

        On 120 of the 121 transcripts on this machine the two orders agree.
        The one that disagrees interleaves two concurrent turns and uses
        five indices twice, so sorting moved its records *away* from what
        the harness wrote. The index still names a result's call; it is not
        an ordering.
        """
        path = write_transcript(
            tmp_path,
            CHILD,
            [record(2, "GENERIC", content="c"), record(0, "USER_INPUT", content="a")],
        )
        assert [r["step_index"] for r in subagents.read_records(path)] == [2, 0]

    def test_an_unreadable_file_is_empty_rather_than_fatal(self, tmp_path):
        assert subagents.read_records(tmp_path / "nothing.jsonl") == []


# ---------------------------------------------------------------------------
# What a conversation delegated
# ---------------------------------------------------------------------------


class TestAnnouncements:
    """Two records make one announcement, and neither is sufficient."""

    def test_the_call_and_its_result_are_paired(self, tmp_path):
        a_delegating_parent(tmp_path)
        got = subagents.announcements(PARENT, brain_dir=tmp_path)
        assert got == [
            {
                "role": "Notes Reader",
                "type_name": "research",
                "prompt": "Please read `notes.txt`.",
                "log_uri": "",
                "agent_id": CHILD,
            }
        ]

    def test_two_subagents_in_one_call_pair_by_position(self, tmp_path):
        """The only correspondence either record offers."""
        write_transcript(
            tmp_path,
            PARENT,
            [
                spawn(
                    1,
                    subagent_arg(role="First"),
                    subagent_arg(role="Second"),
                ),
                announcement(2, "id-one", "id-two"),
            ],
        )
        got = subagents.announcements(PARENT, brain_dir=tmp_path)
        assert [(e["role"], e["agent_id"]) for e in got] == [
            ("First", "id-one"),
            ("Second", "id-two"),
        ]

    def test_a_count_mismatch_drops_the_surplus(self, tmp_path, caplog):
        """Rather than pairing anyway. A row whose id belongs to a
        different subagent opens the wrong transcript under the right
        label, which is worse than a missing row."""
        write_transcript(
            tmp_path,
            PARENT,
            [
                spawn(1, subagent_arg(role="First"), subagent_arg(role="Second")),
                announcement(2, "id-one"),
            ],
        )
        got = subagents.announcements(PARENT, brain_dir=tmp_path)
        assert [(e["role"], e["agent_id"]) for e in got] == [("First", "id-one")]
        assert "reported" in caplog.text

    def test_the_tool_name_is_not_what_is_matched(self, tmp_path):
        """This transport says ``invoke_subagent``; the SDK's inventory says
        ``start_subagent``. A reader keyed to a name is one rename away
        from listing nothing — silently, because a name that matches
        nothing looks exactly like a turn that delegated nothing."""
        write_transcript(
            tmp_path,
            PARENT,
            [
                spawn(1, subagent_arg(), name="start_subagent"),
                announcement(2, CHILD),
            ],
        )
        got = subagents.announcements(PARENT, brain_dir=tmp_path)
        assert [e["agent_id"] for e in got] == [CHILD]

    def test_a_spawn_with_no_result_announces_nothing(self, tmp_path, caplog):
        """The ids are in the result. Without it there is no transcript to
        open, and the honest listing is empty rather than a row with no
        destination."""
        write_transcript(tmp_path, PARENT, [spawn(1, subagent_arg())])
        assert subagents.announcements(PARENT, brain_dir=tmp_path) == []
        assert "no result step" in caplog.text

    def test_a_second_spawn_before_the_first_result_is_reported(self, tmp_path, caplog):
        write_transcript(
            tmp_path,
            PARENT,
            [
                spawn(1, subagent_arg(role="First")),
                spawn(2, subagent_arg(role="Second")),
                announcement(3, "id-two"),
            ],
        )
        got = subagents.announcements(PARENT, brain_dir=tmp_path)
        assert [(e["role"], e["agent_id"]) for e in got] == [("Second", "id-two")]
        assert "never reported" in caplog.text

    def test_an_ordinary_result_is_not_an_announcement(self, tmp_path):
        write_transcript(
            tmp_path,
            PARENT,
            [
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Reading.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/x"}}],
                ),
                record(2, "GENERIC", content="file contents"),
            ],
        )
        assert subagents.announcements(PARENT, brain_dir=tmp_path) == []

    def test_a_conversation_with_no_transcript_announces_nothing(self, tmp_path):
        assert subagents.announcements(PARENT, brain_dir=tmp_path) == []


class TestDescendants:
    """The containment set, which is what makes the RPC not a hole."""

    def test_a_child_is_reachable(self, tmp_path):
        a_delegating_parent(tmp_path)
        assert subagents.descendants(PARENT, brain_dir=tmp_path) == {CHILD}

    def test_a_grandchild_is_reachable_too(self, tmp_path):
        """Wider than the listing needs, deliberately: a containment check
        that has to be widened later is one that gets widened wrongly."""
        a_delegating_parent(tmp_path)
        write_transcript(
            tmp_path,
            CHILD,
            [spawn(1, subagent_arg(role="Deeper")), announcement(2, GRANDCHILD)],
        )
        assert subagents.descendants(PARENT, brain_dir=tmp_path) == {
            CHILD,
            GRANDCHILD,
        }

    def test_the_conversation_itself_is_not_its_own_subagent(self, tmp_path):
        """Including it would let ``get_subagent_transcript`` read the
        session's main transcript through a door meant for its children."""
        a_delegating_parent(tmp_path)
        assert PARENT not in subagents.descendants(PARENT, brain_dir=tmp_path)

    def test_a_self_announcement_does_not_open_that_door_either(self, tmp_path):
        write_transcript(
            tmp_path,
            PARENT,
            [spawn(1, subagent_arg()), announcement(2, PARENT)],
        )
        assert subagents.descendants(PARENT, brain_dir=tmp_path) == set()

    def test_a_cycle_terminates(self, tmp_path):
        write_transcript(
            tmp_path,
            PARENT,
            [spawn(1, subagent_arg()), announcement(2, CHILD)],
        )
        write_transcript(
            tmp_path,
            CHILD,
            [spawn(1, subagent_arg()), announcement(2, PARENT)],
        )
        assert subagents.descendants(PARENT, brain_dir=tmp_path) == {CHILD}

    def test_the_limit_is_reported_rather_than_silently_narrowing(
        self, tmp_path, caplog
    ):
        """A containment check that quietly stopped looking would refuse a
        transcript the user is entitled to, and it would read as "the
        record is gone"."""
        a_delegating_parent(tmp_path)
        write_transcript(
            tmp_path,
            CHILD,
            [spawn(1, subagent_arg()), announcement(2, GRANDCHILD)],
        )
        got = subagents.descendants(PARENT, brain_dir=tmp_path, limit=1)
        assert got == {CHILD}
        assert "Stopped walking" in caplog.text

    def test_an_unknown_conversation_owns_nothing(self, tmp_path):
        assert subagents.descendants(PARENT, brain_dir=tmp_path) == set()


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


class TestRows:
    def test_one_row_per_subagent_with_the_announced_labels(self, tmp_path):
        a_delegating_parent(tmp_path)
        a_child(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert row["agent_id"] == CHILD
        assert row["description"] == "Notes Reader"
        assert row["agent_type"] == "research"

    def test_the_count_is_what_the_tab_will_draw(self, tmp_path):
        """Not the record count: a call and its result are two records in
        one message, and the number beside a row should be the number of
        things the tab draws."""
        a_delegating_parent(tmp_path)
        a_child(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert row["message_count"] == len(subagents.load(CHILD, brain_dir=tmp_path))
        assert row["message_count"] == 2

    def test_the_preview_is_the_subagents_own_first_prompt_unframed(self, tmp_path):
        a_delegating_parent(tmp_path)
        a_child(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert row["preview"] == "Please read `notes.txt`."

    def test_a_row_survives_its_transcript_being_gone(self, tmp_path):
        """The prompt came from the announcement, so a row whose record was
        pruned still says what the subagent was asked to do."""
        a_delegating_parent(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert row["preview"] == "Please read `notes.txt`."
        assert row["message_count"] == 0
        assert row["subpath"] == ""

    def test_the_subpath_is_relative_to_the_store(self, tmp_path):
        """An absolute one would put another product's directory layout in
        the browser."""
        a_delegating_parent(tmp_path)
        a_child(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert row["subpath"] == str(
            Path(CHILD) / subagents.LOG_SUBPATH / "transcript_full.jsonl"
        )

    def test_no_row_carries_a_task_id(self, tmp_path):
        """``task_id`` is what ``stop_task`` takes, and ``subagent_stop`` is
        unbuilt here — a value there would be a handle onto a method that
        would refuse it. This transport's transcript carries no call id to
        put in it anyway."""
        a_delegating_parent(tmp_path)
        a_child(tmp_path)
        (row,) = subagents.rows(PARENT, brain_dir=tmp_path)
        assert "task_id" not in row

    def test_the_listing_is_one_level_deep(self, tmp_path):
        """"What did this session delegate?" is a question about this
        session. A subagent's own subagents belong to its row's transcript,
        where the delegating call is a tool card like any other."""
        a_delegating_parent(tmp_path)
        write_transcript(
            tmp_path,
            CHILD,
            [spawn(1, subagent_arg(role="Deeper")), announcement(2, GRANDCHILD)],
        )
        got = subagents.rows(PARENT, brain_dir=tmp_path)
        assert [r["agent_id"] for r in got] == [CHILD]

    def test_a_session_that_delegated_nothing_lists_nothing(self, tmp_path):
        a_child(tmp_path, conversation_id=PARENT)
        assert subagents.rows(PARENT, brain_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestLoad:
    def test_an_absent_conversation_renders_nothing(self, tmp_path):
        assert subagents.load(CHILD, brain_dir=tmp_path) == []

    def test_the_prompt_is_unframed(self, tmp_path):
        """The ``<USER_REQUEST>`` wrapper and the ``<ADDITIONAL_METADATA>``
        clock are the harness talking to the model. A tab that showed them
        would bury every prompt under plumbing nobody wrote."""
        a_child(tmp_path)
        first = subagents.load(CHILD, brain_dir=tmp_path)[0]
        assert first["role"] == "user"
        assert first["content"] == "Please read `notes.txt`."

    def test_prose_with_no_framing_is_left_alone(self, tmp_path):
        a_child(
            tmp_path,
            records=[record(0, "USER_INPUT", content="Just read it.")],
        )
        assert subagents.load(CHILD, brain_dir=tmp_path)[0]["content"] == (
            "Just read it."
        )

    def test_a_call_and_the_generic_after_it_are_one_tool_block(self, tmp_path):
        """The measured inference, and the one that would be plausible and
        wrong done any other way: no record type carries a tool result and
        no id links the two, so order is the linkage."""
        a_child(tmp_path)
        assistant = subagents.load(CHILD, brain_dir=tmp_path)[1]
        kinds = [b["kind"] for b in assistant["blocks"]]
        assert kinds == ["text", "tool"]
        block = assistant["blocks"][1]
        assert block["tool"]["name"] == "view_file"
        assert block["tool"]["input"] == {"AbsolutePath": "/tmp/temp/x/notes.txt"}
        assert block["result"]["preview"] == "PELICAN"
        assert block["result"]["status"] == "ok"
        assert block["done"] is True

    def test_two_calls_in_one_step_stay_apart_and_pair_in_order(self, tmp_path):
        """The live pump keys a card by step index because a ``tool`` step
        on the stream carries exactly one call. The transcript batches a
        step's calls into one record, so the position is what keeps two
        calls at one step from becoming one card."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Two at once.",
                    tool_calls=[
                        {"name": "view_file", "args": {"AbsolutePath": "/a"}},
                        {"name": "view_file", "args": {"AbsolutePath": "/b"}},
                    ],
                ),
                record(2, "GENERIC", content="first"),
                record(3, "GENERIC", content="second"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        tools = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert [t["tool"]["tool_use_id"] for t in tools] == [
            "agy-tool-1-0",
            "agy-tool-1-1",
        ]
        assert [t["result"]["preview"] for t in tools] == ["first", "second"]

    def test_a_result_two_steps_past_its_call_answers_no_call(self, tmp_path):
        """The arithmetic, stated as a refusal.

        A result names its call as *call index + 1 + position*, so index 3
        after a one-call step 1 belongs to a second call that step never
        made. Accepting it anyway is the pairing this reader was fixed out
        of, and it is silent: the card looks answered.
        """
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="One call.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                ),
                record(3, "GENERIC", content="not mine"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"] is None
        assert block["done"] is False
        assert "not mine" in assistant["blocks"][-1]["content"]
        assert assistant["blocks"][-1]["kind"] == "text"

    def test_a_call_our_dialog_refused_does_not_take_the_next_calls_output(
        self, tmp_path
    ):
        """The measurement that overturned this reader's central inference.

        Read against all 121 transcripts on this machine, 54 of 720 results
        answered no call by the old rule — and the gate probe's own subagent
        transcript says why: a call our permission dialog refuses is written
        **nowhere**, so steps 4, 10 and 14 there have no result at all.
        Pairing by queue order, the ``list_directory`` output lands in the
        refused ``write_to_file`` card, ``files_written_by`` credits a file
        that was never written, and every later card is one call out of
        step. The hole is what the arithmetic is for.
        """
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Writing.",
                    tool_calls=[
                        {
                            "name": "write_to_file",
                            "args": {"TargetFile": "/tmp/temp/x/notes.txt"},
                        }
                    ],
                ),
                # No result: the dialog said no, and nothing recorded that.
                record(
                    3,
                    "PLANNER_RESPONSE",
                    content="Looking instead.",
                    tool_calls=[
                        {"name": "list_directory", "args": {"DirectoryPath": "/tmp"}}
                    ],
                ),
                record(4, "GENERIC", content="notes.txt"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        write, listing = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert write["tool"]["name"] == "write_to_file"
        assert write["result"] is None
        assert write["done"] is False
        assert listing["tool"]["name"] == "list_directory"
        assert listing["result"]["preview"] == "notes.txt"
        assert assistant["files"] == []

    def test_only_the_second_of_two_calls_being_answered_pairs_correctly(
        self, tmp_path
    ):
        """The same hole inside one step rather than between two.

        Two calls at step 1 are answered at 2 and 3; a dialog that refuses
        the first leaves only 3. By queue order that output becomes the
        refused call's, and the call that actually ran is left pending —
        both cards wrong from one missing record.
        """
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Two at once.",
                    tool_calls=[
                        {
                            "name": "write_to_file",
                            "args": {"TargetFile": "/tmp/temp/x/notes.txt"},
                        },
                        {"name": "view_file", "args": {"AbsolutePath": "/b"}},
                    ],
                ),
                record(3, "GENERIC", content="second"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        write, view = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert write["result"] is None
        assert view["result"]["preview"] == "second"
        assert assistant["files"] == []

    def test_a_result_with_no_index_answers_no_call(self, tmp_path):
        """0 is the prompt's index, so a missing one must not read as a
        result for a call at step -1. It cannot be attributed to anything,
        and a blob says so."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="One call.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                ),
                {"type": "GENERIC", "content": "indexless", "status": "DONE"},
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"] is None
        assert "indexless" in assistant["blocks"][-1]["content"]

    def test_a_failure_is_a_record_type_and_attributes_no_files(self, tmp_path):
        """``ERROR_MESSAGE`` is the only account of a failed call this
        transcript keeps, and a refused write must not be reported as a file
        changed. This read ``status == "ERROR"`` until it was measured: no
        record on disk carries that status, so the failure branch was dead
        and every failure rendered as a success."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Writing.",
                    tool_calls=[
                        {
                            "name": "write_to_file",
                            "args": {"TargetFile": "/tmp/temp/x/notes.txt"},
                        }
                    ],
                ),
                record(2, "ERROR_MESSAGE", source="SYSTEM", content="denied"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["status"] == "error"
        assert block["result"]["preview"] == "denied"
        assert block["result"]["files_modified"] == []
        assert assistant["files"] == []

    def test_a_backgrounded_tool_stays_unresolved(self, tmp_path):
        """``RUNNING`` is the second and last status on disk — 43 records,
        never the last line of a file. Rendered ``ok`` it is a card that
        claims a tool finished with whatever the harness had said so far."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Serving.",
                    tool_calls=[
                        {"name": "run_command", "args": {"CommandLine": "npm run dev"}}
                    ],
                ),
                record(
                    2,
                    "GENERIC",
                    status="RUNNING",
                    content="Created At: 2026-09-10T04:00:01Z\nlistening on 8080",
                    created_at="2026-09-10T04:00:09Z",
                ),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["status"] == "pending"
        assert block["result"]["preview"] == "listening on 8080"
        assert block["result"]["duration_ms"] == 0
        assert block["done"] is False

    def test_a_tool_named_result_attaches_like_a_generic_one(self, tmp_path):
        """The vocabulary is 16 types, not the four this first read for.

        Conversations from 2026-08-03 to 08-16 name a result after its tool
        — ``VIEW_FILE``, ``CODE_ACTION``, ``RUN_COMMAND`` — and every one of
        the 115 since 08-29 says ``GENERIC``. Reading only the current
        spelling turned each older result into an unknown-type blob with its
        card stuck pending, which is a whole era of transcripts rendered
        wrongly rather than a missing feature.
        """
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Reading.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                ),
                record(2, "VIEW_FILE", content="contents"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["preview"] == "contents"
        assert block["result"]["status"] == "ok"
        assert block["done"] is True

    def test_the_result_header_is_stripped_and_is_what_times_the_call(self, tmp_path):
        """Every result opens with the harness's own stamps — 674 of 720
        carry both. They are the tool's duration rather than the gap between
        two records, and left in the preview they are two lines of plumbing
        at the top of every card."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Reading.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                    created_at="2026-09-10T04:00:00Z",
                ),
                record(
                    2,
                    "GENERIC",
                    content=(
                        "Created At: 2026-09-10T04:00:02Z\n"
                        "Completed At: 2026-09-10T04:00:05Z\n"
                        "PELICAN"
                    ),
                    created_at="2026-09-10T04:00:30Z",
                ),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["preview"] == "PELICAN"
        assert block["result"]["duration_ms"] == 3000

    def test_a_checkpoint_is_a_system_event_without_an_envelope(self, tmp_path):
        """``CHECKPOINT`` and ``CONVERSATION_HISTORY`` are the harness's own
        notes, not a message from anyone. 📨 is for a real sender, and a
        truncation notice wearing it would claim one it has not got."""
        a_child(
            tmp_path,
            records=[
                record(0, "USER_INPUT", content="Go."),
                record(1, "CHECKPOINT", source="SYSTEM", content="Checkpoint saved."),
            ],
        )
        messages = subagents.load(CHILD, brain_dir=tmp_path)
        notice = [m for m in messages if m.get("system_event")]
        assert len(notice) == 1
        assert notice[0]["content"] == "Checkpoint saved."
        assert "📨" not in notice[0]["content"]

    def test_a_written_file_is_attributed_the_way_a_live_turn_does(self, tmp_path):
        """Through the shared table, so a browsed turn and a live one credit
        the same call with the same path."""
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Writing.",
                    tool_calls=[
                        {
                            "name": "write_to_file",
                            "args": {"TargetFile": "/tmp/temp/x/notes.txt"},
                        }
                    ],
                ),
                record(2, "GENERIC", content="done"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        assert assistant["files"] == ["/tmp/temp/x/notes.txt"]
        assert assistant["turn"]["files_modified"] == ["/tmp/temp/x/notes.txt"]

    def test_every_card_says_it_was_gated(self, tmp_path):
        """Every tool call from a claimed conversation reaches the dialog on
        this transport, a subagent's included — that is what the gate
        claims a subagent's conversation *for*. The live card says so, and
        the browsed card agreeing with it is the point."""
        a_child(tmp_path)
        assistant = subagents.load(CHILD, brain_dir=tmp_path)[1]
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["gated"] is True
        assert block["tool"]["gated"] is True

    def test_no_block_is_attributed_to_a_second_agent(self, tmp_path):
        """This whole message list *is* the subagent's tab, so there is no
        second agent inside it to attribute a block to."""
        a_child(tmp_path)
        for message in subagents.load(CHILD, brain_dir=tmp_path):
            for block in message.get("blocks", []):
                assert block["agent_id"] is None

    def test_a_message_from_another_conversation_is_marked_as_system(self, tmp_path):
        """The harness delivers it by speaking as the user. Attributing it
        to the person reading would be a lie about who said it — the same
        choice ``history._task_notification_card`` makes about Claude's
        background-agent wake-up, which is the same event."""
        a_delegating_parent(tmp_path)
        messages = subagents.load(PARENT, brain_dir=tmp_path)
        notice = [m for m in messages if m.get("system_event")]
        assert len(notice) == 1
        assert notice[0]["role"] == "user"
        assert notice[0]["content"].startswith("📨 ")
        assert "PELICAN" in notice[0]["content"]

    def test_the_frame_is_the_last_tag_and_not_the_first_mention(self, tmp_path):
        """This assertion failed when it was written, which is why it is its
        own test rather than a line in the one above.

        The record opens with a preamble that *mentions* the tag — "The
        following is a <SYSTEM_MESSAGE> not actually sent by the user" — so
        a non-greedy search matches prose about the frame instead of the
        frame, and renders the disclaimer, a stray opening tag and the body
        as one message.
        """
        a_delegating_parent(tmp_path)
        (notice,) = [
            m
            for m in subagents.load(PARENT, brain_dir=tmp_path)
            if m.get("system_event")
        ]
        assert notice["content"] == (
            "📨 [Message] sender=" + CHILD + " content=The word is **PELICAN**."
        )
        assert "SYSTEM_MESSAGE" not in notice["content"]
        assert "not actually sent" not in notice["content"]

    def test_a_message_ends_the_turn_above_it(self, tmp_path):
        """Because that is how the transcript records it: the reply the
        parent then writes is a new turn, not a continuation."""
        a_delegating_parent(tmp_path)
        roles = [m["role"] for m in subagents.load(PARENT, brain_dir=tmp_path)]
        assert roles == ["user", "assistant", "user"]

    def test_an_unknown_record_type_is_shown_rather_than_lost(self, tmp_path, caplog):
        """On a CLI that releases weekly, a record nobody renders must
        degrade to a visible blob instead of a gap in the transcript."""
        a_child(
            tmp_path,
            records=[
                record(1, "PLANNER_RESPONSE", content="Hello."),
                record(2, "SOMETHING_NEW", content="who knows"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        blob = assistant["blocks"][-1]
        assert blob["kind"] == "text"
        assert "SOMETHING_NEW" in blob["content"]
        assert "```json" in blob["content"]
        assert "SOMETHING_NEW" in caplog.text

    def test_a_result_with_no_call_is_shown_rather_than_guessed_at(self, tmp_path):
        """Never observed, and the two ways of guessing are both worse than
        saying so: dropped, the transcript has a hole; folded into the
        prose, the harness's own words are attributed to the agent."""
        a_child(
            tmp_path,
            records=[
                record(1, "PLANNER_RESPONSE", content="No calls here."),
                record(2, "GENERIC", content="orphan output"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        assert "orphan output" in assistant["blocks"][-1]["content"]
        assert assistant["blocks"][-1]["kind"] == "text"

    def test_the_footer_counts_responses_and_calls(self, tmp_path):
        a_child(tmp_path)
        assistant = subagents.load(CHILD, brain_dir=tmp_path)[1]
        assert assistant["turn"]["num_turns"] == 1
        assert assistant["turn"]["tool_calls"] == 1

    def test_the_footer_claims_no_tokens(self, tmp_path):
        """``agy``'s transcript records no usage on any record. Absent
        rather than zero: a zero renders as a turn that cost nothing."""
        a_child(tmp_path)
        for message in subagents.load(CHILD, brain_dir=tmp_path):
            assert "turn_model_usage" not in message.get("turn", {})

    def test_no_turn_claims_a_clean_finish(self, tmp_path):
        """The rule the browsed Claude turn follows for the same reason: a
        badge claiming a clean finish on no evidence is worse than none."""
        a_child(tmp_path)
        for message in subagents.load(CHILD, brain_dir=tmp_path):
            if message["role"] == "assistant":
                assert message["terminalReason"] is None

    def test_a_tab_offers_no_further_tab(self, tmp_path):
        """A subagent's own delegation is rendered — as the
        ``invoke_subagent`` card — but a row here would offer a button into
        a grandchild's transcript, a level the listing does not walk."""
        a_child(
            tmp_path,
            records=[spawn(1, subagent_arg()), announcement(2, GRANDCHILD)],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        assert assistant["subagents"] == []

    def test_a_duration_comes_from_the_two_stamps(self, tmp_path):
        a_child(tmp_path)
        assistant = subagents.load(CHILD, brain_dir=tmp_path)[1]
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["duration_ms"] == 2000

    def test_a_shared_second_reads_as_no_duration_rather_than_a_guess(self, tmp_path):
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Fast.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                ),
                record(2, "GENERIC", content="ok"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["duration_ms"] == 0

    def test_backwards_stamps_do_not_produce_a_negative_wait(self, tmp_path):
        a_child(
            tmp_path,
            records=[
                record(
                    1,
                    "PLANNER_RESPONSE",
                    content="Fast.",
                    tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/a"}}],
                    created_at="2026-09-10T04:00:05Z",
                ),
                record(2, "GENERIC", content="ok", created_at="2026-09-10T04:00:01Z"),
            ],
        )
        (assistant,) = subagents.load(CHILD, brain_dir=tmp_path)
        (block,) = [b for b in assistant["blocks"] if b["kind"] == "tool"]
        assert block["result"]["duration_ms"] == 0


class TestTheRenderedShapeMatchesTheOtherReader:
    """One renderer draws both, so the two readers owe it one shape.

    Asserted against ``claude_code.history`` rather than against a literal
    list of keys: a field added there and not here is exactly the drift
    this is for, and a literal would go stale without failing.
    """

    def test_a_text_block_has_the_fields_history_gives_one(self, tmp_path):
        from aic_dc.claude_code import history

        a_child(tmp_path)
        mine = subagents.load(CHILD, brain_dir=tmp_path)[1]["blocks"][0]
        theirs = history._text_block("b", "text", "hello")
        assert set(mine) == set(theirs)

    def test_an_assistant_message_has_no_field_the_panel_will_not_read(
        self, tmp_path
    ):
        """``restore.js`` copies a fixed set of fields, so anything else
        here is dead weight the browser silently drops."""
        a_child(tmp_path)
        assistant = subagents.load(CHILD, brain_dir=tmp_path)[1]
        assert set(assistant) <= {
            "role",
            "content",
            "blocks",
            "subagents",
            "files",
            "turn",
            "terminalReason",
            "timestamp",
        }
