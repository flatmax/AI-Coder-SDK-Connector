"""The host end of the gate — where a hook's question becomes a dialog.

:mod:`~aic_dc.antigravity.agy.hook` runs inside ``agy``'s process tree and
knows nothing about permissions; it decides *whose* call this is and then
asks. This is what it asks. One unix socket per session, one connection
per tool call, newline-delimited JSON each way.

It owns almost nothing
======================
The queue, the countdown, the localhost rule, the dialog payload and the
diff are all the shared ``PermissionBroker``'s — the *same* broker the
Claude engine and the SDK transport drive, reached through
:class:`~aic_dc.antigravity.permissions.AntigravityPermissionGate`. So a
permission request raised by ``agy`` appears in the same ``pending()``
list, resolves through the same ``resolve()``, and renders in the same
dialog as one raised by any other engine. That is
``specs5/3-engine/permissions.md``'s *one ask path*, held across a third
transport.

What is genuinely this module's is three translations and one lifecycle:
the socket, the registry claim that makes the hook recognise us, and the
conversion between ``agy``'s hook JSON and the broker's Python.

The amend path survives the trip
================================
``PermissionResult.updated_input`` — the user editing a proposed command
before allowing it — maps onto ``agy``'s ``overwrite``, described in its
own documentation as *"merged into the tool call's arguments before it
runs … the modified tool call is what actually executes and is recorded"*.
That is the capability [AG-5](../../../../specs5/plan-ag/decisions.md#ag-5)
chose the raw hook over ``policy.ask_user`` to keep, and it is available
here for the same reason it is on the SDK path.

Arguments go back **denormalised**, in ``agy``'s own CamelCase, because
``overwrite`` is read by the Go side. Sending the dialog's ``file_path``
would merge a key ``agy`` does not know beside the one it does, leaving
the original value in place — an amend that silently does nothing.

There is no call id, so one is made
===================================
The hook payload carries ``conversationId``, ``modelName``, ``stepIdx``,
``toolCall``, ``transcriptPath``, ``workspacePaths`` and
``artifactDirectoryPath`` — and **no tool-call id**. The raw protobuf has
a ``callId``; the hook's JSON does not. The broker needs a stable
identifier per call, so one is composed from the conversation and the step
index, which is unique within a conversation and stable across a retry of
the same step.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5; and
``specs5/3-engine/permissions.md``, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aic_dc.antigravity.agy import registry
from aic_dc.antigravity.permissions import (
    AntigravityPermissionGate,
    denormalise_args,
)

logger = logging.getLogger(__name__)

#: Refused when the payload names no tool. Deny rather than allow: a call
#: we cannot describe is one the dialog cannot show, and allowing it would
#: be consent nobody gave.
UNREADABLE = {
    "decision": "deny",
    "reason": (
        "AIC-DC could not read this tool call well enough to show it for "
        "review, so it was refused. This is an AIC-DC fault, not a refusal "
        "by the user."
    ),
}


class _AgyContext:
    """The attributes the shared broker reads, from an ``agy`` payload.

    A translation object rather than a dict, because the broker reaches for
    these with ``getattr(..., default)`` — so every field Claude has and
    ``agy`` does not degrades to ``None`` on its own rather than needing a
    stub here. Mirrors ``permissions._HookContext``, which does the same
    job for the SDK transport.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        conversation = str(payload.get("conversationId") or "")
        step = payload.get("stepIdx")
        # See the module docstring: agy's hook JSON carries no call id, so
        # one is composed. Unique within a conversation, and stable if the
        # same step is retried.
        self.tool_use_id = f"{conversation}:{step}" if conversation else ""
        self.agent_id = None
        self.suggestions = None
        self.blocked_path = None
        self.decision_reason = None
        self.title = None
        self.display_name = None
        self.description = None


class AgyGateServer:
    """A unix socket that turns ``agy``'s hook calls into dialogs.

    One per session. :meth:`claim` publishes the socket against a
    conversation id so :mod:`~aic_dc.antigravity.agy.hook` will recognise
    calls belonging to it — and, just as importantly, will keep passing
    through every conversation this host has *not* claimed.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        gate: AntigravityPermissionGate,
        config_dir: Path | str | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._gate = gate
        self._config_dir = config_dir
        self._server: Any = None
        self._claimed: str | None = None
        self._refusal: str | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def refuse_all(self, reason: str) -> None:
        """Refuse every subsequent call without asking. This is ⏹.

        There is no halt frame on this transport
        (:mod:`~aic_dc.antigravity.agy.session`), so stopping a turn means
        starving it: the agent asks for a tool, is refused with a reason
        naming the user's stop, and winds down. The same mechanism the
        Claude adapter leans on, where ``cancel_streaming`` denies the
        turn's open permissions *before* interrupting because a released
        dialog is what makes the interrupt actionable.

        Deliberately **not** a dialog. The user has already answered this
        question by pressing stop, and putting it to them again per tool
        call would be the opposite of cancelling.
        """
        self._refusal = reason

    def resume(self) -> None:
        """Stop refusing. Called when a new turn starts, never mid-turn.

        A stop applies to the turn it was pressed during: carrying it into
        the next one would make ⏹ a mode rather than an action, and the
        user would find their next turn refusing everything for no visible
        reason.
        """
        self._refusal = None

    async def start(self) -> None:
        """Listen. Safe to call once; a second call is a no-op."""
        if self._server is not None:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A socket file left by a killed process would make bind fail, and
        # the failure is at session start where it reads as "the engine
        # will not run" rather than as stale state.
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._socket_path)
        )

    def claim(self, conversation_id: str) -> None:
        """Take ownership of a conversation, so the hook stops passing it through.

        Called between ``init`` and the first prompt, which is the only
        window there is: the id is unknown before ``init``, and a tool call
        cannot precede the first prompt. Claiming late would wave the first
        tool call through as somebody else's session.
        """
        registry.claim(
            conversation_id, self._socket_path, config_dir=self._config_dir
        )
        self._claimed = conversation_id

    async def stop(self) -> None:
        """Release the conversation, then close. Never raises.

        Release comes first and deliberately: while the registry entry
        stands, the hook denies calls it cannot get an answer for, so a
        teardown that closed the socket first would refuse any tool call
        racing the shutdown. Releasing first makes those pass through as
        unowned, which is what they are once this host has let go.
        """
        self._refusal = None
        if self._claimed:
            registry.release(self._claimed, config_dir=self._config_dir)
            self._claimed = None
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.exception("The agy gate server did not close cleanly")
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001
            logger.exception("Could not remove the agy gate socket")

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One tool call, from hook JSON to an ``agy`` decision.

        Public because it is the whole of this class's behaviour and is
        worth testing without a socket.
        """
        call = payload.get("toolCall")
        if not isinstance(call, dict):
            return dict(UNREADABLE)
        tool_name = str(call.get("name") or "")
        if not tool_name:
            return dict(UNREADABLE)
        args = call.get("args")
        args = dict(args) if isinstance(args, dict) else {}

        if self._refusal is not None:
            # Stopped. Answered without a dialog: the user already said so.
            return {"decision": "deny", "reason": self._refusal}

        # The narrowing that keeps reads out of the dialog, shared with the
        # SDK transport rather than reimplemented. Calling the broker
        # directly is what would raise a dialog for every read — the exact
        # defect fixed on the SDK path on 2026-09-03, and this is the third
        # transport that could have reintroduced it.
        verdict = self._gate.pre_verdict(tool_name, args)
        if verdict is not None:
            allow, message = verdict
            return {"decision": "allow"} if allow else {
                "decision": "deny",
                "reason": message,
            }

        result = await self._gate.broker.can_use_tool(
            tool_name, args, _AgyContext(payload)
        )

        # Only a denial carries a message — the same duck-typing the SDK
        # transport uses, and for the same reason: `can_use_tool` returns a
        # Claude result object from several places and naming the classes
        # would mean keeping a list of them in step.
        message = getattr(result, "message", None)
        if message is not None:
            return {"decision": "deny", "reason": str(message)}

        amended = getattr(result, "updated_input", None)
        if isinstance(amended, dict) and amended:
            # Back in agy's spelling: `overwrite` is read by the Go side,
            # and a key it does not know would merge beside the real one
            # and change nothing.
            return {
                "decision": "allow",
                "overwrite": denormalise_args(tool_name, amended),
            }
        return {"decision": "allow"}

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One connection, one call. Always answers, then closes.

        Any exception here would leave the hook waiting on a socket that
        never replies, which its own deadline eventually turns into a
        denial — correct, but an hour later and with no explanation. So
        this answers, and the answer on a fault is a refusal that says so.
        """
        try:
            line = await reader.readline()
            payload = json.loads(line.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            answer = await self.decide(payload)
        except Exception:  # noqa: BLE001 - the hook is waiting on us
            logger.exception("The agy gate server could not answer a hook call")
            answer = dict(UNREADABLE)
        try:
            writer.write(json.dumps(answer).encode("utf-8") + b"\n")
            await writer.drain()
        except Exception:  # noqa: BLE001 - the hook may have given up
            logger.debug("The agy hook went away before its answer arrived")
        finally:
            writer.close()
