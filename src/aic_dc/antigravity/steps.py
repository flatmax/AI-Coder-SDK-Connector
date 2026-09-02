"""The ``Step`` → ``Event`` pump: one Antigravity turn as AIC⚡DC events.

The Antigravity half of ``claude_code/messages.py``. It emits the **same**
event names, with the same payload keys, because AG-3 puts both engines
under one RPC namespace: ``ClaudeCodeService.<method>`` appears at 43
methods across 59 webapp files, and a second event vocabulary would fork
every one of those call sites in the layer with the least test coverage.
The browser must not be able to tell which engine is talking, except where
a capability descriptor tells it deliberately (AG-9).

So the contract here is negative as much as positive: **no new event name
is invented.** Anything Antigravity reports that has no Claude counterpart
either maps onto ``systemEvent`` or waits for phase 6 to give it a
descriptor entry.

Three things about ``Step`` that are not obvious, and that the shape of
this module is a response to
=========================================================================

**1. A builtin tool's arguments and its result are the same object.**

This is the finding that most affects the code. Claude sends a
``tool_use`` block and later a separate ``tool_result`` block with its own
id. Antigravity sends the *same* step sub-message twice: once at
``StepStatus.ACTIVE`` with the input fields populated, and again at
``DONE`` with the output fields filled in beside them. ``run_command``
arrives as ``{command_line, working_dir}`` and comes back as
``{command_line, working_dir, exit_code, combined_output}``; the SDK
copies the whole sub-message into ``ToolCall.args`` either way
(``connections/local/event_processor.py:250-308``).

A pump that forwarded ``args`` as the tool input would therefore render a
card whose "input" grew a command's entire stdout when it completed, and
would never emit a result at all. :data:`TOOL_RESULT_FIELDS` is the split,
per tool, and ``test_result_fields_match_the_proto`` checks it against
``localharness_pb2.StepUpdate`` so an SDK release that adds an output
field fails a test instead of leaking it into a tool card.

**2. ``view_file`` does not carry the file.**

Its sub-message is ``{file_path, start_line, end_line, content_offset}`` —
no content. This is the same gap that disqualified ``agy`` as a transport
(AG-2), and it survives into the SDK's *step stream*. It does not
re-open that decision, because the place AIC⚡DC needs file content is the
permission dialog, and the **hook** path carries it in full:
``PreToolArgs.arguments_json`` is free-form JSON from the Go side and
phase 2 measured ``TargetContent`` + ``ReplacementContent`` + a line range
in it. The two paths have different shapes for the same call — the step
stream is the typed proto sub-message (``file_path``, ``diff_block``) and
the hook is the untyped JSON. Phase 4 renders diffs from the hook, and
nothing here should be built as though the stream could serve them.

**3. Steps repeat, and the repeats are the delta channel.**

One logical step arrives many times: ``content_delta`` carries the
increment and ``content`` the accumulated text. Identity is
``(trajectory_id, step_index)``, which is what ``_make_step_id`` builds
and what ``Step.id`` already is. Blocks are keyed by it, so a step's
deltas append to one block rather than opening a new one per frame.

What is deliberately not here
=============================
No permission decisions, no resume, no mirror. This module is a pure
function of the steps it is fed — it holds no connection, spawns nothing,
and every test of it runs offline. The session owns the harness; this owns
the vocabulary.

Governing spec: ``specs5/plan-ag/`` — AG-3, AG-6, AG-9.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aic_dc.claude_code.messages import (
    Event,
    _iso_utc,
    truncate_tool_result,
)

# ``Event`` and the two helpers are imported from the Claude pump rather
# than re-declared, and that is AG-3 rather than convenience. One RPC
# namespace means one event vocabulary; a second ``Event`` class here
# would be the place the two engines' payloads quietly drifted apart, and
# a second timestamp formatter would put two datetime conventions on one
# wire for one browser parser to handle. ``messages`` imports nothing
# heavier than the standard library, so this costs no Claude SDK import.
# ``_iso_utc`` is private to that module and shared deliberately: the
# alternative is copying the format, which is the version that could go
# out of sync.

logger = logging.getLogger(__name__)

#: Per-tool, the sub-message fields that are *output* rather than input.
#:
#: Read off ``localharness_pb2.StepUpdate`` field by field. Everything not
#: listed for a tool is treated as input, which is the safe direction: a
#: new field renders on the card as an argument, where it is visible and
#: wrong, rather than being silently dropped from both halves.
#:
#: ``edit_file.diff_block`` and ``create_file.contents`` are deliberately
#: *not* results. They are the proposed change — the thing the permission
#: dialog renders before it happens — so they belong to the call. What
#: those tools return is nothing at all; their result is that the step
#: reached DONE.
TOOL_RESULT_FIELDS: dict[str, frozenset[str]] = {
    "list_directory": frozenset({"results"}),
    "find_file": frozenset({"output"}),
    "search_directory": frozenset({"num_results"}),
    "view_file": frozenset({"content_offset"}),
    "run_command": frozenset({"exit_code", "combined_output"}),
    "search_web": frozenset({"summary"}),
    "read_url_content": frozenset({"title", "summary", "content_path"}),
    "generate_image": frozenset({"image_paths", "output_path"}),
    "finish": frozenset({"output_string"}),
    "create_file": frozenset(),
    "edit_file": frozenset(),
    "start_subagent": frozenset(),
}

#: Tool arguments that name a file the engine wrote.
#:
#: Feeds ``files_modified`` on a tool result, which is what re-indexes the
#: symbol table and refreshes the file tree. Only tools that actually
#: write appear: ``view_file`` names a path too, and re-indexing on a read
#: would make every turn look like it changed the tree.
TOOL_WRITTEN_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "create_file": ("file_path",),
    "edit_file": ("file_path",),
    "generate_image": ("output_path",),
}

#: The tool that ends a turn rather than doing work. Suppressed from the
#: transcript; see :meth:`StepTranslator._tool_events`.
FINISH_TOOL = "finish"

#: ``StepType`` members, as the strings the pump dispatches on.
#:
#: Named constants rather than inline literals so that every member of the
#: SDK's enum is visibly accounted for in one place — which is what
#: ``surface.STEP_MEMBERS`` claims and what
#: ``test_every_step_member_is_named_in_the_pump`` checks. ``TEXT_RESPONSE``
#: and ``TOOL_CALL`` are listed even though the pump reaches them by
#: reading ``content`` and ``tool_calls`` rather than by comparing the
#: type: a step can carry prose *and* a tool call, so dispatching on the
#: type alone would drop one of them.
STEP_TEXT_RESPONSE = "TEXT_RESPONSE"
STEP_THINKING = "THINKING"
STEP_TOOL_CALL = "TOOL_CALL"
STEP_SYSTEM_MESSAGE = "SYSTEM_MESSAGE"
STEP_COMPACTION = "COMPACTION"
STEP_FINISH = "FINISH"
STEP_UNKNOWN = "UNKNOWN"

#: Sources whose text is the assistant's own voice.
#:
#: ``UNKNOWN`` is here deliberately. An unrecognised source must render
#: rather than be dropped — on an alpha SDK it is how a source this wheel
#: does not know arrives, and a transcript that silently omits the answer
#: is worse than one that renders a line whose provenance is uncertain.
PROSE_SOURCES = frozenset({"MODEL", STEP_UNKNOWN})

#: Sources that are not the model speaking: our own prompt echoed back,
#: and the harness. Rendering either as prose puts words in the
#: assistant's mouth.
ECHOED_SOURCES = frozenset({"USER", "SYSTEM"})

#: The one target the transcript hides — addressed to the tools rather
#: than to the reader. Showing it is showing machine chatter.
MACHINE_TARGET = "ENVIRONMENT"

#: The targets the chat renders. Written as the positive set rather than
#: as "not ENVIRONMENT" so that every ``StepTarget`` member is accounted
#: for by name: ``UNSPECIFIED`` and ``UNKNOWN`` are user-facing on
#: purpose, because rendering an extra line costs less than dropping the
#: answer, and that is a decision rather than a fallthrough.
USER_TARGETS = frozenset({"USER", "UNSPECIFIED", STEP_UNKNOWN})

#: ``StepStatus`` members, likewise as strings.
STATUS_ACTIVE = "ACTIVE"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"
STATUS_CANCELED = "CANCELED"
WAITING_FOR_USER = "WAITING_FOR_USER"

#: Step statuses that end a tool call. ``WAITING_FOR_USER`` is absent by
#: design: the call is live and blocked on a human, and resolving its card
#: would render a pending permission decision as a finished tool.
#:
#: ``CANCELED`` is terminal and is worth naming for a reason phase 0 found
#: the hard way: ``agy`` reports a *permission denial* this way, with no
#: error key at all. The SDK is not assumed to differ, so a cancelled call
#: resolves its card as an error rather than leaving it pending.
_TERMINAL = frozenset({STATUS_DONE, STATUS_ERROR, STATUS_CANCELED})

#: The statuses a call can be in while still running. Together with
#: :data:`_TERMINAL` this must cover ``StepStatus`` exactly — a member in
#: neither set is one the pump would leave a card pending on forever,
#: which ``test_every_status_is_live_or_terminal`` is what catches.
_LIVE = frozenset({STATUS_ACTIVE, WAITING_FOR_USER, STEP_UNKNOWN})


@dataclass
class _Block:
    """A rendered block and its emission state. Mirrors the Claude pump."""

    block_id: str
    kind: str  # "text" | "thinking" | "tool" | "system"
    seq: int = -1
    content: str = ""
    done: bool = False
    tool: dict[str, Any] | None = None
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "seq": max(self.seq, 0),
            "content": self.content,
            "tool": self.tool,
            "agent_id": self.agent_id,
        }


@dataclass
class _ToolCall:
    """A tool call awaiting the step that completes it."""

    tool_use_id: str
    name: str
    input: dict[str, Any]
    started_at: float
    agent_id: str | None = None
    resolved: bool = False


@dataclass
class TurnStats:
    """Pump-local accounting no single step carries."""

    tool_calls: int = 0
    permission_prompts: int = 0
    files_modified: list[str] = field(default_factory=list)
    steps: int = 0
    unknown_steps: int = 0


class StepTranslator:
    """Translates one Antigravity turn's steps into AIC⚡DC events.

    One instance per turn. Feed it every step from ``receive_steps()`` in
    arrival order and emit whatever it returns.

    Parameters
    ----------
    request_id:
        The browser's request ID. Every turn-scoped event carries it and
        it prefixes every block identity, exactly as on the Claude side.
    clock:
        Monotonic clock, injectable so duration assertions in tests do not
        depend on wall time.
    wall_clock:
        Wall clock, for the one field a monotonic reading cannot serve:
        ``invoked_at`` is a time of day a browser renders, and a
        process-local counter is not one.
    """

    def __init__(
        self,
        request_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.request_id = request_id
        self._clock = clock
        self._wall_clock = wall_clock
        self._blocks: dict[str, _Block] = {}
        self._tools: dict[str, _ToolCall] = {}
        self._block_counter = 0
        self._usage: dict[str, int] = {}
        self._stop_reason = ""
        self.stats = TurnStats()

    # ------------------------------------------------------------------
    # The entry point
    # ------------------------------------------------------------------

    def translate(self, step: Any) -> list[Event]:
        """One step in, zero or more events out.

        Defensive by construction rather than by taste. This is an alpha
        SDK whose step shape is the least stable thing in it, so every
        field is read with ``getattr`` and a default, and an unreadable
        step becomes a visible ``systemEvent`` rather than an exception
        that ends the turn. A pump that raises loses the rest of the
        conversation; a pump that renders a strange card loses nothing.
        """
        self.stats.steps += 1
        try:
            return self._translate(step)
        except Exception:  # noqa: BLE001 - an alpha SDK's step shape
            logger.exception("Antigravity step could not be translated")
            self.stats.unknown_steps += 1
            return [
                Event(
                    "systemEvent",
                    {
                        "subtype": "step_unreadable",
                        "data": {"repr": _short(repr(step))},
                    },
                )
            ]

    def _translate(self, step: Any) -> list[Event]:
        events: list[Event] = []
        scope = _scope(step)

        self._absorb_usage(step)

        step_type = _name(getattr(step, "type", None))
        status = _name(getattr(step, "status", None))

        # Errors first: a step can carry both an error and content, and
        # the error is the part that must not be swallowed. StepStatus.ERROR
        # with an empty message still surfaces, because a silent stall is
        # the failure mode the transcript exists to make visible.
        error = getattr(step, "error", "") or ""
        if status == STATUS_ERROR or error:
            events.extend(self._error_event(step, error, status))

        events.extend(self._text_events(step, scope))
        events.extend(self._tool_events(step, scope, status))

        if step_type == STEP_SYSTEM_MESSAGE:
            # The harness speaking, not the model. Routed to systemEvent
            # rather than to a text block because rendering it as prose
            # would put words in the assistant's mouth — the distinction
            # StepSource exists to draw, and the one a transcript is
            # worthless without.
            events.append(
                Event(
                    "systemEvent",
                    {
                        "subtype": "engine_notice",
                        "data": {
                            "step_id": _step_id(step),
                            "message": getattr(step, "content", "") or "",
                        },
                    },
                )
            )
        elif step_type == STEP_COMPACTION:
            # A boundary, after the fact: the model's context was
            # compacted and the steps before it may no longer be in it.
            # Named rather than hidden, because a conversation that
            # silently forgets reads as a model getting worse.
            events.append(
                Event(
                    "systemEvent",
                    {"subtype": "compaction", "data": {"step_id": _step_id(step)}},
                )
            )
        elif step_type == STEP_UNKNOWN and not events:
            # The forward-compatibility escape hatch, and the member the
            # surface probe calls the most important one: a step type this
            # wheel does not know arrives here. It renders as an unknown
            # card and is never dropped.
            self.stats.unknown_steps += 1
            events.append(
                Event(
                    "systemEvent",
                    {
                        "subtype": "unknown_step",
                        "data": {
                            "step_id": _step_id(step),
                            "status": status,
                            "repr": _short(repr(step)),
                        },
                    },
                )
            )

        if status == WAITING_FOR_USER:
            # The state the UI must not render as a hang. The agent is
            # blocked on us — a permission decision or an ask_question —
            # and the difference between "thinking" and "waiting for you"
            # is the whole of whether the user knows to act.
            self.stats.permission_prompts += 1
            events.append(
                Event(
                    "systemEvent",
                    {
                        "subtype": "waiting_for_user",
                        "data": {"step_id": _step_id(step)},
                    },
                )
            )

        return events

    # ------------------------------------------------------------------
    # Text and thinking
    # ------------------------------------------------------------------

    def _text_events(self, step: Any, scope: str | None) -> list[Event]:
        """Prose and reasoning, each accumulating into one block.

        Only ``StepSource.MODEL`` reaches the transcript as the
        assistant's own voice. A USER-sourced step is our own prompt
        echoed back and a SYSTEM-sourced one is the harness talking; both
        would render as the model saying something it did not say.

        ``StepTarget.ENVIRONMENT`` is filtered for the same reason in the
        other direction — it is addressed to the tools rather than the
        reader, and showing it is showing machine chatter. ``UNSPECIFIED``
        and ``UNKNOWN`` are treated as user-facing, because the cost of
        rendering an extra line is smaller than the cost of dropping the
        answer.
        """
        source = _name(getattr(step, "source", None))
        target = _name(getattr(step, "target", None))
        if source in ECHOED_SOURCES or source not in PROSE_SOURCES:
            return []
        if target == MACHINE_TARGET or target not in USER_TARGETS:
            return []

        events = []
        for kind, delta_attr, full_attr in (
            ("thinking", "thinking_delta", "thinking"),
            ("text", "content_delta", "content"),
        ):
            delta = getattr(step, delta_attr, "") or ""
            full = getattr(step, full_attr, "") or ""
            if not delta and not full:
                continue
            block = self._text_block(step, kind, scope)
            # The accumulated field wins when it is present: it is the
            # SDK's own running total, and trusting it over our sum means
            # a dropped delta self-corrects on the next frame instead of
            # leaving the block permanently short.
            block.content = full or (block.content + delta)
            events.append(self._chunk_event(block))
        return events

    def _text_block(self, step: Any, kind: str, scope: str | None) -> _Block:
        block_id = f"{_step_id(step)}:{kind}"
        block = self._blocks.get(block_id)
        if block is None:
            block = _Block(block_id=block_id, kind=kind, agent_id=scope)
            self._blocks[block_id] = block
            self._block_counter += 1
        return block

    def _chunk_event(self, block: _Block) -> Event:
        block.seq += 1
        name = "thinkingChunk" if block.kind == "thinking" else "streamChunk"
        return Event(
            name,
            {
                "block_id": block.block_id,
                "seq": block.seq,
                "content": block.content,
                "done": block.done,
                "agent_id": block.agent_id,
            },
        )

    # ------------------------------------------------------------------
    # Tool cards
    # ------------------------------------------------------------------

    def _tool_events(
        self, step: Any, scope: str | None, status: str
    ) -> list[Event]:
        """One card per call, resolved when its step reaches a terminal status.

        The dedup key is ``ToolCall.id``, which the SDK falls back to
        ``{trajectory_id}:{step_index}`` for when the harness supplied
        none — so a card is stable across the ACTIVE and DONE frames of
        the same call, which is exactly what has to be true for the result
        to attach to it.
        """
        events: list[Event] = []
        for call in getattr(step, "tool_calls", None) or []:
            name = getattr(call, "name", "") or "unknown"
            if name == FINISH_TOOL:
                # The loop's terminator, not a tool the reader cares
                # about. It is a real BuiltinTool and arrives as a real
                # tool call, so a pump that did not name it here would end
                # every single turn with a card saying "finish" — the
                # engine's own bookkeeping rendered as work the agent did.
                # Its payload, `output_string`, is the structured output,
                # which has no UI until AG-9 gives it one.
                continue
            args = dict(getattr(call, "args", None) or {})
            call_id = getattr(call, "id", None) or _step_id(step)

            inputs, results = _split_args(name, args)

            if call_id not in self._tools:
                events.extend(
                    self._tool_use(call_id, name, inputs, call, scope)
                )
            if status in _TERMINAL:
                events.extend(
                    self._tool_result(call_id, name, inputs, results, status)
                )
        return events

    def _tool_use(
        self,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        call: Any,
        scope: str | None,
    ) -> list[Event]:
        self._tools[call_id] = _ToolCall(
            tool_use_id=call_id,
            name=name,
            input=inputs,
            started_at=self._clock(),
            agent_id=scope,
        )
        self.stats.tool_calls += 1
        card = {
            "tool_use_id": call_id,
            "name": name,
            # MCP tools arrive with the server on the call rather than
            # encoded in the name, which is the one place Antigravity is
            # tidier than Claude's `mcp__server__tool` string.
            "server": getattr(call, "server_name", None) or None,
            "input": inputs,
            "status": "pending",
            "invoked_at": _iso_utc(self._wall_clock()),
            "gated": False,
            "agent_id": scope,
            "server_tool": False,
        }
        self._blocks[call_id] = _Block(
            block_id=call_id, kind="tool", tool=card, agent_id=scope
        )
        return [Event("toolUse", card)]

    def _tool_result(
        self,
        call_id: str,
        name: str,
        inputs: dict[str, Any],
        results: dict[str, Any],
        status: str,
    ) -> list[Event]:
        call = self._tools.get(call_id)
        if call is not None:
            if call.resolved:
                return []
            call.resolved = True
            duration_ms = int((self._clock() - call.started_at) * 1000)
        else:  # pragma: no cover - a terminal step for a call never opened
            duration_ms = 0

        text = _render_result(name, results)
        preview, truncated = truncate_tool_result(text)
        # A non-zero exit is a failed command even on a DONE step: the tool
        # ran fine, the thing it ran did not, and a card that reads "ok"
        # over a stack trace is the transcript lying about the turn.
        failed = status in (STATUS_ERROR, STATUS_CANCELED) or _nonzero_exit(results)
        files = [] if failed else _files_written(name, inputs)
        for path in files:
            if path not in self.stats.files_modified:
                self.stats.files_modified.append(path)

        payload = {
            "tool_use_id": call_id,
            "status": "error" if failed else "ok",
            "preview": preview,
            "truncated": truncated,
            "full_bytes": len(text.encode("utf-8")),
            "duration_ms": duration_ms,
            "files_modified": files,
        }
        block = self._blocks.get(call_id)
        if block is not None:
            block.done = True
            if block.tool is not None:
                block.tool = {
                    **block.tool,
                    "status": payload["status"],
                    "result": payload,
                }
        return [Event("toolResult", payload)]

    # ------------------------------------------------------------------
    # Errors, usage, completion
    # ------------------------------------------------------------------

    def _error_event(self, step: Any, error: str, status: str) -> list[Event]:
        return [
            Event(
                "systemEvent",
                {
                    "subtype": "engine_error",
                    "data": {
                        "step_id": _step_id(step),
                        "status": status,
                        "message": error or "the step failed with no message",
                        "http_code": getattr(step, "http_code", 0) or 0,
                    },
                },
            )
        ]

    def _absorb_usage(self, step: Any) -> None:
        """Accumulate ``UsageMetadata`` from a step that carries one.

        Kept, but it is not where the turn's figures come from — see
        :meth:`note_turn_usage`. ``Step.usage_metadata`` is documented as
        "token usage for this specific step's model invocation, or None"
        (SDK ``types.py:914``), and **measured live on 2026-09-02 it was
        ``None`` on all ten steps of a turn that really did call a tool
        and really did bill tokens**. So this path contributes nothing on
        the current SDK and is left in place only because a step that does
        carry usage should not have it dropped.
        """
        self._absorb_usage_metadata(getattr(step, "usage_metadata", None))

    def note_turn_usage(self, usage: Any) -> None:
        """Record the turn's tokens, from the conversation's own diff.

        **The turn's figures come from here, not from the steps.** The SDK
        computes them as ``cumulative_usage - turn_start_usage``
        (``conversation.py:311-319``) and hands the difference over as
        ``Conversation.last_turn_usage``; nothing puts that number on a
        step. Reading only the steps produced an empty ``turnUsage`` on
        every turn — which under AG-6 is the whole of what this engine
        reports in place of a cost, so the browser had a descriptor
        promising tokens and an engine delivering ``{}``.

        Set by the session at turn close, exactly as
        :meth:`note_stop_reason` is and for the same reason: both live on
        the conversation rather than on any step, so the pump cannot reach
        them from inside ``translate``.

        ``None`` is a real answer and leaves the counters alone. The SDK
        returns it for a turn whose total came to zero, and an absent key
        is what lets the browser hide the figure per AG-9 — where a zero
        would have been a measurement.
        """
        self._absorb_usage_metadata(usage)

    def _absorb_usage_metadata(self, usage: Any) -> None:
        """Fold one ``UsageMetadata`` into the turn's counters.

        Tokens only, and no dollar figure is derived from them (AG-6):
        there is no USD anywhere on either Antigravity surface, and the
        only route to one is a price table AIC⚡DC would maintain and that
        would go stale silently — wrong in the direction that matters,
        because a number on screen is believed.

        ``cached_content_token_count`` is forwarded because it is the
        field that actually explains a turn's size here: the measured
        floor is 13,873 input tokens to answer "reply with exactly the
        word: ok", so the cache-hit fraction is the number worth reading.
        """
        if usage is None:
            return
        for name in (
            "prompt_token_count",
            "cached_content_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "total_token_count",
        ):
            value = getattr(usage, name, None)
            if isinstance(value, int):
                # Replaced rather than summed: unlike Claude's per-message
                # usage, these arrive as the connection's own cumulative
                # counters, so adding them would multiply the turn.
                self._usage[name] = value

    #: The stop reason that means nothing happened worth naming.
    #:
    #: The SDK's own words for it: *"Default value; normal completion or
    #: unspecified stop reason"* (``types.py:866``). Every clean turn ends
    #: on it, which makes it the opposite of a terminal reason — it is the
    #: absence of one.
    _NO_STOP_REASON = "UNSPECIFIED"

    def note_stop_reason(self, reason: Any) -> None:
        """Record why the turn ended, for :meth:`stream_complete`.

        Set by the session from the connection rather than read off a
        step, because ``StopReason`` lives on the trajectory state update.
        ``MAX_*_EXCEEDED`` naming which budget cap fired is the whole
        reason AG-6 offers ``BudgetConfig`` in place of a dollar cap.

        **``UNSPECIFIED`` is reported as no reason at all**, and that is a
        translation rather than a filter. The browser's badge table sends
        an unmapped reason to the *header* with ``severity: 'error'``
        (``block-render.js:87-91``) — deliberately, because a reason this
        build has never seen is more likely to matter than not. Forwarding
        the SDK's word for "nothing to report" would therefore stamp a red
        badge reading "UNSPECIFIED" on every normal turn: a label that
        says nothing, in the place reserved for labels that say something
        is wrong. An empty string is what the browser already reads as
        "the engine named no reason", which is exactly the fact.

        Found by the live probe on 2026-09-02, after fixing the bug that
        was hiding it: the reason had been empty for the wrong reason,
        and reading it correctly is what surfaced this one.
        """
        name = _name(reason)
        self._stop_reason = "" if name == self._NO_STOP_REASON else name

    def turn_usage(self) -> dict[str, int]:
        """The turn's token counters, as the SDK reported them."""
        return dict(self._usage)

    def rendered_blocks(self) -> list[dict[str, Any]]:
        """Replay shape for a client that reconnects mid-turn."""
        return [block.to_dict() for block in self._blocks.values()]

    def response_text(self) -> str:
        """The assistant's prose for this turn, blocks joined in order."""
        return "\n\n".join(
            block.content
            for block in self._blocks.values()
            if block.kind == "text" and block.content
        )

    def stream_complete(self) -> list[Event]:
        """The turn's closing events.

        ``turnUsage`` carries tokens and no cost key at all. An absent key
        is what lets the browser hide the figure per AG-9, where a zero
        would have been a measurement — the exact failure the deleted
        ``EngineHealth.mcp`` field is remembered for.
        """
        for block in self._blocks.values():
            block.done = True
        result = {
            "request_id": self.request_id,
            "stop_reason": self._stop_reason,
            "num_tool_calls": self.stats.tool_calls,
            "files_modified": list(self.stats.files_modified),
            "usage": self.turn_usage(),
            "response_text": self.response_text(),
        }
        return [
            Event("turnUsage", {"turn_model_usage": self.turn_usage()}),
            Event("streamComplete", result),
        ]


# ----------------------------------------------------------------------
# Reading an alpha SDK's objects without trusting their shape
# ----------------------------------------------------------------------


def _split_args(name: str, args: dict[str, Any]) -> tuple[dict, dict]:
    """Separate a tool sub-message's inputs from its outputs.

    An unknown tool has no entry, and everything it carries reads as
    input — visible on the card and attributable, rather than silently
    dropped from both halves. Empty output values are discarded so that a
    still-running call does not render a result of ``exit_code: 0``, which
    would say "succeeded" about a command that had not finished.
    """
    result_fields = TOOL_RESULT_FIELDS.get(name)
    if not result_fields:
        return args, {}
    inputs = {k: v for k, v in args.items() if k not in result_fields}
    results = {
        k: v for k, v in args.items() if k in result_fields and v not in (None, "", [])
    }
    return inputs, results


def _render_result(name: str, results: dict[str, Any]) -> str:
    """A tool's outputs as the text the card previews.

    ``run_command`` is special-cased because its two fields are not
    equally interesting: the output is the thing to read and the exit code
    is a one-line header on it. Everything else is rendered generically,
    which keeps a tool added by an SDK bump legible without an edit here.
    """
    if not results:
        return ""
    if name == "run_command":
        output = str(results.get("combined_output", ""))
        code = results.get("exit_code", 0)
        return output if not code else f"exit {code}\n{output}"
    if len(results) == 1:
        return str(next(iter(results.values())))
    return "\n".join(f"{k}: {v}" for k, v in sorted(results.items()))


def _nonzero_exit(results: dict[str, Any]) -> bool:
    code = results.get("exit_code")
    return isinstance(code, int) and code != 0


def _files_written(name: str, inputs: dict[str, Any]) -> list[str]:
    paths = []
    for field_name in TOOL_WRITTEN_PATH_FIELDS.get(name, ()):
        value = inputs.get(field_name)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _scope(step: Any) -> str | None:
    """The subagent this step belongs to, or ``None`` for the main thread.

    ``depth`` rather than the presence of ``parent_trajectory_id``,
    because the top-level trajectory has an id too. Mapped onto the
    Claude pump's ``agent_id`` so the browser routes a subagent's text
    under its row and into its tab with no engine-specific branch —
    AG-R-4's rule that no webapp branch keys off an engine name.
    """
    if not getattr(step, "depth", 0):
        return None
    return getattr(step, "trajectory_id", None) or None


def _step_id(step: Any) -> str:
    """``Step.id``, or the pair it is built from when it is missing."""
    ident = getattr(step, "id", "") or ""
    if ident:
        return str(ident)
    return f"{getattr(step, 'trajectory_id', '')}:{getattr(step, 'step_index', 0)}"


def _name(value: Any) -> str:
    """An enum member's name, whatever it actually is.

    Steps carry ``str``-valued enums, so a member compares equal to its
    own value and a release that turns one into a plain string would keep
    working. Comparing on ``.name`` and falling back to ``str`` is what
    makes that a non-event instead of a silent mis-dispatch.
    """
    if value is None:
        return ""
    return str(getattr(value, "name", value))


#: How much of an unreadable object to log. Enough to identify it, not
#: enough to put a file's contents in a system event.
_MAX_REPR = 300


def _short(text: str) -> str:
    return text if len(text) <= _MAX_REPR else text[:_MAX_REPR] + "…"
