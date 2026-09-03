"""The ``PreToolUse`` hook ``agy`` runs before every tool call.

This is the gate of [AG-14](../../../../specs5/plan-ag/decisions.md#ag-14),
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

Whose call is this?
-------------------
The hook is installed in the user's global ``~/.gemini/config/hooks.json``
because workspace-local hooks are not loaded headlessly on 1.1.25, so it
sees *every* ``agy`` session on the machine, including ones the user runs
themselves. Those must pass through untouched and unstalled.
:mod:`~aic_dc.antigravity.agy.registry` answers that, keyed on
``conversationId``; this module's job is to act on the answer and to fail
in the right direction when it cannot get one.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5, and
``risks.md`` AG-R-12, whose mitigations are requirements of this file:
a ``"*"`` matcher, never exit 0 silently, and a tripwire that asserts the
*file* is unchanged rather than that this hook fired.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from aic_dc.antigravity.agy import registry

logger = logging.getLogger(__name__)

#: What we say when a call is not ours. Also what we say when we cannot
#: tell and own nothing — see :func:`decide`.
ALLOW: dict[str, Any] = {"decision": "allow"}

#: How long to wait for this host to answer. Deliberately unbounded-ish
#: rather than short: the host is waiting on a human reading a diff, and
#: the deadline that actually bounds a dialog is ``agy``'s own hook
#: ``timeout`` plus ``--print-timeout``, both of which the adapter sets.
#: A short value here would re-introduce the fault this design exists to
#: avoid — a refusal that fails because the user was slow.
SOCKET_TIMEOUT_SECONDS = 3600.0


def deny(reason: str) -> dict[str, Any]:
    """A refusal carrying a reason the *model* reads.

    ``agy`` surfaces this as ``tool call denied by pre-tool hook: <reason>``,
    so it is the model's only account of what happened. A bare "denied"
    invites it to try the same thing another way, which is
    ``risks.md`` AG-R-11 — so every reason here says enough for the agent
    to change course rather than re-route.
    """
    return {"decision": "deny", "reason": reason}


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

    conversation_id = payload.get("conversationId")
    entry = registry.lookup(conversation_id, config_dir=config_dir)
    if entry is None:
        # Not ours. The overwhelmingly common case, because this hook is
        # global: the user's own `agy` sessions land here and must leave
        # immediately, unmodified and unstalled.
        return ALLOW

    try:
        answer = asker(entry["socket"], payload)
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


def ask_host(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Put the call to the running app and wait for the human's answer.

    One connection per call, newline-delimited JSON each way. A connection
    per call rather than a shared one because ``agy`` runs this as a fresh
    process every time — there is nothing to keep open between calls, and
    a lock file to share one would be a second thing to fail.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks: list[bytes] = []
        while not chunks or not chunks[-1].endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Read one payload, print one decision. Never exits without printing.

    The bare ``except`` is deliberate and is the point of this function.
    An uncaught exception would end the process with a traceback on stderr
    and **nothing on stdout**, which is the one shape ``agy`` reads as
    allow. So the last thing that can go wrong here still prints a denial.
    """
    config_dir = None
    if argv:
        config_dir = argv[0]
    try:
        raw = sys.stdin.read()
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = None
        result = decide(payload, config_dir=config_dir)
    except BaseException:  # noqa: BLE001 - see the docstring; silence is allow
        logger.exception("The AIC-DC agy gate failed")
        result = deny(
            "The AIC-DC permission gate failed, so the call was refused "
            "rather than allowed without review. This is an AIC-DC fault, "
            "not a refusal by the user."
        )
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv[1:]))
