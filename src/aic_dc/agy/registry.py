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

A claim outlives the process that made it
-----------------------------------------
The trade above is stated for a host that is *running and unreachable*. An
entry left behind by a host that no longer exists is a different thing
wearing the same clothes, and until 2026-09-10 nothing here could tell
them apart: :func:`claim` recorded a ``pid`` and :func:`lookup` never read
it, so a killed session's file denied tool calls forever
(``risks.md`` AG-R-14, residue 3).

**Liveness is asked of two pids, not one**, because the pair is what makes
the answer actionable:

- the **host** pid — the process that would decide, and the one that would
  have removed this file on a clean exit;
- the **agy** pid — the process that would *act*. Our host spawns ``agy``
  as a child, and a child outlives a parent that is killed: ``main.py``
  measured this shape on the other engine, a CLI reparented to init and
  still running 38 seconds later.

A dead host whose ``agy`` is still alive is therefore an **orphaned
agent** — precisely the case where "the entry is stale, wave it through"
would produce the unreviewed write this transport has no second check for.
The entry stands and the hook keeps denying. Only when **both** are gone
is the file a corpse, and a corpse reads as *not ours*: it restores what
:meth:`~aic_dc.agy.gate_server.AgyGateServer.release` promises for a
conversation the user later resumes in their own ``agy``, and it can
ungate nothing, because by definition no process from that session is left
to make a call.

Two limits, stated rather than mitigated. **A recycled pid reads as
alive**, so a corpse can present as an orphan until something reaps it —
the direction that keeps our own tree gated. And **where liveness cannot
be asked, the answer is "alive"**: Windows has no safe probe (see
:func:`process_alive`), and an entry written before this change carries no
``agy_pid``, so both keep the pre-2026-09-10 behaviour instead of
inventing an answer.

The same reading covers a **scope** entry ([AG-18](../../../specs5/plan-ag/decisions.md#ag-18)),
with one wrinkle it creates: a scope claim is deliberately written *before*
``agy`` exists, so its first version cannot name the process that would
act. It therefore reads as alive — the limit above, arrived at by design
rather than by age — until
:meth:`~aic_dc.agy.gate_server.AgyGateServer.claim` rewrites it with the
pid. That matters more here than for a conversation entry, because a unit
this host abandoned is exactly where an orphaned ``agy`` still sits: same
unit, so :func:`scope_owner` still matches it, and reaping on the host pid
alone would have handed an agent under ``--dangerously-skip-permissions``
an unreviewed tree.

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
    agy_pid: int | None = None,
) -> Path:
    """Record that this host owns ``conversation_id``.

    Called the moment the ``init`` frame names the conversation, and
    **before the first prompt is sent** — a tool call can follow the first
    prompt immediately, and a gate that is not yet claiming the
    conversation would wave it through as somebody else's.

    ``agy_pid`` is the conversation's own ``agy`` process, and it is what
    makes an abandoned entry distinguishable from an orphaned agent — see
    this module's § *A claim outlives the process that made it*. Optional
    because it is the caller's to know: absent, the entry is never treated
    as a corpse, which is the behaviour that predates it.
    """
    path = _entry_path(conversation_id, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "conversation_id": str(conversation_id),
        "socket": str(socket_path),
        "pid": int(pid if pid is not None else os.getpid()),
    }
    if agy_pid is not None:
        payload["agy_pid"] = int(agy_pid)
    # Written whole and moved into place: the hook may read this file at
    # any instant, and a half-written entry would parse as "not ours".
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def process_alive(pid: Any) -> bool:
    """Whether ``pid`` names a process that still exists.

    **True is also the answer when the question cannot be asked**, and that
    is the whole of this function's safety argument: every caller here uses
    a live pid as a reason to leave a registry entry standing, and an entry
    that stands makes the hook deny rather than allow. So an unanswerable
    liveness question costs a refusal, never an unreviewed tool call.

    ``os.kill(pid, 0)`` is the probe on POSIX and **must not be reached on
    Windows**, where ``os.kill`` is ``TerminateProcess`` for any signal
    that is not a console event: a liveness check that killed the process
    it asked about would be a far worse bug than the one this exists to
    fix. There is no probe on that platform, so the answer is "alive" and
    the pre-2026-09-10 behaviour stands there — the same shape as
    ``next.md`` § C8's POSIX-only teardown, stated rather than faked.

    A zombie answers signal 0, so a killed host not yet reaped by its own
    shell reads as alive. That delays a reap by moments and never inverts
    it, which is why it is not worth ``waitpid`` (which could not work
    anyway: nothing here is the parent of either process).
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it is simply not ours to signal. Another user's
        # process holding this pid is still a reason not to reap.
        return True
    except OSError:
        return True
    return True


def entry_is_live(entry: Any) -> bool:
    """Whether a registry entry still belongs to something that exists.

    ``True`` while **either** pid answers — the host that would decide, or
    the ``agy`` that would act. ``False`` only for a corpse: a host that is
    gone and an agent that is gone with it. The reasoning for the ``or`` is
    in this module's § *A claim outlives the process that made it*; the
    short form is that a dead host with a live agent is an orphan, and an
    orphan must keep being denied rather than released into the tree.
    """
    if not isinstance(entry, dict):
        return True
    if process_alive(entry.get("pid")):
        return True
    return process_alive(entry.get("agy_pid"))


def reap_stale(config_dir: Path | str | None = None) -> list[str]:
    """Delete every corpse entry, returning the ids removed.

    Called by a host as it starts, beside the stale-socket unlink that has
    the same cause (:meth:`~aic_dc.agy.gate_server.AgyGateServer.start`).
    It is **not** "remove entries that are not mine": a second host on this
    machine has live entries of its own, and the liveness test rather than
    the ownership test is what keeps this sweep from breaking it.

    **Scope entries are swept by the same rule**, on the same reading of
    :func:`entry_is_live`: a unit whose host and whose ``agy`` are both gone
    is a name nothing can ever be placed in again, and one whose ``agy`` is
    still running is the orphan the entry exists to keep denying.

    Failures are logged and skipped. A file that will not go away leaves
    the annoyance this function exists to remove, which is the direction
    that costs nothing but tidiness.
    """
    reaped: list[str] = []
    directory = registry_dir(config_dir)
    try:
        entries = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return reaped
    for path in entries:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Unreadable or half-written. `lookup` already reads this as
            # "not ours", so it denies nothing and there is no liveness
            # question to ask of it — leave it for its owner.
            continue
        if entry_is_live(entry):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not reap a stale agy registry entry")
            continue
        conversation_id = entry.get("conversation_id")
        reaped.append(
            str(conversation_id) if conversation_id is not None else path.stem
        )
    if reaped:
        logger.info(
            "Reaped %d agy registry %s left by a host that is gone: %s",
            len(reaped),
            "entry" if len(reaped) == 1 else "entries",
            ", ".join(reaped),
        )
    return reaped


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

    **A corpse entry is also ``None``** — an entry whose host and whose
    ``agy`` are both gone. Not a relaxation of the paragraph above: a
    session with no process left in it has no call to be gated, so the only
    thing that entry can still do is intercept a conversation the *user*
    resumes in their own ``agy``, which is the interception
    ``probe_agy_isolation.py`` exists to prevent.
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
    if not entry_is_live(entry):
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
    agy_pid: int | None = None,
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

    ``agy_pid`` is therefore **absent on the first write and added by a
    second**, which is the one asymmetry with :func:`claim`: the process
    that would act cannot be named by an entry written before it exists.
    :meth:`~aic_dc.agy.gate_server.AgyGateServer.claim` rewrites the entry
    once the child is up, and until it does the scope is never read as a
    corpse — the pre-2026-09-10 behaviour, which is the direction that keeps
    an orphaned agent gated (see :func:`entry_is_live`).
    """
    path = _scope_path(unit, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "unit": str(unit),
        "socket": str(socket_path),
        "pid": int(pid if pid is not None else os.getpid()),
    }
    if agy_pid is not None:
        payload["agy_pid"] = int(agy_pid)
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

    **Corpse entries do not count**, and this is the tie-breaker's own
    version of AG-R-14 residue 3: counting files rather than sessions meant
    that one unclean exit of ours turned "we own something" permanently
    true, so from then on every unparseable payload from *the user's* own
    ``agy`` was denied — a stale file of ours breaking their work, which is
    the exact direction this function was written to avoid.
    """
    directory = registry_dir(config_dir)
    try:
        paths = [p for p in directory.iterdir() if p.suffix == ".json"]
    except OSError:
        return False
    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written entry is one being claimed right now, which is
            # a session of ours in the making. It counts.
            return True
        if entry_is_live(entry):
            return True
    return False
