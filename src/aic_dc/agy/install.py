"""Putting the gate into the user's own ``agy`` configuration, and taking it out.

This module writes **outside the repository**, to
``~/.gemini/config/hooks.json`` — a file belonging to Google's CLI, which
the user may already be using for their own hooks. That is not a detail to
gloss: it is the most invasive thing this project does, it is required
rather than chosen (workspace-local hooks are not loaded headlessly on
`agy` 1.1.25), and it needs the user's consent rather than a silent write
at startup.

What it costs a session that has nothing to do with us
======================================================
`agy`'s global hooks *"fire unconditionally"*, so while our entry is
installed **every tool call in every `agy` session on the machine** spawns
our hook process, including the interactive one the user runs themselves.
Measured 2026-09-03:

- **~30 ms per tool call**, against ~10 ms for starting Python at all. It
  was ~500 ms until the hook was moved out from under
  ``aic_dc.antigravity``, whose import alone costs that much and pulls in
  the Claude SDK — a tax this module's own existence would have levied on
  the user's unrelated work.

  **Corrected 2026-09-04.** This said 200 ms for a day, measured through
  ``uv run``, which adds ~170 ms of its own startup and is *not* what gets
  installed: :func:`hook_command` writes ``sys.executable``, the
  virtualenv's own interpreter. Measuring the convenience wrapper rather
  than the command under test overstated the cost by sevenfold, and it was
  the number the decision to uninstall on shutdown was made against.
- **Nothing else.** A call belonging to a conversation this host has not
  claimed is answered ``{"decision": "allow"}`` and never reaches a
  dialog, a socket or a queue.

So the honest summary is: standalone ``agy`` keeps working exactly as it
did, and pays about 30 ms per tool call while the gate is installed.

**The gate is sticky, and the corrected measurement is why.** It was
removed on shutdown while the cost was believed to be 200 ms — worth
paying only when it bought something. At 30 ms it is not worth the
surprise of a Settings toggle that silently un-sets itself, or of the next
session refusing to start because the thing the user switched on had been
taken away behind them. :func:`uninstall` is now reached only from
Settings, by the person who put it there.

The failure that used to be unacceptable, and the change that retired it
=======================================================================
`agy` **blocks a tool** when a hook command cannot be run — exit 127,
measured. While this hook was installed **globally**, into the user's own
``~/.gemini/config/hooks.json``, that was a problem belonging to someone
else: a stale entry left by a crash, pointing at a deleted virtualenv,
would stop *their* interactive `agy` working at all, with an error naming
a program they may not recognise. So every command was written as::

    <python> -m aic_dc.agy.hook <config_dir> || printf '{"decision":"allow"}'

**That clause is gone, and what removed it was not a better clause.** It
was [AG-21](../../../specs5/plan-ag/decisions.md#ag-21): the hook now goes
into a config root this app owns and spawns `agy` against, so the only
sessions it can break are this app's own — and for those, a gate that
cannot run is exactly the thing that should stop the turn. Fail-open was
the rent paid for living in somebody else's configuration file, and the
tenancy has ended. See :data:`LEGACY_FALLBACKS`, which keeps the old
string only so :func:`status` can recognise an entry that still carries
it.

The residual case the clause covered — a transient failure to fork while a
turn is genuinely being gated — is now answered the way it should always
have been: the tool is blocked. That is recorded rather than hidden, and
it is the reason :func:`status` reports a stale install loudly.

The fallback's other edge, and the bug it hid (2026-09-05)
==========================================================
*Kept in full although the clause is gone, because the bug was never
about the clause — it was about* :func:`status` *believing a string.*

That reasoning has a premise: *a non-zero exit means this host is not
running*. It is true when the only reason the command can fail is that an
interpreter is gone. It was **false on a PyInstaller release binary**,
where ``sys.executable`` is the frozen binary rather than a Python:
``<binary> -m aic_dc.agy.hook …`` exits 2 with *"unrecognized
arguments"*, so `agy` took the fallback on every call — of a session this
host *was* running and *did* own. An ungated agent, reporting itself
gated, because :func:`status` judged "current" by comparing the command
string and the string was the one we meant to write. (That comparison has
since been loosened to *one interpreter spelled two ways* — see
:func:`_same_command` — which does not weaken this paragraph: the frozen
form is a different argument list, not a different spelling.)

Two changes close it, and they are deliberately at different layers:

- :func:`hook_command` emits ``<binary> --agy-hook <config_dir>`` on a
  frozen build — a suppressed CLI flag whose only caller is that string.
- :func:`install` **probes the command before writing it** and refuses if
  it does not answer (:func:`hook_runs`). That is the general fix: the
  frozen binary was one way to get a correct string naming an unrunnable
  command, and a moved virtualenv is another. Failing closed costs the
  user an error message at the moment they asked for a gate, which is the
  cheapest place to spend it.

Three handlers, one entry (2026-09-11)
======================================
This wrote one ``PreToolUse`` handler until AG-19. It now writes three
under the same name — the gate, a ``PostInvocation`` that ends a stopped
loop, and a ``Stop`` that always permits the stop — because ``agy`` merges
named hooks per event and one name is what makes :func:`uninstall` able to
remove exactly what we added.

The cost to a stranger's session is not three times the old one. The
per-tool-call tax is unchanged, since only the gate fires per tool call;
what is added is two more hook processes **per invocation**, which is a
much coarser unit — measured at 4 invocations for a turn that ran three
tools, against one hook process per tool call.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5, AG-19; ``risks.md``
AG-R-16.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from aic_dc.agy import hook

logger = logging.getLogger(__name__)

#: Where ``agy`` reads global hooks. Not configurable: measured as the only
#: location loaded in headless mode on 1.1.25, and a wrong guess here is a
#: gate that silently never fires.
GLOBAL_HOOKS = Path.home() / ".gemini" / "config" / "hooks.json"

#: Our key in that file. Namespaced so the user's own hooks are visibly
#: not ours, and so :func:`uninstall` can remove exactly one entry.
HOOK_NAME = "aic-dc-gate"

#: Long enough to outlast a human reading a diff. `agy` passes this
#: straight to ``context.WithTimeout`` with no ceiling (measured at
#: 86400), and a hook killed at its deadline exits non-zero, which `agy`
#: treats as a refusal — so a short value here would refuse a call because
#: the user was slow.
HOOK_TIMEOUT_SECONDS = 3600

#: The deadline on the two *invocation* handlers, which wait on no human.
#: A minute rather than an hour, and rather than the documented default of
#: 30 seconds: the question is answered from a latch the host already
#: holds, so a minute is pure headroom, and leaving the default in place
#: would mean a value we never chose bounding the stop.
INVOCATION_TIMEOUT_SECONDS = 60


#: The ``||`` fallback that used to end every command, kept as a string so
#: that :func:`status` can recognise an install written before 2026-09-11
#: and say what is different about it.
#:
#: **It was deleted rather than replaced, and the reason is measured.**
#: The fallback existed because the hook was installed **globally**, into
#: the user's own ``~/.gemini/config/hooks.json``, where it fired for
#: their interactive ``agy`` sessions too — so a hook runner this app
#: could not start would have broken sessions this app has nothing to do
#: with. Fail-open was the price of being in somebody else's config file.
#: [AG-21](../../../specs5/plan-ag/decisions.md#ag-21) moves the hook into
#: a config root this app owns, and the price stops being owed.
#:
#: A *structured deny* — ``|| printf '{"decision":"deny",...}' $?`` — was
#: the alternative, and it is worse than nothing on two counts. ``agy``
#: already fails closed on a command that cannot run: measured
#: 2026-09-11, a runner exiting 127 denies the tool, exits 0 and neither
#: crashes nor hangs, so the clause's entire output is a verdict already
#: reached. And ``||`` *appends*: a runner that dies after emitting a
#: partial object produces that object followed by a second one, which is
#: not JSON at all — so the clause would turn a clean deny into a parse
#: failure precisely in the case it was added for.
#:
#: What replaces it is nothing, on purpose. Faults inside the runner are
#: caught inside the runner and answered with a real ``deny``; faults
#: outside it — missing interpreter, killed process, bad path — are the
#: vendor's to fail closed on, and it does.
LEGACY_FALLBACKS = {
    hook.PRE_TOOL_USE: " || printf '{\"decision\":\"allow\"}'",
    hook.POST_INVOCATION: " || printf '{}'",
    hook.STOP: " || printf '{}'",
}


def hook_command(
    config_dir: Path | str, python: str | None = None, *, event: str | None = None
) -> str:
    """The shell command ``agy`` will run for one lifecycle event.

    **One command and no shell fallback**, since 2026-09-11. The
    ``|| printf '{"decision":"allow"}'`` clause every command used to end
    with is gone with the globalness that forced it — see
    :data:`LEGACY_FALLBACKS` for why deleting it beats replacing it, and
    why an install that still carries it reads as ``stale``.

    **Two forms, because ``sys.executable`` is not always a Python.**
    Under PyInstaller it is the frozen binary, which does not honour
    ``-m`` — so the ``-m`` form exited 2 there, ``agy`` took the fallback,
    and every tool call was auto-approved while :func:`status` reported
    the gate ``current``, because it compares command strings and the
    string matched. An ungated agent that reports itself gated is exactly
    what AG-5 rules out, and it was invisible from a source checkout,
    where the ``-m`` form is correct.

    The frozen form uses ``--agy-hook``, a suppressed flag on the CLI
    whose only caller is this string. Detection is
    ``getattr(sys, "frozen", …)`` — PyInstaller's own marker — and it is
    read from ``python`` when that argument names a different interpreter,
    because a caller passing one is describing an install that is not this
    process.

    **The gate's command is unchanged, and deliberately so.** ``event`` of
    ``None`` or ``PreToolUse`` emits exactly the string this function has
    always emitted, so an install written before AG-19 is still recognised
    as *itself* rather than as a different interpreter — the two are told
    apart by the events that are missing, which is what they are.

    The flag is spelled two ways for the same reason the invocation is:
    ``--event`` is the module's own argument, and the frozen build cannot
    take it, because on that form the arguments belong to this app's CLI
    rather than to the hook. So the frozen build carries
    ``--agy-hook-event``, a second suppressed flag whose only job is to be
    translated back (:mod:`aic_dc.cli`).
    """
    interpreter = python or sys.executable
    frozen = python is None and bool(getattr(sys, "frozen", False))
    invocation = (
        f"{interpreter} --agy-hook {config_dir}"
        if frozen
        else f"{interpreter} -m aic_dc.agy.hook {config_dir}"
    )
    if event and event != hook.PRE_TOOL_USE:
        invocation += (
            f" --agy-hook-event {event}" if frozen else f" {hook.EVENT_FLAG} {event}"
        )
    return invocation


def hook_commands(
    config_dir: Path | str, python: str | None = None
) -> dict[str, str]:
    """Every command this build would install, keyed by event.

    One place the set is enumerated, read by :func:`hook_entry` to write
    them, by :func:`install` to probe them and by :func:`status` to compare
    them — so a fourth event cannot be registered and left unchecked.
    """
    return {
        event: hook_command(config_dir, python, event=event)
        for event in hook.EVENTS
    }


def hook_entry(config_dir: Path | str, python: str | None = None) -> dict[str, Any]:
    """Our whole entry in the user's hooks file.

    Three handlers under one name, which is how ``agy`` groups them:
    ``PreToolUse`` is *grouped* — a ``matcher`` wrapping a ``hooks`` list —
    and the two invocation events are **flat**, a list of handlers with no
    matcher, because there is no tool to match on. Writing the grouped
    shape for a flat event is the kind of mistake that produces a handler
    which simply never fires, so the shapes are written out here rather
    than generated from one template.

    Separate from :func:`install` so that a test can put an entry on disk
    without going through the probe — the states ``install`` now refuses to
    create still have to be readable by :func:`status`.
    """
    commands = hook_commands(config_dir, python)
    return {
        hook.PRE_TOOL_USE: [
            {
                # AG-R-12: every tool, never a list. A blocked tool is an
                # error the model can see, and it will reach for whatever
                # the matcher missed — measured, three routes to one write.
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": commands[hook.PRE_TOOL_USE],
                        "timeout": HOOK_TIMEOUT_SECONDS,
                    }
                ],
            }
        ],
        hook.POST_INVOCATION: [
            {
                "type": "command",
                "command": commands[hook.POST_INVOCATION],
                "timeout": INVOCATION_TIMEOUT_SECONDS,
            }
        ],
        hook.STOP: [
            {
                "type": "command",
                "command": commands[hook.STOP],
                "timeout": INVOCATION_TIMEOUT_SECONDS,
            }
        ],
    }


#: A payload per event, shaped like the one ``agy`` sends, for
#: :func:`hook_runs`. None of them names a conversation, so none is owned
#: by this host — the probe therefore exercises the interpreter, the
#: argument list and the print without touching permission state or a
#: socket.
_PROBES: dict[str, dict[str, Any]] = {
    hook.PRE_TOOL_USE: {"toolCall": {"name": "aic-dc-install-probe"}},
    hook.POST_INVOCATION: {"invocationNum": 0, "initialNumSteps": 0},
    hook.STOP: {"executionNum": 1, "terminationReason": "model_stop"},
}


#: One probe per (command, event) per process. A probe spawns an
#: interpreter, and AG-21 installs a hook into a **fresh root per
#: consultation** — so the unmemoised version would pay three interpreter
#: startups for every consultation, to re-answer a question whose answer
#: cannot change while this process is running: the command names *this*
#: interpreter and *this* package.
#:
#: Deliberately not invalidated. The thing that would make an answer stale
#: is this install being deleted from under a running server, and a server
#: whose own package has been removed has a larger problem than a cached
#: probe.
_PROBE_CACHE: dict[tuple[str, str], str] = {}


def _probed(invocation: str, event: str, verdict: str) -> str:
    """Record and return one probe verdict."""
    _PROBE_CACHE[(invocation, event)] = verdict
    return verdict


def hook_runs(
    command: str, *, event: str = hook.PRE_TOOL_USE, timeout: float = 30.0
) -> str:
    """``""`` if ``command`` answers a probe, else why it did not.

    The check :func:`status` cannot make cheaply and :func:`install` must
    not skip. A hook command is a *string in somebody else's config file*;
    that it is the string we meant to write says nothing about whether the
    thing it names can run. The frozen-binary bug was precisely that gap —
    correct string, unrunnable command — and the same gap catches a moved
    virtualenv or an uninstalled package.

    The command it is handed no longer has a ``|| printf`` fallback to
    strip, but the split survives, because an entry written before
    2026-09-11 still carries one and a probe that ran it would report a
    broken hook as healthy — the fallback prints valid JSON and exits 0,
    which is exactly the shape of a pass. Stripping it is what lets this
    function give the honest answer about a legacy install rather than the
    reassuring one.

    A probe payload with no ``conversationId`` is one the gate does not
    own, so this asks the question in the shape that is guaranteed to be
    cheap and to touch no permission state.

    **Each event is probed with its own payload and judged by its own
    contract**, because the three commands differ by an argument and an
    argument is exactly what the frozen-binary bug got wrong. Only the gate
    is required to print a ``decision``; an invocation hook answering
    ``{}`` — no opinion — is the correct answer and the one a probe should
    expect.
    """
    invocation = command.split("||")[0].strip()
    cached = _PROBE_CACHE.get((invocation, event))
    if cached is not None:
        return cached
    probe = json.dumps(_PROBES.get(event, _PROBES[hook.PRE_TOOL_USE]))
    try:
        completed = subprocess.run(
            invocation,
            shell=True,
            input=probe,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _probed(invocation, event, f"the command could not be run: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        return _probed(invocation, event, f"exit {completed.returncode}: {tail}")
    try:
        answer = json.loads(completed.stdout)
    except ValueError:
        return _probed(
            invocation, event,
            f"printed no JSON decision: {completed.stdout.strip()[:200]!r}",
        )
    if not isinstance(answer, dict):
        return _probed(
            invocation, event,
            f"printed JSON that is not an object: {completed.stdout.strip()[:200]!r}",
        )
    if event == hook.PRE_TOOL_USE and "decision" not in answer:
        return _probed(
            invocation, event,
            f"printed JSON with no decision: {completed.stdout.strip()[:200]!r}",
        )
    return _probed(invocation, event, "")


def _load(path: Path) -> dict[str, Any]:
    """The user's hooks file, or an empty one.

    A file we cannot parse raises rather than being overwritten: it is the
    user's, it may hold hooks they depend on, and replacing it because we
    could not read it would be the worst possible outcome of installing a
    permission gate.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{path} is not valid JSON, so AIC-DC will not modify it. Fix or "
            f"move the file and try again. ({exc})"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} is not a JSON object, so AIC-DC will not modify it.")
    return parsed


def status(
    config_dir: Path | str, *, path: Path | None = None, python: str | None = None
) -> dict[str, Any]:
    """Whether the gate is installed, and whether it would work.

    Answers four states rather than a boolean, because "installed" and
    "installed and usable" are different and the difference is what a
    settings surface has to explain:

    - ``absent`` — no entry of ours. `agy` sessions are ungated by us and
      pay nothing.
    - ``current`` — our entry, pointing at this interpreter, **with every
      handler this build registers**.
    - ``stale`` — our entry, pointing at a **different or missing**
      interpreter. Usually another checkout, or a virtualenv that has
      moved. Still safe for the user thanks to the ``||`` fallback, but our
      own sessions would not be gated by *this* build.

      **Also an entry that is ours and incomplete** (2026-09-11). AG-19
      added a ``PostInvocation`` handler and AG-R-16 a ``Stop`` one, and an
      entry written before them has a working gate and no stop mechanism —
      ⏹ would starve a turn and call itself a halt. That reads as a small
      difference and is not: a control that reports itself a mechanism
      while being a request is the same shape of untruth as an ungated
      agent reporting itself gated, which is what this state exists to
      refuse. It costs an upgrading user one click on a panel that says
      what is missing (``detail``), and it converges; treating it as
      ``current`` would leave the mechanism unarmed and silent forever.

      **Different, not differently spelled** (2026-09-10). This compared
      command strings, so one venv's ``bin/python3`` and ``bin/python`` —
      one program, two names, and which one appears depends only on how
      the process that wrote the entry was started — read as two installs.
      A ``stale`` reading makes :class:`~aic_dc.agy.session.AgySession`
      refuse to start, so a working gate presented as a broken one after
      nothing more than a different entry point resolving the interpreter
      by its other name. Found by ``probe_agy_cgroup_identity.py``'s setup
      on a machine where the gate was live; see
      [AG-R-14](../../../specs5/plan-ag/risks.md#ag-r-14).

      **The consequence was larger than a wrong label**, which is why it is
      recorded here rather than as a cosmetic note:
      ``AgyService.connect`` answers ``gate_not_installed`` on a stale state
      and ``AgyConsultant.available`` goes false with it, so the whole engine
      refuses to start and ``second_opinion`` disappears.
    - ``unreadable`` — the file exists and will not parse, so nothing can
      be said and nothing will be written.
    """
    target = path or GLOBAL_HOOKS
    try:
        data = _load(target)
    except RuntimeError as exc:
        return {"state": "unreadable", "path": str(target), "detail": str(exc)}

    entry = data.get(HOOK_NAME)
    others = sorted(k for k in data if k != HOOK_NAME)
    if not isinstance(entry, dict):
        return {"state": "absent", "path": str(target), "other_hooks": others}

    want = hook_commands(config_dir, python)
    found = _installed_commands(entry)
    differs = [
        event
        for event in hook.EVENTS
        if not _same_command(found.get(event, ""), want[event])
    ]
    report = {
        "state": "current" if not differs else "stale",
        "path": str(target),
        "other_hooks": others,
        # The gate's pair, under the names they have always had: this is
        # what the Settings panel renders, and it is still the answer to
        # "which install is in the file".
        "command": found.get(hook.PRE_TOOL_USE, ""),
        "expected": want[hook.PRE_TOOL_USE],
        "commands": found,
        "expected_commands": want,
        "agy_present": shutil.which("agy") is not None,
    }
    if differs:
        report["missing_events"] = [
            event for event in differs if event not in found
        ]
        report["detail"] = _stale_detail(differs, found)
    return report


def _stale_detail(differs: list[str], found: dict[str, str]) -> str:
    """Why an entry of ours is not this build's, in a sentence.

    Two causes reach ``stale`` and a user can act on only one of them if
    they are told which: another checkout owns the file, or this build
    registers handlers the entry predates. Reinstalling is the answer to
    both, so this explains rather than instructs.
    """
    legacy = [
        event
        for event in differs
        if found.get(event, "").endswith(LEGACY_FALLBACKS.get(event, "\0"))
    ]
    if legacy:
        return (
            "This entry was written before the permission gate moved into "
            "its own configuration root, so its commands still end in the "
            "fail-open fallback — a gate that cannot start would wave every "
            "tool call through instead of blocking it. Reinstalling removes "
            "the fallback."
        )
    absent = [event for event in differs if event not in found]
    if absent and hook.PRE_TOOL_USE not in differs:
        return (
            "The permission gate is installed and is still reviewing every "
            "tool call, but this build also registers "
            + ", ".join(absent)
            + ", which this entry does not have — so stopping a turn would "
            "starve it rather than end it. Reinstalling adds them."
        )
    return (
        "This entry names a different install of AIC-DC, so your own "
        "sessions would not be gated by this one. Reinstalling takes it "
        "over."
    )


#: The fixed middles of the two forms :func:`hook_command` emits. Used to
#: cut an installed command into "the interpreter" and "everything else"
#: without guessing at word boundaries — the interpreter is a path and the
#: rest is ours, so splitting on what we wrote is exact where splitting on
#: whitespace is not.
_INVOCATIONS = (" -m aic_dc.agy.hook ", " --agy-hook ")


def _split_interpreter(command: str) -> tuple[str, str] | None:
    for marker in _INVOCATIONS:
        head, sep, tail = command.partition(marker)
        if sep and head:
            return head, marker + tail
    return None


def _same_interpreter(installed: str, expected: str) -> bool:
    """Whether two paths name the same interpreter, spelled differently.

    A virtualenv ships ``python``, ``python3`` and ``python3.13`` in one
    ``bin`` directory, all pointing at one program, and which name a caller
    gets depends on how it was started: :func:`hook_command` writes
    ``sys.executable``, and a script launched as ``.venv/bin/python3``
    records a different string from one launched as ``.venv/bin/python``.
    Comparing the strings makes those two different installs.

    **Resolving both to their real targets is not the fix**, and this is
    why the parent directories are compared first: every venv's ``python``
    ultimately resolves to the *system* interpreter, so a bare
    :func:`os.path.samefile` would call two different checkouts the same
    install — which is the exact case ``stale`` exists to report. Same
    directory, same file, different spelling is the only difference this
    forgives. Parents are compared through :func:`os.path.realpath` so that
    a checkout reached by a symlink is still itself.
    """
    if installed == expected:
        return True
    try:
        if os.path.realpath(Path(installed).parent) != os.path.realpath(
            Path(expected).parent
        ):
            return False
        return os.path.samefile(installed, expected)
    except OSError:
        # A missing interpreter is the `stale` this function is asked
        # about, not an error to raise at a status caller.
        return False


def _same_command(found: str, want: str) -> bool:
    """Whether an installed hook command is the one we would write.

    String equality first, because it is the answer almost every time and
    it is the only one that needs no filesystem. The fallback exists for
    one difference and forgives no other: the arguments must match exactly,
    and only the interpreter may be spelled another way.
    """
    if found == want:
        return True
    left, right = _split_interpreter(found), _split_interpreter(want)
    if left is None or right is None or left[1] != right[1]:
        return False
    return _same_interpreter(left[0], right[0])


def _handler_command(handlers: Any) -> str:
    """The first command in a flat list of handlers, or ``""``."""
    if not isinstance(handlers, list):
        return ""
    for handler in handlers:
        if isinstance(handler, dict) and handler.get("command"):
            return str(handler["command"])
    return ""


def _installed_commands(entry: dict[str, Any]) -> dict[str, str]:
    """What is actually in the file, by event. Absent events are absent.

    Missing rather than empty, because :func:`status` reports *which*
    handlers an entry does not have and "" would make an event that is
    there with a blank command indistinguishable from one that is not.

    The two shapes are read the way they are written: ``PreToolUse`` is
    grouped under a ``matcher``, the invocation events are flat.
    """
    found: dict[str, str] = {}
    for group in entry.get(hook.PRE_TOOL_USE) or []:
        if not isinstance(group, dict):
            continue
        command = _handler_command(group.get("hooks"))
        if command:
            found[hook.PRE_TOOL_USE] = command
            break
    for event in (hook.POST_INVOCATION, hook.STOP):
        command = _handler_command(entry.get(event))
        if command:
            found[event] = command
    return found


def install(
    config_dir: Path | str, *, path: Path | None = None, python: str | None = None
) -> dict[str, Any]:
    """Add or update our entry, preserving every other key in the file.

    Merged rather than written: the file is the user's, `agy`'s own
    documentation describes hooks from different sources being merged and
    run in sequence, and clobbering somebody's lint-on-write hook to
    install a permission gate would be an unusually rude way to protect
    them.

    **The command is probed before it is written, and a command that does
    not run is refused rather than installed.** This is the one moment
    where failing closed costs the user only an error message: they asked
    for a gate, so telling them it could not be installed is actionable,
    where installing a broken one hands them an ungated agent that reports
    itself gated. The frozen-binary bug produced exactly that, and it is
    not the only way to get there — a virtualenv that has moved, or a
    package uninstalled from under an entry, both end in the same place.

    **Every command is probed, not just the gate's.** The three differ only
    by an argument, and an argument is precisely what the frozen build got
    wrong; probing one and writing three would leave the same gap one event
    to the left.
    """
    target = path or GLOBAL_HOOKS
    commands = hook_commands(config_dir, python)
    for event in hook.EVENTS:
        problem = hook_runs(commands[event], event=event)
        if not problem:
            continue
        logger.error(
            "Refusing to install an agy %s hook that does not run: %s",
            event,
            problem,
        )
        return {
            "state": "unrunnable",
            "path": str(target),
            "command": commands[event],
            "event": event,
            "detail": (
                f"The permission gate was not installed, because the "
                f"{event} command it would write does not run here — "
                f"{problem}. Installing it anyway would write a hook that "
                f"cannot answer, while this panel reported the gate as "
                f"active."
            ),
        }
    data = _load(target)
    data[HOOK_NAME] = hook_entry(config_dir, python)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written whole and moved into place. `agy` may read this file at any
    # instant, and a half-written one would fail to parse — which, for a
    # file of hooks, means the user's own hooks stop running too.
    tmp = target.with_suffix(".json.aic-dc-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    logger.info("Installed the AIC-DC agy gate into %s", target)
    return status(config_dir, path=target, python=python)


def installable(config_dir: Path | str, python: str | None = None) -> dict[str, Any]:
    """Whether the gate *would* install, touching no file.

    AG-21 gave every consultation a **fresh** config root, which leaves
    nothing to inspect before one starts: :func:`status` on a root that
    does not exist yet answers ``absent``, and that is true and useless. A
    caller asking "can a consultation be gated" is really asking whether
    the commands this build would write can run — the same probe
    :func:`install` makes before it writes, memoised per process, so the
    question costs nothing after the first time.

    Two states, deliberately not three: ``ready`` or ``unrunnable``. There
    is no ``stale`` here, because there is no installed entry to be stale
    against.
    """
    commands = hook_commands(config_dir, python)
    for event in hook.EVENTS:
        problem = hook_runs(commands[event], event=event)
        if problem:
            return {
                "state": "unrunnable",
                "event": event,
                "command": commands[event],
                "detail": (
                    f"The {event} command this build would write does not "
                    f"run here — {problem}."
                ),
                "expected_commands": commands,
                "agy_present": shutil.which("agy") is not None,
            }
    return {
        "state": "ready",
        "expected_commands": commands,
        "agy_present": shutil.which("agy") is not None,
    }


def retire_global() -> bool:
    """Take our entry back out of the user's own ``hooks.json``.

    Returns whether one was there. This app put it there, this app no
    longer reads it, and until AG-21 it was the gate — so a machine that
    upgrades inherits a hook that fires for every ``agy`` the user starts
    by hand, routing to a socket that belongs to a process which may not be
    running. The old entry carries the ``|| printf '{"decision":"allow"}'``
    fallback, so it fails open rather than blocking them, which is exactly
    what makes it easy not to notice.

    AG-21's stated benefit is that the user's own interactive ``agy`` is
    untouched. That is not true of an upgraded machine until this runs, so
    it runs on connect rather than waiting for somebody to find the button
    in Settings. Removing it needs no permission: it is ours, and it is
    dead.

    Deliberately **not** latched per process. It is one small JSON read
    that returns ``False`` without logging when there is nothing there, and
    a latch would mean a stale entry written between two connects in the
    same session survives until restart.
    """
    return uninstall()


def uninstall(*, path: Path | None = None) -> bool:
    """Remove only our entry. Returns whether one was there.

    The file is left in place with the user's other hooks intact, and is
    removed entirely only if ours was the last thing in it — an empty
    ``{}`` left behind would be litter in somebody else's configuration.
    """
    target = path or GLOBAL_HOOKS
    try:
        data = _load(target)
    except RuntimeError:
        # Unparseable. Not ours to repair, and certainly not ours to
        # delete: the user may be mid-edit.
        logger.warning("Not touching %s: it does not parse", target)
        return False
    if HOOK_NAME not in data:
        return False
    del data[HOOK_NAME]
    if data:
        tmp = target.with_suffix(".json.aic-dc-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    else:
        target.unlink(missing_ok=True)
    logger.info("Removed the AIC-DC agy gate from %s", target)
    return True
