"""Every engine's ``toolResult`` status is one the webapp can draw.

The status on a ``toolResult`` crosses a language boundary, and nothing
on either side of it checked the other. ``toolStatus`` in
``webapp/src/chat-panel/blocks.js`` knows ``ok``, ``error`` and
``interrupted``, and reads **anything else as still running** — no
error, no log, just a card with a blue dot and an elapsed clock that
never stops. The ``agy`` translator said ``"success"``, every finished
call on that transport rendered that way, and each side's tests passed:
the Python ones pinned ``"success"``, the webapp ones used hand-written
payloads that said ``"ok"``.

So the vocabulary is read **from the webapp's source** rather than
copied here, and the payloads come from the **real translators**. A
status added on one side and not the other fails this file, whichever
side moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from aic_dc.agy.steps import AgyTranslator
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.claude_code.messages import TurnTranslator
from tests.test_agy_steps import TOOL_ACTIVE, TOOL_DONE, frame
from tests.test_antigravity_steps import tool_step

BLOCKS_JS = Path(__file__).resolve().parents[1] / "webapp/src/chat-panel/blocks.js"


def webapp_statuses() -> frozenset[str]:
    """The result statuses ``toolStatus`` maps to something other than pending."""
    source = BLOCKS_JS.read_text(encoding="utf-8")
    body = re.search(
        r"export function toolStatus\(block\) \{(.*?)\n\}", source, re.DOTALL
    )
    assert body, "toolStatus not found in blocks.js — has it moved or been renamed?"
    found = frozenset(re.findall(r"status === '(\w+)'", body.group(1)))
    # A regex reading JavaScript can quietly stop matching. Without these
    # floors, a rewrite of `toolStatus` would empty the set, and the
    # assertions below would then fail for the wrong reason or not at all.
    assert {"ok", "error"} <= found, f"read {sorted(found)} from toolStatus"
    return found


def claude_results() -> list[dict]:
    ticks = iter(range(10_000))
    out = []
    for call_id, is_error in (("toolu_ok", False), ("toolu_err", True)):
        t = TurnTranslator("r1", clock=lambda: float(next(ticks)))
        t.translate(
            AssistantMessage(
                content=[ToolUseBlock(id=call_id, name="Grep", input={})],
                model="m",
                message_id="msg_1",
            )
        )
        events = t.translate(
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id=call_id, content="x", is_error=is_error)
                ]
            )
        )
        out += [e.payload for e in events if e.name == "toolResult"]
    return out


def sdk_results() -> list[dict]:
    out = []
    for status in ("DONE", "ERROR"):
        t = StepTranslator("r1")
        t.translate(tool_step("run_command", {"command_line": "ls"}))
        events = t.translate(
            tool_step(
                "run_command",
                {"command_line": "ls", "exit_code": 0, "combined_output": "a"},
                status=status,
            )
        )
        out += [e.payload for e in events if e.name == "toolResult"]
    return out


def agy_results() -> list[dict]:
    out = []
    for done in (TOOL_DONE, dict(TOOL_DONE, state="ERROR")):
        t = AgyTranslator("r1")
        t.translate(frame(TOOL_ACTIVE))
        events = t.translate(frame(done))
        out += [e.payload for e in events if e.name == "toolResult"]
    return out


@pytest.mark.parametrize(
    "engine", [claude_results, sdk_results, agy_results], ids=lambda f: f.__name__
)
def test_a_finished_call_reads_as_finished_in_the_webapp(engine):
    results = engine()
    # One success and one failure, so neither branch of a translator's
    # `"error" if failed else …` goes unchecked.
    assert [r["status"] for r in results] == ["ok", "error"]
    assert {r["status"] for r in results} <= webapp_statuses()
