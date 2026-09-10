"""The hooks ``agy`` runs, and the one of them that must never fail open.

Three lifecycle events reach this module, from three entries
:mod:`~aic_dc.agy.install` writes into one hooks file. They are here
together because they share a socket, an owner lookup and a process:

- ``PreToolUse`` — **the gate**, before every tool call. The subject of
  most of what follows, and the only one with the tree behind it.
- ``PostInvocation`` — **the stop**, after each invocation's tool calls
  finish. Answers ``terminationBehavior: "terminate"`` while ⏹ is latched,
  which ends the loop whatever the agent concluded
  ([AG-19](../../../specs5/plan-ag/decisions.md#ag-19)).
- ``Stop`` — when the execution loop terminates. Always lets it, and
  **that is a report rather than a veto**: measured 2026-09-12, a rival
  hook's ``continue`` holds the turn open whether ours runs before it or
  not, and short-circuits ours entirely when it runs first
  ([AG-R-16](../../../specs5/plan-ag/risks.md#ag-r-16)).

**The three fail in opposite directions, deliberately** — see § *Every
path prints* and § *An invocation hook fails open*.

The gate
--------
It is the gate of [AG-14](../../../specs5/plan-ag/decisions.md#ag-14),
and on this transport **it is the only gate there is**. ``agy``'s own
headless permission layer cannot prompt — it auto-denies anything it would
otherwise ask about, logged as ``Print mode: soft-denying tool
confirmation`` — so an adapter has to pass ``--dangerously-skip-permissions``
to get a turn to run at all. That flag does not relax a gate here; it
removes one that cannot ask, in favour of this one, which can.

The consequence is worth stating plainly because it shapes every decision
below: **if this module is wrong, writes happen unreviewed.** There is no
second check behind it.

The contract
------------
``agy`` runs the configured command via ``sh -c``, writes one JSON object
to its stdin, and reads one JSON object from its stdout. We answer with
``{"decision": "allow"}`` or ``{"decision": "deny", "reason": …}``.

``"ask"`` exists and is useless: headlessly it auto-denies. We never send
it. The human is asked by *this app*, through its own dialog, and what
goes back is the answer they gave.

Every path prints, and the one that must not
--------------------------------------------
Four of ``agy``'s five failure modes fail closed — a hook that times out,
exits non-zero, prints malformed JSON, or does not exist all block the
tool. The fifth does not: **exit 0 with empty stdout is parsed as ``{}``,
whose empty decision defaults to allow.** That is the single fail-open path
on this transport and it is entirely ours to avoid, which is why
:func:`main` is written so that every exit — including one taken by an
unexpected exception — goes through a print.

An invocation hook fails open, and that is not the same mistake
---------------------------------------------------------------
``{}`` is the *correct* answer for ``PostInvocation`` and ``Stop`` — it is
the documented "no opinion", and it is what this module prints whenever it
cannot get one. So the invocation hooks fail in the direction the gate
must never fail in, and AG-19 says so rather than leaving it to be
noticed: **what a failure there costs is a loop that keeps running, not a
write that goes unreviewed.** The tree is protected by ``PreToolUse``,
which is still refusing every call, so a stop that fails to end the loop
mechanically degrades to the starvation it replaced. Ending a stranger's
loop because we could not reach our own host would be the worse error, and
it is the one this direction rules out.

**The event is stamped here rather than read from the payload**, and that
is a safety property rather than tidiness. The host answers a different
shape per event, and ``{}`` — right for an invocation hook — is exactly
the shape ``agy`` reads as *allow* on a tool call. A payload allowed to
name its own event could ask for a tool call to be answered in the shape
that waves it through. So :func:`main` learns the event from **argv**,
which only :mod:`~aic_dc.agy.install` writes, and overwrites whatever the
payload claimed.

Whose call is this?
-------------------
The hook is installed in the user's global ``~/.gemini/config/hooks.json``
because workspace-local hooks are not loaded headlessly on 1.1.25, so it
sees *every* ``agy`` session on the machine, including ones the user runs
themselves. Those must pass through untouched and unstalled.
:mod:`~aic_dc.agy.registry` answers that, keyed on
``conversationId``; this module's job is to act on the answer and to fail
in the right direction when it cannot get one.

**Ownership is not only "is there an entry".** Since 2026-09-10 the
registry also asks whether anything is left alive behind one, so a file a
killed host abandoned answers *not ours* rather than denying against a
socket nobody is holding — while an entry whose ``agy`` outlived its host
still denies, because that is an orphaned agent and not a stale file. The
reasoning is in :mod:`~aic_dc.agy.registry` § *A claim outlives the process
that made it*; nothing in this module changes for it, which is the point of
having asked the registry rather than the socket.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5, AG-19, and
``risks.md`` AG-R-12 and AG-R-16, whose mitigations are requirements of
this file: a ``"*"`` matcher, never exit 0 silently, a ``Stop`` handler
that never says ``continue`` — and a tripwire that asserts the *file* is
unchanged rather than that this hook fired, which is the shape every claim
here should have had: AG-R-16's own mitigation was believed for a day on
the strength of the documentation, and measurement refuted it.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from aic_dc.agy import registry, scope

logger = logging.getLogger(__name__)

#: The lifecycle events this app registers. Ordered as the module
#: docstring introduces them, and iterated by :mod:`~aic_dc.agy.install`
#: so a new one cannot be added to the writer and forgotten by the reader.
PRE_TOOL_USE = "PreToolUse"
POST_INVOCATION = "PostInvocation"
STOP = "Stop"
EVENTS = (PRE_TOOL_USE, POST_INVOCATION, STOP)

#: The flag :mod:`~aic_dc.agy.install` writes to name the event. Absent on
#: the gate's own command, which is left in the exact shape it has always
#: had so that an existing install is recognised as itself.
EVENT_FLAG = "--event"

#: The key stamped onto every payload forwarded to the host, naming the
#: event it came from. See the module docstring § *An invocation hook
#: fails open* for why this is written here and never read from ``agy``.
EVENT_KEY = "hookEvent"

#: What we say when a call is not ours. Also what we say when we cannot
#: tell and own nothing — see :func:`decide`.
ALLOW: dict[str, Any] = {"decision": "allow"}

#: An invocation hook with no opinion. The documented default, and the
#: answer to every failure on those two events.
PROCEED: dict[str, Any] = {}

#: What ends the loop, whatever the agent had planned next. AG-19.
TERMINATE: dict[str, Any] = {"terminationBehavior": "terminate"}

#: How long to wait for this host to answer a *tool call*. Deliberately
#: unbounded-ish rather than short: the host is waiting on a human reading
#: a diff, and the deadline that actually bounds a dialog is ``agy``'s own
#: hook ``timeout`` plus ``--print-timeout``, both of which the adapter
#: sets. A short value here would re-introduce the fault this design
#: exists to avoid — a refusal that fails because the user was slow.
SOCKET_TIMEOUT_SECONDS = 3600.0

#: How long to wait on an *invocation* event. Short where the gate's is
#: long, because there is no human in this question: the host answers it
#: from a latch it is already holding. Spending the gate's hour here would
#: stall a loop that has finished its work on a host that has gone away.
INVOCATION_TIMEOUT_SECONDS = 15.0


def deny(reason: str) -> dict[str, Any]:
    """A refusal carrying a reason the *model* reads.

    ``agy`` surfaces this as ``tool call denied by pre-tool hook: <reason>``,
    so it is the model's only account of what happened. A bare "denied"
    invites it to try the same thing another way, which is
    ``risks.md`` AG-R-11 — so every reason here says enough for the agent
    to change course rather than re-route.
    """
    return {"decision": "deny", "reason": reason}


def _owner(
    payload: dict[str, Any], config_dir: Path | str | None
) -> dict[str, Any] | None:
    """The registry entry gating this payload's conversation, or ``None``.

    Shared by all three events, because *whose call is this* is one
    question and two copies of the answer is how the gate and the stop
    would quietly start disagreeing about which sessions are ours.
    """
    entry = registry.lookup(payload.get("conversationId"), config_dir=config_dir)
    if entry is None:
        # AG-R-14. Nobody registered this conversation — which is the
        # ordinary case for a stranger's session, and *also* what a
        # subagent, a grandchild, and a child whose announcement has not
        # arrived yet all look like. The two are told apart by asking the
        # kernel rather than the registry: a call from inside a scope this
        # host created is ours no matter who never wrote its id down.
        #
        # Second, not first, because it is the fallback path: a machine
        # without systemd has no scopes and every call takes the branch
        # above, exactly as it did before this existed.
        entry = registry.scope_owner(scope.current_cgroup(), config_dir=config_dir)
    return entry


def decide(
    payload: Any,
    *,
    config_dir: Path | str | None = None,
    ask: Any = None,
) -> dict[str, Any]:
    """The whole decision, as a pure function of the payload and the registry.

    Separated from :func:`main` so the safety properties can be tested
    without a subprocess, a socket or ``agy`` — every branch below is a
    case in ``tests/test_agy_gate.py``, and the ones that matter are the
    failures.

    ``ask`` is the host call, injected. Defaults to :func:`ask_host`.
    """
    asker = ask if ask is not None else ask_host

    if not isinstance(payload, dict):
        # Unparseable, so there is no id and no way to tell whose call this
        # is. The tie goes to whether we are running anything at all: a
        # host owning nothing cannot be the intended gate, and refusing
        # here would break a stranger's session on our bug.
        if registry.owns_anything(config_dir):
            return deny(
                "AIC-DC could not read this tool call, and it may belong to a "
                "session AIC-DC is gating. Refused rather than allowed "
                "unreviewed. This is an AIC-DC fault, not a refusal by the user."
            )
        return ALLOW

    entry = _owner(payload, config_dir)
    if entry is None:
        # Not ours. The overwhelmingly common case, because this hook is
        # global: the user's own `agy` sessions land here and must leave
        # immediately, unmodified and unstalled.
        return ALLOW

    try:
        answer = asker(entry["socket"], {**payload, EVENT_KEY: PRE_TOOL_USE})
    except Exception as exc:  # noqa: BLE001 - a gate must not raise
        logger.exception("The AIC-DC gate could not be reached")
        # Ours, and unreachable. This is the direction the registry split
        # exists to make available: a dead host makes our own sessions
        # un-runnable rather than un-gated.
        return deny(
            f"AIC-DC is gating this session but could not be reached "
            f"({type(exc).__name__}), so the call was refused rather than "
            f"allowed without review. This is an AIC-DC fault, not a refusal "
            f"by the user."
        )

    if not isinstance(answer, dict) or answer.get("decision") not in (
        "allow",
        "deny",
    ):
        return deny(
            "AIC-DC returned no usable decision for this call, so it was "
            "refused rather than allowed unreviewed. This is an AIC-DC fault, "
            "not a refusal by the user."
        )
    return answer


def decide_invocation(
    payload: Any,
    *,
    config_dir: Path | str | None = None,
    ask: Any = None,
) -> dict[str, Any]:
    """Whether this conversation's loop should end now. AG-19.

    ``PostInvocation`` fires after an invocation's tool calls have been
    resolved, so this is the first moment after ⏹ at which the loop can be
    ended by something other than the agent's own judgement. The host holds
    the latch — the same one the gate reads to refuse tool calls — and this
    asks it.

    **The answer is built here rather than forwarded.** A dict from the
    socket is inspected for one value and then thrown away, so a host bug
    cannot put ``injectSteps``, a ``decision``, or anything else into
    ``agy``'s hands through this path.

    Every failure returns :data:`PROCEED`: an unreadable payload, a
    conversation nobody claimed, an unreachable host, an answer that makes
    no sense. See the module docstring § *An invocation hook fails open*.
    """
    asker = ask if ask is not None else ask_host

    if not isinstance(payload, dict):
        return dict(PROCEED)

    entry = _owner(payload, config_dir)
    if entry is None:
        return dict(PROCEED)

    try:
        answer = asker(entry["socket"], {**payload, EVENT_KEY: POST_INVOCATION})
    except Exception:  # noqa: BLE001 - a hook must not raise
        logger.exception("The AIC-DC gate could not be asked whether to stop")
        return dict(PROCEED)

    if isinstance(answer, dict) and answer.get("terminationBehavior") == "terminate":
        return dict(TERMINATE)
    return dict(PROCEED)


def report_stop(
    payload: Any,
    *,
    config_dir: Path | str | None = None,
    ask: Any = None,
) -> dict[str, Any]:
    """Tell the host the loop ended, and never object to it. AG-R-16.

    ``agy``'s ``Stop`` contract accepts ``{"decision": "continue"}``, which
    **blocks the stop and re-enters the loop**. This app never sends it,
    and the return is a literal rather than anything derived from the
    socket so that no failure and no host bug can reach that string.

    **What this is not is a vote.** It was written believing that one
    handler answering ``{}`` would be enough to carry a stop through a
    merged hooks file — the mitigation
    [AG-R-16](../../../specs5/plan-ag/risks.md#ag-r-16) asked for, and
    recorded there as unverified. ``scripts/probe_agy_stop_merge.py``
    measured it on 2026-09-12 and it is false twice over: a rival
    ``continue`` holds the turn open with ours in the merge, in **either**
    key order, and when the rival runs first **this handler is not run at
    all** — a ``continue`` short-circuits the handlers after it. Being
    registered is not the same as being asked.

    So its value is the *side effect*, and that is now the reason it
    exists. The payload carries ``terminationReason``, which is how the
    host tells a loop it ended from one a stranger's hook ended, and there
    is nowhere else to read it. The reply is read and discarded, and
    reading it is not waste: ``agy`` is blocked on this process, so waiting
    for the answer is what guarantees the host has recorded the stop before
    the turn's ``result`` frame is emitted.

    The defence that does hold is one layer down — the gate's refusal stays
    armed after a stopped turn ends, so a revived loop reaches the working
    tree through a gate still saying no. See
    :meth:`aic_dc.agy.gate_server.AgyGateServer.resume`.
    """
    asker = ask if ask is not None else ask_host

    if isinstance(payload, dict):
        entry = _owner(payload, config_dir)
        if entry is not None:
            try:
                asker(entry["socket"], {**payload, EVENT_KEY: STOP})
            except Exception:  # noqa: BLE001 - the stop happens regardless
                logger.exception("The AIC-DC gate could not be told a turn ended")
    return dict(PROCEED)


def ask_host(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Put the call to the running app and wait for the human's answer.

    One connection per call, newline-delimited JSON each way. A connection
    per call rather than a shared one because ``agy`` runs this as a fresh
    process every time — there is nothing to keep open between calls, and
    a lock file to share one would be a second thing to fail.

    The deadline is read off the stamped event rather than passed in, so
    an injected ``ask`` in a test keeps its two positional arguments and
    the two timeouts stay one decision made in one place.
    """
    timeout = (
        SOCKET_TIMEOUT_SECONDS
        if payload.get(EVENT_KEY, PRE_TOOL_USE) == PRE_TOOL_USE
        else INVOCATION_TIMEOUT_SECONDS
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks: list[bytes] = []
        while not chunks or not chunks[-1].endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def parse_argv(argv: list[str] | None) -> tuple[str | None, str]:
    """``(config_dir, event)`` from the command :mod:`install` wrote.

    **Anything unrecognised is the gate**, and that is the fail-closed
    direction rather than a fallback chosen for tidiness: an event this
    function could not name, answered as an invocation hook, would print
    ``{}`` — and ``{}`` on a tool call is *allow*. A hand-edited hooks file
    is the only way to get here, and it gets the strict handler.
    """
    args = list(argv or [])
    event = PRE_TOOL_USE
    positional: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == EVENT_FLAG and index + 1 < len(args):
            if args[index + 1] in (POST_INVOCATION, STOP):
                event = args[index + 1]
            index += 2
            continue
        positional.append(args[index])
        index += 1
    return (positional[0] if positional else None), event


def main(argv: list[str] | None = None) -> int:
    """Read one payload, print one answer. Never exits without printing.

    The bare ``except`` is deliberate and is the point of this function.
    An uncaught exception would end the process with a traceback on stderr
    and **nothing on stdout**, which is the one shape ``agy`` reads as
    allow on a tool call. So the last thing that can go wrong here still
    prints — a denial for the gate, and ``{}`` for the two events where
    ``{}`` is what "no opinion" means.
    """
    config_dir, event = parse_argv(argv)
    if event == POST_INVOCATION:
        handler: Any = decide_invocation
        fallback: dict[str, Any] = dict(PROCEED)
    elif event == STOP:
        handler = report_stop
        fallback = dict(PROCEED)
    else:
        handler = decide
        fallback = deny(
            "The AIC-DC permission gate failed, so the call was refused "
            "rather than allowed without review. This is an AIC-DC fault, "
            "not a refusal by the user."
        )
    try:
        raw = sys.stdin.read()
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = None
        result = handler(payload, config_dir=config_dir)
    except BaseException:  # noqa: BLE001 - see the docstring; silence is allow
        logger.exception("The AIC-DC agy %s hook failed", event)
        result = fallback
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv[1:]))
