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
import shutil
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aic_dc.agy import roots
from aic_dc.agy import tools as agy_tools
from aic_dc.antigravity.steps import TurnStats, terminal_reason_for
from aic_dc.claude_code.messages import (
    CANCELLED_TERMINAL_REASONS,
    Event,
    files_written_by,
    truncate_tool_result,
)

logger = logging.getLogger(__name__)

#: ``state`` values that end a step. Anything else leaves it in flight.
TERMINAL_STATES = frozenset({"DONE", "ERROR", "CANCELED"})

#: How a tool whose name this pump could not read is listed to the model
#: that asked. Phrased as prose rather than as a placeholder token
#: because that is where it is rendered — into "this one reached for X
#: and Y" — and a reader seeing `<unknown>` there would reasonably think
#: it was the tool's name.
UNNAMED_TOOL = "a tool it did not name"

#: What this app can say about a call the consultation's allowlist
#: barred, once that call has reported. Three words rather than a
#: boolean, because a review round found that collapsing them into
#: *refused* is the app asserting something it does not know.
#:
#: ``REFUSED`` — the gate's own mark came back with it, so this app
#: refused it and nothing was retrieved. That is the only state in
#: which the header's *nothing below was read* is a fact.
#: ``BREACHED`` — it came back with output the gate never wrote, so
#: it ran. :meth:`AgyTranslator._breach_notice`.
#: ``UNVERIFIED`` — it ended, and this app cannot establish which of
#: the two happened: no mark, no output, or no result at all because
#: the process died mid-call. :meth:`AgyTranslator._note_unverified`.
REFUSED = "refused"
BREACHED = "breached"
UNVERIFIED = "unverified"

#: The ``step_type`` members this pump dispatches on. Listed so an unknown
#: one is *recognised as unknown* rather than silently matching nothing —
#: the vocabulary was documented as three members and turned out to have
#: at least four.
KNOWN_STEP_TYPES = frozenset(
    {"user_input", "agent_response", "tool", "system_message", "subagent"}
)

#: ``state`` → the status word the browser's LED table knows.
#:
#: Not free prose: ``subagent-tabs.js``'s ``_TERMINAL_LED`` maps
#: ``completed`` to green, ``failed`` to red and ``stopped``/``killed`` to
#: amber, and anything it does not recognise falls through to amber. So a
#: cancelled subagent is reported as ``stopped`` — which is both what it
#: is and the word that lands on the colour meant for it, rather than
#: arriving at the same colour by not being understood.
SUBAGENT_STATUS = {
    "DONE": "completed",
    "ERROR": "failed",
    "CANCELED": "stopped",
}


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def subagent_entries(step: Any) -> list[dict[str, Any]]:
    """The subagents one ``subagent`` step announces, in order.

    Module level and shared, because **two callers need this and they need
    it for different reasons**: the pump turns each entry into a row, and
    :class:`~aic_dc.agy.session.AgySession` claims each entry's
    conversation so the subagent's tool calls reach the permission gate.
    A second copy of this parse is how one of them would quietly stop
    seeing a subagent the other could see — and the one that stops seeing
    is a containment hole rather than a missing tab.
    """
    if not isinstance(step, dict):
        return []
    info = step.get("subagent_info")
    if not isinstance(info, dict):
        return []
    entries = info.get("subagents")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


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

    def __init__(
        self,
        request_id: str,
        *,
        agent_id: str | None = None,
        repo_root: Path | None = None,
        conversation_id: str | None = None,
        config_root: Path | None = None,
    ) -> None:
        self.request_id = request_id
        # Both are needed to collect a generated image, and neither is in
        # the stream: `generate_image` names no path (see the comment
        # above `locate_generated_image`), so
        # the destination is this repository and the source is found under
        # the conversation's own directory. Supplied by the service, which
        # has the session, rather than scraped from frames — the engine's
        # `init` frame is consumed before this translator exists.
        #
        # Left unset by the consultant, which builds a translator of its
        # own and collects after the turn from the whole frame list. That
        # is not a second implementation of this: it locates with the same
        # function, and the two differ only in that one of them may fall
        # back to the newest file in the directory.
        self._repo_root = Path(repo_root).resolve() if repo_root else None
        self._conversation_id = conversation_id or ""
        # **The config root this turn's `agy` is running against**, which is
        # where its brain and scratch directories are — not this server's
        # `HOME`, which is what both were pinned to until 2026-09-11
        # (AG-R-18). AG-21 gives the master and the consultant a root each,
        # so there is no single right value for a module constant, and
        # getting it wrong is silent in both directions: an image that is
        # never found, and a diverted-write detector that never fires.
        #
        # Optional, because a translator can be built for a stream that
        # carries neither — every test of text handling builds one. The two
        # places that need it say so out loud when it is missing rather than
        # returning a quiet empty string, because "no image was collected"
        # and "nobody told this translator where to look" are different
        # facts and only one of them is a bug.
        self._config_root = Path(config_root) if config_root else None
        # A file older than the turn belongs to an earlier turn of the
        # same conversation. See `locate_generated_image`.
        self._started_at = time.time()
        # When set, every block and card this translator produces is
        # attributed to that agent rather than to the main thread — which
        # is how a consultation on this transport gets its own tab
        # (AG-13, reached over `agy` by AG-16). The SDK translator has
        # carried this since the consultation tab was built; this one was
        # written for the engine, where there is only ever the main
        # thread, and hardcoded `None` in the four places below.
        #
        # There is no per-step scope to derive here, unlike the SDK
        # translator's `_scope`: `agy`'s stream carries no trajectory or
        # depth field at all, so a nested trajectory is invisible to this
        # pump and the whole turn belongs to one agent. That is why a
        # subagent's *work* is not read out of this stream at all — it is
        # read off disk from the conversation `agy` writes for the
        # subagent, by `agy/subagents.py`, which is what makes the
        # `subagent_transcripts` surface supported here. What this pump
        # contributes is the announcement below, and nothing more.
        self._agent_id = agent_id or None
        # Every subagent this turn announced, by its own conversation id.
        # Held rather than emitted and forgotten because the tab's
        # *content* is not in this stream: `log_uri` is reported once, in
        # the announcement, and is the only route to what the subagent
        # actually did.
        self._subagents: dict[str, dict[str, Any]] = {}
        #: Subagents the user pressed ⏹ on, by conversation id. See
        #: `mark_stopped` — `agy` reports a starved subagent as DONE, so
        #: the terminal word for these is ours rather than the stream's.
        self._stopped: set[str] = set()
        #: The tool names this turn's policy allows, when the turn is a
        #: consultation; ``None`` on the engine, where the dialog decides
        #: per call and there is no static answer to compare against.
        #:
        #: Set by :meth:`AgyConsultant._run`, which is the only place the
        #: policy and the translator are both in scope. It is what lets a
        #: *denial* be told from a *failure* without reading `agy`'s error
        #: prose: under a static policy a tool that is not on the list
        #: cannot have run, so a failed call of one was refused by this
        #: app — which is a fact this app owns rather than a string the
        #: vendor round-trips.
        self._allowed: frozenset[str] | None = None
        #: Tools this consultation reached for and got nothing back
        #: from, in the order they were first reached for. A dict rather
        #: than a set because the order is the reader's: "it tried the
        #: web, then tried to read a file" is a sentence, and a set would
        #: shuffle it.
        #:
        #: **Not only the calls this app's own gate refused,
        #: deliberately.** What the reader of an answer needs to know is
        #: that the consultation tried to fetch something and has none of
        #: it, which is true of a call this app refused *and* of one that
        #: died before reaching the gate. Keying this on the gate's
        #: refusal mark made the warning depend on which component said
        #: no, so an argument rejected upstream, or a gate that failed and
        #: took the call with it, produced an answer that may describe a
        #: page nobody fetched with nothing said about it — the original
        #: bug, reached by a different road. See :meth:`_is_barred`.
        self._ungrounded: dict[str, None] = {}
        #: Tools this consultation was not permitted to use that reported
        #: *output* anyway. Insertion-ordered and expected to stay empty
        #: forever: it is populated only where the gate did not hold, and
        #: every measured run has left it empty. It exists because the
        #: bridge header states, unconditionally and in this app's own
        #: voice, that nothing was read — and this is the one observation
        #: that can make that sentence false. See :meth:`_breach_notice`.
        self._breached: dict[str, None] = {}
        #: Tools this consultation was not permitted to use that ended
        #: without this app being able to establish whether anything came
        #: back from them — no refusal mark, no output, or no result at
        #: all. Insertion-ordered. The header qualifies itself for these
        #: rather than claiming they retrieved nothing, which is the one
        #: thing this app cannot know about them. See
        #: :meth:`_note_unverified`.
        self._unverified: dict[str, None] = {}
        #: Tools this consultation was *permitted* that the binary it ran
        #: against never advertised. Insertion-ordered, and the odd one out
        #: in this group of four: the other three are things a tool call
        #: did, this one is a thing that was true before the first prompt
        #: was sent. Set by :meth:`note_unadvertised` from what `agy`'s own
        #: ``init`` frame declared, and read where an empty answer or a
        #: timeout has to be explained — because "the tool it needed to
        #: end its turn is not a tool this binary has" is the difference
        #: between a diagnosis and a shrug.
        #: [AG-R-22](../../../specs5/plan-ag/risks.md#ag-r-22).
        self._unadvertised: dict[str, None] = {}
        #: This consultation's refusal-token issuer, from its stamped
        #: policy. ``None`` on the master engine and on a consultation
        #: nobody stamped. See :meth:`note_consultation`.
        self._refusals: Any = None
        #: Call ids that have already been given a ``toolResult``. A
        #: barred call that reported an error is drawn as finished on
        #: *this app's* reading rather than on `agy`'s state word, so the
        #: frame that says ``ERROR`` can still arrive afterwards for a
        #: card the reader has already seen settle — and would draw a
        #: second result against the same id, which `blocks.js` keys tool
        #: cards by. One result per call, first telling wins, because the
        #: first telling is the one that carries the reason.
        self._settled: set[str] = set()
        self._text: dict[int, str] = {}
        self._seq: dict[int, int] = {}
        self._tools: dict[int, dict[str, Any]] = {}
        self._usage: dict[str, int] = {}
        self._status = ""
        self._response = ""
        #: Whether the user stopped this turn. Set by `note_cancelled`
        #: rather than read off the stream, because the stream cannot say
        #: so — see that method.
        self._cancelled = False
        #: Whether the freeze has been announced. Once per turn: the
        #: suppression runs on every frame after the stop and one card per
        #: withheld frame would bury the sentence it is there to say.
        self._announced_stop = False
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
        if self._cancelled:
            # **The view is frozen; the meter is not.** AG-19's residual
            # gap: `PostInvocation` fires *between* invocations, so a turn
            # already inside one runs to its own end and a prose answer
            # asks permission for nothing and cannot be starved at all.
            # Killing the process would stop it and costs 3.7s of dead
            # session, so the specified handling is presentational —
            # *"stop updating the view … let the stream drain into the
            # warm process"*. This is that line. Frames are still read, so
            # the process stays usable for the next turn; they just stop
            # becoming view.
            #
            # Usage is absorbed anyway because the turn is still spending.
            # Hiding that would be a cost the UI cannot account for, which
            # is AG-R-6's family and the reason `_absorb_result` was fixed.
            # Everything else — blocks, prose, tool cards, the counters the
            # footer renders — stops here, so the footer describes the turn
            # the user was shown rather than one that kept growing behind a
            # frozen screen.
            self._absorb_usage(step)
            return self._announce_stop()
        return self._step(step)

    def _announce_stop(self) -> list[Event]:
        """Say once that the view has stopped updating.

        Without this the freeze is indistinguishable from a hang: text
        simply stops arriving, which is also what a model thinking looks
        like. AG-19 asks for the stop to be *badged*, and the footer's
        badge cannot appear until the turn ends — which on the case this
        exists for is the thing taking the time.

        Distinct from `stop_ignored`, which `AgySession` raises after
        :data:`~aic_dc.agy.session.STOP_OVERDUE_SECONDS` and which offers
        the force-restart. This one says *the stop landed and the screen is
        final*; that one says *it has been a while and here is the
        escalation*. A turn that winds down promptly shows only this.
        """
        if self._announced_stop:
            return []
        self._announced_stop = True
        return [
            Event(
                "systemEvent",
                {"subtype": "stop_acknowledged", "data": {}},
            )
        ]

    def _step(self, step: dict[str, Any]) -> list[Event]:
        step_type = str(step.get("step_type") or "")
        index = step.get("step_index")
        index = int(index) if isinstance(index, int) else -1
        state = str(step.get("state") or "")

        if step_type == "agent_response":
            return self._agent_response(step, index, state)
        if step_type == "tool":
            return self._tool(step, index, state)
        if step_type == "subagent":
            return self._subagent(step, index, state)
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
                    "agent_id": self._agent_id,
                },
            )
        ]

    def mark_stopped(self, agent_id: str) -> None:
        """Record that the *user* stopped this subagent.

        Measured 2026-09-10 by ``scripts/probe_agy_subagent_stop.py``: a
        subagent starved by an aimed refusal is reported by ``agy`` as
        **DONE**, not ``CANCELED``. Which is defensible from the harness's
        side — the step did finish, once the agent read the refusal and
        wound down — and is the wrong thing to put on a row, because
        ``DONE`` maps to ``completed`` and a green LED over work the user
        stopped.

        So the status is overridden here rather than inferred from the
        stream. This host knows something the stream does not report: that
        a human pressed ⏹ on this conversation. ``stopped`` is the word
        ``subagent-tabs.js``'s LED table maps to amber, and it is what
        happened.

        Not a guess about the agent's *behaviour* — a subagent producing
        only prose is never refused anything and finishes its work
        normally, and this will still label it stopped. That is the honest
        reading of the row all the same: it reports what the user did to
        it, and the transcript beside it reports what it managed to do.
        """
        if agent_id:
            self._stopped.add(str(agent_id))

    @property
    def subagents(self) -> frozenset[str]:
        """Every subagent conversation id this turn has announced.

        Read by :meth:`~aic_dc.agy.service.AgyService.stop_task`, which
        must not aim a refusal at a conversation this turn never spawned:
        a subagent's ``agent_id`` is `agy`'s own conversation id, so an
        unchecked one is "stop any Antigravity conversation on this
        machine by id" — the same containment `agy/subagents.py` states
        for reading one.
        """
        return frozenset(self._subagents)

    def _subagent(self, step: dict[str, Any], index: int, state: str) -> list[Event]:
        """A delegation, as the row and tab the strip already renders.

        **Antigravity names its subagents by announcement, not by scope.**
        The SDK transport marks every step of a nested trajectory with
        ``depth`` and ``trajectory_id``, which is what
        ``antigravity/steps.py``'s ``_scope`` reads. This stream has
        neither — a fact recorded at ``agy`` 1.1.22 and still true at
        1.1.27 — and the conclusion drawn from it, that a subagent is
        therefore invisible here, was wrong. A ``subagent`` step carries
        ``subagent_info.subagents[]``, each entry naming a
        ``conversation_id`` of its own, its ``role``, its ``type_name``
        and a ``log_uri`` pointing at the transcript it writes. Measured
        2026-09-09 by ``scripts/probe_agy_subagent_frames.py``.

        That is enough for AG-13's five-point contract, which asks for an
        *identity* rather than for the SDK's mechanism: the same id on the
        event and on the blocks, a label, and a terminal flag. So no new
        event name and no webapp change — ``subagent-tabs.js`` joins on
        identifiers alone.

        The id is the subagent's own ``conversation_id`` rather than a
        minted one, which is the opposite of the consultation bridge's
        choice (``antigravity/bridge.py._new_agent_id``) and for the
        reason that decision states: it mints because an in-process MCP
        handler *cannot learn* an id. Here the engine supplies one, it is
        stable across the ACTIVE and DONE frames, and it is also the key
        to the transcript on disk — so borrowing it costs nothing and buys
        the join that content will need.

        A list, not an entry: one step can announce several subagents, and
        each gets its own row. The composed fallback id keeps a delegation
        visible if a release ever omits the conversation id — this pump
        renders what it cannot read rather than dropping it.
        """
        self._absorb_usage(step)
        terminal = state in TERMINAL_STATES

        events: list[Event] = []
        for position, entry in enumerate(subagent_entries(step)):
            agent_id = (
                str(entry.get("conversation_id") or "")
                or f"agy-subagent-{index}-{position}"
            )
            known = self._subagents.get(agent_id)
            if known is None:
                self.stats.tool_calls += 1
                known = {
                    "agent_id": agent_id,
                    # Kept for the tab's content, which is not in this
                    # stream: the subagent's steps go to its own
                    # transcript, and this is the only place its location
                    # is reported.
                    "log_uri": str(entry.get("log_uri") or ""),
                    "role": str(entry.get("role") or ""),
                    "type_name": str(entry.get("type_name") or ""),
                    "initial_prompt": str(entry.get("initial_prompt") or ""),
                }
                self._subagents[agent_id] = known
            events.append(
                Event(
                    "subagentEvent",
                    {
                        # All three, because `streaming.js` falls back
                        # through them in this order and a payload that
                        # sets only one relies on which fallback ran.
                        "task_id": agent_id,
                        "agent_id": agent_id,
                        "tool_use_id": agent_id,
                        # Labels only, per the contract. `role` is what
                        # the model called this subagent — "Notes Reader"
                        # — which is the description a Claude `Task`
                        # carries; `type_name` is its kind.
                        "description": known["role"],
                        "subagent_type": known["type_name"],
                        "task_type": str(step.get("tool_name") or "subagent"),
                        # A subagent the user stopped is `stopped`,
                        # whatever `agy` says the step's state was — it
                        # says DONE, measured. See `mark_stopped`.
                        "status": (
                            "stopped"
                            if agent_id in self._stopped
                            else SUBAGENT_STATUS.get(state, "running")
                        )
                        if terminal
                        else "running",
                        # Without this the tab streams for the rest of the
                        # session: the browser sets
                        # `state.streaming = !row.terminal`.
                        "terminal": terminal,
                    },
                )
            )
        return events

    def _tool(self, step: dict[str, Any], index: int, state: str) -> list[Event]:
        self._absorb_usage(step)
        info = step.get("tool_info")
        info = info if isinstance(info, dict) else {}
        name = str(step.get("tool_name") or info.get("name") or "")
        params = info.get("parameters")
        params = dict(params) if isinstance(params, dict) else {}
        call_id = f"agy-tool-{index}"
        # Read before the card is drawn, because a call that could not
        # have run is over whatever `agy` calls the state it is in. The
        # state words are the vendor's and can grow a `BLOCKED` or a
        # `REJECTED` without notice; *this tool cannot run here and it has
        # reported an error* is a thing this app knows on its own.
        message = _error_message(info)
        # **The key, not the message.** `_outcome` needs to know whether
        # the frame reported a failure *at all*, and the message is the
        # wrong instrument for that: `agy` is Go, its JSON omits empty
        # fields, and an error whose message is the empty string arrives
        # with the message gone and the `type` still there. Reading
        # `bool(message)` would call such a frame a clean completion,
        # which is the one mistake in this method that manufactures a
        # containment alarm out of a vendor serialising a default. The
        # key survives an empty message, an error spelled as a bare
        # string, and an explicit `null`.
        has_error = "error" in info
        # `tool_info.output` is per-tool rather than universal, so a
        # completed call with none is complete with no output — never left
        # pending, which would spin forever.
        output = info.get("output")
        barred = self._is_barred(name)
        # **A barred call that has reported anything is over.** This used
        # to require an *error*, which left the one shape that matters
        # most — a barred call that came back with output, meaning the
        # gate did not hold — waiting for the vendor to say a terminal
        # word before this app would look at it. The gate's answer is
        # final and output from a call that could not run is not going to
        # be revised into a success, so either is enough to settle it.
        stopped = barred and bool(message or output)

        if call_id in self._settled:
            # See `_settled`. Nothing is re-emitted: the card exists, its
            # result is drawn, and the file bookkeeping below has already
            # run once for this call.
            return []

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
                "agent_id": self._agent_id,
                "server_tool": False,
            }
            self._tools[call_id] = card
            events = [Event("toolUse", card)]
            if state not in TERMINAL_STATES and not stopped:
                return events
        else:
            events = []
            if state not in TERMINAL_STATES and not stopped:
                return events

        # A completed write that landed somewhere else. Checked here
        # because this is the first moment the target and the outcome are
        # both known, and reported as a system event rather than folded
        # into the tool card: the card says the call succeeded, which is
        # what `agy` told us, and the correction has to be louder than the
        # thing it corrects.
        if name in agy_tools.MUTATING_TOOLS:
            diverted = _diverted_copy(params, self._config_root)
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

        # **A barred call that settles has failed, whatever word `agy`
        # put on it.** This used to read `state == "ERROR" or stopped`,
        # which asks the vendor whether a call this app had already
        # established could not run here went well. The state words are
        # the vendor's: a release reporting a hook-denied step as `DONE`
        # with the refusal in `output` rather than `error` would have
        # drawn a green success card for a call that retrieved nothing
        # and — because the grounding test took `failed` — left both the
        # notice and the bridge header silent. Every other hole found in
        # this mechanism was the app being quiet in a case nobody had
        # imagined; this is the shape where it is *wrong*, which a review
        # round was right to separate from the rest.
        #
        # There is no exception to it, and the version that carved one
        # out for a breach was wrong twice over. It read `barred and not
        # message and bool(output)` and then *dropped* `failed` for that
        # call, on the reasoning that a tool which ran did not fail — true
        # about the word, and it drew a **green success card for an
        # escaped call** and left the same green card standing for a
        # refusal a release chose to report in `output` instead of
        # `error`, which is the exact frame shape this whole invariant
        # was added for. A breach is not a success. It fails like
        # everything else the allowlist bars, and what makes it different
        # is said in its own row, above the card, where it cannot be read
        # as the vendor's word for the call.
        #
        # `bool(message)` in its own right, not folded into `stopped`:
        # `stopped` is barred-only, so on the master engine — and on the
        # consultation's one allowed tool — this used to reduce to `state
        # == "ERROR"` alone, and a call whose error `agy` reported under
        # a `DONE` state drew green with its reason discarded.
        outcome = self._outcome(name, message, output, state, has_error)
        failed = state == "ERROR" or bool(message) or bool(outcome)
        if outcome == BREACHED:
            self._breached.setdefault(name, None)
            events = events + self._breach_notice(name)
        # **A failed call says why, and this pump used to throw the reason
        # away.** `agy` reports a failure in `tool_info.error`, never in
        # `output`, so every denial and every tool error reached the card
        # with `preview: ""` and drew the literal "No output." — measured
        # on both surfaces: a consultation denied `read_url_content`, and
        # the master denied `view_file`. The reason was in the frame each
        # time, in this app's own words, and was discarded one line from
        # the browser (AG-R-22).
        #
        # `not output` rather than `output is None`: a failed call that
        # named an empty output is still a failure with nothing to read,
        # and the version of this that tested for `None` would have left
        # exactly those cards blank.
        if not output and failed:
            output = message
        self._settled.add(call_id)
        notice: list[Event] = []
        if outcome == REFUSED:
            notice = self._note_ungrounded(name)
        elif outcome == UNVERIFIED:
            notice = self._note_unverified(name)
        # Which files this call put on disk, from the one shared table
        # (`claude_code.messages.files_written_by`, which knows all three
        # engines' tool vocabularies). This payload had no such key at
        # all, so the file tree never learned that a turn had written
        # anything: the user's write landed and the picker went on showing
        # the repository as it was before. Empty on a failed call — a
        # refused write modified nothing, and saying otherwise would make
        # the tree reload for a file that is not there.
        # A breached call is a failed one and still wrote what it
        # wrote: the gate did not hold, so the tree has to learn about
        # the file the same way it learns about a permitted write.
        files = (
            files_written_by(name, params)
            if not failed or outcome == BREACHED
            else []
        )
        content = "" if output is None else str(output)
        # `files_written_by` is a table of tools to their *path arguments*
        # and `generate_image` has none, so it cannot answer for this one
        # on any transport — adding `ImageName` to that table would encode
        # a name as a path and no file exists at it. The image is found
        # instead, and only then is there a path to report.
        if name == "generate_image" and not failed:
            collected = self._collect_image(params)
            if collected:
                files = [collected]
                content = (
                    f"{content}\nCollected into the repository: {collected}"
                ).strip()
        for path in files:
            if path not in self.stats.files_modified:
                self.stats.files_modified.append(path)
        # `preview`/`truncated`/`full_bytes`, not `content` — the three
        # fields `block-render.js` renders a result body from, and the
        # same three the Claude and SDK pumps send. This sent `content`
        # from the day it was written, and `renderToolResult` reads
        # `result.preview` with an empty-string default, so **every tool
        # card on this transport drew the literal "No output."** while the
        # output sat unread in the payload beside it. Nothing failed:
        # `applyToolResult` spreads the payload onto the block without
        # mapping, and an absent key is a card with nothing in it rather
        # than an error. AG-R-17.
        #
        # `truncate_tool_result` is the Claude pump's, shared rather than
        # re-derived: two implementations of "how much of a tool result
        # does a card show" would show a user different amounts on
        # different engines for the same reason.
        preview, truncated = truncate_tool_result(content)
        # The refusal notice lands *after* the card that provoked it and
        # before whatever the model writes next, which is the order a
        # reader needs: the call, why it was refused, and then the prose
        # produced without it.
        return events + [
            Event(
                "toolResult",
                {
                    "tool_use_id": call_id,
                    "name": name,
                    "status": "error" if failed else "success",
                    "preview": preview,
                    "truncated": truncated,
                    "full_bytes": len(content.encode("utf-8")),
                    "duration_ms": _duration_ms(step.get("duration_seconds")),
                    "agent_id": self._agent_id,
                    "files_modified": files,
                },
            )
        ] + notice

    def _collect_image(self, params: dict[str, Any]) -> str:
        """Copy a generated image into the repository, and say where.

        Returns ``""`` and logs rather than raising, on every failure. The
        consultant's equivalent raises, and the difference is the caller:
        there, one call exists to produce one image and a collection that
        failed is the call failing. Here it is one step of a turn that has
        others, and a pump that raised would lose the rest of the turn to
        a picture.

        The destination keeps the name the model chose — it is required to
        be lowercase, underscored and at most three words, which is
        already a filename — and takes the extension ``agy`` actually
        produced, since the tool honours no requested one. ``agy``'s own
        ``<name>_<epoch_ms>`` spelling is the fallback when that name is
        taken, so a second image never silently overwrites the first.
        """
        if self._repo_root is None or not self._conversation_id:
            return ""
        if self._config_root is None:
            logger.error(
                "agy generated an image named %r and this translator was built "
                "without a config root, so there is nowhere to look for it "
                "(AG-R-18)",
                params.get("ImageName") or params.get("image_name") or "",
            )
            return ""
        brain = roots.brain_dir(self._config_root)
        image_name = str(params.get("ImageName") or params.get("image_name") or "")
        source = locate_generated_image(
            brain,
            self._conversation_id,
            image_name,
            # A second of slack for coarse mtime granularity, not for a
            # previous turn — those are minutes away, not milliseconds.
            min_mtime=self._started_at - 1.0,
        )
        if source is None:
            logger.warning(
                "agy generated an image named %r and it was not found under %s",
                image_name,
                brain / self._conversation_id,
            )
            return ""
        # `.name` because the model chose this string: the schema asks for
        # a name, but nothing enforces that it is one, and a destination
        # is not a place to find out.
        stem = Path(image_name.strip()).name or source.stem
        try:
            destination = self._repo_root / f"{stem}{source.suffix}"
            if destination.exists():
                destination = self._repo_root / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError as exc:
            logger.warning("agy generated %s and it could not be collected: %s", source, exc)
            return ""
        return str(destination)

    def _absorb_result(self, result: dict[str, Any]) -> list[Event]:
        """The turn's last frame, and the one number in it that lies.

        **A refused turn echoes the previous turn's usage verbatim.**
        Measured 2026-09-10 while probing ``/fork``: one real turn cost
        5,423 input tokens over 1.84 seconds, and the turn ``agy`` then
        refused reported ``status: ERROR`` with *the same* ``input_tokens``
        and ``duration_seconds`` and one output token. Nothing ran, and it
        was billed for the work of the turn before it.

        :meth:`_absorb_usage` takes last-wins rather than summing, so this
        never doubled anything — which is why it survived: it is a
        misreport rather than a multiplication, and a cost the UI cannot
        distinguish from a real one is
        [AG-R-6](../../../specs5/plan-ag/risks.md#ag-r-6)'s family.

        So a non-``SUCCESS`` result contributes no usage at all. What
        remains is whatever the turn's own step frames accumulated, which
        is the honest answer in both directions: a turn that did work
        before failing keeps the tokens it spent, and a turn that ran
        nothing reports nothing rather than reporting somebody else's.
        The empty status is included with ``SUCCESS`` because it is a
        frame that named no status rather than one that named a failure.
        """
        self._status = str(result.get("status") or "")
        response = result.get("response")
        # **Not after a stop**, and this is the half that makes the freeze
        # real rather than cosmetic. `agy` assembles the whole answer here,
        # the browser takes the settled message's content from it, and the
        # deltas it duplicates are the ones the view stopped rendering. So
        # accepting it would freeze the screen for the length of the turn
        # and then paste the complete reply in at the footer — a worse
        # reading of the stop than not freezing at all. `response_text`
        # falls back to the accumulated deltas, which is exactly what was
        # on screen when ⏹ was pressed.
        if isinstance(response, str) and not self._cancelled:
            self._response = response
        if self._status in ("SUCCESS", ""):
            self._absorb_usage(result)
        return []

    def _is_barred(self, name: str, /) -> bool:
        """Whether this tool could not have run in this consultation.

        One condition, and it is a fact about the policy rather than a
        reading of anybody's prose: a consultation runs under a static
        allowlist, so a name that is not on it cannot have executed. Two
        different questions are answered from it — whether the call is
        *over* (:meth:`_tool`, where a barred tool that reported an error
        can have nothing further to say) and whether the answer is
        *missing something* (:meth:`_outcome`).

        **This used to be half of a conjunction with the policy's own
        refusal mark, and the mark lost the argument twice.** It was
        introduced to keep a call that failed *before* reaching the gate
        from being reported as containment that held, which was right
        about the claim and wrong about where to enforce it: the claim
        lives in the error text on the card, which says *"AIC-DC refused
        this tool call."* when that is what happened and says `agy`'s own
        words when it is not, so nothing has to be inferred at all. Then
        it was measured to fail open — P24 arm C, arguments rejected
        against the tool's schema in 203ms with our hook never called —
        and the second review round pointed out that requiring the mark
        for *lifecycle* leaves that card spinning forever, which reads to
        a human as a retrieval still in flight. The mark remains what the
        gate says; it is no longer what this pump listens for. Its
        travelling, truncation and versioning worries went with it.

        **The empty name is barred, and that is the point of having an
        allowlist.** This used to require a name — ``and name`` — so a
        tool step this pump could not read the name of was treated as
        permitted. The names come from the vendor's keys (``tool_name``,
        then ``tool_info.name``); a release that renamed either would
        empty them, and the version of this test that asked for a name
        would then have called every tool in the consultation allowed,
        settled its card green, and told the model that asked that
        nothing had been reached for. An allowlist that cannot identify
        what it is being asked about must answer *no*. Found by a review
        round, and it is the only direction this predicate could fail in
        that is not merely noisy.
        """
        return bool(self._allowed is not None and name not in self._allowed)

    def _is_our_refusal(self, name: str, *parts: Any) -> bool:
        """Whether any of these carries *this call's own* refusal.

        One of the single-use tokens the gate minted for this
        consultation, issued for this tool and not yet seen coming back.
        The issuer is :class:`~aic_dc.agy.gate_server.Refusals` and the
        spending happens here, once per call id, because `_tool` returns
        early for a call it has already settled.

        This was a search for the policy's fixed ``MARK`` until this
        app's own review found that a fixed string is a *content* test:
        the mark is written in this repository's source, tests and specs,
        so a gate that failed open on a consultation that then read one
        of those files would have returned it as retrieved content and
        had the escape filed as a refusal — the exact falsehood the
        classification exists to prevent, arriving through it.

        It was then a single token minted per consultation, until an
        eighth review round pointed out who reads a refusal: the
        consultant. One denial hands the model the secret, and a gate
        that fails open afterwards lets any tool that quotes the
        conversation carry it home — where this test runs *before* the
        output test, so the escape is filed as a denial. A token spent on
        the frame that showed it to the model is worth nothing by the
        time the model could repeat it.

        No issuer answers false, always. An unstamped consultation is one
        this app cannot prove anything about, and the honest failure is
        to claim nothing.
        """
        if self._refusals is None:
            return False
        return bool(self._refusals.spend(name, *parts))

    def _outcome(
        self,
        name: str,
        message: str,
        output: Any,
        state: str,
        has_error: bool,
    ) -> str:
        """What this app knows about a barred call that has reported.

        :data:`REFUSED`, :data:`BREACHED`, :data:`UNVERIFIED`, or ``""``
        for a call the allowlist permits, about which this asks nothing.

        **The three are not the same sentence, and a review round was
        right that collapsing them is this app asserting what it does not
        know.** The header above every answer says, in this app's own
        voice, that nothing below was read. That is a fact about a call
        the gate refused. It is *false* about a call that ran. And about
        a call that ended with neither the gate's mark nor any output —
        an argument rejected upstream, a state word this pump does not
        recognise, a process killed between the request and the result —
        it is a guess dressed as a fact, which is the failure this whole
        mechanism exists to prevent, committed by the component that was
        supposed to prevent it.

        **The gate's own refusal, in the one direction it is sound.** It
        says *this app refused this call*, and nothing else on the wire
        says that — see :meth:`_is_our_refusal` for why it is a token
        rather than the policy's fixed mark.
        Two earlier rounds removed it from the lifecycle test and from
        the ungrounded test, and both were right: a card that waits for
        the mark before settling spins forever when the mark never comes,
        and a warning that requires the mark is silent exactly when the
        gate failed and took the call with it. Neither of those is this.
        Here its *absence* is never read as containment — absence is what
        produces :data:`UNVERIFIED`, the weaker claim — so the mark can
        only ever strengthen what is said, and a release that stopped
        carrying it would cost this app its certainty rather than its
        honesty. Measured present in P26 arm G, in `tool_info.error`, on
        both denied calls.

        **What it is still blind to, deliberately: who refused.** The
        predicate this replaces earned that, and it holds. A call `agy`
        rejected against its own schema before the hook ever saw it is
        as absent from the answer as one this app denied, and the reader
        of an answer has no use for the difference — which is why
        :data:`UNVERIFIED` warns rather than reassures, and why the
        per-call attribution stays where it belongs, on the card, in
        whosever words came back with it.

        Read from the output as well as the error, because the frame
        shape that started this argument is a release that reports a
        denial under `DONE` with the refusal text in ``output``. Such a
        call is refused, not breached: the bytes in it are this app's own
        sentence coming home, not a page somebody fetched.

        **Output is not the test for whether a call ran, and used to be
        the only one.** ``if output`` is false for ``""`` — so a barred
        call that escaped and printed nothing would have been filed
        :data:`UNVERIFIED`, a warning where the alarm belongs. The
        obvious repair, ``output is not None``, was proposed by a review
        round and killed by P28: on this transport ``output`` is not
        *empty* on a call that retrieved nothing, it is **absent**, and
        the module header above records it absent from a **completed**
        ``find_by_name`` as well. It does not separate ran from did-not-run
        in either direction, and no test built on it can.

        What does separate them is the pair ``state`` and *did the frame
        report a failure at all*. Every shape P28 measured that did not
        run carries an ``error`` — the gate's denial, the vendor's schema
        rejection. A call that ran to completion and produced nothing is
        ``DONE`` with none. So a barred call that reaches ``DONE`` clean
        **ran**, whatever it did or did not print, and that is a breach.

        **The blind spot this leaves, which is accepted rather than
        fixed.** A barred call that escaped the gate and *then* failed in
        the host — ``stat: no such file``, ``dial tcp: connection
        refused`` — really did execute, and lands :data:`UNVERIFIED`
        here. It is indistinguishable on the wire from a call the vendor
        rejected against its own schema before the gate ever saw it: both
        are a barred tool, a terminal state, an error this app did not
        write, and no token. Promoting one without the other means
        parsing the vendor's error prose, counting milliseconds, or
        testing whether ``parameters`` came back — and those are the
        vendor-shaped heuristics that every round of this argument has
        rightly refused, because they rot on a weekly release and because
        a false breach on a model's schema typo is an alarm nobody can
        keep believing. The trade is detection soundness for alarm
        precision, and the cost is stated in `AG-R-22`: a catastrophic
        gate failure whose calls all fail in the host is reported as
        unverified noise rather than as a breach. What it never does is
        assert containment — :data:`UNVERIFIED` already withdraws the
        header's conclusion — so the blind spot costs this app its alarm,
        never its honesty.
        """
        if not self._is_barred(name):
            return ""
        if self._is_our_refusal(name, message, output):
            return REFUSED
        if output:
            return BREACHED
        if state == "DONE" and not has_error:
            return BREACHED
        return UNVERIFIED

    def _note_ungrounded(self, name: str) -> list[Event]:
        """Record a call that took nothing back, and notice the first.

        A nameless call is recorded under :data:`UNNAMED_TOOL` rather than
        under ``""``. It is barred — :meth:`_is_barred` bars what it
        cannot identify — so it belongs in this list, and the bridge
        renders the list into a sentence a model reads: an empty string
        there would produce *"reached for view_file and , and got nothing
        back"*. Counting it and declining to name it is the honest
        rendering of a call whose name this pump could not read.
        """
        first = not self._ungrounded
        self._ungrounded.setdefault(name or UNNAMED_TOOL, None)
        return self._grounding_notice() if first else []

    def _note_unverified(self, name: str) -> list[Event]:
        """Record a call whose outcome this app could not establish.

        The mirror of :meth:`_note_ungrounded`, and the reason there are
        two: that one's sentence is *nothing was read or retrieved*,
        which is a fact about a refusal and a fabrication about a call
        that was killed between the request and the result. A review
        round named the category — an indeterminate outcome resolved into
        an affirmative certification of safety — and it is a third kind
        of defect beside the two this mechanism already chased. Silence
        is an omission. A false statement is a commission. This one is
        the app being *certain*, which is worse than either, because the
        header's whole value is that a model can act on it.

        Three shapes reach here, and the sentence has to be true of all
        of them: a call killed between the request and the result; a
        call the vendor rejected against its own schema before the gate
        ever ran, which carries a message that is not ours; and a
        refusal this app cannot authenticate as its own. The last is why
        the wording is *cannot account for* rather than *reported
        nothing* — there may well be a message on that card, and the
        point is that this app did not write it.

        Once per consultation and nameless in the prose, for
        :meth:`_grounding_notice`'s reason: a list frozen at one entry
        while further cards render beneath it reads as an inventory that
        has stopped being kept. The provoking tool is in the payload and
        the full list is on :attr:`unverified_tools`, which the bridge
        reads after the turn.
        """
        first = not self._unverified
        self._unverified.setdefault(name or UNNAMED_TOOL, None)
        if not first:
            return []
        return [
            Event(
                "systemEvent",
                {
                    "subtype": "consultation_unverified",
                    "data": {
                        "agent_id": self._agent_id,
                        "tool": next(iter(self._unverified), ""),
                        "message": (
                            "This consultation reached for a tool, and "
                            "what came back is something this app cannot "
                            "account for: no refusal it can recognise as "
                            "its own gate's, and nothing reported as a "
                            "result. "
                            "A second opinion runs with no tools and no "
                            "repository access, so almost certainly nothing "
                            "was retrieved; but almost certainly is not the "
                            "same as nothing, and this app will not claim "
                            "the stronger one. Treat any file contents, page "
                            "contents, search results or live values below "
                            "as unverified rather than as absent."
                        ),
                    },
                },
            )
        ]

    def _breach_notice(self, name: str) -> list[Event]:
        """The gate did not hold, said louder than the card that hides it.

        A tool absent from the consultation's allowlist that comes back
        with output and no error was *run*. Nothing in the frames is
        wrong at that point — the card renders the vendor's success and
        its output, which is exactly what happened — and that is the
        problem: the thing that needs saying is not in the call, it is in
        the paragraph this app has already committed to writing above the
        answer, which says nothing was read.

        ``engine_error`` was the first cut, on the reasoning that the
        diverted-write correction a few lines up reuses it for the same
        shape — a card that says success and a correction that has to be
        louder. It is wrong here twice. That entry renders as *"the
        engine reported an error"*, and the engine reported nothing: this
        app is reporting on the engine. And it sets ``collapse``, which
        lets the next error of that subtype in the turn replace the one
        row saying the gate failed. So this has a subtype of its own,
        with a toast and no collapse.

        It names the tool, unlike :meth:`_grounding_notice`, because this
        one cannot go stale in the way that one could: a list of what was
        refused grows while the reader watches, but an escape is a
        specific call that a specific person now has to go and look at.
        """
        return [
            Event(
                "systemEvent",
                {
                    "subtype": "consultation_breach",
                    "data": {
                        # Inside `data`, which is where `onSystemEvent`
                        # reads it from to route the row into the
                        # consultation's own tab. A copy at the top level
                        # is read by nothing.
                        "agent_id": self._agent_id,
                        "message": (
                            f"{name or 'A tool'} is not permitted in a second "
                            f"opinion, and it returned output rather than an "
                            f"error — so it ran. The consultation's gate did "
                            f"not hold for that call, and anything the answer "
                            f"says about what it read may be real. Nothing in "
                            f"this repository was changed by it unless the "
                            f"card above says a file was written, but this is "
                            f"worth looking at: it should not be possible."
                        )
                    },
                },
            )
        ]

    def settle_pending(self) -> list[Event]:
        """Finish every card the turn left open, once, at the end.

        A tool card is drawn ``pending`` the moment ``agy`` mentions the
        call and settled when the call reports. **Nothing closed the gap
        when the call never reported.** A consultation that timed out,
        crashed, hit a token ceiling or was killed between the two frames
        left a spinner on screen under a finished answer, which reads as
        a retrieval still running — a reader waiting for it to resolve is
        waiting on something that is already over, which is the AG-R-22
        belief arrived at from the other end. Raised by a review round as
        the surviving half of the settling bug, and it is not specific to
        consultations: the same spinner outlives a master-engine turn
        whose process died mid-call.

        The result says the turn ended rather than guessing why, because
        this runs on every ending — success with an unreported call,
        crash, and stop alike — and the one thing common to all of them
        is that no result ever arrived.

        A barred call settled here is :data:`UNVERIFIED`, never
        ungrounded. It is tempting to call it ungrounded — from the
        answer's point of view a retrieval that never reported looks
        exactly as absent as one that was refused — and it is the same
        mistake in a new place. This app does not know that the call was
        refused. It knows the process stopped while the call was in
        flight, which is a statement about *this app's* view of a
        subprocess and not about what did or did not reach the model
        inside it. Saying "nothing came back" there certifies a clean
        refusal for an abortion, and a review round was right to
        separate them. The notice can still fire this late because the
        bridge reads the lists after the turn either way.

        **Public, because the two ways a turn can end do not share a
        code path.** :meth:`stream_complete` is the master engine's
        ending; a *consultation* never reaches it — ``AgyConsultant._run``
        iterates ``stream_frames`` itself and the bridge closes the tab —
        so a flush that lived only in the footer would have fixed the
        spinner everywhere except the surface the review round was
        talking about. Idempotent by :attr:`_settled`, so both callers
        may run and the second finds nothing to do.
        """
        events: list[Event] = []
        for call_id, card in self._tools.items():
            if call_id in self._settled:
                continue
            self._settled.add(call_id)
            name = str(card.get("name") or "")
            message = (
                "The turn ended before this call reported a result. "
                "Whether anything came back from it is not known."
            )
            events.append(
                Event(
                    "toolResult",
                    {
                        "tool_use_id": call_id,
                        "name": name,
                        "status": "error",
                        "preview": message,
                        "truncated": False,
                        "full_bytes": len(message.encode("utf-8")),
                        "duration_ms": 0,
                        "agent_id": self._agent_id,
                        "files_modified": [],
                    },
                )
            )
            if self._is_barred(name):
                events.extend(self._note_unverified(name))
        return events

    def _grounding_notice(self) -> list[Event]:
        """The app saying what the answer is being produced without.

        AG-R-22: the gate holds and the model narrates around it. Denied
        `read_url_content`, `agy` has been measured both declining honestly
        *and* answering with invented page contents, from the same policy
        and the same shape of prompt — so whether the reader is told cannot
        be left to the model's prose. This app ran the gate, so it is the
        one component that knows, and this is it saying so.

        **At the first refusal, not at the end of the turn.** The earlier
        version of this raised one notice when the ``result`` frame was
        absorbed, which read well and failed twice: a consultation that
        was stopped, timed out or crashed never emits that frame, so the
        prose it had already streamed stayed on screen with nothing
        against it; and a notice underneath a thousand words the reader
        has already believed arrives after the damage. Raised here it
        precedes everything the model writes after being refused.

        **Still once per consultation**, because the sentence is about the
        answer rather than about the call: a model refused three tools has
        not learned three things. The later refusals are not silent — each
        one's own card carries the reason inline, in place, in order,
        which is where per-call attribution belongs — and the full list
        reaches the model that asked, through :attr:`ungrounded_tools` and
        the bridge.

        **And so it names no tools.** It used to name the one that
        provoked it, which is a list frozen at one entry while further
        refused cards render beneath it — a reviewer read it as stale on
        sight, and was right to: "reached for search_web" sitting above a
        refused `read_url_content` reads as an inventory that has stopped
        being kept. A notice raised once may either name nothing or
        update, and naming nothing is the honest half of that choice here,
        because the tool names are already on the cards and the sentence's
        actual subject is the answer. What is left is a property of the
        policy rather than a count of what has happened so far, so there
        is no later frame that can falsify it.

        It states only what this app did and can prove — which tool was
        reached for, that this app refused it, and that nothing was
        therefore retrieved. The conditional in the last clause is
        deliberate: an answer that honestly says "I could not look" is not
        a false one, and a notice that called it a confabulation would be
        the same overclaiming it exists to correct.

        **Scoped by ``agent_id``, so it lands in the consultation's own
        tab.** The first version left it unscoped, to put it in the main
        transcript beside the tool call that asked for the second opinion,
        on the reasoning that the tab is opt-in. That said it twice in the
        one place and not at all in the other: the answer handed back to
        the asking model already opens with
        :func:`~aic_dc.antigravity.bridge._grounding`'s header, and that
        header is the first paragraph of the ``second_opinion`` result
        card — which is a louder position in the main transcript than a
        floating row beneath it, and is there whether or not this fires.
        What the tab had was two refused cards and nothing saying what
        they meant for the answer. This is that sentence, in the place
        that lacked it.

        A reader who opens neither is still told: the toast fires from the
        same event, and the result card carries the header.
        """
        return [
            Event(
                "systemEvent",
                {
                    "subtype": "consultation_ungrounded",
                    "data": {
                        "agent_id": self._agent_id,
                        # The call that provoked it, singular and fixed at
                        # the moment it fired. A `tools` list here would be
                        # the same frozen-inventory trap as naming them in
                        # the sentence, one layer down where nothing
                        # renders it and nobody would notice it going
                        # stale. The whole list lives on
                        # `ungrounded_tools`, which is read after the turn.
                        "tool": next(iter(self._ungrounded), ""),
                        "message": (
                            "This consultation reached for a tool and got "
                            "nothing back, and anything else it reaches for "
                            "will come back the same way: a second opinion "
                            "runs with no tools and no repository access. "
                            "Each such call is shown on its own card, with "
                            "whatever reason came back with it. Nothing was "
                            "read or retrieved, so any file contents, page "
                            "contents, search results or live values in what "
                            "follows are the model's own rather than a "
                            "source's."
                        ),
                    },
                },
            )
        ]

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

    def note_consultation(
        self, allowed: Iterable[str], refusals: Any = None
    ) -> None:
        """Tell this pump it is a consultation, and what may run.

        Called once, before the first frame, with the allowlist and the
        refusal-token issuer from the consultation's own stamped
        :class:`~aic_dc.agy.gate_server.StaticPolicy`. See
        :attr:`_allowed`, :meth:`_is_barred` and :meth:`_is_our_refusal`.

        It used to take the policy's refusal *mark* — a fixed string — and
        match it in every error to decide whether a failure was this app's
        doing. Two review rounds and one live probe took that apart: the
        claim it made belongs in the error text the card already shows,
        and requiring it made both the warning and the card's lifecycle
        fail open on anything `agy` rejected before our hook ran. The
        allowlist is the durable half — it is this app's own object, it
        travels nowhere, and nothing can truncate it.

        ``refusals`` is the mark's job back in a narrower form, and only
        because the fixed string could not do it safely: it is written in
        this repository, so a gate that failed open on a consultation
        that then read this source would have sent the mark back as
        *retrieved content* and had an escape recorded as a refusal. It
        is the consultation's
        :class:`~aic_dc.agy.gate_server.Refusals` — the object that
        minted a single-use token for each denial — and it is taken by
        duck type rather than imported, because the pump must not depend
        on the gate to translate a frame.

        Default ``None``, and an unstamped consultation can therefore
        never conclude that anything was refused: every barred call
        becomes breached or unverified. That is the safe direction — it
        costs certainty, never honesty.
        """
        self._allowed = frozenset(allowed)
        self._refusals = refusals

    @property
    def ungrounded_tools(self) -> tuple[str, ...]:
        """Tools this consultation reached for and got nothing back from.

        Read by :class:`~aic_dc.antigravity.bridge.ConsultantBridge` after
        the turn, so the model that asked for the second opinion is told
        which retrievals its answer is missing. Empty on the engine, and
        empty for the consultation that never reached for anything —
        which is most of them, and why the bridge says something
        different, and shorter, in that case.

        Named for the finding rather than for the cause: these are calls
        that came back carrying this app's own refusal, so it is this app
        and not the vendor that knows they returned no data. A call that
        ended any other way is on :attr:`unverified_tools` instead. See
        :meth:`_outcome`.
        """
        return tuple(self._ungrounded)

    @property
    def breached_tools(self) -> tuple[str, ...]:
        """Tools that ran in a consultation that permits none.

        Empty in every measured run, and the bridge's only reason to
        retract the sentence it otherwise states unconditionally. See
        :meth:`_breach_notice`.
        """
        return tuple(self._breached)

    @property
    def unverified_tools(self) -> tuple[str, ...]:
        """Tools this consultation reached for to no established end.

        Read by :class:`~aic_dc.antigravity.bridge.ConsultantBridge` after
        the turn, alongside :attr:`ungrounded_tools`, and kept apart from
        it because the sentence the bridge writes about the two is not
        the same sentence: one says nothing came back, the other says
        this app cannot say. See :meth:`_outcome`.
        """
        return tuple(self._unverified)

    def note_unadvertised(self, names: Iterable[str]) -> None:
        """Record permitted tools this ``agy`` binary does not have.

        Called once, from :meth:`AgyConsultant._run`, straight after the
        handshake and before the prompt goes in — the only moment where the
        policy's allowlist and the vendor's ``init`` inventory are both in
        scope. Ordinarily called with nothing, and it stays a no-op then.

        **It records; it does not act.** Nothing here permits a tool,
        renames one, or refuses a launch. That is the answer to
        [AG-R-22](../../../specs5/plan-ag/risks.md#ag-r-22)'s open question:
        a renamed ``finish`` would strand every consultation at the bridge
        timeout with nothing to show for the tokens, but *guessing* which
        of a new binary's 57 names is the control tool by the shape of the
        name is precisely the reasoning the allowlist exists to refuse. So
        the fact is carried to the message that has to explain the outcome
        and no further.

        Nor is a launch refused on it, and that is measured rather than
        cautious: AG-R-22's P24 arm D answered without making a single tool
        call, so a consultation whose ``finish`` has gone missing is
        *likely* to fail, not certain to. Refusing would trade a
        diagnosable failure for a guaranteed one.
        """
        for name in names:
            self._unadvertised.setdefault(name, None)

    @property
    def unadvertised_tools(self) -> tuple[str, ...]:
        """Permitted tools absent from this binary's ``init`` inventory.

        Empty whenever the inventory could not be read at all, which is
        deliberate and is the reason
        :attr:`~aic_dc.agy.session.AgySession.advertised_tools` defines an
        empty advertisement as *no claim*: an older binary, or any frame
        that carried no list, must not make this app announce that every
        tool it permits has gone missing.
        """
        return tuple(self._unadvertised)

    def note_cancelled(self) -> None:
        """The user stopped this turn. AG-19.

        Told rather than detected, because **the stream does not carry
        it**. A loop ended by this app's ``PostInvocation`` handler reports
        ``status: "SUCCESS"`` with an empty ``response`` — success meaning
        only that the loop exited without an unhandled error — and a turn
        starved by the gate reports the same. Rendered as they arrive, both
        draw as a completed answer that happens to say nothing, which is
        the one reading of a stop that is worse than no feedback at all.

        :class:`~aic_dc.agy.session.AgySession` is what knows, since it
        holds the latch ⏹ set and can ask the gate whether the loop was
        ended mechanically or merely refused.

        **Called while the turn is still streaming, not only after it.**
        It was originally called once, between the last frame and the
        footer, which was enough to badge the turn and nothing more: the
        pump learned about the stop only when there was nothing left to
        suppress, so a stopped prose turn streamed its entire answer and
        *then* said it had been stopped. The session now tells the pump as
        soon as the latch is set, which is what turns this flag from a
        footer field into the switch that stops the view — see
        :meth:`_translate`. Idempotent, because both call sites remain:
        the late one still covers a stop with no frame after it.
        """
        self._cancelled = True

    def stream_complete(self) -> list[Event]:
        """The turn's closing events, in the shape the browser already reads.

        ``agy``'s status word becomes a ``terminal_reason`` — the browser's
        name for it, under the browser's vocabulary. The translation and
        its three rules are :func:`~aic_dc.antigravity.steps.terminal_reason_for`.

        *This docstring used to claim that "the browser reads an
        unrecognised reason as something worth a red badge", and it was
        describing a consumer this key did not reach.* The value was
        computed for a badge table that reads ``terminal_reason`` and put
        on the wire as ``stop_reason``, so the sentence was true about the
        browser and false about this payload for as long as both existed.
        AG-R-17 recorded it as an open question rather than a bug, which is
        the part worth remembering: the reasoning was sound and aimed at
        the wrong key.

        ``cancelled`` is the browser's own word, from the vocabulary the
        Claude transport already fills in (``messages.py``): the HUD reads
        it for "interrupted" and ``computeTurnOutcome`` reads it to keep
        the LED green, because a turn the user stopped is not a fault. So a
        stop renders here without the chat panel learning that a third
        transport exists — AG-R-4.
        """
        terminal_reason = terminal_reason_for(self._status)
        # Before the footer, because a card that never reported is a
        # spinner the footer would close the turn underneath. See
        # `settle_pending`.
        return self.settle_pending() + [
            Event("turnUsage", {"turn_model_usage": self.turn_usage()}),
            Event(
                "streamComplete",
                {
                    "request_id": self.request_id,
                    # `terminal_reason`, not `stop_reason`. See
                    # `antigravity.steps.terminal_reason_for` for the
                    # mapping and why it is not a rename; the short of it
                    # is that nothing read the old key, and this payload
                    # has no `is_error` either, so a turn `agy` reported as
                    # `ERROR` reached the panel with no verdict at all.
                    "terminal_reason": terminal_reason,
                    # Told *or* derived, the same one-line rule the Claude
                    # pump uses (`messages.py`), against the same shared
                    # set. The latch covers ⏹; the reason covers a turn
                    # `agy` cancelled on its own — a headless permission
                    # denial reports `CANCELED` with exit 0, so without
                    # this half such a turn renders as a completed answer
                    # that happens to say nothing.
                    "cancelled": self._cancelled
                    or terminal_reason in CANCELLED_TERMINAL_REASONS,
                    # `tool_calls`, not `num_tool_calls`. Both Antigravity
                    # translators spelled it the second way and **nothing
                    # anywhere read it**: the chat panel's `renderTurnFooter`
                    # and the HUD's turn row both read `tool_calls`, which is
                    # what the Claude transport emits. So the footer's whole
                    # "N tool calls, M asked" line was missing on this
                    # transport and on the SDK one, silently, since each was
                    # written. AG-R-4 cuts this way too: the browser must not
                    # learn engine names, which means an engine may not
                    # invent its own spelling of a shared field.
                    "tool_calls": self.stats.tool_calls,
                    # Counted since the `stats` object was shared and never
                    # put on the wire — the dialog count the footer renders
                    # beside the tool count. `_note_permission_prompt` was
                    # fixed to *collect* this; this is where it arrives.
                    "permission_prompts": self.stats.permission_prompts,
                    # The turn's files, accumulated per call rather than
                    # left empty: the footer lists what the turn touched,
                    # and an empty list said "nothing" for every turn that
                    # wrote something.
                    "files_modified": list(self.stats.files_modified),
                    "usage": self.turn_usage(),
                    # `response`, not `response_text`. The settled assistant
                    # message takes its content from `result.response`
                    # (`chat-panel/streaming.js`), which is the Claude pump's
                    # spelling; the old key was read by nothing, so every turn
                    # on both Antigravity transports settled with empty
                    # content. The rendered blocks carried the prose, which is
                    # why it was invisible.
                    "response": self.response_text(),
                },
            ),
        ]


def scratch_dir(root: Path | str) -> Path:
    """Where ``agy`` puts a write it declined to make where it was asked.

    See :data:`aic_dc.antigravity.rules` for why this is not simply a
    ``trustedWorkspaces`` question.

    A function of the config root rather than a constant pinned to this
    server's ``HOME``, for [AG-R-18](../../../specs5/plan-ag/risks.md#ag-r-18)'s
    reason and with this module's own twist on it: the diverted-write
    check is a *diagnostic that fires only when something has gone
    wrong*, so a wrong directory here does not misreport, it reports
    nothing at all. A detector that has quietly stopped detecting is the
    exact failure this directory keeps recording, and pinning it to a root
    the agent no longer uses is how it would have happened.
    """
    return roots.vendor_dir(root) / "scratch"

# Where ``agy`` puts an image, because the caller cannot say.
#
# **Measured on 2026-09-08**, and it falsified the question AG-16 left
# open — *which name does ``generate_image``'s output path carry* — by
# showing there is no such argument on either transport. The binary's own
# declaration for that tool, read back out of the conversation store, has
# exactly six properties (``Prompt``, ``ImageName``, ``AspectRatio``,
# ``ImagePaths``, ``toolAction``, ``toolSummary``) under
# ``additionalProperties: false``, so a path cannot even be smuggled in:
# the call would be rejected inside ``agy`` while declaring permissions,
# which is before any hook runs. The harness chooses the location,
# ``brain/<conversation_id>/<ImageName>_<epoch_ms>.jpg``, and the
# requested extension is not honoured — a request for ``.png`` produced
# JPEG.
#
# ``output_path`` is a **result** field, which
# [`sdk-surface.md`](../../../specs5/plan-ag/sdk-surface.md) recorded in
# the right column all along. It reads as an argument on the SDK
# transport only because that stream merges a tool's results back into
# its ``args`` at ``DONE`` (phase 3, finding 1); ``agy`` does not merge,
# so on this transport the path is nowhere in the machine-readable stream
# at all — only in the model's prose, which AG-R-3 forbids believing.
#
# So the image is **collected** rather than requested, from data the
# frames do carry: the conversation's own id and the name the model
# chose. Lives here rather than in ``consultant.py``, where it was
# written, because the engine needs the same collection and two copies of
# a path into another product's application directory is the copy that
# drifts.
#
# **The directory itself is** :func:`aic_dc.agy.roots.brain_dir`, and was
# a module constant here until 2026-09-11 (AG-R-18). Where it lives is a
# property of the config root the turn is running against, and this
# module no longer has an opinion on which one that is.




def locate_generated_image(
    brain_dir: Path,
    conversation_id: str,
    image_name: str,
    *,
    allow_newest: bool = False,
    min_mtime: float = 0.0,
) -> Path | None:
    """The file ``agy`` wrote for this conversation, or ``None``.

    Matched by the name the model chose, which is the only handle the
    frames give. Sub-directories (``scratch``, ``.system_generated``,
    ``.user_uploaded``) are skipped, since an image is a file.

    ``allow_newest`` adds a final "anything in the directory" pass and is
    **the consultant's option, not the engine's**. It is safe there for a
    reason that does not survive the move: a consultation is one process
    holding one conversation for the length of one call, so nothing else
    ever writes into that directory. An engine conversation is long-lived
    and resumable, so its directory accumulates every image of every turn
    and "the newest file in it" would confidently return one from an hour
    ago.

    ``min_mtime`` is the guard that replaces it for the engine: the
    translator is built per turn, so a file older than the turn belongs to
    an earlier one. Without it, a resumed conversation whose model reuses
    an ``ImageName`` would collect the previous turn's picture and report
    success.
    """
    if not conversation_id:
        return None
    directory = brain_dir / conversation_id
    if not directory.is_dir():
        return None
    stem = image_name.strip()
    patterns = [f"{stem}_*", f"{stem}.*"] if stem else []
    if allow_newest:
        patterns.append("*")
    for pattern in patterns:
        try:
            files = [
                path
                for path in directory.glob(pattern)
                if path.is_file() and path.stat().st_mtime >= min_mtime
            ]
        except OSError:
            return None
        if files:
            return max(files, key=lambda path: path.stat().st_mtime)
    return None


def _diverted_copy(
    params: dict[str, Any], config_root: Path | None
) -> tuple[str, str] | None:
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
    if not isinstance(raw, str) or not raw or config_root is None:
        return None
    try:
        target = Path(raw)
        if target.exists():
            return None
        candidate = scratch_dir(config_root) / target.name
        if candidate.is_file():
            return (str(target), str(candidate))
    except OSError:  # noqa: BLE001 - a diagnostic, never a control path
        return None
    return None


def _error_message(info: dict[str, Any]) -> str:
    """Why a failed step failed, from `agy`'s two spellings of it.

    Measured as a mapping — ``{"type": "TOOL_ERROR", "message": ...}`` —
    but the SDK transport's translator treats its own error field as a
    bare string, so neither shape is assumed here. Anything else stringifies
    rather than raising: this runs while rendering a card, and a translator
    that raised would fail the turn over the *decoration* of a call that
    had already failed.
    """
    error = info.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")
    return str(error) if error else ""


def _duration_ms(seconds: Any) -> int:
    if isinstance(seconds, (int, float)) and seconds >= 0:
        return int(seconds * 1000)
    return 0
