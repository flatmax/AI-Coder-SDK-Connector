"""The message pump's translation layer.

The one component that knows SDK message types. Everything downstream —
the service, the transport, the browser — sees AC-DC events only, so an
SDK upgrade that renames a message class touches this file and nothing
else (``specs5/3-engine/session.md`` § Message Taxonomy → UI).

Deliberately pure: :class:`TurnTranslator` does no I/O and holds no
client reference, so the whole taxonomy can be tested by feeding it
constructed SDK objects. :mod:`ac_dc.claude_code.session` owns the async
iteration; this owns the meaning.

Three properties are easy to get wrong and are therefore spelled out:

- **Nothing is dropped.** An unknown system subtype or an unknown content
  block goes to a generic path. A CLI upgrade that adds a block kind must
  degrade to "shown but not specially styled", never to silence.
- **Chunks are cumulative within a block, not across the turn.** The
  native engine re-sent the whole turn on every token, which made drops
  harmless but is quadratic on long agentic turns. Each event now carries
  a block identity and that block's content so far.
- **Subclasses before superclass.** Six SDK message types subclass
  ``SystemMessage`` (the four ``Task*Message``, ``HookEventMessage``,
  ``MirrorErrorMessage``). Dispatching on ``SystemMessage`` first would
  swallow all of them.

Reference: ``specs-reference/3-engine/session.md`` § Schemas — the event
payload shapes below are that document's, field for field.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numeric constants (specs-reference/3-engine/session.md § Numeric constants)
# ---------------------------------------------------------------------------

# Tool card header: one line, enough to recognise the call.
TOOL_INPUT_SUMMARY_CHARS = 200

# Tool result body: whichever limit is hit first. `truncated` and
# `full_bytes` accompany the preview so the UI can offer "show all".
TOOL_RESULT_PREVIEW_CHARS = 4000
TOOL_RESULT_PREVIEW_LINES = 120

# Terminal reasons that mean the user (or a budget) stopped the turn
# rather than the model finishing it.
CANCELLED_TERMINAL_REASONS = frozenset({"aborted_streaming", "aborted_tools"})

# Tool name → the input key naming the file it writes. Used to attribute
# file changes to a tool result before the PostToolUse hook lands in a
# later phase; the hook is authoritative once it exists.
_FILE_WRITING_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One server-push call to ``AcApp.<name>`` on every browser.

    ``turn_scoped`` events take the originating request ID as their first
    argument; the rest are session-wide and take only the payload.
    """

    name: str
    payload: Any
    turn_scoped: bool = True


# ---------------------------------------------------------------------------
# Internal per-turn bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """A rendered block in the transcript, and its emission state."""

    block_id: str
    kind: str  # "text" | "thinking" | "tool" | "system"
    # Highest seq emitted for this block; -1 until the first chunk, so the
    # first one the browser sees is 0.
    seq: int = -1
    content: str = ""
    done: bool = False
    # The tool card, for kind == "tool". Held here so a reconnecting
    # client can re-render the card from the block list alone.
    tool: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """The ``RenderedBlock`` replay shape for a reconnecting client."""
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "seq": max(self.seq, 0),
            "content": self.content,
            "tool": self.tool,
        }


@dataclass
class _ToolCall:
    """A tool call awaiting its result."""

    tool_use_id: str
    name: str
    input: dict[str, Any]
    started_at: float
    agent_id: str | None = None
    resolved: bool = False


@dataclass
class TurnStats:
    """Pump-local accounting that no SDK message carries on its own."""

    tool_calls: int = 0
    permission_prompts: int = 0
    files_modified: list[str] = field(default_factory=list)
    mirror_gap: bool = False


class TurnTranslator:
    """Translates one turn's SDK messages into AC-DC events.

    One instance per turn. Feed it every message from
    ``receive_response()`` in arrival order and emit whatever it returns.

    Parameters
    ----------
    request_id:
        The browser's request ID for this turn. Every turn-scoped event
        carries it, and it prefixes every content block's identity.
    clock:
        Monotonic clock, injectable so tool-duration assertions in tests
        do not depend on wall time.
    """

    def __init__(
        self, request_id: str, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.request_id = request_id
        self._clock = clock

        self.session_id: str | None = None
        self.cancelled = False
        self.complete = False
        self.stats = TurnStats()

        # block_id → block. Ordered by insertion, which is arrival order,
        # which is the order the transcript renders in.
        self._blocks: dict[str, _Block] = {}
        self._block_counter = 0

        # (agent scope, streaming message id, content index) → block_id.
        # Keyed this way rather than on StreamEvent.uuid because that uuid
        # identifies the *event*, not the message it belongs to.
        self._partial_blocks: dict[tuple[str, str, int], str] = {}
        # agent scope → the message id currently streaming in that scope.
        self._streaming_message: dict[str, str] = {}

        self._tools: dict[str, _ToolCall] = {}
        # tool_use_ids a permission dialog was shown for. Written by
        # note_permission_prompt, read when the card is built.
        self._gated: set[str] = set()
        self.user_message_id: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def translate(self, message: Any) -> list[Event]:
        """Translate one SDK message into zero or more events.

        Never raises for an unrecognised message: an exception here would
        abort the pump mid-turn, and the SDK's message set grows.
        """
        try:
            return self._dispatch(message)
        except Exception:
            logger.exception(
                "Failed to translate %s for request %s; continuing",
                type(message).__name__,
                self.request_id,
            )
            return []

    def note_permission_prompt(self, tool_use_id: str | None = None) -> None:
        """Record that a permission dialog was shown during this turn.

        Called by the permission layer rather than derived from a
        message, because a prompt is a thing AC-DC did, not a thing the
        engine reported. Feeds the click-through metric in R-12.

        With a ``tool_use_id``, the tool card for that call is marked
        ``gated`` in place, so a client that reconnects after the dialog
        was answered still sees which calls were asked about. No event is
        emitted: the card's own ``toolUse`` event has already gone out and
        the live signal is the dialog itself.
        """
        self.stats.permission_prompts += 1
        if not tool_use_id:
            return
        # Recorded either way: the control request can arrive before the
        # assistant message that carries the card, in which case the card
        # is born gated instead of being patched afterwards.
        self._gated.add(tool_use_id)
        block = self._blocks.get(tool_use_id)
        if block is not None and block.tool is not None:
            block.tool = {**block.tool, "gated": True}

    def rendered_blocks(self) -> list[dict[str, Any]]:
        """Replay payload for a client that reconnects mid-turn."""
        return [b.to_dict() for b in self._blocks.values()]

    def response_text(self) -> str:
        """Assistant text for this turn, blocks concatenated in order."""
        return "".join(b.content for b in self._blocks.values() if b.kind == "text")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, message: Any) -> list[Event]:
        from claude_agent_sdk import (
            AssistantMessage,
            ConversationResetMessage,
            HookEventMessage,
            MirrorErrorMessage,
            RateLimitEvent,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
            TaskUpdatedMessage,
            UserMessage,
        )

        # SystemMessage subclasses first — see the module docstring.
        if isinstance(message, TaskStartedMessage):
            return self._task_event("started", message)
        if isinstance(message, TaskProgressMessage):
            return self._task_event("progress", message)
        if isinstance(message, TaskUpdatedMessage):
            return self._task_event("updated", message)
        if isinstance(message, TaskNotificationMessage):
            return self._task_event("notification", message)
        if isinstance(message, HookEventMessage):
            return self._hook_event(message)
        if isinstance(message, MirrorErrorMessage):
            return self._mirror_error(message)

        if isinstance(message, SystemMessage):
            return self._system_message(message)
        if isinstance(message, AssistantMessage):
            return self._assistant_message(message)
        if isinstance(message, UserMessage):
            return self._user_message(message)
        if isinstance(message, StreamEvent):
            return self._stream_event(message)
        if isinstance(message, RateLimitEvent):
            return self._rate_limit(message)
        if isinstance(message, ResultMessage):
            return self._result(message)
        if isinstance(message, ConversationResetMessage):
            # Not in the spec's taxonomy — present in the SDK's Message
            # union, so it can arrive. The engine reset the conversation
            # underneath us, which is worth showing rather than dropping.
            return [
                Event(
                    "systemEvent",
                    {
                        "subtype": "conversation_reset",
                        "data": {
                            "new_conversation_id": getattr(
                                message, "new_conversation_id", None
                            )
                        },
                    },
                )
            ]

        logger.warning(
            "Unknown SDK message type %s; routed to the generic channel",
            type(message).__name__,
        )
        return [
            Event(
                "systemEvent",
                {"subtype": "unknown_message", "data": {"type": type(message).__name__}},
            )
        ]

    # ------------------------------------------------------------------
    # System messages
    # ------------------------------------------------------------------

    def _system_message(self, message: Any) -> list[Event]:
        subtype = getattr(message, "subtype", "") or ""
        data = getattr(message, "data", None) or {}

        if subtype == "init":
            # A CLI-owned dict, not a typed SDK object: read with .get()
            # and keep the original under `raw` for the debug view.
            self.session_id = data.get("session_id") or self.session_id
            return [
                Event(
                    "sessionStarted",
                    {
                        "session_id": data.get("session_id"),
                        "model": data.get("model"),
                        "cwd": data.get("cwd"),
                        "tools": data.get("tools") or [],
                        "mcp_servers": data.get("mcp_servers") or [],
                        "slash_commands": data.get("slash_commands") or [],
                        "permission_mode": data.get("permissionMode")
                        or data.get("permission_mode"),
                        "raw": data,
                    },
                )
            ]

        if subtype == "compact_boundary":
            # There is no CompactBoundary class in the SDK; the subtype
            # falls through to a generic SystemMessage, so the payload is
            # untyped and read defensively. The CLI nests it under
            # compact_metadata; older shapes put it at the top level.
            meta = data.get("compact_metadata") or data
            return [
                Event(
                    "compactionEvent",
                    {
                        "stage": "compact_boundary",
                        "pre_tokens": meta.get("pre_tokens"),
                        "post_tokens": meta.get("post_tokens"),
                        "trigger": meta.get("trigger"),
                        "raw": data,
                    },
                )
            ]

        logger.debug("Generic system message subtype=%s", subtype)
        return [Event("systemEvent", {"subtype": subtype, "data": data})]

    def _hook_event(self, message: Any) -> list[Event]:
        data = getattr(message, "data", None) or {}
        return [
            Event(
                "hookEvent",
                {
                    "phase": getattr(message, "subtype", None),
                    "hook_event_name": getattr(message, "hook_event_name", None)
                    or data.get("hook_event_name"),
                    "tool_name": data.get("tool_name"),
                    "outcome": data.get("outcome"),
                    "exit_code": data.get("exit_code"),
                    "raw": data,
                },
            )
        ]

    def _mirror_error(self, message: Any) -> list[Event]:
        """A ``SessionStore.append`` batch failed: the mirror has a gap.

        Non-fatal — the turn continues, and the engine's own transcript is
        unaffected. The service folds this into its ``EngineHealth``
        record before broadcasting, which is why the payload is partial
        and the event is not turn-scoped.
        """
        self.stats.mirror_gap = True
        key = getattr(message, "key", None)
        logger.warning(
            "Mirrored transcript gap for request %s: %s",
            self.request_id,
            getattr(message, "error", ""),
        )
        return [
            Event(
                "engineHealth",
                {
                    "mirror_gap": True,
                    "error": getattr(message, "error", ""),
                    "key": list(key) if isinstance(key, (list, tuple)) else key,
                },
                turn_scoped=False,
            )
        ]

    def _task_event(self, kind: str, message: Any) -> list[Event]:
        """One of the four ``Task*Message`` types → ``subagentEvent``."""
        from claude_agent_sdk import TERMINAL_TASK_STATUSES

        data = getattr(message, "data", None) or {}
        patch = getattr(message, "patch", None) or {}
        # `updated` carries its status inside the patch; the dataclass
        # hoists it to .status but only when present in the patch.
        status = getattr(message, "status", None) or patch.get("status")
        usage = getattr(message, "usage", None)
        return [
            Event(
                "subagentEvent",
                {
                    "type": kind,
                    "task_id": getattr(message, "task_id", None),
                    # A task's agent_id is the transcript key; the CLI
                    # reports it in the payload rather than the dataclass.
                    "agent_id": data.get("agent_id") or patch.get("agent_id"),
                    "tool_use_id": getattr(message, "tool_use_id", None),
                    "description": getattr(message, "description", None)
                    or patch.get("description")
                    or "",
                    "task_type": getattr(message, "task_type", None),
                    "status": status,
                    "last_tool_name": getattr(message, "last_tool_name", None),
                    "usage": dict(usage) if usage else None,
                    "summary": getattr(message, "summary", None),
                    "output_file": getattr(message, "output_file", None),
                    # A task can reach a terminal status via `updated`
                    # with no `notification` at all — stop_task() reports
                    # status="killed" that way — so the tab's activity LED
                    # keys on this flag from either message.
                    "terminal": bool(status) and status in TERMINAL_TASK_STATUSES,
                },
            )
        ]

    def _rate_limit(self, message: Any) -> list[Event]:
        info = getattr(message, "rate_limit_info", None)
        return [
            Event(
                "rateLimit",
                {
                    "status": getattr(info, "status", None),
                    "rate_limit_type": getattr(info, "rate_limit_type", None),
                    # Unix seconds, not ISO and not milliseconds.
                    "resets_at": getattr(info, "resets_at", None),
                    "utilization": getattr(info, "utilization", None),
                    "overage_status": getattr(info, "overage_status", None),
                    "overage_resets_at": getattr(info, "overage_resets_at", None),
                    "overage_disabled_reason": getattr(
                        info, "overage_disabled_reason", None
                    ),
                    "raw": getattr(info, "raw", None) or {},
                },
            )
        ]

    # ------------------------------------------------------------------
    # Assistant and user messages
    # ------------------------------------------------------------------

    def _assistant_message(self, message: Any) -> list[Event]:
        """A completed assistant message: finalise each of its blocks.

        Arrives after the partial stream for the same message, so blocks
        are matched back to the identities the partials already used —
        the browser must not render a second copy of text it animated.
        The completed content is authoritative: a dropped delta is
        corrected here rather than persisting as a hole.
        """
        from claude_agent_sdk import (
            ServerToolResultBlock,
            ServerToolUseBlock,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        scope = getattr(message, "parent_tool_use_id", None) or ""
        message_key = getattr(message, "message_id", None) or getattr(
            message, "uuid", None
        )
        events: list[Event] = []

        for index, block in enumerate(getattr(message, "content", None) or []):
            if isinstance(block, TextBlock):
                events.extend(
                    self._finalise_text_block(
                        scope, message_key, index, "text", block.text
                    )
                )
            elif isinstance(block, ThinkingBlock):
                events.extend(
                    self._finalise_text_block(
                        scope, message_key, index, "thinking", block.thinking
                    )
                )
            elif isinstance(block, ToolUseBlock):
                events.extend(self._tool_use(block.id, block.name, block.input, scope))
            elif isinstance(block, ServerToolUseBlock):
                # A tool the API ran server-side (web_search, advisor, …).
                # Same card, no permission gate, no MCP server.
                events.extend(
                    self._tool_use(
                        block.id, block.name, block.input, scope, server_tool=True
                    )
                )
            elif isinstance(block, ToolResultBlock):
                events.extend(
                    self._tool_result(
                        block.tool_use_id, block.content, bool(block.is_error)
                    )
                )
            elif isinstance(block, ServerToolResultBlock):
                events.extend(
                    self._tool_result(block.tool_use_id, block.content, False)
                )
            else:
                events.extend(self._unknown_block(scope, message_key, index, block))

        error = getattr(message, "error", None)
        if error:
            # An API-level failure the assistant message reports directly
            # (authentication_failed, billing_error, …). The result
            # message will also carry it, but the transcript should show
            # where in the turn it happened.
            events.append(
                Event(
                    "systemEvent",
                    {
                        "subtype": "assistant_error",
                        "data": {"error": error, "model": getattr(message, "model", None)},
                    },
                )
            )
        return events

    def _user_message(self, message: Any) -> list[Event]:
        """Tool results, plus the replay of the user's own message.

        ``--replay-user-messages`` (the mandatory partner of file
        checkpointing) echoes each user message back with the ID that
        ``rewind_files()`` takes. That echo must not be rendered — the
        browser drew the user's message before the turn started.
        """
        from claude_agent_sdk import ToolResultBlock

        origin = getattr(message, "origin", None) or {}
        content = getattr(message, "content", None)

        if isinstance(content, str) or (origin or {}).get("kind") == "human":
            self._capture_user_message_id(message)
            return []

        events: list[Event] = []
        for block in content or []:
            if isinstance(block, ToolResultBlock):
                events.extend(
                    self._tool_result(
                        block.tool_use_id, block.content, bool(block.is_error)
                    )
                )
            else:
                # A non-result block in a user message is the replay of a
                # multimodal user turn (text plus images). Not rendered,
                # but its ID is the rewind checkpoint.
                self._capture_user_message_id(message)
        return events

    def _capture_user_message_id(self, message: Any) -> None:
        """Remember the first replayed user-message ID of the turn."""
        if self.user_message_id is None:
            uuid = getattr(message, "uuid", None)
            if uuid:
                self.user_message_id = uuid

    # ------------------------------------------------------------------
    # Streaming partials
    # ------------------------------------------------------------------

    def _stream_event(self, message: Any) -> list[Event]:
        """Token-level deltas → cumulative per-block chunks."""
        event = getattr(message, "event", None) or {}
        event_type = event.get("type")
        scope = getattr(message, "parent_tool_use_id", None) or ""

        if event_type == "message_start":
            msg_id = ((event.get("message") or {}).get("id")) or ""
            self._streaming_message[scope] = msg_id
            return []

        if event_type == "content_block_start":
            index = event.get("index")
            kind = ((event.get("content_block") or {}).get("type")) or "text"
            if index is None:
                return []
            # Opening the block here (rather than on first delta) is what
            # makes a tool-use or empty block appear in arrival order.
            self._partial_block(scope, index, _stream_kind(kind))
            return []

        if event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta") or {}
            text = delta.get("text") or delta.get("thinking") or ""
            if index is None or not text:
                # input_json_delta streams a tool's arguments; the card
                # is emitted with the complete input from the assistant
                # message, so partial JSON has nowhere useful to go.
                return []
            kind = "thinking" if delta.get("type") == "thinking_delta" else "text"
            block = self._partial_block(scope, index, kind)
            block.content += text
            return [self._chunk_event(block)]

        if event_type == "content_block_stop":
            index = event.get("index")
            if index is None:
                return []
            key = (scope, self._streaming_message.get(scope, ""), index)
            block_id = self._partial_blocks.get(key)
            if block_id is None:
                return []
            block = self._blocks[block_id]
            if block.done:
                return []
            block.done = True
            return [self._chunk_event(block)]

        # message_delta / message_stop / anything new: protocol framing
        # with no transcript meaning.
        return []

    def _partial_block(self, scope: str, index: int, kind: str) -> _Block:
        """Find or open the block for a streaming ``(scope, index)``."""
        key = (scope, self._streaming_message.get(scope, ""), index)
        block_id = self._partial_blocks.get(key)
        if block_id is None:
            block = self._open_block(kind)
            self._partial_blocks[key] = block.block_id
            return block
        return self._blocks[block_id]

    # ------------------------------------------------------------------
    # Block finalisation
    # ------------------------------------------------------------------

    def _finalise_text_block(
        self,
        scope: str,
        message_key: str | None,
        index: int,
        kind: str,
        content: str,
    ) -> list[Event]:
        key = (scope, message_key or self._streaming_message.get(scope, ""), index)
        block_id = self._partial_blocks.get(key)
        if block_id is None:
            # No partials for this block — either streaming is off or the
            # block never produced a delta. Open it now.
            block = self._open_block(kind)
            self._partial_blocks[key] = block.block_id
        else:
            block = self._blocks[block_id]

        already_final = block.done and block.content == content
        block.content = content
        block.done = True
        if already_final:
            return []
        return [self._chunk_event(block)]

    def _unknown_block(
        self, scope: str, message_key: str | None, index: int, block: Any
    ) -> list[Event]:
        """Render a block kind this build does not know about.

        Shown, not styled, and never silent: an SDK that grows a seventh
        block type should degrade to a visible JSON blob rather than
        losing part of the assistant's answer.
        """
        kind_name = type(block).__name__
        logger.warning("Unknown assistant content block %s; rendering generically", kind_name)
        try:
            body = json.dumps(_block_fields(block), indent=2, default=str)
        except Exception:
            body = repr(block)
        return self._finalise_text_block(
            scope,
            message_key,
            index,
            "text",
            f"```json\n// unrecognised content block: {kind_name}\n{body}\n```",
        )

    def _open_block(self, kind: str) -> _Block:
        """Assign a new block identity: ``{request_id}:b{n}``."""
        block = _Block(block_id=f"{self.request_id}:b{self._block_counter}", kind=kind)
        self._block_counter += 1
        self._blocks[block.block_id] = block
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
            },
        )

    # ------------------------------------------------------------------
    # Tool cards
    # ------------------------------------------------------------------

    def _tool_use(
        self,
        tool_use_id: str,
        name: str,
        tool_input: dict[str, Any],
        scope: str,
        *,
        server_tool: bool = False,
    ) -> list[Event]:
        if tool_use_id in self._tools:
            # The same assistant message can be re-delivered; one card per
            # call, keyed by the SDK's own id.
            return []
        self._tools[tool_use_id] = _ToolCall(
            tool_use_id=tool_use_id,
            name=name,
            input=tool_input or {},
            started_at=self._clock(),
            agent_id=scope or None,
        )
        self.stats.tool_calls += 1
        card = {
            "tool_use_id": tool_use_id,
            "name": name,
            "server": mcp_server_name(name),
            "input_summary": summarise_tool_input(tool_input),
            "input": tool_input or {},
            "status": "pending",
            # True when the permission layer showed a dialog for this call.
            # Usually set by note_permission_prompt after the card exists;
            # true here when the control request beat the message carrying it.
            "gated": tool_use_id in self._gated,
            "agent_id": scope or None,
            "server_tool": server_tool,
        }
        # Tool cards use the SDK's tool_use_id as their block identity,
        # because the result references it — no correlation table needed
        # on either side.
        self._blocks[tool_use_id] = _Block(
            block_id=tool_use_id, kind="tool", done=False, tool=card
        )
        return [Event("toolUse", card)]

    def _tool_result(
        self, tool_use_id: str, content: Any, is_error: bool
    ) -> list[Event]:
        call = self._tools.get(tool_use_id)
        if call is not None:
            if call.resolved:
                return []
            call.resolved = True
            duration_ms = int((self._clock() - call.started_at) * 1000)
        else:
            # A result for a call we never saw. Emit it anyway — the
            # browser can attach it by id — but say so, because it means
            # the pump missed an assistant message.
            logger.warning(
                "Tool result for unknown tool_use_id %s in request %s",
                tool_use_id,
                self.request_id,
            )
            duration_ms = 0

        text = flatten_tool_result(content)
        preview, truncated = truncate_tool_result(text)
        files = _files_modified(call, is_error)
        for path in files:
            if path not in self.stats.files_modified:
                self.stats.files_modified.append(path)

        payload = {
            "tool_use_id": tool_use_id,
            "status": "error" if is_error else "ok",
            "preview": preview,
            "truncated": truncated,
            "full_bytes": len(text.encode("utf-8")),
            "duration_ms": duration_ms,
            "files_modified": files,
        }

        block = self._blocks.get(tool_use_id)
        if block is not None:
            block.done = True
            if block.tool is not None:
                # So a client that reconnects after the result renders the
                # card resolved rather than stuck on "pending".
                block.tool = {**block.tool, "status": payload["status"], "result": payload}

        return [Event("toolResult", payload)]

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def _result(self, message: Any) -> list[Event]:
        """``ResultMessage`` → ``streamComplete``: the turn footer."""
        self.complete = True
        self.session_id = getattr(message, "session_id", None) or self.session_id
        terminal_reason = getattr(message, "terminal_reason", None)
        cancelled = self.cancelled or (terminal_reason in CANCELLED_TERMINAL_REASONS)

        model_usage = getattr(message, "model_usage", None)
        result: dict[str, Any] = {
            "session_id": self.session_id,
            "response": self.response_text() or (getattr(message, "result", None) or ""),
            "subtype": getattr(message, "subtype", None),
            "terminal_reason": terminal_reason,
            "is_error": bool(getattr(message, "is_error", False)),
            "num_turns": getattr(message, "num_turns", 0),
            "duration_ms": getattr(message, "duration_ms", 0),
            "duration_api_ms": getattr(message, "duration_api_ms", 0),
            # Per-turn, unlike the two fields below — and main-agent-loop
            # only, so it excludes the subagents that make a turn expensive.
            "usage": getattr(message, "usage", None),
            # Per-model, camelCase keys, passed through as the SDK gives
            # them: a turn that used a subagent on a cheaper model reports
            # both models here. **Cumulative across the session**, not this
            # turn's — the CLI's own schema says so, and
            # `EngineSession._price_turn` adds the per-turn difference beside
            # it rather than reinterpreting this field.
            "model_usage": {k: dict(v) for k, v in (model_usage or {}).items()} or None,
            # Also cumulative: the session's running estimate, not this
            # turn's cost. See ac_dc.claude_code.cost.
            "total_cost_usd": getattr(message, "total_cost_usd", None),
            "tool_calls": self.stats.tool_calls,
            "permission_prompts": self.stats.permission_prompts,
            "files_modified": list(self.stats.files_modified),
            "cancelled": cancelled,
            "mirror_gap": self.stats.mirror_gap,
            # Ours, not the SDK's: the ID rewind_files() takes, so the UI
            # can offer undo on this turn's user message.
            "user_message_id": self.user_message_id,
        }
        for name in ("permission_denials", "deferred_tool_use", "api_error_status", "errors"):
            value = getattr(message, name, None)
            if value:
                result[name] = _plain(value)
        if result.get("deferred_tool_use"):
            # Only a PreToolUse hook returning "defer" produces this, and
            # AC-DC's hooks are strictly observational, so it means a hook
            # started making decisions.
            logger.warning(
                "Result carries deferred_tool_use; a hook returned a permission "
                "decision, which shadows can_use_tool"
            )
        return [Event("streamComplete", result)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream_kind(content_block_type: str) -> str:
    """Map a streaming ``content_block.type`` to a rendered block kind."""
    if content_block_type == "thinking":
        return "thinking"
    if content_block_type in ("tool_use", "server_tool_use"):
        return "tool"
    return "text"


def mcp_server_name(tool_name: str) -> str | None:
    """Extract the MCP server from an ``mcp__<server>__<tool>`` name."""
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    return parts[1] if len(parts) >= 3 else None


def summarise_tool_input(tool_input: dict[str, Any] | None) -> str:
    """One-line, length-capped rendering of a tool's input."""
    if not tool_input:
        return ""
    parts = []
    for key, value in tool_input.items():
        rendered = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={rendered}")
    summary = " ".join(" ".join(str(p).split()) for p in parts)
    if len(summary) > TOOL_INPUT_SUMMARY_CHARS:
        return summary[: TOOL_INPUT_SUMMARY_CHARS - 1] + "…"
    return summary


def truncate_tool_result(text: str) -> tuple[str, bool]:
    """Cap a tool result at the character or line limit, whichever first."""
    lines = text.splitlines()
    truncated = False
    if len(lines) > TOOL_RESULT_PREVIEW_LINES:
        text = "\n".join(lines[:TOOL_RESULT_PREVIEW_LINES])
        truncated = True
    if len(text) > TOOL_RESULT_PREVIEW_CHARS:
        text = text[:TOOL_RESULT_PREVIEW_CHARS]
        truncated = True
    return text, truncated


def flatten_tool_result(content: Any) -> str:
    """Flatten a tool result's content to text for previewing.

    Public because history rendering flattens the same shape read back off
    disk. Two implementations of this would show a user a different preview
    for a live result and for the same result reopened tomorrow.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text") or json.dumps(content, default=str)
    return str(content)


def files_written_by(tool_name: str, tool_input: dict[str, Any] | None) -> list[str]:
    """Paths a tool call writes, deduced from the call's own input.

    The one home for the tool-name → path-key table. History rendering has
    to attribute files the same way the live path does, and the narrow name
    is the honest one: this sees only the four file tools, so a file changed
    by ``Bash`` is not in the answer (CC-18).

    Attribution from the *input* is itself a stopgap — the ``PostToolUse``
    hook reports what was actually written and supersedes this once it
    lands — but it must be the same stopgap in both directions.
    """
    key = _FILE_WRITING_TOOLS.get(tool_name)
    if key is None or not tool_input:
        return []
    path = tool_input.get(key)
    return [path] if isinstance(path, str) and path else []


def _files_modified(call: _ToolCall | None, is_error: bool) -> list[str]:
    """:func:`files_written_by` for a live call, which may have failed."""
    if call is None or is_error:
        return []
    return files_written_by(call.name, call.input)


def _block_fields(block: Any) -> dict[str, Any]:
    """Best-effort field dict for an unrecognised content block."""
    import dataclasses

    if dataclasses.is_dataclass(block):
        return dataclasses.asdict(block)
    if isinstance(block, dict):
        return block
    return {
        name: getattr(block, name)
        for name in dir(block)
        if not name.startswith("_") and not callable(getattr(block, name, None))
    }


def _plain(value: Any) -> Any:
    """Coerce an SDK dataclass into something JSON-serialisable."""
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
