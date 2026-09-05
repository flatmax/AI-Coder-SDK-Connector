"""``agy``'s NDJSON stream, translated into the events the browser reads.

The counterpart of :mod:`aic_dc.antigravity.steps` for the CLI transport.
It emits the **same** event vocabulary — ``streamChunk``, ``toolUse``,
``toolResult``, ``systemEvent``, ``turnUsage``, ``streamComplete`` — so
the chat panel needs no branch for a third transport. AG-R-4: the browser
must not learn engine names.

Two shapes that look alike and are not
======================================
Everything here is written against a capture taken 2026-09-03 in
bidirectional mode, recorded in ``sdk-surface.md`` § *The stream, measured
in bidirectional mode*. Three things in it would each produce a plausible,
wrong pump:

**Frames are nested under their own event name** — ``{"event":
"step_update", "step_update": {…}}`` — not flat. A parser written against
the flat shape reads ``None`` for every field *without erroring*, which is
the failure ``diff_agy_init`` was corrected for at 1.1.22 on a different
frame. So :func:`unwrap` is a named function with a test rather than an
inline ``.get``.

**``text_delta`` is a real delta.** The SDK's ``streamChunk`` carries the
*whole accumulated block* and the browser replaces by ``block_id``;
``agy`` sends only the new fragment. So this module accumulates and emits
the running total, and the browser's replace-by-id stays correct for both
transports. A pump that forwarded the fragment would render only the last
few words of every message — and one that accumulated the SDK's would
repeat every prefix. Neither raises anything.

**``step_type`` is not the closed vocabulary it was recorded as.** The
``-p`` captures gave three members; a plain read-a-file turn produced a
fourth, ``system_message``. On a CLI releasing weekly, an unknown member
is rendered as a system notice rather than dropped — the same rule
``StepType.UNKNOWN`` earns on the SDK side, for the same reason.

What is deliberately absent
===========================
``tool_info.output`` was **not** present on a completed ``find_by_name``
here, though the 1.1.22 correction found it for ``run_command``. So it is
per-tool rather than universal and nothing may require it: a tool result
with no output is reported as complete with none, never as pending
forever.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-R-4;
``sdk-surface.md`` § *The stream, measured in bidirectional mode*.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aic_dc.agy import tools as agy_tools
from aic_dc.antigravity.steps import TurnStats
from aic_dc.claude_code.messages import Event, files_written_by

logger = logging.getLogger(__name__)

#: ``state`` values that end a step. Anything else leaves it in flight.
TERMINAL_STATES = frozenset({"DONE", "ERROR", "CANCELED"})

#: The ``step_type`` members this pump dispatches on. Listed so an unknown
#: one is *recognised as unknown* rather than silently matching nothing —
#: the vocabulary was documented as three members and turned out to have
#: at least four.
KNOWN_STEP_TYPES = frozenset(
    {"user_input", "agent_response", "tool", "system_message"}
)


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def unwrap(frame: Any, event: str) -> dict[str, Any] | None:
    """The payload nested under its own event name, or ``None``.

    ``{"event": "result", "result": {…}}``. Named rather than inlined
    because reading this shape flat is the mistake that does not announce
    itself: every field comes back ``None`` and the turn renders empty.
    """
    if not isinstance(frame, dict) or frame.get("event") != event:
        return None
    inner = frame.get(event)
    return inner if isinstance(inner, dict) else None


class AgyTranslator:
    """One turn's worth of ``agy`` frames, as browser events.

    Stateful for one reason: ``text_delta`` is a delta and the browser
    wants the running total. Everything else could be a function.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self._text: dict[int, str] = {}
        self._seq: dict[int, int] = {}
        self._tools: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, int] = {}
        self._status = ""
        self._response = ""
        # The *same* accounting object the SDK transport's translator
        # carries, and it is shared rather than reinvented because a
        # caller they share reaches straight into it:
        # `AntigravityService._note_permission_prompt` — inherited by
        # `AgyService` — does `translator.stats.permission_prompts += 1`.
        # Without this attribute every permission dialog on this transport
        # raised `AttributeError` there. It was caught and logged as
        # "Could not record the permission prompt on the turn", so the
        # dialog still worked and only the turn's prompt count was lost,
        # which is why 4,300 green tests and a working gate did not show
        # it. Found by watching the log during the phase-8 write run.
        self.stats = TurnStats()

    # ------------------------------------------------------------------
    # Frames in
    # ------------------------------------------------------------------

    def translate(self, frame: Any) -> list[Event]:
        """One frame to zero or more events. Never raises.

        A frame this pump cannot read is reported as a system notice, not
        dropped: on a weekly-releasing CLI a frame nobody renders is how a
        new capability arrives as silence in the chat.
        """
        try:
            return self._translate(frame)
        except Exception:  # noqa: BLE001 - a pump must not kill a turn
            logger.exception("Could not translate an agy frame")
            return [
                Event(
                    "systemEvent",
                    {
                        "subtype": "step_unreadable",
                        "data": {"repr": repr(frame)[:400]},
                    },
                )
            ]

    def _translate(self, frame: Any) -> list[Event]:
        if not isinstance(frame, dict):
            return []
        event = frame.get("event")
        if event == "result":
            return self._absorb_result(unwrap(frame, "result") or {})
        if event != "step_update":
            # `init` is consumed by the session, which needs the
            # conversation id before this translator exists.
            return []
        step = unwrap(frame, "step_update")
        if step is None:
            return []
        return self._step(step)

    def _step(self, step: dict[str, Any]) -> list[Event]:
        step_type = str(step.get("step_type") or "")
        index = step.get("step_index")
        index = int(index) if isinstance(index, int) else -1
        state = str(step.get("state") or "")

        if step_type == "agent_response":
            return self._agent_response(step, index, state)
        if step_type == "tool":
            return self._tool(step, index, state)
        if step_type == "user_input":
            # Our own prompt, echoed. The browser already rendered it
            # optimistically when the user pressed send.
            return []
        if step_type == "system_message":
            return [
                Event(
                    "systemEvent",
                    {
                        "subtype": "engine_notice",
                        "data": {
                            "step_id": str(index),
                            "message": str(step.get("text") or ""),
                        },
                    },
                )
            ]
        if step_type not in KNOWN_STEP_TYPES:
            # Rendered, never dropped — see the module docstring.
            return [
                Event(
                    "systemEvent",
                    {
                        "subtype": "unknown_step",
                        "data": {
                            "step_id": str(index),
                            "step_type": step_type,
                            "state": state,
                        },
                    },
                )
            ]
        return []

    def _agent_response(
        self, step: dict[str, Any], index: int, state: str
    ) -> list[Event]:
        self._absorb_usage(step)
        delta = step.get("text_delta")
        if not isinstance(delta, str) or not delta:
            return []
        # Accumulate: agy sends fragments, the browser replaces by block
        # id. Doing this here rather than in the browser keeps one rule in
        # the client for both transports.
        self._text[index] = self._text.get(index, "") + delta
        self._seq[index] = self._seq.get(index, 0) + 1
        return [
            Event(
                "streamChunk",
                {
                    "block_id": f"agy-text-{index}",
                    "seq": self._seq[index],
                    "content": self._text[index],
                    "done": state in TERMINAL_STATES,
                    "agent_id": None,
                },
            )
        ]

    def _tool(self, step: dict[str, Any], index: int, state: str) -> list[Event]:
        self._absorb_usage(step)
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        name = str(step.get("tool_name") or info.get("name") or "")
        params = info.get("parameters")
        params = dict(params) if isinstance(params, dict) else {}
        call_id = f"agy-tool-{index}"

        if call_id not in self._tools:
            self.stats.tool_calls += 1
            card = {
                "tool_use_id": call_id,
                "name": name,
                "server": None,
                "input": params,
                "status": "pending",
                "invoked_at": _iso_utc(),
                # Every mutating call on this transport passes the gate,
                # so the card says so from the moment it appears rather
                # than after the fact.
                "gated": True,
                "agent_id": None,
                "server_tool": False,
            }
            self._tools[call_id] = card
            events = [Event("toolUse", card)]
            if state not in TERMINAL_STATES:
                return events
        else:
            events = []
            if state not in TERMINAL_STATES:
                return events

        # A completed write that landed somewhere else. Checked here
        # because this is the first moment the target and the outcome are
        # both known, and reported as a system event rather than folded
        # into the tool card: the card says the call succeeded, which is
        # what `agy` told us, and the correction has to be louder than the
        # thing it corrects.
        if name in agy_tools.MUTATING_TOOLS:
            diverted = _diverted_copy(params)
            if diverted is not None:
                target, copy = diverted
                logger.warning("agy diverted a write: %s -> %s", target, copy)
                events = events + [
                    Event(
                        "systemEvent",
                        {
                            "subtype": "engine_error",
                            "data": {
                                "message": (
                                    f"The agent reported writing {target}, but "
                                    f"the file is not there — agy put it in "
                                    f"{copy} instead and reported success. "
                                    f"This is a known agy behaviour, not a "
                                    f"failure of the edit: the content is in "
                                    f"that file. Nothing in the repository was "
                                    f"changed."
                                )
                            },
                        },
                    )
                ]

        # `tool_info.output` is per-tool rather than universal, so a
        # completed call with none is complete with no output — never left
        # pending, which would spin forever.
        output = info.get("output")
        failed = state == "ERROR"
        # Which files this call put on disk, from the one shared table
        # (`claude_code.messages.files_written_by`, which knows all three
        # engines' tool vocabularies). This payload had no such key at
        # all, so the file tree never learned that a turn had written
        # anything: the user's write landed and the picker went on showing
        # the repository as it was before. Empty on a failed call — a
        # refused write modified nothing, and saying otherwise would make
        # the tree reload for a file that is not there.
        files = [] if failed else files_written_by(name, params)
        for path in files:
            if path not in self.stats.files_modified:
                self.stats.files_modified.append(path)
        return events + [
            Event(
                "toolResult",
                {
                    "tool_use_id": call_id,
                    "name": name,
                    "status": "error" if failed else "success",
                    "content": "" if output is None else str(output),
                    "duration_ms": _duration_ms(step.get("duration_seconds")),
                    "agent_id": None,
                    "files_modified": files,
                },
            )
        ]

    def _absorb_result(self, result: dict[str, Any]) -> list[Event]:
        self._status = str(result.get("status") or "")
        response = result.get("response")
        if isinstance(response, str):
            self._response = response
        self._absorb_usage(result)
        return []

    def _absorb_usage(self, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        # Later frames carry running totals rather than increments, so the
        # last one wins. Summing them would multiply the bill by the number
        # of steps.
        for key, value in usage.items():
            if isinstance(value, int):
                self._usage[key] = value

    # ------------------------------------------------------------------
    # The turn's close
    # ------------------------------------------------------------------

    def turn_usage(self) -> dict[str, int]:
        return dict(self._usage)

    def response_text(self) -> str:
        """The turn's prose.

        Prefers ``result.response``, which is ``agy``'s own assembly of it,
        and falls back to the accumulated deltas for a turn that ended
        without a result — a cancel, or a process that died.
        """
        if self._response:
            return self._response
        return "\n\n".join(self._text[k] for k in sorted(self._text) if self._text[k])

    def stream_complete(self) -> list[Event]:
        """The turn's closing events, in the shape the browser already reads.

        ``SUCCESS`` becomes an empty stop reason, because the browser reads
        an unrecognised reason as something worth a red badge — the lesson
        phase 3 learned when ``UNSPECIFIED`` would have put one on every
        clean turn. Anything else is forwarded verbatim, since that is the
        only account of why a turn stopped early.
        """
        stop_reason = "" if self._status in ("SUCCESS", "") else self._status
        return [
            Event("turnUsage", {"turn_model_usage": self.turn_usage()}),
            Event(
                "streamComplete",
                {
                    "request_id": self.request_id,
                    "stop_reason": stop_reason,
                    "num_tool_calls": self.stats.tool_calls,
                    # The turn's files, accumulated per call rather than
                    # left empty: the footer lists what the turn touched,
                    # and an empty list said "nothing" for every turn that
                    # wrote something.
                    "files_modified": list(self.stats.files_modified),
                    "usage": self.turn_usage(),
                    "response_text": self.response_text(),
                },
            ),
        ]


#: Where ``agy`` puts a write it declined to make where it was asked.
#: See :data:`aic_dc.antigravity.rules` for why this is not simply a
#: ``trustedWorkspaces`` question.
SCRATCH_DIR = Path.home() / ".gemini" / "antigravity-cli" / "scratch"


def _diverted_copy(params: dict[str, Any]) -> tuple[str, str] | None:
    """``(target, scratch_copy)`` when a write went somewhere else.

    [AG-R-3](../../../specs5/plan-ag/risks.md#ag-r-3) is the worst failure
    this transport has, because it is **silent and looks like success**:
    ``agy`` writes the file into its own scratch directory, tells the model
    it succeeded, and the file tree and diff viewer — both rooted at the
    repo — show nothing. The user's reading is "the agent lied about
    editing my file", and there is no path from that symptom to the cause,
    which sits in a settings file belonging to another product.

    Detected here rather than prevented at startup, and that is a decision
    forced by measurement. `risks.md` specifies a startup health check
    asserting "the repo root is a workspace the engine will write to" — but
    a check phrased against ``trustedWorkspaces`` **passes on a machine
    where writes divert anyway**, measured three times on 2026-09-05 from
    inside a trusted root. The only honest check is an actual write, and a
    write costs a turn on the user's subscription at every startup.

    So the check runs where it is free: a completed write already names its
    target, so one ``stat`` says whether the file is there. Deliberately
    **narrow** — it fires only when the target is missing *and* a file of
    that name is sitting in the scratch directory. That pair has no
    innocent explanation, where "the file is missing" alone has several
    (the model named a path it never created, a tool that failed for an
    unrelated reason), and a false alarm about a write that did land would
    be worse than the silence it replaces.
    """
    raw = params.get("TargetFile") or params.get("OutputPath")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        target = Path(raw)
        if target.exists():
            return None
        candidate = SCRATCH_DIR / target.name
        if candidate.is_file():
            return (str(target), str(candidate))
    except OSError:  # noqa: BLE001 - a diagnostic, never a control path
        return None
    return None


def _duration_ms(seconds: Any) -> int:
    if isinstance(seconds, (int, float)) and seconds >= 0:
        return int(seconds * 1000)
    return 0
