"""Keeping the OAuth access token alive across a resumed session.

The bug this module exists for
------------------------------
Every AIC⚡DC server start after the first is a *resume*, and resuming
with a ``session_store`` makes the SDK materialise a temporary
``CLAUDE_CONFIG_DIR`` and point the CLI subprocess at it
(``_internal/session_resume.py``; see :mod:`aic_dc.claude_code.resume_cleanup`
for the cleanup half of the same mechanism). When the SDK seeds that
directory it writes ``.credentials.json`` with ``claudeAiOauth.refreshToken``
**deliberately removed** (``_write_redacted_credentials``), because a
single-use refresh token spent under a redirected config dir would rotate
the parent's stored credentials out from under it and lock the user out.

That redaction is correct, and it leaves the CLI child holding an access
token it has no way to renew. When the token passes its ``expiresAt`` the
child says::

    Failed to authenticate: OAuth session expired and could not be refreshed

— a string that appears nowhere in this repository, because it comes from
the CLI. Observed live: a materialised ``.credentials.json`` with keys
``accessToken, expiresAt, rateLimitTier, refreshTokenExpiresAt, scopes,
subscriptionType`` and no ``refreshToken``, beside a ``~/.claude`` copy
that had both.

The assumption that was wrong
-----------------------------
``specs5/5-webapp/viewers-hud.md`` justified never repairing an expired
token with "it costs nothing in practice because AIC⚡DC *spawns* the CLI
and a turn is what keeps the file fresh". It does not. The spawned CLI
cannot refresh — it has no refresh token — and it writes to the temp dir
rather than to ``~/.claude``. Turns taken inside AIC⚡DC never refresh
anything, so an install used *only* through AIC⚡DC drifts until the
access token lapses and every turn fails. Only an interactive ``claude``
in a terminal was keeping it alive, which is why "run the CLI, then
restart AIC⚡DC" was the folk remedy.

What this module does about it
------------------------------
Two halves, both of which treat the parent's ``~/.claude/.credentials.json``
as the only authority and never hand the child a refresh token:

- :func:`ensure_fresh` runs *before* connect. If the parent's token is
  expired or close to it, it asks a short-lived CLI — run against the
  **real** config dir, where the refresh token still lives — to refresh
  in place, then re-reads the file to check that it worked. The
  materialisation that follows then snapshots a fresh token.
- :func:`propagate` runs *during* a session. Refreshing the parent does
  nothing for a child that already holds a stale snapshot, so the fresh
  access token is copied into the live materialised file. The child picks
  it up without a reconnect, and ``refreshToken`` stays absent — the
  SDK's guarantee is preserved, not worked around.

Verified rather than assumed
----------------------------
Which CLI invocation performs a refresh is not a documented contract, so
nothing here assumes one. :func:`ensure_fresh` walks an escalating ladder
(:data:`REFRESH_LADDER`) and after each rung re-reads ``expiresAt`` to see
whether it actually moved. The cheap rung is ``claude auth status``, which
costs no tokens; the fallback is a one-turn ``claude -p``, which is the
invocation the user's own terminal was performing when it fixed this by
hand, so the last rung is known-good by observation.

``--bare`` is deliberately *not* used for the fallback even though it is
the cheapest-looking flag: its own help says "OAuth and keychain are never
read", so a bare CLI would skip the very credential this module is trying
to renew.

Never fatal
-----------
A failed refresh does not fail the connect. The token may still be valid,
the machine may be on Bedrock or an API key, or the network may be down —
and a session that would have worked must not be refused by its own
pre-flight check. Failure is reported for the health banner and the
connect proceeds.

Read-only about secrets
-----------------------
Nothing here logs a token, puts one in an exception message, or copies one
anywhere but the child config dir the SDK already created for it. The
subprocess inherits the environment with ``CLAUDE_CONFIG_DIR`` pinned to
the real directory, so a server that is itself running under a redirected
config dir cannot accidentally refresh into a scratch copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Refresh when the access token has this little life left. Fifteen minutes
# is longer than any plausible connect plus first turn, so a session that
# passes this check will not expire between the check and the user's first
# message. It is also short enough that a normal restart does not spend a
# subprocess on a token with hours left on it.
REFRESH_MARGIN_SECONDS = 15 * 60

# How long a refresh rung may take. The cheap rung is a version-print-scale
# call; the fallback is a one-token turn. Sixty seconds is generous for
# both and bounded well inside the connect budget, which this runs before.
REFRESH_TIMEOUT_SECONDS = 60.0

# The escalating ladder of invocations tried until ``expiresAt`` moves.
#
# Ordered by cost, not by confidence. ``auth status`` is a metadata call
# that bills nothing, and it is first because if it does refresh, that is
# the whole fix for free. The ``-p`` turn is last because it costs a real
# (tiny) turn, and it is present because it is the one rung observed to
# work: it is what a user typing "hi" into a terminal was doing on the
# occasions that unstuck this by hand.
#
# Each entry is the argument list appended to the resolved CLI path.
REFRESH_LADDER: tuple[tuple[str, ...], ...] = (
    ("auth", "status", "--json"),
    ("-p", "hi", "--max-turns", "1"),
)


@dataclass(frozen=True)
class RefreshOutcome:
    """What :func:`ensure_fresh` did, for the log and the health banner.

    ``detail`` is always safe to show a user and never contains a token.
    """

    #: True when the parent's token is usable now — either it never needed
    #: refreshing, or a rung moved ``expiresAt`` forward.
    ok: bool
    #: True when a refresh was actually attempted (a subprocess ran).
    attempted: bool
    #: One sentence for the health banner, or ``None`` when nothing
    #: noteworthy happened.
    detail: str | None = None


def read_expiry(credential_path: Path) -> int | None:
    """The ``claudeAiOauth.expiresAt`` in ``credential_path``, in ms.

    ``None`` when the file is missing, unreadable, not JSON, or carries no
    OAuth block — every one of which is a legitimate state (an API-key
    machine has no such file) and none of which is this module's business
    to complain about. The caller treats ``None`` as "not a subscription
    login I can reason about" and does nothing.
    """
    try:
        data = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Cannot read %s for its token expiry: %s", credential_path, exc)
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    expires_at = oauth.get("expiresAt")
    return expires_at if isinstance(expires_at, int) else None


def seconds_remaining(expires_at_ms: int, *, now_ms: int | None = None) -> float:
    """Life left on ``expires_at_ms``, negative once it has lapsed."""
    now = now_ms if now_ms is not None else _now_ms()
    return (expires_at_ms - now) / 1000.0


def needs_refresh(expires_at_ms: int | None, *, now_ms: int | None = None) -> bool:
    """Whether a token expiring at ``expires_at_ms`` should be renewed now.

    An unknown expiry is *not* a reason to refresh: a machine with no
    OAuth block is on an API key or a gateway, and spawning a CLI at it
    would be a subprocess spent to learn nothing.
    """
    if expires_at_ms is None:
        return False
    return seconds_remaining(expires_at_ms, now_ms=now_ms) < REFRESH_MARGIN_SECONDS


async def ensure_fresh(
    *,
    credential_path: Path | None,
    config_dir: Path,
    cli_path: str,
    cwd: Path | None = None,
) -> RefreshOutcome:
    """Renew the parent's access token in place if it is close to expiry.

    Runs before connect, so that the temp config dir the SDK is about to
    materialise snapshots a token with a full lifetime ahead of it rather
    than the tail of an old one.

    Parameters
    ----------
    credential_path:
        The parent's ``.credentials.json``, or ``None`` when this machine
        has none — in which case there is nothing to refresh and nothing
        to report.
    config_dir:
        The **real** config directory. Pinned into the subprocess's
        ``CLAUDE_CONFIG_DIR`` so the refresh lands in the file the next
        materialisation will read, even if this process is itself running
        under a redirected config dir.
    cli_path:
        The binary already resolved and version-checked by the caller, so
        the refresh and the session agree on which CLI they mean.
    cwd:
        Working directory for the probe. The repo root by preference: it
        is already trusted, so no first-run trust prompt can appear.
    """
    if credential_path is None:
        return RefreshOutcome(ok=True, attempted=False)

    before = read_expiry(credential_path)
    if not needs_refresh(before):
        return RefreshOutcome(ok=True, attempted=False)

    remaining = seconds_remaining(before) if before is not None else 0.0
    logger.info(
        "Access token in %s expires in %.0fs; refreshing before connect",
        credential_path,
        remaining,
    )

    for rung in REFRESH_LADDER:
        if await _run_rung(rung, config_dir=config_dir, cli_path=cli_path, cwd=cwd):
            after = read_expiry(credential_path)
            if after is not None and (before is None or after > before):
                logger.info(
                    "Access token refreshed via `claude %s`; %.0fs of life ahead",
                    " ".join(rung),
                    seconds_remaining(after),
                )
                return RefreshOutcome(
                    ok=True,
                    attempted=True,
                    detail=f"Access token refreshed before connect (`claude {' '.join(rung)}`).",
                )
        logger.debug("`claude %s` did not move the token expiry", " ".join(rung))

    # Every rung ran and the expiry never moved. Said plainly, with the
    # remedy that is known to work, because the alternative the user
    # actually gets is the CLI's own opaque line half an hour later.
    logger.warning(
        "Could not refresh the access token in %s; the session may fail "
        "with an authentication error when it expires",
        credential_path,
    )
    return RefreshOutcome(
        ok=False,
        attempted=True,
        detail=(
            "The Claude subscription access token is expired or expiring and "
            "could not be refreshed automatically. Run `claude` in a terminal "
            "to renew it, then restart the engine."
        ),
    )


async def _run_rung(
    args: tuple[str, ...],
    *,
    config_dir: Path,
    cli_path: str,
    cwd: Path | None,
) -> bool:
    """Run one ladder rung to completion. True when it exited cleanly.

    Output is captured rather than inherited — a refresh probe must not
    print into the server's terminal — and discarded rather than logged,
    because ``auth status --json`` echoes account identifiers and there is
    no diagnostic here worth that. The exit status is the whole signal;
    whether the *token* moved is decided by the caller re-reading the file,
    which is the only check that cannot be fooled by a rung that succeeds
    without refreshing.
    """
    env = dict(os.environ)
    # Pin the real directory. Without this a server that inherited a
    # redirected CLAUDE_CONFIG_DIR would refresh into a scratch copy and
    # spend the parent's single-use refresh token to do it — the exact
    # accident `_write_redacted_credentials` exists to prevent.
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        process = await asyncio.create_subprocess_exec(
            cli_path,
            *args,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        logger.debug("Could not spawn `claude %s`: %s", " ".join(args), exc)
        return False

    try:
        async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
            return await process.wait() == 0
    except TimeoutError:
        logger.debug("`claude %s` timed out; killing it", " ".join(args))
        _terminate(process)
        return False
    except asyncio.CancelledError:
        # A cancelled connect must not leave a CLI behind; the orphan
        # guard in `main._kill_cli_children` does not know about this one.
        _terminate(process)
        raise


def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill ``process``, tolerating one that already exited."""
    try:
        process.kill()
    except (ProcessLookupError, OSError) as exc:
        logger.debug("Refresh probe was already gone: %s", exc)


def propagate(*, credential_path: Path | None, materialized_dir: Path | None) -> bool:
    """Copy the parent's fresh access token into a live session's config dir.

    The other half of the fix, and the half that spares the user a
    reconnect. Refreshing ``~/.claude`` does nothing for a CLI child that
    is already running against a snapshot taken before the refresh — it
    has no refresh token and will not go looking. Writing the new access
    token into the file it *does* read keeps that session alive in place.

    ``refreshToken`` is never written. The child's file keeps exactly the
    shape the SDK gave it, so the invariant that made the SDK redact it in
    the first place — a redirected config dir must not be able to rotate
    the parent's credentials — holds unchanged. This only ever moves a
    short-lived access token *downhill*, from the authority to the copy.

    Returns True when a newer token was written.
    """
    if credential_path is None or materialized_dir is None:
        return False
    child_path = materialized_dir / ".credentials.json"
    parent_expiry = read_expiry(credential_path)
    if parent_expiry is None:
        return False
    if parent_expiry <= (read_expiry(child_path) or 0):
        return False

    try:
        parent = json.loads(credential_path.read_text(encoding="utf-8"))
        oauth = parent.get("claudeAiOauth") if isinstance(parent, dict) else None
        if not isinstance(oauth, dict):
            return False
        oauth.pop("refreshToken", None)
        _write_private(child_path, json.dumps(parent))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Never fatal: the session is running, and the worst case without
        # this is the expiry failure it was already heading for.
        logger.debug("Could not propagate the refreshed token to %s: %s", child_path, exc)
        return False

    logger.info(
        "Propagated the refreshed access token into %s; %.0fs of life ahead",
        child_path,
        seconds_remaining(parent_expiry),
    )
    return True


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, mode 0600.

    Atomic because the CLI child may read this file at any moment and a
    half-written credential file is an authentication failure with a
    confusing cause. The temp file is created in the destination directory
    so the replace is a rename within one filesystem, and it is chmodded
    before it carries any content.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _now_ms() -> int:
    """Wall-clock milliseconds, matching ``expiresAt``'s own epoch.

    Wall clock rather than monotonic on purpose: this is compared against
    a timestamp minted by the auth server, so it has to be the same clock
    even though that makes it sensitive to the machine's time being wrong.
    """
    return int(time.time() * 1000)
