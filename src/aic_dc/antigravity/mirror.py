"""The repo-local transcript mirror for both Antigravity transports.

Phase 5 of ``specs5/plan-ag/``. This is an **observer**, not a
``SessionStore`` implementation, and the difference is the whole shape of
the module: Antigravity owns an opaque ``save_dir`` and exposes no store
protocol to implement (``sdk-surface.md`` § *What does not translate*),
while :class:`~aic_dc.claude_code.session_store.RepoSessionStore` already
takes its root as a constructor argument, stores plain dicts as JSONL and
dedups on ``uuid``. So the work is not "write a store"; it is "write
entries the existing reader accepts, into a root of our own".

One observer, two transports
============================
:class:`~aic_dc.antigravity.steps.StepTranslator` and
:class:`~aic_dc.agy.steps.AgyTranslator` emit the *same event names* —
``streamChunk``, ``thinkingChunk``, ``toolUse``, ``toolResult``,
``systemEvent``, ``streamComplete`` — which is what lets one observer
serve both. That was a phase-8 decision made for a different reason and
this is the payoff.

**They do not emit the same payloads**, and that is the trap this module
is written around. A ``toolResult`` is
``{tool_use_id, status: "ok"|"error", preview, …}`` from the SDK pump and
``{tool_use_id, name, status: "success"|"error", content, …}`` from the
``agy`` pump. :func:`_result_text` and :func:`_result_failed` are the one
place that knows both spellings; a second reader elsewhere is the quiet
"looks fine, does nothing" failure this plan has now paid for four times.

What the parser requires, and what it does not
==============================================
Measured against the SDK's own reader rather than guessed
(``delivery.md`` § *Phase 5 groundwork*):

- **The session id must be a UUID.** ``get_session_messages_from_store``
  calls ``_validate_uuid`` first and returns ``[]`` for anything else —
  no error, and indistinguishable from "no such session". So the mirror
  keys on the *engine's own* conversation id, which is a UUID on both
  transports, and never on a readable name of our own. That is also what
  makes resume work: the same id is what ``agy --conversation <id>`` and
  ``SessionContinuationMode.RESUME`` take.
- **An entry needs only ``{uuid, type, message}``.** ``sessionId``,
  ``cwd``, ``isSidechain``, ``version``, ``gitBranch`` and the rest are
  all droppable.
- **``parentUuid`` is not droppable once there are two entries.**
  ``_build_conversation_chain`` finds the terminal entry and walks *back*
  through ``parentUuid``; with no links every entry is its own terminal,
  the walk picks the last one and the chain is one message long. A single
  entry parses either way, which is why the minimal-shape bisect did not
  surface this. So every entry names its predecessor, and the chain is
  re-seeded from disk on the first append of a resumed session.

``timestamp`` is written although the parser drops it, because the
history browser reads it back off the raw entries for turn durations,
session ordering and previews.

What is deliberately not mirrored
=================================
**Token counters are not written at all**, and the reason is placement
rather than squeamishness. This engine has no per-message usage: the SDK
reports the *turn's* tokens as a diff taken at close
(``Conversation.last_turn_usage``) and ``agy`` sends a running total on
its result frame, so there is no entry either of them belongs on. Writing
them onto an entry of our own invention cost a real wart — the history
browser counted an extra message per turn for an assistant entry that
rendered nothing — and bought no figure on screen, because the counters
share no field name with the four ``history._Turn.freeze`` sums and the
live footer skips this engine's flat shape too. So every assistant entry
of a turn carries an **empty** ``usage`` under one shared ``message.id``,
which is the CLI's own arrangement and is what makes a browsed turn count
as one engine turn rather than one per block. The turn's tokens are the
live footer's, reported where they are measured.

**``systemEvent`` reaches the transcript only for compaction.** A
compaction is the one subtype with a CLI counterpart — a ``system`` entry
with ``subtype: "compact_boundary"``, which ``history._compaction_divider``
already renders. ``engine_error``, ``engine_notice`` and
``waiting_for_user`` have no entry type that renders, and their home is
the events log rather than the transcript
(``claude_code/events_log.py``: *"the store is never given an entry the
CLI did not write"*). Recording them as an invented entry type would be a
line nothing reads.

Governing spec: ``specs5/plan-ag/`` — AG-1, AG-3, AG-9.
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Event names this observer acts on. Everything else on the wire —
#: ``turnUsage``, ``permissionRequest``, ``subagentEvent`` — is live UI
#: traffic with no transcript counterpart.
MIRRORED_EVENTS = frozenset(
    {
        "streamChunk",
        "thinkingChunk",
        "toolUse",
        "toolResult",
        "systemEvent",
        "streamComplete",
    }
)


class SessionMirror:
    """Turns one Antigravity conversation's events into stored entries.

    Parameters
    ----------
    store:
        A :class:`~aic_dc.claude_code.session_store.RepoSessionStore`
        rooted at *this engine's* directory (AG-1). A record written by
        one engine must not be reachable by the other, and a separate root
        makes that true by construction rather than by a check somebody
        has to remember to write.
    directory:
        The repo root, for the store's ``project_key``.
    model:
        A callable returning the model name in force, so a mid-session
        ``set_model`` is reflected without the mirror holding a stale copy.
    clock, new_uuid:
        Injectable so a test can assert on exact entries.
    """

    def __init__(
        self,
        store: Any,
        directory: Path | str,
        *,
        model: Callable[[], str] = lambda: "",
        clock: Callable[[], float] = time.time,
        new_uuid: Callable[[], str] = lambda: str(_uuid.uuid4()),
    ) -> None:
        self._store = store
        self._directory = str(directory)
        self._model = model
        self._clock = clock
        self._new_uuid = new_uuid

        self._session_id: str | None = None
        self._turn_id: str | None = None
        #: The uuid the next entry points back to. ``None`` means "not
        #: seeded yet", which is different from "no parent" — see
        #: :meth:`_seed_chain`.
        self._parent: str | None = None
        self._seeded = False
        #: The text or thinking block still accumulating, if any. Held
        #: because a ``streamChunk`` carries the *accumulated* content
        #: rather than a delta, so writing one per chunk would write the
        #: same prose a hundred times.
        self._pending: dict[str, Any] | None = None
        #: A prompt sent before the engine had named its conversation.
        #: See :meth:`note_prompt`.
        self._deferred_prompt: str | None = None

    # ------------------------------------------------------------------
    # Which conversation is being mirrored
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        """The conversation id entries are being filed under."""
        return self._session_id

    async def attach(self, conversation_id: str | None) -> None:
        """Follow the engine's conversation id. Idempotent and cheap.

        Called wherever that id could have changed — after a connect,
        after a resume, and once per event while a turn streams — because
        on the SDK transport it **does** change mid-turn: the SDK derives
        ``Conversation.conversation_id`` from the event processor's main
        trajectory, so it is empty until the first step arrives. Calling
        this on every event is what turns that into a detail rather than
        into a lost first turn.

        A non-UUID id is refused rather than stored, because the
        transcript reader validates the session id as a UUID and answers
        ``[]`` for anything else — with no error, and indistinguishable
        from a session that does not exist.
        """
        if not conversation_id:
            return
        if conversation_id == self._session_id:
            return
        if not _is_uuid(conversation_id):
            logger.warning(
                "Not mirroring conversation %r: the transcript reader "
                "validates the session id as a UUID and answers with an "
                "empty list for anything else, which renders as a session "
                "that does not exist.",
                conversation_id,
            )
            return
        self._session_id = conversation_id
        self._parent = None
        self._seeded = False
        deferred, self._deferred_prompt = self._deferred_prompt, None
        if deferred is not None:
            await self._write_prompt(deferred)

    def detach(self) -> None:
        """Stop mirroring. The conversation this was filing under is over.

        Called on ``new_session`` and on teardown. Everything in flight is
        dropped rather than flushed: a half-accumulated block belongs to a
        conversation the user has just discarded.
        """
        self._session_id = None
        self._turn_id = None
        self._parent = None
        self._seeded = False
        self._pending = None
        self._deferred_prompt = None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def note_prompt(self, text: str, *, request_id: str = "") -> None:
        """Record the human's prompt, which opens a turn.

        Stored verbatim, framing and all: ``history.strip_framing``
        removes the ``<aic-dc-ui-context>`` block at read time, and doing
        it here would make the transcript disagree with what the model was
        actually sent.

        **Held rather than dropped when the engine has not named its
        conversation yet.** That is the ordinary case for the very first
        turn on the SDK transport, and a prompt written after the answer
        it produced would be worse than one written late: the reader folds
        a turn from its prompt forward, so the order on disk *is* the
        conversation.
        """
        self._pending = None
        self._turn_id = f"ag_{request_id}" if request_id else f"ag_{self._new_uuid()}"
        if self._session_id is None:
            self._deferred_prompt = text
            return
        await self._write_prompt(text)

    async def _write_prompt(self, text: str) -> None:
        await self._append(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
            }
        )

    async def observe(self, event: Any) -> None:
        """One outbound event in, zero or more stored entries out.

        Never raises. A mirror that raised would take the turn's event
        stream down with it, which trades a missing transcript for a
        missing conversation.
        """
        if self._session_id is None:
            return
        name = getattr(event, "name", "")
        if name not in MIRRORED_EVENTS:
            return
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return
        try:
            await self._observe(name, payload)
        except Exception:  # noqa: BLE001 - a mirror must not kill a turn
            logger.exception("Could not mirror a %s event", name)

    async def _observe(self, name: str, payload: dict[str, Any]) -> None:
        if name in ("streamChunk", "thinkingChunk"):
            await self._absorb_chunk(name, payload)
            return
        # Everything below is an ordered event, so any prose still
        # accumulating belongs *before* it in the transcript.
        await self._flush_text()
        if name == "toolUse":
            await self._append(self._assistant_entry([_tool_use_block(payload)]))
        elif name == "toolResult":
            await self._append(_tool_result_entry(payload))
        elif name == "systemEvent":
            if payload.get("subtype") == "compaction":
                await self._append(_compaction_entry(payload))
        elif name == "streamComplete":
            # Nothing of its own to write. The flush above is the point:
            # the turn's last block of prose is not `done` until here on
            # the SDK transport, whose chunks carry `done: False` until
            # `stream_complete` sets them.
            self._turn_id = None

    async def _absorb_chunk(self, name: str, payload: dict[str, Any]) -> None:
        block_id = payload.get("block_id")
        if not isinstance(block_id, str) or not block_id:
            return
        kind = "thinking" if name == "thinkingChunk" else "text"
        pending = self._pending
        if pending is not None and pending["block_id"] != block_id:
            await self._flush_text()
            pending = None
        if pending is None:
            pending = {"block_id": block_id, "kind": kind, "content": ""}
            self._pending = pending
        content = payload.get("content")
        # The accumulated total, not a delta — both pumps send the running
        # string, for the same reason: the browser replaces by block id.
        if isinstance(content, str):
            pending["content"] = content
        if payload.get("done"):
            await self._flush_text()

    async def _flush_text(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        content = pending["content"]
        if not content:
            return
        block = (
            {"type": "thinking", "thinking": content, "signature": ""}
            if pending["kind"] == "thinking"
            else {"type": "text", "text": content}
        )
        await self._append(self._assistant_entry([block]))

    def _assistant_entry(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        """One assistant entry, all of a turn's sharing one ``message.id``.

        The shared id and the repeated ``usage`` are the CLI's own
        arrangement rather than a shortcut: it writes one entry per
        content block and repeats the API message's id and usage across
        all of them, which is why ``history._Turn.absorb`` deduplicates on
        the id. Following it here is what makes a browsed Antigravity turn
        count as **one** engine turn rather than as one per block.

        ``usage`` is empty for the reason the module docstring gives: this
        engine has no per-message figure, so there is nothing true to put
        here, and an entry invented to hold the turn's total was counted
        as a message by the history browser.
        """
        return {
            "type": "assistant",
            "message": {
                "id": self._turn_id or f"ag_{self._new_uuid()}",
                "role": "assistant",
                "model": self._model(),
                "content": content,
                "usage": {},
            },
        }

    async def _append(self, entry: dict[str, Any]) -> None:
        """Stamp one entry with its identity and its parent, and store it."""
        session_id = self._session_id
        if session_id is None:
            return
        await self._seed_chain()
        entry = dict(entry)
        entry["uuid"] = self._new_uuid()
        entry["parentUuid"] = self._parent
        entry["timestamp"] = _iso(self._clock())
        key = self._key(session_id)
        try:
            await self._store.append(key, [entry])
        except Exception:  # noqa: BLE001 - a lost line is not a lost turn
            logger.exception("Could not mirror an entry of %s", session_id)
            return
        self._parent = entry["uuid"]

    async def _seed_chain(self) -> None:
        """Point the next entry at the last one already on disk.

        A resumed conversation — or one this process is re-attaching to
        after a restart — already has a transcript, and an entry with no
        parent would start a second chain in the same file. The reader
        picks *one* terminal and walks back from it, so the older half of
        the conversation would silently stop rendering.
        """
        if self._seeded or self._session_id is None:
            return
        self._seeded = True
        try:
            entries = await self._store.load(self._key(self._session_id)) or []
        except Exception:  # noqa: BLE001 - an unreadable mirror is not fatal
            logger.exception("Could not read %s to continue its chain", self._session_id)
            return
        for entry in reversed(entries):
            if isinstance(entry, dict) and isinstance(entry.get("uuid"), str):
                self._parent = entry["uuid"]
                return

    def _key(self, session_id: str) -> dict[str, Any]:
        from claude_agent_sdk import project_key_for_directory

        return {
            "project_key": project_key_for_directory(self._directory),
            "session_id": session_id,
        }


# ---------------------------------------------------------------------------
# Entry builders — the CLI's shape, from either pump's payload
# ---------------------------------------------------------------------------


def _tool_use_block(payload: dict[str, Any]) -> dict[str, Any]:
    """A tool call, as the content block the CLI would have written.

    ``input`` is stored as the engine sent it, in the engine's own
    argument names. The browser builds a card header from it and
    ``files_written_by`` attributes writes from it, and both already know
    both vocabularies — translating here would put a third spelling on
    disk.
    """
    tool_input = payload.get("input")
    return {
        "type": "tool_use",
        "id": str(payload.get("tool_use_id") or ""),
        "name": str(payload.get("name") or ""),
        "input": tool_input if isinstance(tool_input, dict) else {},
    }


def _tool_result_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """A tool result, as the ``user`` entry the CLI would have written.

    A tool reporting back is a ``user`` entry whose content is nothing but
    ``tool_result`` blocks — that is how ``history._is_tool_reply``
    recognises one, and getting it wrong would render every tool result as
    a prompt the user typed.
    """
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": str(payload.get("tool_use_id") or ""),
                    "content": _result_text(payload),
                    "is_error": _result_failed(payload),
                }
            ],
        },
    }


def _compaction_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """The boundary the context was condensed at.

    A ``system`` entry with ``subtype: "compact_boundary"``, which is what
    ``history._compaction_divider`` already looks for — the one
    ``systemEvent`` subtype with a CLI counterpart. The parser drops it
    from the message list, correctly, because it is a marker rather than a
    message; it stays in the chain and renders as a divider before the
    next prompt.
    """
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "compactMetadata": {
            "preTokens": data.get("pre_tokens"),
            "postTokens": data.get("post_tokens"),
            "trigger": data.get("trigger") or "auto",
        },
    }


#: Result-status strings that mean the call succeeded.
#:
#: Two spellings for one fact, because the two pumps chose differently —
#: ``StepTranslator`` says ``"ok"`` and ``AgyTranslator`` says
#: ``"success"``. Named here, once, rather than compared inline in two
#: places: an unrecognised status must read as a failure, and a set that
#: forgot one spelling would mark every successful ``agy`` tool call as an
#: error in the browsed transcript while the live one showed it green.
_RESULT_OK = frozenset({"ok", "success"})


def _result_failed(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "") not in _RESULT_OK


def _result_text(payload: dict[str, Any]) -> str:
    """The result body, whichever pump produced it.

    ``preview`` is the SDK pump's already-truncated text and ``content``
    is ``agy``'s. Both are read, first one wins, because a reader that
    knew only one would store an empty result for half the transport
    surface — visible only as tool cards that render with no output.
    """
    for field in ("preview", "content"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_uuid(value: str) -> bool:
    """Whether the transcript reader will accept this as a session id."""
    try:
        _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _iso(epoch_seconds: float) -> str:
    """The CLI's timestamp format: UTC, milliseconds, trailing ``Z``."""
    stamp = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
