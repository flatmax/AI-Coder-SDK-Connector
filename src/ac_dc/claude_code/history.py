"""Reading a mirrored transcript back as something a browser can render.

The store holds the CLI's own transcript, verbatim and opaque. This module
turns it into the ``MessageDict`` list the chat panel already renders — the
same shape the live pump produces, so a reopened session looks like the one
the user watched happen.

**Rendering happens here, at read time, never at write time.** A tool card's
input summary and a truncated result preview are display decisions; writing
them into storage would be a second version of the truth that can disagree
with the first (``specs5/3-engine/history.md`` § What the Browser Reads).

Two things about the shape of the CLI's transcript drive most of the code
below, and both were established by reading real transcripts rather than
from documentation:

- **One entry per content block.** An assistant API message whose content is
  ``[thinking, tool_use]`` is written as *two* entries sharing one
  ``message.id``, each carrying a single-element ``content`` list and a copy
  of the same ``usage``. So entry granularity already is block granularity,
  and per-message facts must be deduplicated by ``message.id`` before they
  are summed.
- **Tool results arrive as ``user`` entries** whose content is a
  ``tool_result`` block. A "user message" in the transcript is therefore
  either something a human typed or a tool reporting back, and only the
  former starts a new turn.

What the transcript does **not** hold is a result entry: there is no
``terminal_reason``, no ``num_turns`` and no ``total_cost_usd`` anywhere in
it. The turn footer is rebuilt from what is there — per-model usage, tool
counts, wall-clock duration from the prompt to the turn's last entry — and
the fields that cannot be recovered are omitted rather than guessed. See
``specs-reference/3-engine/history.md`` § Browse rendering.

Governing spec: ``specs5/3-engine/history.md``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ac_dc.claude_code.messages import (
    files_written_by,
    flatten_tool_result,
    mcp_server_name,
    summarise_tool_input,
    truncate_tool_result,
)

if TYPE_CHECKING:
    from claude_agent_sdk import SessionMessage, SessionStore

logger = logging.getLogger(__name__)


# The session list's ``preview`` field. Long enough to tell two sessions
# apart in a row of a list, short enough not to wrap it.
PREVIEW_CHARS = 100

# The framing wrapper `session.build_framing` puts around UI context. It is
# stripped on the way out: the model needed it, the user did not write it,
# and showing it would bury every historical prompt under a context blob.
_FRAMING_OPEN = "<ac-dc-ui-context>"
_FRAMING_CLOSE = "</ac-dc-ui-context>"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def load_session(
    store: SessionStore,
    session_id: str,
    directory: str,
    *,
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One session's transcript, rendered, with its events interleaved.

    The conversation itself comes from the SDK's parser, which owns the
    ``parentUuid`` chain walk and the visibility rules — the entry shape is
    the CLI's internal union and is exactly the kind of thing that changes
    underneath a reader.

    The raw entries are read as well, for the two facts the parser drops
    and that browsing needs: each entry's ``timestamp``, and the
    ``compact_boundary`` system entries that mark where the model's memory
    was condensed. That is a lookup of two auxiliary fields by uuid, not a
    second parse of the conversation.
    """
    from claude_agent_sdk import get_session_messages_from_store

    messages = await get_session_messages_from_store(store, session_id, directory)
    if not messages:
        return []

    from claude_agent_sdk import project_key_for_directory

    key = {
        "project_key": project_key_for_directory(directory),
        "session_id": session_id,
    }
    try:
        entries = await store.load(key) or []
    except OSError:
        # The transcript was readable a moment ago (the parser just used
        # it), so this is a race with a delete rather than a broken
        # session. Render the conversation without timestamps.
        logger.warning("Could not re-read %s for timestamps", session_id)
        entries = []

    return render_messages(
        messages, entries, events or [], session_id=session_id
    )


async def list_sessions(
    store: SessionStore,
    directory: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """The session list, most recently modified first.

    Two SDK calls per listing. The first is cheap — with our store's
    summary sidecars it costs one batch read and no per-session loads — and
    supplies the title, the first prompt and the creation time. The second
    is one parse per listed session, and is what makes ``message_count``
    exact; ``limit`` is therefore the bound on the work this does.

    The derived index will cache the second call's answers
    (``specs5/3-engine/history.md`` § The Derived Index). Until it exists
    this recomputes them, which the spec sanctions: a cold index is a
    performance problem, never a correctness one.
    """
    from claude_agent_sdk import (
        get_session_messages_from_store,
        list_sessions_from_store,
    )

    infos = await list_sessions_from_store(store, directory, limit=limit)
    summaries: list[dict[str, Any]] = []
    for info in infos:
        try:
            messages = await get_session_messages_from_store(
                store, info.session_id, directory
            )
        except OSError:
            logger.warning("Could not read session %s while listing", info.session_id)
            messages = []
        summaries.append(summarise_session(info, messages))
    return summaries


def summarise_session(
    info: Any, messages: list[SessionMessage]
) -> dict[str, Any]:
    """The seven-field summary a session row is drawn from.

    ``total_cost_usd`` is always ``None``: cost is not in the transcript,
    the CLI computes it from a pricing table we do not have, and it is null
    under subscription billing anyway. ``None`` renders as no cost at all,
    which is the honest outcome — see ``formatCost`` in the frontend, whose
    whole purpose is that a missing cost must never print as ``$0.00``.

    ``first_role`` is structurally ``"user"``: a transcript begins with the
    prompt that created the session, and a post-compaction continuation
    begins with the compact summary, which is also a user entry. It is kept
    because the session-row renderer reads it.
    """
    preview = (info.first_prompt or info.summary or "").strip()
    return {
        "session_id": info.session_id,
        "timestamp": _iso_from_epoch_ms(info.created_at),
        "message_count": len(messages),
        "preview": preview[:PREVIEW_CHARS],
        "first_role": "user" if messages else "",
        # Every session in the store loaded above; a row with no messages
        # is one whose transcript is gone or unparseable, which is exactly
        # what the browser labels non-resumable rather than failing on.
        "resumable": bool(messages),
        "total_cost_usd": None,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_messages(
    messages: list[SessionMessage],
    entries: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    """Parsed messages plus raw entries plus our events → one message list.

    Pure, and deliberately so: the whole taxonomy is testable by handing it
    constructed inputs, which is the same arrangement
    :class:`~ac_dc.claude_code.messages.TurnTranslator` uses for the live
    path.

    Assistant entries are folded into turns, because that is what the panel
    renders: one card per turn carrying an ordered block list, not one card
    per API message. A turn runs from a human prompt to the next one.
    """
    by_uuid = {
        entry["uuid"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("uuid"), str)
    }

    rendered: list[dict[str, Any]] = []
    turn: _Turn | None = None
    # When the prompt that opened the current turn was submitted, so the
    # turn's duration spans what the user actually waited for — the same
    # thing the live path's `duration_ms` measures.
    asked_at: str | None = None

    for message in messages:
        entry = by_uuid.get(getattr(message, "uuid", "") or "", {})
        body = getattr(message, "message", None) or {}
        if getattr(message, "type", None) == "user":
            if _is_tool_reply(body):
                if turn is not None:
                    turn.absorb_results(body, entry)
                continue
            if turn is not None:
                rendered.append(turn.freeze())
                turn = None
            divider = _compaction_divider(entry, by_uuid)
            if divider is not None:
                rendered.append(divider)
            rendered.append(_user_message(message, entry, session_id))
            timestamp = entry.get("timestamp")
            asked_at = timestamp if isinstance(timestamp, str) else None
        else:
            if turn is None:
                turn = _Turn(asked_at)
            turn.absorb(body, entry)

    if turn is not None:
        rendered.append(turn.freeze())

    return _interleave(rendered, events)


def _user_message(
    message: SessionMessage, entry: dict[str, Any], session_id: str
) -> dict[str, Any]:
    """One human prompt, with its framing removed and its images pointed at.

    Image blocks are replaced by references rather than data URIs. A session
    with a handful of screenshots is megabytes of base64, and a history load
    that carried them would send all of it to every client on every open;
    ``history_image`` fetches one on demand.
    """
    body = getattr(message, "message", None) or {}
    content = body.get("content")
    text_parts: list[str] = []
    image_refs: list[dict[str, Any]] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                source = block.get("source") or {}
                image_refs.append(
                    {
                        "session_id": session_id,
                        "entry_uuid": getattr(message, "uuid", "") or "",
                        "block": index,
                        "media_type": source.get("media_type") or "",
                    }
                )

    rendered: dict[str, Any] = {
        "role": "user",
        "content": strip_framing("\n".join(part for part in text_parts if part)),
    }
    if image_refs:
        rendered["image_refs"] = image_refs
    if entry.get("isCompactSummary"):
        # Not something the user typed: the CLI's own summary of everything
        # it dropped, replayed as a user turn because that is how the model
        # receives it. Marked so the panel can style it as a system note
        # rather than attribute it to the person reading it.
        rendered["compact_summary"] = True
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, str):
        rendered["timestamp"] = timestamp
    return rendered


def strip_framing(text: str) -> str:
    """Drop the ``<ac-dc-ui-context>`` block AC-DC prepended to a prompt.

    The framing is ours, not the user's, and a history that showed it would
    read as though the user typed their file selection out by hand every
    turn.

    The selected-file list inside it is *not* recovered into ``files``:
    parsing our own generated prose back into structure would be a second
    reading of something the browser can get from the derived index, and a
    reworded header sentence would turn a correct render into a wrong one.
    """
    if not text.startswith(_FRAMING_OPEN):
        return text
    end = text.find(_FRAMING_CLOSE)
    if end == -1:
        # Framing that was opened and never closed. Left alone: truncating
        # at a guess could cut into the user's own words.
        return text
    return text[end + len(_FRAMING_CLOSE) :].lstrip("\n")


def _is_tool_reply(body: dict[str, Any]) -> bool:
    """Whether a ``user`` entry is a tool reporting back rather than a human.

    A tool reply's content is a list of ``tool_result`` blocks and nothing
    else. A human's is a string, or a list carrying text and images.
    """
    content = body.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _compaction_divider(
    entry: dict[str, Any], by_uuid: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """The divider that belongs immediately before a compact summary.

    A ``compact_boundary`` entry is a ``system`` entry, so the SDK's parser
    drops it — correctly, since it is not a message. It is still in the
    chain, as the parent of the compact-summary user entry the CLI writes
    next, and that parent link is how the divider gets placed without a
    second chain walk of our own.

    ``compactMetadata`` is camelCase on disk and the panel's renderer takes
    snake_case, so the three fields it reads are translated here rather
    than passed through.
    """
    parent = entry.get("parentUuid")
    if not isinstance(parent, str):
        return None
    boundary = by_uuid.get(parent)
    if boundary is None or boundary.get("subtype") != "compact_boundary":
        return None
    meta = boundary.get("compactMetadata") or {}
    divider: dict[str, Any] = {
        "role": "user",
        "content": str(boundary.get("content") or "Conversation compacted"),
        "system_event": True,
        "compaction": {
            "pre_tokens": meta.get("preTokens"),
            "post_tokens": meta.get("postTokens"),
            "trigger": meta.get("trigger"),
        },
    }
    timestamp = boundary.get("timestamp")
    if isinstance(timestamp, str):
        divider["timestamp"] = timestamp
    return divider


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


class _Turn:
    """One assistant turn under construction, from its transcript entries.

    Mirrors what ``freezeBlocks`` produces on the live path, field for
    field, because both feed the same renderer: a settled assistant message
    carrying an ordered ``blocks`` list, the files it touched, and a footer
    summary. Anything the transcript cannot supply is left out, never
    defaulted — an invented ``terminal_reason`` of "completed" would badge
    a turn as having finished cleanly on no evidence.
    """

    def __init__(self, asked_at: str | None = None) -> None:
        # The prompt's timestamp, when there is one. A turn that produced a
        # single entry has no internal span to measure, and reporting 0 ms
        # for it would read as instantaneous.
        self._asked_at = asked_at
        self.blocks: list[dict[str, Any]] = []
        # tool_use_id → the block, so a result arriving in a later entry is
        # attached by the SDK's own id with no correlation table.
        self._tools: dict[str, dict[str, Any]] = {}
        # tool_use_id → the ISO timestamp of the entry that opened it, so a
        # call's duration is the real gap between call and result.
        self._started: dict[str, str] = {}
        # message.id → its usage, deduplicated: the CLI repeats one API
        # message's usage on every entry it split that message into, so
        # summing entries would multiply a turn's tokens by its block count.
        self._usage: dict[str, tuple[str, dict[str, Any]]] = {}
        self._first_timestamp: str | None = None
        self._last_timestamp: str | None = None
        self._tool_calls = 0
        self._files: list[str] = []
        self._text: list[str] = []

    # -- accumulation ---------------------------------------------------

    def absorb(self, body: dict[str, Any], entry: dict[str, Any]) -> None:
        """Fold one assistant entry's content blocks into the turn."""
        self._note_time(entry)
        message_id = body.get("id")
        usage = body.get("usage")
        if isinstance(message_id, str) and isinstance(usage, dict):
            self._usage[message_id] = (str(body.get("model") or ""), usage)

        uuid = str(entry.get("uuid") or "")
        content = body.get("content")
        if not isinstance(content, list):
            return
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            self._absorb_block(block, uuid, index, entry)

    def _absorb_block(
        self, block: dict[str, Any], uuid: str, index: int, entry: dict[str, Any]
    ) -> None:
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "")
            self._text.append(text)
            self.blocks.append(_text_block(f"{uuid}:{index}", "text", text))
        elif kind == "thinking":
            self.blocks.append(
                _text_block(f"{uuid}:{index}", "thinking", str(block.get("thinking") or ""))
            )
        elif kind in ("tool_use", "server_tool_use"):
            self._absorb_tool_use(block, entry, server_tool=kind == "server_tool_use")
        elif kind == "tool_result":
            # An assistant entry carrying a result is unusual but legal;
            # route it the same way so the card resolves either way.
            self._attach_result(block, entry)
        else:
            # Never silent. A CLI that grows a block kind this build has
            # never seen must degrade to a visible blob rather than to a
            # hole in the middle of an answer.
            logger.warning("Unknown transcript content block %r; rendering as JSON", kind)
            self.blocks.append(
                _text_block(
                    f"{uuid}:{index}",
                    "text",
                    "```json\n"
                    f"// unrecognised content block: {kind}\n"
                    f"{json.dumps(block, indent=2, default=str)}\n```",
                )
            )

    def _absorb_tool_use(
        self, block: dict[str, Any], entry: dict[str, Any], *, server_tool: bool
    ) -> None:
        tool_use_id = block.get("id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        if tool_use_id in self._tools:
            return
        name = str(block.get("name") or "")
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        self._tool_calls += 1

        if _is_todo_write(name):
            # One live plan per turn, not fifteen snapshots. The superseded
            # cards stay in the list because dropping them would renumber
            # block order; the renderer skips them.
            for existing in self.blocks:
                if existing["kind"] == "tool" and _is_todo_write(
                    (existing.get("tool") or {}).get("name") or ""
                ):
                    existing["superseded"] = True

        card = {
            "tool_use_id": tool_use_id,
            "name": name,
            "server": mcp_server_name(name),
            "input_summary": summarise_tool_input(tool_input),
            "input": tool_input,
            "status": "pending",
            # The transcript records denials, never that a dialog was shown
            # and answered, so this is set only where there is evidence.
            "gated": False,
            "agent_id": None,
            "server_tool": server_tool,
        }
        rendered = {
            "block_id": tool_use_id,
            "kind": "tool",
            "seq": 0,
            "content": "",
            "done": False,
            "agent_id": None,
            "tool": card,
            "result": None,
            "gated": False,
            "denial": None,
            "superseded": False,
        }
        self.blocks.append(rendered)
        self._tools[tool_use_id] = rendered
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, str):
            self._started[tool_use_id] = timestamp

    def absorb_results(self, body: dict[str, Any], entry: dict[str, Any]) -> None:
        """Attach a tool-reply entry's results to the calls they answer."""
        self._note_time(entry)
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self._attach_result(block, entry)

    def _attach_result(self, block: dict[str, Any], entry: dict[str, Any]) -> None:
        tool_use_id = block.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return
        rendered = self._tools.get(tool_use_id)
        if rendered is None:
            # A result whose call is in an earlier turn, or whose entry was
            # lost. Dropped rather than rendered on its own: a card with no
            # header reads as a rendering bug.
            logger.debug("Tool result for %s has no call in this turn", tool_use_id)
            return

        text = flatten_tool_result(block.get("content"))
        preview, truncated = truncate_tool_result(text)
        is_error = bool(block.get("is_error"))
        name = (rendered.get("tool") or {}).get("name") or ""
        files = [] if is_error else files_written_by(name, rendered["tool"]["input"])
        for path in files:
            if path not in self._files:
                self._files.append(path)

        payload = {
            "tool_use_id": tool_use_id,
            "status": "error" if is_error else "ok",
            "preview": preview,
            "truncated": truncated,
            "full_bytes": len(text.encode("utf-8")),
            "duration_ms": _elapsed_ms(
                self._started.get(tool_use_id), entry.get("timestamp")
            ),
            "files_modified": files,
        }
        rendered["result"] = payload
        rendered["done"] = True
        rendered["tool"] = {
            **rendered["tool"],
            "status": payload["status"],
            "result": payload,
        }

        denial_kind = entry.get("toolDenialKind")
        if denial_kind:
            # The user (or a rule they set) stopped this call. That outranks
            # the error-shaped result it produced, because "error" would
            # hide who caused it.
            rendered["gated"] = True
            rendered["tool"] = {**rendered["tool"], "gated": True}
            rendered["denial"] = {
                "action": "deny",
                "reason": preview,
                # Live, this names the client that answered the dialog. The
                # transcript records the kind of rule instead, which is a
                # different fact, so the field is left empty rather than
                # filled with something that reads like an answer.
                "resolvedBy": "",
            }

    def _note_time(self, entry: dict[str, Any]) -> None:
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str):
            return
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._last_timestamp = timestamp

    # -- freezing -------------------------------------------------------

    def freeze(self) -> dict[str, Any]:
        """The settled assistant message for this turn."""
        model_usage: dict[str, dict[str, Any]] = {}
        for model, usage in self._usage.values():
            bucket = model_usage.setdefault(model, {})
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                value = usage.get(field)
                if isinstance(value, int):
                    bucket[field] = bucket.get(field, 0) + value

        summary: dict[str, Any] = {
            "tool_calls": self._tool_calls,
            # Distinct API messages, which is what the footer's "engine
            # turns" counts. Not `num_turns` from a result message — there
            # is none — but the same quantity, counted from its source.
            "num_turns": len(self._usage),
            "files_modified": list(self._files),
        }
        if model_usage:
            summary["model_usage"] = model_usage
        duration = _elapsed_ms(
            self._asked_at or self._first_timestamp, self._last_timestamp
        )
        if duration:
            summary["duration_ms"] = duration

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self._text),
            "blocks": self.blocks,
            "subagents": [],
            "files": list(self._files),
            "turn": summary,
            # Absent from the transcript. Null draws no badge at all, which
            # the panel documents as the deliberate behaviour: a badge
            # claiming a clean finish is worse than no badge.
            "terminalReason": None,
        }
        if self._first_timestamp:
            message["timestamp"] = self._first_timestamp
        return message


def _text_block(block_id: str, kind: str, content: str) -> dict[str, Any]:
    return {
        "block_id": block_id,
        "kind": kind,
        "seq": 0,
        "content": content,
        "done": True,
        "agent_id": None,
    }


def _is_todo_write(name: str) -> bool:
    return name == "TodoWrite" or name.endswith("__TodoWrite")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _interleave(
    rendered: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge our operational events into the transcript by timestamp.

    A stable merge, and the transcript wins a tie: a commit recorded in the
    same millisecond as the message that triggered it belongs after it.

    An event with no usable timestamp goes to the end rather than to the
    front, so a malformed record cannot claim to predate the conversation.
    """
    if not events:
        return rendered

    cards = [_event_message(record) for record in events]
    cards = [card for card in cards if card is not None]
    if not cards:
        return rendered

    merged = [(_sort_key(m), 0, i, m) for i, m in enumerate(rendered)]
    merged += [(_sort_key(c), 1, i, c) for i, c in enumerate(cards)]
    merged.sort(key=lambda item: item[:3])
    return [item[3] for item in merged]


def _event_message(record: dict[str, Any]) -> dict[str, Any] | None:
    """One ``events.jsonl`` record as a system-event card."""
    if not isinstance(record, dict):
        return None
    content = record.get("content")
    if not isinstance(content, str):
        return None
    card: dict[str, Any] = {
        "role": "user",
        "content": content,
        "system_event": True,
        "event": record.get("event"),
    }
    for source, target in (("timestamp", "timestamp"), ("request_id", "request_id")):
        value = record.get(source)
        if isinstance(value, str) and value:
            card[target] = value
    return card


def _sort_key(message: dict[str, Any]) -> tuple[int, float]:
    """``(has_timestamp, epoch_seconds)`` — untimestamped sorts last."""
    stamp = _parse_iso(message.get("timestamp"))
    if stamp is None:
        return (1, 0.0)
    return (0, stamp)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> float | None:
    """ISO 8601 → epoch seconds, tolerating the CLI's trailing ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _elapsed_ms(start: Any, end: Any) -> int:
    """Milliseconds between two ISO timestamps; ``0`` when either is absent.

    Never negative. Entries are written in order, but a clock step during a
    turn should not make a duration read as though the result preceded the
    call.
    """
    first = _parse_iso(start)
    last = _parse_iso(end)
    if first is None or last is None:
        return 0
    return max(0, int((last - first) * 1000))


def _iso_from_epoch_ms(value: Any) -> str:
    """Epoch milliseconds → ISO 8601, or ``""`` when there is no time."""
    if not isinstance(value, int) or value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
