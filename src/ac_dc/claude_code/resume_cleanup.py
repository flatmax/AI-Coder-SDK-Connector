"""The temp config dir a resumed session leaves behind on Ctrl-C.

Resuming with a ``session_store`` set makes the SDK materialise our copy
of the session into a temporary directory laid out like ``~/.claude/``
and point the CLI subprocess at it through ``CLAUDE_CONFIG_DIR``
(``_internal/session_resume.py``). The SDK removes that directory in
``ClaudeSDKClient.disconnect()``, and AC⚡DC's graceful path honours that
— :func:`~ac_dc.claude_code.session._quiet_disconnect` reaches it from
every teardown that runs inside the event loop.

``main._signal_handler`` does not. It exits via ``os._exit``, which is
the whole reason ``main._kill_cli_children`` exists — the SDK's only
orphan guard for its children is an ``atexit`` hook, and ``os._exit``
skips ``atexit``. This cleanup was lost to exactly the same gap, one
layer up, and it needed exactly the same hand-written answer.

Measured after two restarts, before this module existed:
``/tmp/claude-resume-fatfygyc``, 900 KiB, abandoned by a Ctrl-C, nothing
referencing it. Each abandoned directory holds a full transcript copy
and a ``.credentials.json``. The SDK redacts ``refreshToken`` from that
file deliberately — a single-use token spent under a redirected config
dir would revoke the parent's own credentials — and the directory is
``0700`` with the file ``0600``, so this was never exposure to another
user on the machine. What it was: a live ``accessToken``, valid until
its ``expiresAt``, plus a transcript, accumulating **one copy per launch
cycle** and surviving until the next reboot. Auto-resume makes every
server start after the first a resume, so there is no rare path here.

Registered rather than discovered. Sweeping ``/tmp`` for the
``claude-resume-`` prefix would also match the live directory of another
AC⚡DC or a plain ``claude`` running beside us, and removing that is a
worse bug than the leak.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Every temp config dir this process caused to be created. Not pruned on
# a successful disconnect: :func:`purge` removes with ``ignore_errors``,
# so a directory the SDK already cleaned costs one failed ``rmtree`` and
# nothing else, and a set that means "dirs we caused" cannot go stale in
# the direction that matters. The set is bounded by the number of
# connects in the process's life.
_DIRS: set[Path] = set()


def remember(client: Any) -> Path | None:
    """Record the temp config dir ``client`` resumed through, if any.

    Returns the path, or ``None`` when this connect was not a resume, or
    was a resume the SDK served without materialising anything.

    ``_materialized`` is private SDK surface, and reached once — here.
    The guard is the same bargain ``_kill_cli_children`` strikes with
    ``_ACTIVE_CHILDREN``: an SDK that moves or renames it costs us this
    cleanup, never the connect. Nothing downstream may treat the return
    value as proof of a resume.
    """
    try:
        materialized = getattr(client, "_materialized", None)
        if materialized is None:
            return None
        config_dir = Path(materialized.config_dir)
    except Exception as exc:
        logger.debug("Cannot read the SDK's materialized resume dir: %s", exc)
        return None
    _DIRS.add(config_dir)
    logger.debug("Resumed through %s; registered for shutdown cleanup", config_dir)
    return config_dir


def purge() -> None:
    """Remove every registered temp config dir, synchronously.

    Called from ``main._signal_handler`` immediately before ``os._exit``,
    and **after** ``_kill_cli_children``. The order is a requirement, not
    a preference: the directory is the live ``CLAUDE_CONFIG_DIR`` of the
    children being killed, and pulling it out from under a CLI still
    flushing its transcript would trade a disk leak for a write error on
    the way out.

    Best-effort by construction. ``ignore_errors`` covers the directory
    the SDK already removed on a graceful disconnect, which is the normal
    case for every client but the last one, and it covers a partial
    removal — a leaked directory is what we started with, so failing
    loudly here would only replace it with a server that will not exit.

    Blocking in a signal handler is acceptable for the same reason the
    kill's grace period is: the next statement is ``os._exit``, nothing
    else is waiting on this thread, and the work is an ``rmtree`` over
    roughly a megabyte.
    """
    for config_dir in list(_DIRS):
        shutil.rmtree(config_dir, ignore_errors=True)
    _DIRS.clear()
