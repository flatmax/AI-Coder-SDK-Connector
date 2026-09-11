"""What a subagent did, read out of ``agy``'s own conversation store.

The other half of ``subagent_rows``. The pump can say *that* a delegation
happened — a ``subagent`` step names each subagent's ``conversation_id``,
its role and its type (:func:`aic_dc.agy.steps.subagent_entries`) — and it
cannot say what the subagent then did, because **a subagent's steps never
appear on the parent's stream**. They go to a conversation of its own. So
a live subagent tab on this transport had a label over an empty feed, and
the read the browser falls back to
(``loadSubagentFeedIfEmpty`` → ``get_subagent_transcript``) reached a
router refusal and rendered it as ⚠️ prose inside the tab. This module is
what makes that read answerable.

Why a reader rather than a change to the mirror
===============================================
``antigravity/mirror.py`` deliberately does not mirror ``subagentEvent``,
so our own store holds no record of a session's subagents at all. Adding
one would still not help: the mirror only ever sees what crosses the
stream, and the subagent's work does not cross it. The record exists only
in ``agy``'s store, so that is where it is read from — one read of the
parent's transcript serves the live listing and the browsed one alike,
and ``MIRRORED_EVENTS`` needs no change.

Reading another product's application directory is a real cost and it is
paid knowingly: this is the same directory
:func:`~aic_dc.agy.roots.brain_dir` already resolves to collect a
generated image, and the alternative is a tab that says a subagent did
nothing.

**Every function here takes ``brain_dir`` and none of them defaults it.**
They defaulted to a module constant pinned to this *server's* ``HOME``
until 2026-09-11, which was correct only while there was one config root
in the world. AG-21 gives ``agy`` a private one — a stable root for the
master and an ephemeral one per consultation — and under two roots a
default is not merely wrong, it is *silently* wrong: the scan finds an
empty directory and reports, accurately and uselessly, that the subagent
did nothing. That is [AG-R-18](../../../specs5/plan-ag/risks.md#ag-r-18),
and a required argument is the fix, because it is the only version a
caller cannot forget.

What was measured, on 2026-09-10, at ``agy`` 1.1.28
===================================================
Against the capture the 2026-09-09 subagent probe left on disk. Four
findings, each of which would have produced a plausible and wrong reader:

- **A subagent's conversation is a conversation.** It lives at
  ``<brain_dir>/<conversation_id>/.system_generated/logs/``, addressed by
  the id the announcement already carries, and its records are the same
  vocabulary a parent's are — so there is one reader, not two.
- **``transcript_full.jsonl`` is the file to read.** The sibling
  ``transcript.jsonl`` records each tool argument *double-encoded* as a
  JSON string (``"AbsolutePath": "\\"/tmp/x/notes.txt\\""``), which
  renders as a quoted quotation in every tool card. The full file records
  them as values. Both are read, in that order, because a release that
  stopped writing the full one should degrade to a shabby card rather
  than to no tab.
- **A ``GENERIC`` step is the preceding call's result.** There is no id
  linking them and no ``tool_result`` record type. Nothing else in the
  vocabulary carries tool output. *What* links them is the second
  measurement below, which corrected this one.
- **The child's id reaches the parent only in prose.** The announcement
  the parent's transcript holds is a ``GENERIC`` step whose content is
  ``Created the following subagents:`` followed by a JSON object per
  subagent — spelled ``conversationId``/``logAbsoluteUri``, where the
  live stream spells the same facts ``conversation_id``/``log_uri``. This
  is the one place AG-R-3's "do not believe the model's prose" is not the
  rule that applies, because this prose is the *harness's*, printed as a
  tool result: the model neither wrote it nor could change it.

What the second measurement corrected, same day, 121 transcripts
================================================================
The four findings above came from one capture, and one capture is how a
reader ends up right about the case it was written from. Re-measured
against every ``transcript_full.jsonl`` in the brain tree — 121 files,
1,927 records, ``agy`` and the Antigravity IDE, 2026-08-03 to 09-09 —
three of the four survived and **the linkage did not**.

- **A result names its call by step index, not by order, and a refused
  call leaves no record at all.** The gate probe's own subagent is the
  proof: it made seven calls, three were denied by our dialog, and the
  transcript simply has no record at step indices 4, 10 and 14. A reader
  pairing the next result with the oldest open call therefore renders
  ``list_dir``'s output *inside the denied ``replace_file_content`` card*,
  every card after the first denial one call out of step, and
  ``files_written_by`` crediting a file to a write that never ran. The
  rule that holds across all 121 files is arithmetic: a result sits at
  ``call_step + 1 + position``, so a step with *k* calls is followed by up
  to *k* consecutive results and a gap is a call that produced nothing.
  Measured: 698 calls answered at ``+1``, 54 with nothing there at all.
- **There is no ``ERROR`` status.** The only two values on disk are
  ``DONE`` and ``RUNNING``; the reader tested for ``ERROR`` and so had a
  dead branch where its failure handling was. Failure is a *record type* —
  ``ERROR_MESSAGE``, which is a call's result under a different name — and
  in the current format a tool that ran and failed says so **only in
  prose** ("The command exited with code 1"), which this does not sniff
  (``specs5/5-webapp/chat.md`` § *Card Anatomy*: the flag, never the
  text). So a failed *command* renders as a completed card with its error
  as the body, and only a refused or malformed call is marked.
- **``RUNNING`` is a backgrounded tool**, not a half-written file: 43
  records, none of them last in their file, each saying *"Tool is running
  as a background process"*. Rendered as a card that never resolved rather
  than as a success with no output.
- **Every result's content opens with two lines of harness plumbing** —
  ``Created At:`` and, unless the tool was backgrounded, ``Completed At:``
  (674 of 720). Stripped, because the card header already shows both
  facts, and used for the duration, which makes it the tool's own rather
  than the gap between two records.
- **The vocabulary is 16 types, not 4**, and ten of the twelve extras are
  results named after their tool — but every one of those belongs to a
  conversation from 2026-08-16 or earlier. All 115 conversations from
  2026-08-29 on use ``GENERIC`` for every tool, which is what promotes the
  first finding above from "true of one capture" to "true of every
  transcript the versions this project drives have written".
- **Append order is the order, and ``step_index`` is not unique.** One
  file of the 121 interleaves two concurrent turns and carries five
  duplicated indices, so sorting by index reorders it *away* from what the
  harness wrote. The sort is gone. That file is the 2026-09-03 concurrency
  probe, and a session of ours owns its conversation, so it is a shape we
  cannot produce — but reading in file order costs nothing and is right in
  both cases.

Containment
===========
:func:`descendants` exists so that ``get_subagent_transcript`` is not
"read any ``agy`` conversation on this machine by id" — which would
include the user's own Antigravity IDE conversations, that AIC⚡DC never
owned. A requested id has to be reachable by announcement from the
session doing the asking. Same family as AG-R-12, and the caller must
still check that the *session* is one of ours; this module knows nothing
about our mirror.

Governing spec: ``specs5/plan-ag/`` — AG-13, AG-14, AG-3, AG-9, AG-R-12.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from aic_dc.claude_code.history import PREVIEW_CHARS
from aic_dc.claude_code.messages import (
    files_written_by,
    truncate_tool_result,
)

logger = logging.getLogger(__name__)

#: Where a conversation's logs sit under its own directory.
LOG_SUBPATH = Path(".system_generated") / "logs"

#: The transcript files, best first. See the module docstring: the two
#: differ in how they encode tool arguments, and only the first is usable
#: without unquoting every value.
TRANSCRIPT_FILES = ("transcript_full.jsonl", "transcript.jsonl")

#: Record ``type`` values that carry one tool call's result.
#:
#: ``GENERIC`` is the only one this reader was written against and the only
#: one any ``agy`` this project drives writes — every result in all 115
#: conversations from 2026-08-29 on, whatever the tool. The tool-named
#: members are the older format's, still on the same disk: the 2026-08-03
#: to 08-16 conversations name a result after the tool that produced it
#: (``view_file`` → ``VIEW_FILE``, ``write_to_file`` → ``CODE_ACTION``,
#: ``list_dir`` → ``LIST_DIRECTORY``). They are listed rather than left to
#: the unknown-record blob because a downgraded ``agy`` would write them
#: again, and because ``sdk-surface.md``'s 2026-09-03 reading mistook that
#: older set for the current one — a list that is wrong in the same
#: direction twice is worth pinning in code.
#:
#: ``ERROR_MESSAGE`` is here and not in :data:`SYSTEM_TYPES` despite its
#: ``source`` being ``SYSTEM``: it sits at a call's result index and is a
#: failed call's only structured account of itself.
RESULT_TYPES = frozenset(
    {
        "GENERIC",
        "CODE_ACTION",
        "ERROR_MESSAGE",
        "GENERATE_IMAGE",
        "GREP_SEARCH",
        "INVOKE_SUBAGENT",
        "LIST_DIRECTORY",
        "READ_URL_CONTENT",
        "RUN_COMMAND",
        "SEARCH_WEB",
        "VIEW_FILE",
    }
)

#: Records the harness writes *about* the conversation rather than in it.
#:
#: All three arrive with ``source: "SYSTEM"`` and none is anybody's turn:
#: ``SYSTEM_MESSAGE`` is another conversation speaking, ``CHECKPOINT`` says
#: the transcript above it was truncated, ``CONVERSATION_HISTORY`` is a
#: context injection listing the user's other conversations. They are shown
#: as system events rather than folded into the turn, for the reason
#: :meth:`_Turn.absorb_result` gives about the harness's words: a
#: truncation notice rendered as a block would read as something the
#: subagent said.
SYSTEM_TYPES = frozenset(
    {"SYSTEM_MESSAGE", "CHECKPOINT", "CONVERSATION_HISTORY"}
)

#: Every ``type`` this reader dispatches on.
#:
#: Anything outside it is *recognised* as unknown rather than silently
#: matching nothing, which is the rule the pump follows for ``step_type``
#: and for the same reason: this is a CLI that releases weekly, and a
#: record nobody renders is a hole in the middle of a transcript.
KNOWN_TYPES = (
    frozenset({"USER_INPUT", "PLANNER_RESPONSE"}) | RESULT_TYPES | SYSTEM_TYPES
)

#: The announcement's own spelling of a subagent's conversation id.
#:
#: Matched rather than JSON-parsed because it arrives *inside* a prose
#: result — ``Created the following subagents:`` and then one object per
#: subagent, with no array around them, so there is no document to parse.
#: A match per subagent, in announcement order.
_CONVERSATION_ID = re.compile(r'"conversationId"\s*:\s*"([^"]+)"')

#: How ``agy`` frames the prompt it hands a conversation.
#:
#: First opening tag, unlike ``_SYSTEM_MESSAGE`` below. Nothing prefixes
#: this frame, and a prompt that quotes the tag — asking about this very
#: transcript, say — must come back whole rather than truncated at its own
#: quotation.
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)

#: How ``agy`` frames a message one conversation sends another.
#:
#: The leading greedy ``.*`` is load-bearing, and a failing test put it
#: there rather than a reading of the file. This record opens with a
#: preamble that *mentions* the tag — "The following is a <SYSTEM_MESSAGE>
#: not actually sent by the user" — so the first opening tag in the
#: content is prose about the frame and not the frame. Matching it
#: rendered the preamble, the real opening tag and the body as one
#: message: a stray ``<SYSTEM_MESSAGE>`` in the middle of the text, and
#: the harness's disclaimer presented as something a conversation said.
#: Greedy takes the last opening tag, which is the one that frames.
_SYSTEM_MESSAGE = re.compile(
    r".*<SYSTEM_MESSAGE>\s*(.*?)\s*</SYSTEM_MESSAGE>", re.DOTALL
)

#: The two lines of plumbing every result record's content opens with.
#:
#: ``Created At: <iso>`` on all 720 measured, ``Completed At: <iso>`` on the
#: 674 that were not backgrounded. Both are stripped from the card body —
#: the header above it already shows an invocation time and a duration, so
#: leaving them in spends the first two lines of every tool card restating
#: it — and both are read for the duration, which is the tool's own rather
#: than the gap between two records. Anchored, so a result whose *payload*
#: begins with those words keeps them.
_RESULT_HEADER = re.compile(
    r"\ACreated At: *(?P<created>[^\n]*)\n(?:Completed At: *(?P<completed>[^\n]*)\n)?"
)

#: Ceiling on :func:`descendants`' walk.
#:
#: A backstop against a cycle the visited set has somehow not caught, not
#: a real bound: a turn that announced 200 conversations has other
#: problems. Reaching it is logged rather than swallowed, because a
#: containment check that quietly stopped looking would refuse a
#: transcript the user is entitled to and read as "the record is gone".
MAX_CONVERSATIONS = 200


# ---------------------------------------------------------------------------
# Locating and reading
# ---------------------------------------------------------------------------


def transcript_path(
    conversation_id: str, *, brain_dir: Path
) -> Path | None:
    """The best transcript file for one conversation, or ``None``.

    ``None`` covers both "``agy`` has never heard of this id" and "its
    directory holds no transcript", and the caller reports either as an
    unreadable record rather than as an empty conversation — the same rule
    ``history_load`` follows.

    ``conversation_id`` is checked for path separators rather than
    trusted. It arrives from the browser, and ``brain_dir / "../.."`` is
    a directory traversal into the user's home. The ownership check in
    :func:`descendants` is the real containment; this is the one that
    holds even if a caller forgets it.
    """
    if not conversation_id or not _safe_id(conversation_id):
        return None
    directory = brain_dir / conversation_id / LOG_SUBPATH
    for name in TRANSCRIPT_FILES:
        candidate = directory / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # noqa: PERF203 - a probe, not a control path
            continue
    return None


def read_records(path: Path) -> list[dict[str, Any]]:
    """One transcript file's records, in the order the file holds them.

    A line that does not parse is skipped and counted, never fatal: this
    file belongs to a live process that appends to it, so the last line
    can be half-written at the moment it is read. Losing the tail of a
    transcript is a smaller failure than losing the whole tab.

    **Append order, not ``step_index`` order**, and this used to sort. On
    120 of the 121 transcripts measured the two are the same; on the odd
    one out — two concurrent turns interleaved on one conversation, five
    indices used twice — sorting moved records *away* from what the
    harness wrote. ``step_index`` is still what names a result's call
    (:meth:`_Turn.absorb_result`); it is just not an ordering.
    """
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read the agy transcript %s: %s", path, exc)
        return []
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            skipped += 1
    if skipped:
        logger.warning("Skipped %d unparseable line(s) in %s", skipped, path)
    return records


# ---------------------------------------------------------------------------
# What a conversation delegated
# ---------------------------------------------------------------------------


def announcements(
    conversation_id: str, *, brain_dir: Path
) -> list[dict[str, Any]]:
    """The subagents one conversation announced, in order.

    ``[{agent_id, role, type_name, prompt, log_uri}]`` — the same facts
    the live stream's announcement carries, under the names
    :func:`aic_dc.agy.steps.subagent_entries` reports them by, so the row
    a listing builds and the row the pump emits describe a subagent the
    same way.

    Two records make one announcement and neither is sufficient. The
    ``PLANNER_RESPONSE`` carries the *call*, whose ``Subagents`` argument
    holds the role, the type and the prompt; the ``GENERIC`` result that
    follows it carries the *ids*. They are paired by position, because
    that is the only correspondence either record offers.

    A mismatch in those counts is logged and the surplus dropped, rather
    than paired anyway: a row whose id belongs to a different subagent
    would open the wrong transcript under the right label, which is worse
    than a missing row in exactly the way this project keeps choosing
    against.
    """
    path = transcript_path(conversation_id, brain_dir=brain_dir)
    if path is None:
        return []
    return _announcements_from(read_records(path), conversation_id)


def _announcements_from(
    records: list[dict[str, Any]], conversation_id: str
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for record in records:
        spawned = _spawn_arguments(record)
        if spawned:
            # A second spawn call before the first one's result would mean
            # the earlier announcement's ids were never printed. Reported,
            # because the alternative is pairing them with the wrong
            # result.
            if pending:
                logger.warning(
                    "agy conversation %s announced %d subagent(s) whose ids "
                    "were never reported; they have no readable transcript",
                    conversation_id,
                    len(pending),
                )
            pending = spawned
            continue
        # Any result type, not `GENERIC` alone: the older format names this
        # one `INVOKE_SUBAGENT`, and a listing that went blank on a
        # downgraded `agy` would look exactly like a turn that delegated
        # nothing. The ids are what identifies it either way.
        if not pending or str(record.get("type") or "") not in RESULT_TYPES:
            continue
        ids = _CONVERSATION_ID.findall(str(record.get("content") or ""))
        if len(ids) != len(pending):
            logger.warning(
                "agy conversation %s announced %d subagent(s) and reported "
                "%d id(s); pairing only the ones that line up",
                conversation_id,
                len(pending),
                len(ids),
            )
        # `strict=False` is the behaviour this wants and it is spelled out
        # rather than defaulted: the counts disagreeing is a case with a
        # decision behind it — logged just above, surplus dropped — so
        # raising here would turn a partial listing into no listing.
        for entry, agent_id in zip(pending, ids, strict=False):
            found.append({**entry, "agent_id": agent_id})
        pending = []
    if pending:
        logger.warning(
            "agy conversation %s announced %d subagent(s) with no result "
            "step, so no id was reported for them",
            conversation_id,
            len(pending),
        )
    return found


def _spawn_arguments(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``Subagents`` entries of every spawn call in one record.

    Matched on the *argument* rather than on the tool's name, and that is
    a decision the two transports force: this transport spells the tool
    ``invoke_subagent`` and the SDK's tool inventory spells it
    ``start_subagent`` (``sdk-surface.md``), so a reader keyed to a name
    is one rename from listing nothing at all — silently, because a name
    that matches nothing is indistinguishable from a turn that delegated
    nothing. The ``Subagents`` list is the shape that carries the fact.
    """
    if str(record.get("type") or "") != "PLANNER_RESPONSE":
        return []
    calls = record.get("tool_calls")
    if not isinstance(calls, list):
        return []
    entries: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            continue
        announced = args.get("Subagents")
        if not isinstance(announced, list):
            continue
        for entry in announced:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    # `role` and `type_name` rather than `Role` and
                    # `TypeName`: the live announcement's names, so
                    # `subagent-tabs.js` joins on one vocabulary.
                    "role": str(entry.get("Role") or ""),
                    "type_name": str(entry.get("TypeName") or ""),
                    "prompt": str(entry.get("Prompt") or ""),
                    # Reported by the result, not the call. Filled in by
                    # the caller where the announcement names it; the key
                    # is here so every entry has the same shape.
                    "log_uri": "",
                }
            )
    return entries


def descendants(
    conversation_id: str,
    *,
    brain_dir: Path,
    limit: int = MAX_CONVERSATIONS,
) -> set[str]:
    """Every conversation reachable by announcement from this one.

    The containment set: an ``agent_id`` in here was delegated to, however
    indirectly, by the session asking to read it. Anything else is
    somebody else's conversation, and on a machine where the user also
    runs the Antigravity IDE that is a real distinction rather than a
    theoretical one.

    Transitive rather than one level deep, because a subagent can
    delegate too. Nothing in the UI opens a grandchild's transcript today
    — :func:`rows` lists one level, which is what "what did this session
    delegate?" means — so this is wider than the current caller needs.
    That is deliberate: a containment check that has to be widened later
    is one that will be widened wrongly, and a visited set makes the
    breadth free.

    The conversation itself is **not** in the result. It is not its own
    subagent, and including it would let ``get_subagent_transcript`` read
    the session's main transcript through a door meant for its children.
    """
    seen: set[str] = set()
    frontier = [conversation_id]
    while frontier:
        current = frontier.pop()
        for entry in announcements(current, brain_dir=brain_dir):
            agent_id = entry["agent_id"]
            if agent_id in seen or agent_id == conversation_id:
                continue
            if len(seen) >= limit:
                logger.warning(
                    "Stopped walking agy's subagent tree at %d conversations; "
                    "a transcript below that point will read as unreadable",
                    limit,
                )
                return seen
            seen.add(agent_id)
            frontier.append(agent_id)
    return seen


def rows(
    conversation_id: str, *, brain_dir: Path
) -> list[dict[str, Any]]:
    """One listing row per subagent this conversation delegated to.

    The shape ``list_subagent_transcripts`` answers in, matching
    ``claude_code.history.list_subagents`` field for field so the history
    browser's listing needs no branch: ``agent_id``, ``subpath``,
    ``message_count``, ``preview``, and ``description``/``agent_type``
    where the announcement supplied them.

    **No ``task_id``.** Claude's is the spawn call's ``tool_use_id``, and
    this transport's transcript carries no call id anywhere — the gate has
    to compose one out of ``conversationId`` and a step index because the
    hook JSON has none either. The field is left out rather than filled
    with the conversation id: ``task_id`` is what ``stop_task`` takes, and
    ``subagent_stop`` is unbuilt here, so a value there would be a handle
    onto a method that would refuse it.

    ``message_count`` is what :func:`load` will actually render, not the
    record count. Those differ by design — a call and its result are two
    records in one message — and the number beside a row should be the
    number of things the tab draws.

    One level deep. "What did this session delegate?" is a question about
    this session; a subagent's own subagents belong to its row's
    transcript, where the delegating call is a tool card like any other.
    """
    listing: list[dict[str, Any]] = []
    for entry in announcements(conversation_id, brain_dir=brain_dir):
        agent_id = entry["agent_id"]
        path = transcript_path(agent_id, brain_dir=brain_dir)
        records = read_records(path) if path is not None else []
        row: dict[str, Any] = {
            "agent_id": agent_id,
            # Relative to the brain directory, which is where the record
            # is: an absolute path would put another product's directory
            # layout in the browser, and the store subkey Claude reports
            # here has no counterpart on this transport.
            "subpath": (
                str(path.relative_to(brain_dir)) if path is not None else ""
            ),
            "message_count": len(_render(records, agent_id)),
            # The subagent's own first prompt when its transcript is
            # readable, and the announced prompt when it is not — the same
            # text either way, since the announcement is where the prompt
            # came from. A row whose transcript is gone still says what the
            # subagent was asked to do.
            "preview": (_first_prompt(records) or entry["prompt"])[:PREVIEW_CHARS],
        }
        if entry["role"]:
            # What the model called this subagent — "Notes Reader" — which
            # is the fact a Claude `Task`'s `description` carries.
            row["description"] = entry["role"]
        if entry["type_name"]:
            row["agent_type"] = entry["type_name"]
        listing.append(row)
    return listing


# ---------------------------------------------------------------------------
# Rendering one conversation
# ---------------------------------------------------------------------------


def load(conversation_id: str, *, brain_dir: Path) -> list[dict[str, Any]]:
    """One subagent's transcript, as messages the chat panel draws.

    Rendered rather than raw, for the reason ``history.load_subagent``
    gives: a subagent tab draws through the same panel code as the main
    transcript, and handing the browser ``agy``'s record union would put
    another product's internal vocabulary in the frontend.

    Empty when there is nothing to read, which the caller turns into a
    stated reason. A subagent that ran wrote records, so nothing here
    means the record is gone rather than that the subagent said nothing.
    """
    path = transcript_path(conversation_id, brain_dir=brain_dir)
    if path is None:
        return []
    return _render(read_records(path), conversation_id)


def _render(
    records: list[dict[str, Any]], conversation_id: str
) -> list[dict[str, Any]]:
    """The pure half of :func:`load`, so the taxonomy is testable.

    Same arrangement — and same reason — as ``history.render_messages``:
    every inference below is exercised by handing it constructed records
    rather than by writing files.
    """
    messages: list[dict[str, Any]] = []
    turn = _Turn()

    def flush() -> None:
        nonlocal turn
        if turn.any():
            messages.append(turn.freeze())
        turn = _Turn()

    for record in records:
        kind = str(record.get("type") or "")
        content = record.get("content")
        content = content if isinstance(content, str) else ""
        created_at = record.get("created_at")
        created_at = created_at if isinstance(created_at, str) else ""

        if kind == "USER_INPUT":
            flush()
            messages.append(
                _user_message(_unframe(_USER_REQUEST, content), created_at)
            )
            turn.asked_at = created_at
        elif kind in SYSTEM_TYPES:
            # None of these is anybody's turn. A `SYSTEM_MESSAGE` is another
            # conversation speaking, which the harness delivers by *speaking
            # as the user*; the other two are the harness's own notes about
            # the record. Each ends the turn above it and opens the one
            # below, because that is how the transcript places them — and
            # each is marked as a system event so the panel labels it
            # "System" rather than attributing it to the person reading.
            # Exactly the choice `history._task_notification_card` makes
            # about Claude's background-agent wake-up, which is the same
            # event.
            flush()
            messages.append(_system_message(kind, content, created_at))
            turn.asked_at = created_at
        elif kind == "PLANNER_RESPONSE":
            turn.absorb_response(record, content, created_at)
        elif kind in RESULT_TYPES:
            turn.absorb_result(record, kind, content, created_at)
        else:
            logger.warning(
                "Unknown agy transcript record type %r in %s; rendering it "
                "as JSON",
                kind,
                conversation_id,
            )
            turn.absorb_unreadable(record, kind)

    flush()
    return messages


def _system_message(kind: str, content: str, created_at: str) -> dict[str, Any]:
    """One of :data:`SYSTEM_TYPES`, as a message the panel labels "System".

    Only ``SYSTEM_MESSAGE`` is unframed and marked, because only it is a
    message: 📨 says *somebody sent this*, and a truncation notice wearing
    it would claim a sender it has not got.
    """
    if kind == "SYSTEM_MESSAGE":
        body = _unframe(_SYSTEM_MESSAGE, content)
        return _user_message(
            f"📨 {body}" if body else content, created_at, system=True
        )
    return _user_message(content, created_at, system=True)


def _user_message(
    content: str, timestamp: str, *, system: bool = False
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": content}
    if system:
        message["system_event"] = True
    if timestamp:
        message["timestamp"] = timestamp
    return message


class _Turn:
    """One assistant turn under construction, from ``agy``'s records.

    The counterpart of ``history._Turn``, and it produces the same frozen
    shape — an ordered ``blocks`` list, the files the turn touched, and a
    footer — because both feed one renderer. What differs is entirely
    upstream: there are no ids to correlate a result with its call, so the
    correlation is arithmetic on step indices, and there is no usage
    anywhere in the file, so the footer carries no token counts.

    Nothing is defaulted. ``turn_model_usage`` is absent rather than zero
    (``agy``'s transcript records no tokens at all, on any record) and
    ``terminalReason`` is null rather than "completed", which is the rule
    the browsed Claude turn follows for the same reason: a badge claiming
    a clean finish on no evidence is worse than no badge.
    """

    def __init__(self) -> None:
        #: The prompt's timestamp, so the turn's duration is what the user
        #: waited for rather than the span of the model's own records.
        self.asked_at = ""
        self.blocks: list[dict[str, Any]] = []
        self._files: list[str] = []
        self._text: list[str] = []
        self._responses = 0
        self._tool_calls = 0
        self._first = ""
        self._last = ""
        #: The ``step_index`` of the response whose calls are open, and the
        #: cards it opened keyed by their position in that step. A result
        #: names its call by arithmetic on the two — see
        #: :meth:`absorb_result`. Keyed rather than listed so that a
        #: malformed entry in ``tool_calls`` costs its own card and does not
        #: shift the ones after it onto the wrong results.
        self._call_step: int | None = None
        self._cards: dict[int, dict[str, Any]] = {}

    def any(self) -> bool:
        return bool(self.blocks)

    # -- accumulation ---------------------------------------------------

    def absorb_response(
        self, record: dict[str, Any], content: str, created_at: str
    ) -> None:
        self._note_time(created_at)
        self._responses += 1
        index = _step_index(record)
        if content:
            self._text.append(content)
            self.blocks.append(_text_block(f"agy-text-{index}", "text", content))
        # A response supersedes the previous one's open calls whether or not
        # it makes any of its own. A call still unanswered when the model
        # spoke again is one whose result is never coming — measured, and
        # that is what a call our dialog refused looks like from here. Left
        # open, it would collect the *next* call's output.
        self._call_step = None
        self._cards = {}
        calls = record.get("tool_calls")
        if not isinstance(calls, list):
            return
        opened: dict[int, dict[str, Any]] = {}
        for position, call in enumerate(calls):
            if isinstance(call, dict):
                opened[position] = self._open_call(call, index, position, created_at)
        if opened:
            self._call_step = index
            self._cards = opened

    def _open_call(
        self,
        call: dict[str, Any],
        index: int,
        position: int,
        created_at: str,
    ) -> dict[str, Any]:
        name = str(call.get("name") or "")
        args = call.get("args")
        args = dict(args) if isinstance(args, dict) else {}
        self._tool_calls += 1
        card = {
            # The step index and the call's place in it. The live pump
            # keys a card `agy-tool-<step>` because a `tool` step on the
            # stream carries exactly one call; the transcript batches a
            # step's calls into one record, so the position is what keeps
            # two calls made at one step apart.
            "tool_use_id": f"agy-tool-{index}-{position}",
            "name": name,
            "server": None,
            "input": args,
            "status": "pending",
            "invoked_at": created_at,
            # Every tool call from a claimed conversation reaches the
            # dialog on this transport, a subagent's included — that is
            # what `AgyGateServer` claims a subagent's conversation *for*.
            # The live card says the same thing for the same reason, and
            # the browsed card agreeing with it is the point.
            "gated": True,
            # None, not the conversation id: this whole message list *is*
            # the subagent's tab, so there is no second agent inside it to
            # attribute a block to. `history.load_subagent` renders
            # Claude's subagent blocks the same way.
            "agent_id": None,
            "server_tool": False,
        }
        rendered = {
            "block_id": card["tool_use_id"],
            "kind": "tool",
            "seq": 0,
            "content": "",
            "done": False,
            "agent_id": None,
            "tool": card,
            "result": None,
            "gated": True,
            "denial": None,
            "superseded": False,
        }
        self.blocks.append(rendered)
        return rendered

    def absorb_result(
        self, record: dict[str, Any], kind: str, content: str, created_at: str
    ) -> None:
        """Attach a result record to the call it answers.

        **The linkage is arithmetic, and it was the one inference here that
        a second measurement overturned.** A result's ``step_index`` is its
        call's index, plus one, plus the call's position within that step —
        so a response making three calls is followed by up to three
        consecutive results, and *up to* is the whole point: a call our
        permission dialog refused produces **no record at all**, leaving a
        hole in the indices. Pairing each result with the oldest open call
        instead, which is what this did, put the next tool's output inside
        the refused call's card and knocked every card after a denial one
        call out of step, with ``files_written_by`` crediting a file to a
        write that never ran.

        A result whose call cannot be named is rendered as a fenced blob
        rather than dropped or attributed to the model: dropped, the
        transcript has a hole in it; folded into the prose, the harness's
        own words are attributed to the agent.
        """
        self._note_time(created_at)
        rendered = None
        if self._call_step is not None:
            position = _step_index(record) - self._call_step - 1
            candidate = self._cards.get(position)
            if candidate is not None and candidate["result"] is None:
                rendered = candidate
        if rendered is None:
            self.absorb_unreadable(record, kind)
            return
        card = rendered["tool"]
        started, finished, body = _strip_result_header(content)
        preview, truncated = truncate_tool_result(body)
        # There is no `is_error` on this record and **no `ERROR` status**:
        # the only two statuses on disk are `DONE` and `RUNNING`. A failure
        # is a record type, and a tool that ran and failed says so in prose
        # this deliberately does not sniff — `specs5/5-webapp/chat.md`
        # § Card Anatomy makes the status flag the only thing a card's
        # failure styling may read.
        failed = kind == "ERROR_MESSAGE"
        # A backgrounded tool: the harness says it started and there is no
        # completion to report, so the card stays unresolved rather than
        # claiming a success with no output.
        running = str(record.get("status") or "") == "RUNNING"
        status = "error" if failed else "pending" if running else "ok"
        files = files_written_by(card["name"], card["input"]) if status == "ok" else []
        for path in files:
            if path not in self._files:
                self._files.append(path)
        payload = {
            "tool_use_id": card["tool_use_id"],
            "status": status,
            "preview": preview,
            "truncated": truncated,
            "full_bytes": len(body.encode("utf-8")),
            # The header's own pair where the record carries it, which is
            # the tool's duration rather than the gap between two records;
            # the records' stamps otherwise. 0 for a backgrounded tool,
            # because nothing has finished, and 0 where two stamps share a
            # second — which the footer renders as no duration at all.
            "duration_ms": (
                0
                if running
                else _elapsed_ms(started, finished)
                if started and finished
                else _elapsed_ms(card["invoked_at"], created_at)
            ),
            "files_modified": files,
        }
        rendered["result"] = payload
        rendered["done"] = not running
        rendered["tool"] = {**card, "status": payload["status"], "result": payload}

    def absorb_unreadable(self, record: dict[str, Any], kind: str) -> None:
        """A record this reader has no rendering for, shown rather than lost.

        The rule ``history._absorb_block`` and the ``agy`` pump both
        follow: on a CLI that releases weekly, a record nobody renders
        must degrade to a visible blob instead of to a gap in the middle
        of a transcript.
        """
        index = _step_index(record)
        self.blocks.append(
            _text_block(
                f"agy-unreadable-{index}",
                "text",
                "```json\n"
                f"// unrendered agy transcript record: {kind or '(no type)'}\n"
                f"{json.dumps(record, indent=2, default=str)}\n```",
            )
        )

    def _note_time(self, created_at: str) -> None:
        if not created_at:
            return
        if not self._first:
            self._first = created_at
        self._last = created_at

    # -- freezing -------------------------------------------------------

    def freeze(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "tool_calls": self._tool_calls,
            # One `PLANNER_RESPONSE` is one model response, which is the
            # same quantity the footer's "engine turns" counts on the
            # Claude side — counted there from distinct API message ids,
            # here from the records that *are* those messages.
            "num_turns": self._responses,
            "files_modified": list(self._files),
        }
        duration = _elapsed_ms(self.asked_at or self._first, self._last)
        if duration:
            summary["duration_ms"] = duration
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "\n\n".join(part for part in self._text if part),
            "blocks": self.blocks,
            # Empty, and not because a subagent cannot delegate. Its own
            # delegation *is* rendered — as the `invoke_subagent` card in
            # this list — and a row here would offer a button into a
            # grandchild's transcript, which is a level the listing does
            # not walk. Left empty rather than half-built.
            "subagents": [],
            "files": list(self._files),
            "turn": summary,
            "terminalReason": None,
        }
        if self._first:
            message["timestamp"] = self._first
        return message


# ---------------------------------------------------------------------------
# Small shared pieces
# ---------------------------------------------------------------------------


def _text_block(block_id: str, kind: str, content: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "kind": kind,
        "seq": 0,
        "content": content,
        "done": True,
        "agent_id": None,
    }


def _safe_id(value: str) -> bool:
    """Whether a conversation id can be used as a directory name.

    ``agy`` mints UUIDs, so this is not a parser: it rejects the
    separators and the parent reference that would turn ``brain_dir /
    <id>`` into a path outside the store.
    """
    return not (set(value) & {"/", "\\", "\0"}) and value not in (".", "..")


def _step_index(record: dict[str, Any]) -> int:
    """A record's ``step_index``, or a value that can answer no call.

    Not 0 for a record without one: 0 is the prompt's index, and the
    arithmetic in :meth:`_Turn.absorb_result` would read a record that lost
    its index as the answer to whichever call sat at step -1. Every record
    on disk has one; this is the branch that keeps a release that drops it
    from mis-attributing output rather than merely losing it.
    """
    index = record.get("step_index")
    return index if isinstance(index, int) else 1 << 30


def _strip_result_header(content: str) -> tuple[str, str, str]:
    """A result's ``Created At``/``Completed At`` stamps and its payload.

    ``("", "", content)`` where the header is absent — which is every
    record that is not a tool result, and a tool result from a release
    that stops writing it.
    """
    match = _RESULT_HEADER.match(content)
    if match is None:
        return "", "", content
    return (
        (match.group("created") or "").strip(),
        (match.group("completed") or "").strip(),
        content[match.end() :],
    )


def _first_prompt(records: list[dict[str, Any]]) -> str:
    for record in records:
        if str(record.get("type") or "") == "USER_INPUT":
            content = record.get("content")
            return _unframe(_USER_REQUEST, content if isinstance(content, str) else "")
    return ""


def _unframe(pattern: re.Pattern[str], content: str) -> str:
    """The body of one of ``agy``'s framing tags, or the text unchanged.

    ``strip_framing``'s counterpart for somebody else's framing. A
    subagent's prompt arrives wrapped in ``<USER_REQUEST>`` alongside an
    ``<ADDITIONAL_METADATA>`` block stating the local time; the wrapper
    and the metadata are the harness talking to the model, and a tab that
    showed them would bury every prompt under plumbing nobody wrote.

    Unchanged when the tag is absent, never truncated at a guess — the
    same choice ``strip_framing`` makes about framing that was opened and
    never closed.

    *Which* opening tag counts is the pattern's business rather than this
    function's, and the two callers want opposite answers for measured
    reasons — see :data:`_USER_REQUEST` and :data:`_SYSTEM_MESSAGE`.
    """
    match = pattern.search(content)
    return match.group(1) if match else content


def _elapsed_ms(start: str, end: str) -> int:
    """The gap between two ``created_at`` stamps, or 0.

    0 for anything unreadable or backwards, and the footer draws no
    duration for 0 — which is the honest rendering of "the file does not
    say" as well as of "it took less than a millisecond".
    """
    if not start or not end:
        return 0
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0
    delta = int((last - first).total_seconds() * 1000)
    return delta if delta > 0 else 0
