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
        ]

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
        return [
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


def _duration_ms(seconds: Any) -> int:
    if isinstance(seconds, (int, float)) and seconds >= 0:
        return int(seconds * 1000)
    return 0
