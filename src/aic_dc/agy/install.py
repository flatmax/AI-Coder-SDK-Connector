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

The failure that would be unacceptable, and how it is closed
============================================================
`agy` **blocks a tool** when a hook command cannot be run — exit 127,
measured. A stale entry left behind by a crash, pointing at a virtualenv
that has since been deleted, would therefore stop the user's own `agy`
working at all, with an error naming a program they may not recognise.

The installed command is wrapped so that cannot happen::

    <python> -m aic_dc.agy.hook <config_dir> || printf '{"decision":"allow"}'

The fallback is sound rather than merely convenient. :func:`aic_dc.agy.hook.main`
is written to exit 0 on *every* path, including a denial and including an
unexpected exception — so a non-zero exit means the interpreter itself
could not start, which means this host is not running, which means it owns
no conversations, which means allow is the correct answer. The one case it
does not cover is a transient failure to fork while a turn is genuinely
being gated; that is recorded rather than hidden, and it is the reason
:func:`status` reports a stale install loudly.

Governing spec: ``specs5/plan-ag/`` — AG-14.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

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


def hook_command(config_dir: Path | str, python: str | None = None) -> str:
    """The shell command ``agy`` will run for every tool call.

    The ``||`` fallback is the safety net described in the module
    docstring: our hook exits 0 on every path it controls, so a non-zero
    exit means this host could not start and therefore owns nothing.
    """
    interpreter = python or sys.executable
    return (
        f"{interpreter} -m aic_dc.agy.hook {config_dir} "
        "|| printf '{\"decision\":\"allow\"}'"
    )


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
    - ``current`` — our entry, pointing at this interpreter.
    - ``stale`` — our entry, pointing at a **different or missing**
      interpreter. Usually another checkout, or a virtualenv that has
      moved. Still safe for the user thanks to the ``||`` fallback, but our
      own sessions would not be gated by *this* build.
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

    want = hook_command(config_dir, python)
    found = _installed_command(entry)
    state = "current" if found == want else "stale"
    return {
        "state": state,
        "path": str(target),
        "other_hooks": others,
        "command": found,
        "expected": want,
        "agy_present": shutil.which("agy") is not None,
    }


def _installed_command(entry: dict[str, Any]) -> str:
    for group in entry.get("PreToolUse") or []:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks") or []:
            if isinstance(handler, dict) and handler.get("command"):
                return str(handler["command"])
    return ""


def install(
    config_dir: Path | str, *, path: Path | None = None, python: str | None = None
) -> dict[str, Any]:
    """Add or update our entry, preserving every other key in the file.

    Merged rather than written: the file is the user's, `agy`'s own
    documentation describes hooks from different sources being merged and
    run in sequence, and clobbering somebody's lint-on-write hook to
    install a permission gate would be an unusually rude way to protect
    them.
    """
    target = path or GLOBAL_HOOKS
    data = _load(target)
    data[HOOK_NAME] = {
        "PreToolUse": [
            {
                # AG-R-12: every tool, never a list. A blocked tool is an
                # error the model can see, and it will reach for whatever
                # the matcher missed — measured, three routes to one write.
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(config_dir, python),
                        "timeout": HOOK_TIMEOUT_SECONDS,
                    }
                ],
            }
        ]
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written whole and moved into place. `agy` may read this file at any
    # instant, and a half-written one would fail to parse — which, for a
    # file of hooks, means the user's own hooks stop running too.
    tmp = target.with_suffix(".json.aic-dc-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    logger.info("Installed the AIC-DC agy gate into %s", target)
    return status(config_dir, path=target, python=python)


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
