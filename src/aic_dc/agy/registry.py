"""Which ``agy`` conversations this host owns, readable by a separate process.

The gate of [AG-14](../../../specs5/plan-ag/decisions.md#ag-14) has a
problem the SDK's does not: **it runs in the user's own configuration.**
Workspace-local ``hooks.json`` is not loaded by ``agy`` 1.1.25 in headless
mode, so the hook must live in ``~/.gemini/config/hooks.json``, which
``agy``'s own documentation says *"fires unconditionally"*. It will
therefore be handed every tool call from every ``agy`` session on the
machine, including the interactive one the user is running themselves.

Intercepting those would be unacceptable — it would put a permission
dialog from *our* app in front of a conversation we have nothing to do
with, or worse, block it. So the hook has to answer one question before
anything else: **is this conversation ours?**

Why a directory of files rather than asking the host
----------------------------------------------------
The obvious design is to ask the running app over its socket and let it
say "not mine". It is wrong, and the reason is the failure case: if AIC⚡DC
is not running, or has crashed, *every* question goes unanswered. Treating
silence as "not mine" would ungate our own sessions; treating it as "mine"
would break the user's. One channel cannot distinguish them.

A registry splits the question in two, and each half fails safely:

- **Ownership** is a fact on disk, written when a conversation starts and
  removed when it ends. Absent means *not ours*, which is the correct
  answer for the user's own sessions and stays correct when this app is
  not running at all.
- **The decision** is asked over the socket named in that file, and only
  for conversations we already know are ours. Silence there is a fault,
  and :mod:`~aic_dc.agy.hook` denies on it.

So a dead host makes our sessions un-runnable rather than un-gated, and
leaves the user's untouched. That is the trade AG-5 requires — the dialog
is a requirement of this engine, not a feature.

**The id is the key because it is the only thing that works.**
``conversationId`` on the hook payload is byte-identical to the
``conversation_id`` on the stream's ``init`` frame, and ``init`` is the
first event of the stream, so a host knows the id it owns before any tool
call can arrive. ``workspacePaths`` — the field one would reach for first —
is **empty** in every payload captured on 1.1.25, in both ``-p`` and
bidirectional modes. Measured in ``sdk-surface.md`` § *Bidirectional mode,
and the isolation key*.

Governing spec: ``specs5/plan-ag/decisions.md`` AG-14.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the registry lives, under the user config directory this app
#: already owns. Deliberately *not* under ``~/.gemini/`` — that tree
#: belongs to Google's products, and writing our bookkeeping into it would
#: make our state their problem on an uninstall.
DEFAULT_DIRNAME = "agy-sessions"


def registry_dir(config_dir: Path | str | None = None) -> Path:
    """The registry directory, created on demand.

    Takes the config dir rather than resolving one, so a test never writes
    into the developer's real configuration and two hosts on one machine
    can be pointed at different registries.
    """
    base = Path(config_dir) if config_dir else Path.home() / ".config" / "aic-dc"
    return base / DEFAULT_DIRNAME


def _entry_path(conversation_id: str, config_dir: Path | str | None = None) -> Path:
    # The id comes off a JSON payload written by another program, so it is
    # untrusted input on a filesystem path. `Path(...).name` collapses any
    # traversal to a bare filename; an id containing a separator cannot
    # reach outside the registry.
    safe = Path(str(conversation_id)).name
    return registry_dir(config_dir) / f"{safe}.json"


def claim(
    conversation_id: str,
    socket_path: str | Path,
    *,
    config_dir: Path | str | None = None,
    pid: int | None = None,
) -> Path:
    """Record that this host owns ``conversation_id``.

    Called the moment the ``init`` frame names the conversation, and
    **before the first prompt is sent** — a tool call can follow the first
    prompt immediately, and a gate that is not yet claiming the
    conversation would wave it through as somebody else's.
    """
    path = _entry_path(conversation_id, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": str(conversation_id),
        "socket": str(socket_path),
        "pid": int(pid if pid is not None else os.getpid()),
    }
    # Written whole and moved into place: the hook may read this file at
    # any instant, and a half-written entry would parse as "not ours".
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def release(conversation_id: str, *, config_dir: Path | str | None = None) -> None:
    """Give up ownership. Safe to call twice, and on an id never claimed."""
    try:
        _entry_path(conversation_id, config_dir).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - teardown must not raise
        logger.exception("Could not release the agy registry entry")


def lookup(
    conversation_id: Any, *, config_dir: Path | str | None = None
) -> dict[str, Any] | None:
    """The registry entry for a conversation, or ``None`` if we do not own it.

    ``None`` is the answer for every conversation this host did not start,
    which is the common case: the hook is installed globally and most of
    what it sees belongs to the user.

    An unreadable or malformed entry also reads as ``None``. That is the
    safe direction *for this function* — it means "not ours", so a corrupt
    file cannot make us intercept a stranger's session. The opposite risk,
    a conversation of ours going ungated because its entry got corrupted,
    is not this function's to carry: the hook denies whenever it cannot
    reach the host it was told about.
    """
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    path = _entry_path(conversation_id, config_dir)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or not entry.get("socket"):
        return None
    return entry


#: Filename prefix for a *scope* entry, which maps a systemd unit to the
#: socket of the host that created it. Distinct from a conversation entry so
#: that :func:`owns_anything` and :func:`lookup` cannot confuse the two — one
#: is keyed by an id `agy` chose, the other by a unit we chose.
SCOPE_PREFIX = "scope-"


def _scope_path(unit: str, config_dir: Path | str | None = None) -> Path:
    safe = Path(str(unit)).name
    return registry_dir(config_dir) / f"{SCOPE_PREFIX}{safe}.json"


def claim_scope(
    unit: str,
    socket_path: str | Path,
    *,
    config_dir: Path | str | None = None,
    pid: int | None = None,
) -> Path:
    """Record that conversations inside ``unit`` belong to this host.

    **Written before ``agy`` is launched**, which is the whole point
    ([AG-R-14](../../../specs5/plan-ag/risks.md#ag-r-14)). A conversation
    claim races the child that it describes, because the child exists before
    anyone can name it; a scope claim describes a *container* that does not
    exist yet either, so there is nothing to be late for. Every process the
    kernel later puts in that group — a subagent, a grandchild, a shell
    command's own subprocess — is covered by an entry that was already on
    disk before any of them could run.
    """
    path = _scope_path(unit, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "unit": str(unit),
        "socket": str(socket_path),
        "pid": int(pid if pid is not None else os.getpid()),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def release_scope(unit: str, *, config_dir: Path | str | None = None) -> None:
    """Give up a scope. Safe to call twice, and on a unit never claimed."""
    try:
        _scope_path(unit, config_dir).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - teardown must not raise
        logger.exception("Could not release the agy scope entry")


def scope_owner(
    cgroup: str, *, config_dir: Path | str | None = None
) -> dict[str, Any] | None:
    """The host owning the scope ``cgroup`` names, or ``None``.

    Called by the hook with its **own** cgroup, which the kernel guarantees
    it inherited from whatever launched it. This is the question a claim
    cannot answer in time: a subagent's first call, and a grandchild's every
    call, arrive with a conversation id nobody has registered — but they
    arrive from inside a group that was registered before the session
    started.

    **Matched against the units this host registered**, never against the
    name's shape. A prefix test would let any process in a scope somebody
    else happened to call ``aic-dc-…`` route into our dialog; a directory
    of entries we wrote cannot be imitated by naming a unit.

    ``None`` for every cgroup that matches nothing, which is the answer for
    the user's own sessions — they sit in their terminal's scope — and keeps
    [AG-R-12](../../../specs5/plan-ag/risks.md#ag-r-12) true by the same
    passthrough that has always kept it true.
    """
    if not isinstance(cgroup, str) or not cgroup:
        return None
    directory = registry_dir(config_dir)
    try:
        entries = sorted(directory.glob(f"{SCOPE_PREFIX}*.json"))
    except OSError:
        return None
    for path in entries:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or not entry.get("socket"):
            continue
        unit = str(entry.get("unit") or "")
        # `.scope` rather than a bare substring: systemd renders the unit in
        # the path as `<unit>.scope`, and matching the bare name would also
        # match a longer unit that merely starts with ours.
        if unit and f"{unit}.scope" in cgroup:
            return entry
    return None


def owns_anything(config_dir: Path | str | None = None) -> bool:
    """Whether this host claims **any** conversation right now.

    Read by the hook for the one case it cannot otherwise decide: a payload
    it could not parse. With no id there is no way to tell whose call it
    is, and the two failure directions are not symmetric — denying breaks
    the user's own sessions on a bug of ours, allowing ungates one of ours.

    So the tie is broken on whether we are running a session *at all*. If
    this host owns nothing, an unparseable payload cannot be ours and is
    waved through; if it owns something, the call might be, and it is
    refused. Narrow, and it fails toward the user's work rather than
    toward silence.
    """
    directory = registry_dir(config_dir)
    try:
        return any(p.suffix == ".json" for p in directory.iterdir())
    except OSError:
        return False
