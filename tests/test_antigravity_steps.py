"""Tests for aic_dc.antigravity.steps — the Step → Event pump.

Two assertions here are load-bearing and the rest is coverage.

**The first is that a tool's result is not its input.** Antigravity sends
the same sub-message twice — once at ``ACTIVE`` with the arguments and
again at ``DONE`` with the outputs filled in beside them — and the SDK
copies the whole thing into ``ToolCall.args`` both times. A pump that
forwarded ``args`` would render a card whose "input" grew a command's
entire stdout on completion, and would emit no result at all.
``TestResultFieldsMatchTheProto`` checks the split table against
``localharness_pb2.StepUpdate`` field by field, so an SDK release that
adds an output field fails here rather than leaking it onto a card.

**The second is that no new event name is invented.** AG-3 puts both
engines under one RPC namespace, where ``ClaudeCodeService.<method>``
appears at 43 methods across 59 webapp files. ``TestVocabulary`` asserts
every name this pump emits is one the Claude pump already emits, because
the browser must not be able to tell which engine is talking.

Everything runs offline against fake steps. The pump holds no connection
and spawns nothing, which is most of why it is a separate module.
"""

from __future__ import annotations

import types as pytypes

import pytest

from aic_dc.antigravity import steps as ag_steps
from aic_dc.antigravity.steps import StepTranslator


class FakeStep:
    """A ``Step`` with only the attributes a test sets.

    Deliberately not a pydantic model and deliberately missing fields:
    the pump reads every attribute with ``getattr`` and a default because
    this is an alpha SDK, and a fake that supplied everything would test
    the wrong thing.
    """

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):  # noqa: D105 - absent means absent
        raise AttributeError(name)


class FakeCall:
    def __init__(self, name, args, id=None, server_name=None):
        self.name = name
        self.args = args
        self.id = id
        self.server_name = server_name


def text_step(content, *, delta="", **kw):
    fields = {
        "id": "t:1",
        "type": "TEXT_RESPONSE",
        "source": "MODEL",
        "target": "USER",
        "status": "ACTIVE",
        "content": content,
        "content_delta": delta,
        "depth": 0,
    }
    fields.update(kw)
    return FakeStep(**fields)


def tool_step(name, args, *, status="ACTIVE", call_id="c1", **kw):
    fields = {
        "id": "t:2",
        "type": "TOOL_CALL",
        "source": "MODEL",
        "target": "ENVIRONMENT",
        "status": status,
        "tool_calls": [FakeCall(name, args, id=call_id)],
        "depth": 0,
    }
    fields.update(kw)
    return FakeStep(**fields)


def names(events):
    return [e.name for e in events]


# ----------------------------------------------------------------------
# The first one that matters: args are not results
# ----------------------------------------------------------------------


class TestArgumentsAndResultsAreSplit:
    def test_a_running_command_shows_its_command_and_no_result(self):
        t = StepTranslator("r1")
        events = t.translate(
            tool_step("run_command", {"command_line": "ls", "working_dir": "/tmp"})
        )
        assert names(events) == ["toolUse"]
        assert events[0].payload["input"] == {
            "command_line": "ls",
            "working_dir": "/tmp",
        }

    def test_a_finished_command_reports_its_output_as_the_result(self):
        t = StepTranslator("r1")
        t.translate(tool_step("run_command", {"command_line": "ls"}))
        events = t.translate(
            tool_step(
                "run_command",
                {"command_line": "ls", "exit_code": 0, "combined_output": "a\nb\n"},
                status="DONE",
            )
        )
        assert names(events) == ["toolResult"]
        assert events[0].payload["preview"] == "a\nb\n"
        assert events[0].payload["status"] == "ok"

    def test_the_card_input_never_grows_the_output(self):
        """The failure this split exists to prevent, asserted directly."""
        t = StepTranslator("r1")
        first = t.translate(tool_step("run_command", {"command_line": "ls"}))
        t.translate(
            tool_step(
                "run_command",
                {"command_line": "ls", "combined_output": "x" * 5000},
                status="DONE",
            )
        )
        assert "combined_output" not in first[0].payload["input"]

    def test_a_nonzero_exit_is_an_error_even_on_a_done_step(self):
        """The tool ran; the thing it ran did not.

        A card reading "ok" over a failed build is the transcript lying
        about the turn.
        """
        t = StepTranslator("r1")
        t.translate(tool_step("run_command", {"command_line": "false"}))
        events = t.translate(
            tool_step(
                "run_command",
                {"command_line": "false", "exit_code": 1, "combined_output": "boom"},
                status="DONE",
            )
        )
        assert events[0].payload["status"] == "error"
        assert "exit 1" in events[0].payload["preview"]

    def test_an_empty_output_is_not_reported_as_a_result(self):
        """A still-running call must not render ``exit_code: 0``."""
        inputs, results = ag_steps._split_args(
            "run_command", {"command_line": "ls", "combined_output": ""}
        )
        assert results == {}

    def test_an_unknown_tool_shows_everything_as_input(self):
        """The safe direction: visible and wrong beats silently dropped."""
        inputs, results = ag_steps._split_args("tool_from_the_future", {"a": 1})
        assert inputs == {"a": 1}
        assert results == {}

    def test_a_proposed_edit_belongs_to_the_call_not_the_result(self):
        """``diff_block`` is what the dialog renders *before* it happens."""
        inputs, results = ag_steps._split_args(
            "edit_file", {"file_path": "a.py", "diff_block": "-x\n+y"}
        )
        assert inputs["diff_block"] == "-x\n+y"
        assert results == {}


class TestResultFieldsMatchTheProto:
    """The table, checked against the wire it describes.

    Skipped where the SDK is absent — a base install is a one-engine
    install (AG-R-10) — and otherwise strict: every tool AIC⚡DC classifies
    must still exist on ``StepUpdate``, and every field it calls a result
    must still be a field of that tool's sub-message.
    """

    @pytest.fixture
    def step_update(self):
        pb = pytest.importorskip(
            "google.antigravity.proto.localharness_pb2",
            reason="the SDK wheel is not installed",
        )
        return pb.StepUpdate.DESCRIPTOR

    def test_every_classified_tool_is_still_on_the_wire(self, step_update):
        from google.antigravity.connections.local.local_connection_config import (
            BUILTIN_TOOL_PROTO_FIELDS,
        )

        wire = {tool.value for tool in BUILTIN_TOOL_PROTO_FIELDS}
        unknown = set(ag_steps.TOOL_RESULT_FIELDS) - wire
        assert not unknown, (
            f"steps.py classifies tools the harness no longer sends: "
            f"{sorted(unknown)}. Delete the rows."
        )

    def test_every_result_field_is_a_real_proto_field(self, step_update):
        from google.antigravity.connections.local.local_connection_config import (
            BUILTIN_TOOL_PROTO_FIELDS,
        )

        by_tool = {t.value: f for t, f in BUILTIN_TOOL_PROTO_FIELDS.items()}
        for tool, result_fields in ag_steps.TOOL_RESULT_FIELDS.items():
            field = step_update.fields_by_name[by_tool[tool]]
            actual = {f.name for f in field.message_type.fields}
            missing = result_fields - actual
            assert not missing, (
                f"{tool} no longer carries {sorted(missing)}. The card will "
                "render it as an argument until this table is updated."
            )

    def test_no_tool_sub_message_has_an_unclassified_field(self, step_update):
        """The gate that catches an SDK bump adding an output field.

        Every field of every classified tool must be *decided about* —
        named as a result here, or knowingly left as an input. A new field
        is neither, and that is what this fails on.
        """
        from google.antigravity.connections.local.local_connection_config import (
            BUILTIN_TOOL_PROTO_FIELDS,
        )

        known_inputs = {
            "list_directory": {"directory_path"},
            "find_file": {"directory_path", "query"},
            "search_directory": {"directory_path", "query"},
            "view_file": {"file_path", "start_line", "end_line"},
            "create_file": {"file_path", "contents"},
            "edit_file": {"file_path", "diff_block"},
            "run_command": {"command_line", "working_dir"},
            "search_web": {"query", "domain"},
            "read_url_content": {"url"},
            "generate_image": {"prompt", "image_name", "aspect_ratio"},
            "finish": set(),
            "start_subagent": set(),
        }
        by_tool = {t.value: f for t, f in BUILTIN_TOOL_PROTO_FIELDS.items()}
        for tool, results in ag_steps.TOOL_RESULT_FIELDS.items():
            field = step_update.fields_by_name[by_tool[tool]]
            actual = {f.name for f in field.message_type.fields}
            undecided = actual - results - known_inputs[tool]
            assert not undecided, (
                f"{tool} grew {sorted(undecided)} in this SDK release. Decide "
                "whether each is an argument or a result; a result left "
                "undecided renders on the card as an argument."
            )

    def test_view_file_still_does_not_carry_the_file(self, step_update):
        """The gap that disqualified ``agy``, surviving into the SDK stream.

        Not a defect to fix here — the permission dialog reads content
        from the *hook*, whose ``arguments_json`` carries it in full — but
        a fact phase 4 must not design against. If a release adds content
        to this sub-message, that is worth knowing.
        """
        actual = {
            f.name for f in step_update.fields_by_name["view_file"].message_type.fields
        }
        assert "content" not in actual and "contents" not in actual


# ----------------------------------------------------------------------
# The second one that matters: one vocabulary, two engines
# ----------------------------------------------------------------------


class TestVocabulary:
    """AG-3: the browser must not be able to tell which engine is talking."""

    def emitted_names(self):
        t = StepTranslator("r1")
        seen = set()
        for step in (
            text_step("hi", delta="hi"),
            FakeStep(
                id="t:9",
                type="THINKING",
                source="MODEL",
                target="USER",
                status="ACTIVE",
                thinking="mm",
                thinking_delta="mm",
                depth=0,
            ),
            tool_step("run_command", {"command_line": "ls"}),
            tool_step(
                "run_command",
                {"command_line": "ls", "exit_code": 0, "combined_output": "x"},
                status="DONE",
            ),
            FakeStep(id="t:5", type="COMPACTION", status="DONE", depth=0),
            FakeStep(id="t:6", type="UNKNOWN", status="UNKNOWN", depth=0),
        ):
            seen.update(names(t.translate(step)))
        seen.update(names(t.stream_complete()))
        return seen

    def test_every_event_name_is_one_the_claude_pump_also_emits(self):
        """Checked against the Claude pump's source, by name.

        Not by a regex over ``Event("…")``: that pump builds its two chunk
        names in a conditional and passes the result as a variable, so a
        constructor-shaped pattern misses exactly the two events most
        likely to drift. Searching for the quoted literal anywhere in the
        module is the weaker match and the one that does not lie.
        """
        from pathlib import Path

        import aic_dc.claude_code.messages as claude_messages

        source = Path(claude_messages.__file__).read_text(encoding="utf-8")
        invented = [name for name in sorted(self.emitted_names())
                    if f'"{name}"' not in source]
        assert not invented, (
            f"The Antigravity pump invents event names the Claude pump does "
            f"not emit: {invented}. AG-3 puts both engines under one RPC "
            "namespace, so a new name forks every webapp call site that "
            "handles it."
        )

    def test_it_emits_the_ones_a_transcript_needs(self):
        assert {"streamChunk", "toolUse", "toolResult"} <= self.emitted_names()


class TestEveryStepMemberIsNamedInThePump:
    """The cross-check ``surface.STEP_MEMBERS`` cannot derive.

    ``steps.py`` compares enum members on ``.name`` against string
    literals, because ``Step``'s enums are ``str``-valued and an alpha SDK
    turning one into a plain string would otherwise mis-dispatch
    silently. That costs the probe its syntactic signal, so the step rows
    are declared by hand — and this is what stops the declaration drifting
    from the code.
    """

    def test_pump_names_every_step_type_source_target_and_status(self):
        from pathlib import Path

        from aic_dc.antigravity import surface

        source = Path(ag_steps.__file__).read_text(encoding="utf-8")
        missing = []
        for qualified, (status, _note) in surface.STEP_MEMBERS.items():
            enum_name, member = qualified.split(".")
            if enum_name == "StopReason" or status != surface.HANDLED:
                continue
            if f'"{member}"' not in source:
                missing.append(qualified)
        assert not missing, (
            f"surface.STEP_MEMBERS calls these handled but steps.py never "
            f"names them: {missing}. Either the pump lost a branch or the "
            "table is claiming coverage it does not have."
        )

    def test_every_status_is_live_or_terminal(self):
        """A status in neither set leaves a tool card pending forever.

        The one bucket a card cannot recover from: no result ever arrives,
        the spinner never stops, and nothing in the transcript says why.
        Checked against the SDK's enum rather than against a list here, so
        a 0.1.x release adding a status fails this instead of shipping it.
        """
        types = pytest.importorskip("google.antigravity").types
        members = {member.name for member in types.StepStatus}
        covered = ag_steps._TERMINAL | ag_steps._LIVE
        assert members <= covered, (
            f"StepStatus gained {sorted(members - covered)}. Decide whether "
            "each ends a tool call or leaves it running; a status in neither "
            "set leaves the card pending forever."
        )


# ----------------------------------------------------------------------
# Text, and who is allowed to speak
# ----------------------------------------------------------------------


class TestText:
    def test_model_prose_reaches_the_chat(self):
        t = StepTranslator("r1")
        events = t.translate(text_step("hello", delta="hello"))
        assert names(events) == ["streamChunk"]
        assert events[0].payload["content"] == "hello"

    def test_deltas_accumulate_into_one_block(self):
        t = StepTranslator("r1")
        first = t.translate(text_step("he", delta="he"))
        second = t.translate(text_step("hello", delta="llo"))
        assert first[0].payload["block_id"] == second[0].payload["block_id"]
        assert second[0].payload["seq"] == 1
        assert second[0].payload["content"] == "hello"

    def test_the_accumulated_field_wins_over_our_sum(self):
        """A dropped delta self-corrects instead of leaving a short block."""
        t = StepTranslator("r1")
        t.translate(text_step("", delta="he"))
        events = t.translate(text_step("hello", delta="XX"))
        assert events[0].payload["content"] == "hello"

    def test_our_own_prompt_echoed_back_is_not_rendered(self):
        t = StepTranslator("r1")
        assert t.translate(text_step("ls the repo", source="USER")) == []

    def test_machine_chatter_is_not_rendered_as_prose(self):
        t = StepTranslator("r1")
        assert t.translate(text_step("tool input", target="ENVIRONMENT")) == []

    def test_an_unspecified_target_is_treated_as_user_facing(self):
        """Dropping the answer costs more than rendering an extra line."""
        t = StepTranslator("r1")
        assert names(t.translate(text_step("hi", target="UNSPECIFIED")))

    def test_thinking_has_its_own_channel(self):
        t = StepTranslator("r1")
        events = t.translate(
            FakeStep(
                id="t:3",
                type="THINKING",
                source="MODEL",
                target="USER",
                status="ACTIVE",
                thinking="pondering",
                thinking_delta="pondering",
                depth=0,
            )
        )
        assert names(events) == ["thinkingChunk"]


# ----------------------------------------------------------------------
# Tool cards
# ----------------------------------------------------------------------


class TestToolCards:
    def test_one_card_per_call_across_repeated_steps(self):
        t = StepTranslator("r1")
        t.translate(tool_step("list_directory", {"directory_path": "."}))
        again = t.translate(tool_step("list_directory", {"directory_path": "."}))
        assert names(again) == []
        assert t.stats.tool_calls == 1

    def test_a_result_arrives_once(self):
        t = StepTranslator("r1")
        t.translate(tool_step("list_directory", {"directory_path": "."}))
        done = {"directory_path": ".", "results": "a\nb"}
        assert names(t.translate(tool_step("list_directory", done, status="DONE")))
        assert names(t.translate(tool_step("list_directory", done, status="DONE"))) == []

    def test_a_write_is_reported_for_reindexing(self):
        t = StepTranslator("r1")
        t.translate(tool_step("create_file", {"file_path": "a.py", "contents": "x"}))
        events = t.translate(
            tool_step(
                "create_file", {"file_path": "a.py", "contents": "x"}, status="DONE"
            )
        )
        assert events[0].payload["files_modified"] == ["a.py"]
        assert t.stats.files_modified == ["a.py"]

    def test_a_failed_write_modifies_nothing(self):
        t = StepTranslator("r1")
        t.translate(tool_step("create_file", {"file_path": "a.py"}))
        events = t.translate(
            tool_step("create_file", {"file_path": "a.py"}, status="ERROR", error="no")
        )
        assert events[-1].payload["files_modified"] == []

    def test_a_read_does_not_look_like_a_change(self):
        t = StepTranslator("r1")
        t.translate(tool_step("view_file", {"file_path": "a.py"}))
        events = t.translate(
            tool_step("view_file", {"file_path": "a.py"}, status="DONE")
        )
        assert events[0].payload["files_modified"] == []

    def test_the_finish_tool_is_not_a_card(self):
        """Otherwise every turn ends with a card saying "finish"."""
        t = StepTranslator("r1")
        assert t.translate(tool_step("finish", {"output_string": "{}"})) == []

    def test_a_waiting_call_is_not_resolved(self):
        """A pending permission decision is not a finished tool."""
        t = StepTranslator("r1")
        t.translate(tool_step("edit_file", {"file_path": "a.py"}))
        events = t.translate(
            tool_step("edit_file", {"file_path": "a.py"}, status="WAITING_FOR_USER")
        )
        assert "toolResult" not in names(events)

    def test_an_mcp_server_is_named_on_the_card(self):
        t = StepTranslator("r1")
        step = tool_step("thing", {})
        step.tool_calls = [FakeCall("thing", {}, id="c9", server_name="srv")]
        assert t.translate(step)[0].payload["server"] == "srv"

    def test_duration_is_measured_between_the_two_steps(self):
        ticks = iter([100.0, 100.5])
        t = StepTranslator("r1", clock=lambda: next(ticks))
        t.translate(tool_step("list_directory", {"directory_path": "."}))
        events = t.translate(
            tool_step("list_directory", {"directory_path": "."}, status="DONE")
        )
        assert events[0].payload["duration_ms"] == 500


# ----------------------------------------------------------------------
# The states a UI must not mistake for a hang or a success
# ----------------------------------------------------------------------


class TestStatesThatMustBeVisible:
    def test_waiting_for_user_is_announced(self):
        t = StepTranslator("r1")
        events = t.translate(
            FakeStep(id="t:7", type="TOOL_CALL", status="WAITING_FOR_USER", depth=0)
        )
        assert events[-1].payload["subtype"] == "waiting_for_user"
        assert t.stats.permission_prompts == 1

    def test_an_error_step_surfaces_even_with_no_message(self):
        t = StepTranslator("r1")
        events = t.translate(FakeStep(id="t:8", type="UNKNOWN", status="ERROR", depth=0))
        payloads = [e.payload for e in events if e.name == "systemEvent"]
        assert any(p["subtype"] == "engine_error" for p in payloads)

    def test_an_unknown_step_type_is_rendered_not_dropped(self):
        t = StepTranslator("r1")
        events = t.translate(FakeStep(id="t:9", type="UNKNOWN", status="DONE", depth=0))
        assert events[0].payload["subtype"] == "unknown_step"
        assert t.stats.unknown_steps == 1

    def test_a_compaction_is_named(self):
        t = StepTranslator("r1")
        events = t.translate(FakeStep(id="t:4", type="COMPACTION", status="DONE", depth=0))
        assert events[0].payload["subtype"] == "compaction"

    def test_a_harness_notice_is_not_the_assistant_speaking(self):
        t = StepTranslator("r1")
        events = t.translate(
            FakeStep(
                id="t:10",
                type="SYSTEM_MESSAGE",
                source="SYSTEM",
                target="USER",
                status="DONE",
                content="restarting",
                depth=0,
            )
        )
        assert names(events) == ["systemEvent"]
        assert events[0].payload["data"]["message"] == "restarting"

    def test_an_unreadable_step_does_not_end_the_turn(self):
        """A pump that raises loses the rest of the conversation."""

        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("shape moved")

        t = StepTranslator("r1")
        events = t.translate(Exploding())
        assert events[0].payload["subtype"] == "step_unreadable"


# ----------------------------------------------------------------------
# Subagents, usage, completion
# ----------------------------------------------------------------------


class TestSubagentScope:
    def test_depth_zero_is_the_main_thread(self):
        t = StepTranslator("r1")
        assert t.translate(text_step("hi"))[0].payload["agent_id"] is None

    def test_a_nested_trajectory_is_attributed(self):
        t = StepTranslator("r1")
        events = t.translate(text_step("hi", depth=1, trajectory_id="sub-7"))
        assert events[0].payload["agent_id"] == "sub-7"


class TestUsage:
    def usage_step(self, total):
        return text_step(
            "hi",
            usage_metadata=pytypes.SimpleNamespace(
                prompt_token_count=10,
                cached_content_token_count=5,
                candidates_token_count=2,
                thoughts_token_count=1,
                total_token_count=total,
            ),
        )

    def test_counters_are_replaced_not_summed(self):
        """They arrive cumulative; adding them would multiply the turn."""
        t = StepTranslator("r1")
        t.translate(self.usage_step(13))
        t.translate(self.usage_step(20))
        assert t.turn_usage()["total_token_count"] == 20

    def test_no_dollar_figure_is_invented(self):
        """AG-6: there is no USD anywhere on either Antigravity surface."""
        t = StepTranslator("r1")
        t.translate(self.usage_step(13))
        assert not any("cost" in key or "usd" in key for key in t.turn_usage())

    def test_the_cache_counter_is_forwarded(self):
        """The field that explains a turn's size on this engine."""
        t = StepTranslator("r1")
        t.translate(self.usage_step(13))
        assert t.turn_usage()["cached_content_token_count"] == 5


class TestCompletion:
    def test_stream_complete_carries_the_stop_reason(self):
        t = StepTranslator("r1")
        t.note_stop_reason(pytypes.SimpleNamespace(name="MAX_TOTAL_TOKENS_EXCEEDED"))
        events = t.stream_complete()
        assert events[-1].payload["stop_reason"] == "MAX_TOTAL_TOKENS_EXCEEDED"

    def test_turn_usage_has_no_cost_key(self):
        t = StepTranslator("r1")
        assert set(t.stream_complete()[0].payload) == {"turn_model_usage"}

    def test_blocks_replay_for_a_reconnecting_client(self):
        t = StepTranslator("r1")
        t.translate(text_step("hello", delta="hello"))
        blocks = t.rendered_blocks()
        assert blocks[0]["content"] == "hello"
        assert blocks[0]["kind"] == "text"

    def test_response_text_is_the_assistant_prose(self):
        t = StepTranslator("r1")
        t.translate(text_step("hello", delta="hello"))
        t.translate(tool_step("list_directory", {"directory_path": "."}))
        assert t.response_text() == "hello"
