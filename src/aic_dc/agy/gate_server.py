"""The host end of the gate — where a hook's question becomes a dialog.

:mod:`~aic_dc.agy.hook` runs inside ``agy``'s process tree and
knows nothing about permissions; it decides *whose* call this is and then
asks. This is what it asks. One unix socket per session, one connection
per tool call, newline-delimited JSON each way.

**Three events arrive on it, not one.** ``PreToolUse`` is the dialog and
everything below is about it; ``PostInvocation`` asks whether the loop
should end now, and ``Stop`` reports that it did (AG-19, AG-R-16). Those
two carry no tool and raise no dialog — they read the same latched stop
the gate reads, which is the whole reason they are here rather than in a
second socket with a second copy of that state.

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
That is the capability [AG-5](../../../specs5/plan-ag/decisions.md#ag-5)
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
import dataclasses
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from aic_dc.agy import hook, registry, scope
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

#: What ``agy`` puts in a ``Stop`` payload's ``terminationReason`` when a
#: hook ended the loop, rather than the model or a limit. Measured
#: 2026-09-10 beside ``NO_TOOL_CALL`` for an ordinary end; it is the word
#: that lets this host tell a loop **it** ended from one a stranger's hook
#: ended in the same merged hooks file (AG-R-16).
HOOK_TERMINATION = "TERMINAL_CUSTOM_HOOK"


@dataclasses.dataclass(frozen=True)
class StaticPolicy:
    """A fixed answer for every tool call, with no dialog in it.

    The consultant's posture (AG-16), and it is a *capability
    restriction* rather than a permission decision — the same distinction
    :mod:`aic_dc.antigravity.consultant` draws for the SDK transport,
    where a one-shot consultation runs with a static allowlist while the
    engine runs with the dialog.

    Two reasons a consultation cannot use the dialog, and the second is
    the one that makes this structural rather than a preference:

    - **It was already answered.** A consultation only happens inside a
      ``mcp__aic-dc-antigravity__*`` tool call, which reached the dialog by
      the ordinary path before any of this ran. Asking again, per tool the
      consultant's own agent reaches for, would put a second permission
      question in front of a user who has already said yes to the thing
      they can see.
    - **Nobody is watching the right window.** The Claude turn that asked
      is blocked on the tool result, so a dialog raised here interrupts a
      turn to ask about a call the user never made and cannot evaluate.

    ``allowed`` is therefore the whole policy: a name in it runs, anything
    else is denied with ``reason``, and the reason is prose the model
    reads — it is what steers a refused agent into answering rather than
    into looking for another route (AG-R-11's mechanism, used positively).
    """

    #: Tool names, in ``agy``'s own spelling, that may run without asking.
    allowed: frozenset[str]
    #: What the model is told when it reaches for anything else.
    reason: str

    @classmethod
    def of(cls, allowed: Iterable[str], reason: str) -> StaticPolicy:
        """Build one from any iterable of names."""
        return cls(allowed=frozenset(allowed), reason=reason)


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
    conversation id so :mod:`~aic_dc.agy.hook` will recognise
    calls belonging to it — and, just as importantly, will keep passing
    through every conversation this host has *not* claimed.
    """

    def __init__(
        self,
        socket_path: Path | str,
        *,
        gate: AntigravityPermissionGate | None = None,
        config_dir: Path | str | None = None,
        policy: StaticPolicy | None = None,
    ) -> None:
        if (gate is None) == (policy is None):
            # Refused at construction rather than at the first tool call.
            # A gate server with neither would answer every call by
            # raising inside `_handle`, which fails closed — but a session
            # whose every tool is refused for an internal reason is a
            # defect that reads as an unhelpful model, and one with *both*
            # would have two answers to one question.
            raise ValueError(
                "an agy gate server needs exactly one of `gate` (the "
                "dialog) or `policy` (a static allowlist); it was given "
                + ("both" if gate is not None else "neither")
            )
        self._socket_path = Path(socket_path)
        self._gate = gate
        self._policy = policy
        self._config_dir = config_dir
        self._server: Any = None
        # A set, not one id: this host owns its session's conversation and
        # its subagents' while they run. See `claim`.
        self._claimed: set[str] = set()
        self._refusal: str | None = None
        #: Conversations stopped one at a time — ⏹ on a subagent's row,
        #: where `_refusal` is ⏹ on the whole turn. Keyed by conversation
        #: id, valued by the reason the agent reads. Cleared by `resume`
        #: with its turn-wide sibling.
        self._refused_conversations: dict[str, str] = {}
        #: Conversations whose loop *this* server ended, by answering
        #: `terminate` to a `PostInvocation` (AG-19), and **how many times**.
        #: Recorded rather than inferred, and it answers three questions:
        #: the session reads it to badge a stopped turn honestly, `note_stop`
        #: reads it to tell our own termination from a stranger's hook ending
        #: our loop in the same merged file, and a count above one is a loop
        #: that came back after we ended it — see `decide_invocation`.
        #: Cleared by `resume`.
        self._terminations: dict[str, int] = {}
        #: The systemd scope `agy` will be launched into, or None where the
        #: platform has none. Named at construction rather than at `start`
        #: so `AgySession` can read it while assembling argv, and held here
        #: rather than on the session because the registry entry it backs
        #: maps the unit to *this server's socket* — the same pairing
        #: `claim` publishes, for the calls a claim cannot cover in time.
        self._scope_unit: str | None = (
            scope.unit_name() if scope.available() else None
        )
        #: The `agy` pid already written onto the scope entry, so the rewrite
        #: `claim` does happens once per session rather than once per
        #: subagent. `None` until the child exists — the scope claim is
        #: published before it does.
        self._scope_agy_pid: int | None = None

    @property
    def asks(self) -> bool:
        """Whether this gate can put a call to the user.

        ``False`` for a consultation's static allowlist. Read where the
        difference matters rather than inferred from the presence of a
        broker, so a caller cannot be wrong about which posture it built.
        """
        return self._gate is not None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def scope_unit(self) -> str | None:
        """The systemd scope to launch ``agy`` into, or ``None``.

        Read by :class:`~aic_dc.agy.session.AgySession` when it assembles
        argv. ``None`` is an ordinary answer, not a failure: it is every
        platform without a systemd user manager, and it means the session
        runs exactly as it did before AG-R-14 — routed by conversation
        claims, with that row's residues intact.
        """
        return self._scope_unit

    def refuse_conversation(self, conversation_id: str, reason: str) -> None:
        """Refuse one conversation's calls, leaving the rest of the turn.

        This is ⏹ **on a single subagent**, and it is the same starvation
        :meth:`refuse_all` performs, aimed. It can be aimed because the
        hook payload names the conversation every call comes from and this
        host claims a subagent's conversation as its announcement arrives
        (AG-R-14) — so by the time a user can see a row to press stop on,
        the id on that row is one the gate already recognises.

        **Three limits, and all three were known before this was written**
        (``src/aic_dc/capabilities.py``, ``subagent_stop``):

        - A subagent producing only **prose** never asks for a tool, so
          there is nothing to refuse and it runs to its end. The same limit
          :meth:`refuse_all` has for a whole turn, one level down.
        - A subagent's **own** subagent has a conversation id of its own,
          which an id-scoped refusal does not match. It is caught here
          anyway when its parent's next call is refused and the parent
          winds down, but not immediately, and not if it never reports
          back.
        - What ``agy`` then *reports* for the starved subagent is the
          stream's business, not this method's: it returns when the
          refusal is recorded, which is a fact about the gate rather than
          about the agent.

        Cleared by :meth:`resume` with the turn-wide refusal, for the same
        reason — a stop applies to the turn it was pressed during.
        """
        if not conversation_id:
            return
        self._refused_conversations[str(conversation_id)] = reason

    def is_refusing(self, conversation_id: str) -> bool:
        """Whether this conversation has been stopped. For the caller's report."""
        return str(conversation_id) in self._refused_conversations

    def refuse_all(self, reason: str) -> None:
        """Refuse every subsequent call without asking. This is ⏹.

        There is no halt frame on this transport
        (:mod:`~aic_dc.agy.session`), so stopping a turn means
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

        The per-conversation refusals go with it, and that matters more
        than it looks: a subagent's conversation id is `agy`'s own, so a
        refusal left standing would still be aimed at it if the user
        resumed that conversation later — the interception
        `registry.release` exists to prevent, arriving through a different
        door.
        """
        self._refusal = None
        self._refused_conversations.clear()
        self._terminations.clear()

    async def start(self) -> None:
        """Listen. Safe to call once; a second call is a no-op."""
        if self._server is not None:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A socket file left by a killed process would make bind fail, and
        # the failure is at session start where it reads as "the engine
        # will not run" rather than as stale state.
        self._socket_path.unlink(missing_ok=True)
        # The registry entries that same killed process left behind are the
        # same problem one layer up, and worth sweeping from here for the
        # same reason: this is the moment a host exists again to do it, and
        # a corpse entry costs the *user's* own sessions rather than ours
        # (`registry.owns_anything`). Only corpses go — a second host on
        # this machine has live entries in the same directory.
        registry.reap_stale(self._config_dir)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._socket_path)
        )
        # AG-R-14. Published *before* `agy` is launched, which is what makes
        # it a fix rather than a narrower race: a conversation claim can be
        # late because the conversation exists before we are told its id,
        # and a scope claim cannot, because the scope does not exist until
        # the session creates it. Everything the kernel later puts inside —
        # a subagent, a grandchild, a shell command's own child — is
        # covered by an entry that was already on disk.
        if self._scope_unit:
            registry.claim_scope(
                self._scope_unit, self._socket_path, config_dir=self._config_dir
            )

    def claim(self, conversation_id: str, *, agy_pid: int | None = None) -> None:
        """Take ownership of a conversation, so the hook stops passing it through.

        Called between ``init`` and the first prompt, which is the only
        window there is: the id is unknown before ``init``, and a tool call
        cannot precede the first prompt. Claiming late would wave the first
        tool call through as somebody else's session.

        **More than one, since 2026-09-09.** A session owns its own
        conversation and, while they run, its subagents' — each of which
        ``agy`` gives a conversation id of its own. One claim per host was
        not a simplification but a hole: the hook passes through every
        conversation nobody has claimed, so a subagent's every tool call
        went ungated past a dialog the user had approved only the *spawn*
        through. Measured by ``scripts/probe_agy_subagent_gate.py``.

        ``agy_pid`` is the process this conversation runs in, recorded so a
        later reader can tell an entry this host abandoned from one whose
        agent is still running without it. The caller supplies it because
        this class never spawns anything — see
        :func:`aic_dc.agy.registry.entry_is_live`.

        **It is also where the scope entry gets its second pid**, and this is
        the only place it can: :meth:`start` publishes that entry before the
        child exists, precisely so nothing can beat it onto disk, which
        leaves it naming a container and no process. Written once per
        session — every subagent carries the same pid, because they all run
        inside this session's one ``agy``.
        """
        if not conversation_id:
            return
        registry.claim(
            conversation_id,
            self._socket_path,
            config_dir=self._config_dir,
            agy_pid=agy_pid,
        )
        self._claimed.add(conversation_id)
        if (
            self._scope_unit
            and agy_pid is not None
            and agy_pid != self._scope_agy_pid
        ):
            registry.claim_scope(
                self._scope_unit,
                self._socket_path,
                config_dir=self._config_dir,
                agy_pid=agy_pid,
            )
            self._scope_agy_pid = agy_pid

    def release(self, conversation_id: str) -> None:
        """Give up one conversation, leaving the rest claimed.

        For a subagent that has finished. Releasing matters as much as
        claiming: a registry entry is a file that outlives the process, so
        a claim left behind would make this host intercept a *later*
        session of the user's own that happened to resume that
        conversation — the isolation property ``probe_agy_isolation.py``
        exists to hold.
        """
        if conversation_id not in self._claimed:
            return
        registry.release(conversation_id, config_dir=self._config_dir)
        self._claimed.discard(conversation_id)

    async def stop(self) -> None:
        """Release the conversation, then close. Never raises.

        Release comes first and deliberately: while the registry entry
        stands, the hook denies calls it cannot get an answer for, so a
        teardown that closed the socket first would refuse any tool call
        racing the shutdown. Releasing first makes those pass through as
        unowned, which is what they are once this host has let go.
        """
        self._refusal = None
        # Every claim, not just the session's own: a subagent still
        # running at teardown has one too, and leaving it behind is the
        # interception-after-the-fact this class's `release` warns about.
        for conversation_id in sorted(self._claimed):
            registry.release(conversation_id, config_dir=self._config_dir)
        self._claimed.clear()
        # And the scope, for the same reason and with more force: it is
        # matched on a unit name rather than on an id `agy` generated, so a
        # unit left behind would adopt any later process systemd happened
        # to place in a scope of that name.
        if self._scope_unit:
            registry.release_scope(self._scope_unit, config_dir=self._config_dir)
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

        # Stopped one subagent, rather than the turn. Checked *after* the
        # turn-wide refusal because a turn-wide stop subsumes it, and
        # before everything else for the same reason that one is: the user
        # has answered this question already and re-asking it per call
        # would be the opposite of cancelling.
        aimed = self._refused_conversations.get(
            str(payload.get("conversationId") or "")
        )
        if aimed is not None:
            logger.info(
                "Refusing %s: its conversation was stopped by the user", tool_name
            )
            return {"decision": "deny", "reason": aimed}

        if self._policy is not None:
            # A consultation (AG-16). Terminal on purpose: this does not
            # fall through to `pre_verdict`, because that path exists to
            # decide *which* calls reach the dialog and there is no dialog
            # here. A read is denied as firmly as a write — the SDK
            # consultant enables no tools at all for a second opinion, and
            # this is the nearest posture `agy` allows, since its tool set
            # is the binary's rather than ours to restrict.
            if tool_name in self._policy.allowed:
                return {"decision": "allow"}
            logger.debug("Consultation gate refused %s", tool_name)
            return {"decision": "deny", "reason": self._policy.reason}

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

    def was_terminated(self, conversation_id: str) -> bool:
        """Whether this server ended that conversation's loop. AG-19.

        Read by :class:`~aic_dc.agy.session.AgySession` when it closes a
        turn out, because a loop ended this way reports ``status:
        "SUCCESS"`` with empty prose — which, rendered as it arrives, is a
        completed answer that happens to say nothing. It is the *stop*
        having worked, and the footer has to say so.
        """
        return str(conversation_id) in self._terminations

    def was_revived(self, conversation_id: str) -> bool:
        """Whether a loop this server ended came back. AG-R-16.

        More than one termination for one conversation in one turn: the
        stop was answered, the loop ended, and something put it back. Read
        by :class:`~aic_dc.agy.session.AgySession` so that a turn which has
        outlasted the user's stop can say *why* rather than only *that*.
        """
        return self._terminations.get(str(conversation_id), 0) > 1

    def decide_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Whether this conversation's loop ends here. AG-19.

        ``PostInvocation``, and the answer is the latch:
        :meth:`refuse_all` for ⏹ on the turn, :meth:`refuse_conversation`
        for ⏹ on one subagent's row. **No new state and no dialog** — this
        is the same stop the gate has been refusing tool calls with since
        it was pressed, asked at the one moment ``agy`` will act on it
        mechanically rather than leave it to the agent's judgement.

        It is what makes the aimed stop reach a subagent that asks for
        nothing: :meth:`refuse_conversation` records three limits, the
        first of which is that a subagent producing only prose cannot be
        starved. Its loop still ends here, at the end of the invocation it
        is in.

        Returns ``{}`` for every conversation that was not stopped, which
        is nearly all of them — including a consultation's, whose gate has
        a static policy and no ⏹ at all.
        """
        conversation_id = str(payload.get("conversationId") or "")
        stopped = self._refusal is not None or (
            conversation_id in self._refused_conversations
        )
        if not stopped:
            return {}
        named = conversation_id or "an unnamed conversation"
        if conversation_id:
            count = self._terminations.get(conversation_id, 0) + 1
            self._terminations[conversation_id] = count
            # **A loop we already ended, asking again.** Measured
            # 2026-09-12: with a third-party `Stop` hook answering
            # `continue`, this terminates and that revives, eight times in
            # sixteen seconds, bounded only by `--print-timeout` — which
            # `AgySession` sets to 12h. Warned on the second termination
            # only, because after that it is the same fact repeating and the
            # log is the one place a runaway turn is legible.
            #
            # Not acted on here, deliberately. The remedies are ending the
            # process (AG-19 demotes that to an explicit user escalation,
            # since it ends a session the user is holding) and bounding the
            # turn — both decisions above this class. What this owes is to
            # stop the condition being silent.
            if count == 2:
                logger.warning(
                    "The loop for %s was revived after AIC-DC ended it, so "
                    "the user's stop is being overridden — most likely by a "
                    "`Stop` hook this app does not own answering `continue` "
                    "(see AG-R-16). The turn will keep re-entering until "
                    "`--print-timeout` expires.",
                    named,
                )
        logger.info("Ending the loop for %s: the user stopped it", named)
        return {"terminationBehavior": "terminate"}

    def note_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record that a loop ended, and never object to it. AG-R-16.

        The ``Stop`` event, whose payload carries the ``terminationReason``
        this host has no other way to see. Answering ``{}`` is the whole
        decision and it is not conditional: ``{"decision": "continue"}``
        would re-enter a loop the user stopped, and the one thing worth
        guaranteeing about this path is that the string never appears on
        it. :func:`aic_dc.agy.hook.report_stop` does not forward this
        return value either, so both ends hold that property alone.

        **It does not follow that our ``{}`` protects the stop.** Measured
        2026-09-12: a hook this app does not own, answering ``continue``,
        holds the turn open whichever order the two run in — and runs ours
        not at all when it goes first. This handler's value is the reading
        below, not a veto. See [AG-R-16](../../../specs5/plan-ag/risks.md#ag-r-16).

        What it does with the payload is tell one hook-ended loop from
        another. ``TERMINAL_CUSTOM_HOOK`` on a conversation
        :meth:`decide_invocation` never terminated means **somebody
        else's** ``Stop``, ``PostInvocation`` or plugin ended our turn —
        the merge AG-R-16 describes, arriving from the other direction.
        That is logged rather than acted on: it is a fact about the user's
        own configuration, and the useful thing is to name it.
        """
        conversation_id = str(payload.get("conversationId") or "")
        reason = str(payload.get("terminationReason") or "")
        if reason == HOOK_TERMINATION and not self.was_terminated(conversation_id):
            logger.warning(
                "A hook AIC-DC does not own ended the loop for %s. Another "
                "entry in the shared hooks file is acting on this app's "
                "conversations (see AG-R-16).",
                conversation_id or "an unnamed conversation",
            )
        return {}

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
            # Routed on the key the hook stamps from *its* argv, never on
            # the shape of the payload: a tool call sniffed as an
            # invocation event would be answered `{}`, and `{}` on a tool
            # call is allow. An absent key is the gate, which is both the
            # conservative reading and what an older hook process sends.
            event = payload.get(hook.EVENT_KEY) or hook.PRE_TOOL_USE
            if event == hook.POST_INVOCATION:
                answer = self.decide_invocation(payload)
            elif event == hook.STOP:
                answer = self.note_stop(payload)
            else:
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
