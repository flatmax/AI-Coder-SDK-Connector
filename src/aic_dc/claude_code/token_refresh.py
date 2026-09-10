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

"Did not move" is not "could not refresh"
-----------------------------------------
The re-read above answers *whether a rung refreshed*, and this module used
to read it as whether the token was **all right** — so a ladder that ran
and moved nothing reported a broken login. Measured on 2026-09-10 against
a healthy token with 7.9 hours of life left: ``claude auth status --json``
exits 0, prints account metadata and carries no expiry field at all, and
``claude -p hi --max-turns 1`` exits 0 in about three seconds. Neither
changed ``expiresAt`` by a byte. The CLI refreshes when *it* considers a
refresh due, and its threshold is narrower than
:data:`REFRESH_MARGIN_SECONDS`, so every connect landing in the gap between
the two margins ran both rungs, saw nothing move, and told the user to go
fix a credential that was fine.

So the criterion is the token's *usability*, asked once after the ladder: a
token still ahead of ``now`` has not failed, whoever renewed it or didn't.
Two states remain degradations, and they are separated because they have
different remedies — an access token past its own ``expiresAt`` that the
ladder could not move (:data:`REFRESH_FAILED_DETAIL`), and a
``refreshTokenExpiresAt`` in the past, which no invocation of any CLI can
repair and which is therefore checked *before* a subprocess is spent on it
(:data:`LOGIN_REQUIRED_DETAIL`). A banner that fires on ordinary restarts
is the fastest way to teach a user to ignore the one that matters.

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

# The access token has lapsed and the ladder could not move it. The remedy
# is the one observed to work by hand, and it is deliberately vague about
# *why* the CLI would not refresh, because this branch does not know.
REFRESH_FAILED_DETAIL = (
    "The Claude subscription access token has expired and the CLI would not "
    "renew it. Run `claude` in a terminal to renew it, then restart the engine."
)

# The refresh token itself has lapsed. A different failure with a different
# fix: no rung of the ladder, and no restart, can recover this one — the
# credential has to be signed in again.
LOGIN_REQUIRED_DETAIL = (
    "The Claude subscription login has expired: its refresh token has lapsed, "
    "so nothing can renew it automatically. Run `claude auth login` in a "
    "terminal to sign in again."
)

# What this module can put on the health banner. Held as constants and
# published as a set because a degradation is a *standing* condition that
# the record deduplicates and retires by its text: a caller that refreshes
# successfully later has to be able to name what it withdrew. An
# interpolated figure would make one standing condition read as a new one
# on every watchdog tick.
DEGRADATION_SENTENCES: tuple[str, ...] = (REFRESH_FAILED_DETAIL, LOGIN_REQUIRED_DETAIL)


@dataclass(frozen=True)
class RefreshOutcome:
    """What :func:`ensure_fresh` did, for the log and the health banner.

    ``detail`` is always safe to show a user and never contains a token.
    """

    #: True when the parent's token is usable now: it never needed
    #: refreshing, a rung moved ``expiresAt`` forward, or the ladder
    #: changed nothing and the token is still ahead of the clock anyway.
    #: False only for the two states a user has to act on.
    ok: bool
    #: True when a refresh was actually attempted (a subprocess ran). A
    #: lapsed refresh token is a failure with this ``False``: it is decided
    #: from the file, because no subprocess could have changed the answer.
    attempted: bool
    #: One sentence for the health banner, or ``None`` when there is
    #: nothing a *user* should be told. A rung that ran and refreshed
    #: nothing is logged, not banner-worthy.
    detail: str | None = None


def _oauth_block(credential_path: Path) -> dict | None:
    """The ``claudeAiOauth`` object in ``credential_path``, or ``None``.

    ``None`` when the file is missing, unreadable, not JSON, or carries no
    OAuth block — every one of which is a legitimate state (an API-key
    machine has no such file) and none of which is this module's business
    to complain about. Callers treat ``None`` as "not a subscription login
    I can reason about" and do nothing.
    """
    try:
        data = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.debug("Cannot read %s for its OAuth block: %s", credential_path, exc)
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def read_expiry(credential_path: Path) -> int | None:
    """The ``claudeAiOauth.expiresAt`` in ``credential_path``, in ms.

    ``None`` for every shape that is not a subscription login — see
    :func:`_oauth_block` — and for an ``expiresAt`` that is not a number.
    """
    oauth = _oauth_block(credential_path)
    if oauth is None:
        return None
    expires_at = oauth.get("expiresAt")
    return expires_at if isinstance(expires_at, int) else None


def read_refresh_expiry(credential_path: Path) -> int | None:
    """The ``claudeAiOauth.refreshTokenExpiresAt`` in ``credential_path``, in ms.

    The field that decides whose problem a failed refresh is. While it is in
    the future, a refresh that did not happen is a refresh the CLI did not
    consider due, and nothing is wrong; once it is in the past, no
    invocation of any CLI can renew anything and only an interactive login
    will do.

    ``None`` means *cannot tell* — the field is absent, or is not a number —
    and cannot tell is never reported as a failure. The observed file
    carries it (see the module docstring's key list), but a credential
    written by an older CLI need not, and inventing a lapsed login from a
    missing field would be the same defect this distinction exists to fix.
    """
    oauth = _oauth_block(credential_path)
    if oauth is None:
        return None
    expires_at = oauth.get("refreshTokenExpiresAt")
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

    # The one state no rung can repair, asked before a subprocess is spent
    # finding that out. It is also the only failure here that is genuinely
    # the user's to act on, and its remedy is not the other one's.
    refresh_expiry = read_refresh_expiry(credential_path)
    if refresh_expiry is not None and seconds_remaining(refresh_expiry) <= 0:
        logger.warning(
            "The refresh token in %s lapsed; only an interactive login can renew this credential",
            credential_path,
        )
        return RefreshOutcome(ok=False, attempted=False, detail=LOGIN_REQUIRED_DETAIL)

    remaining = seconds_remaining(before) if before is not None else 0.0
    # Neither this line nor the one below says "before connect", though both
    # did: the watchdog calls this mid-session too, so half of every such
    # sentence was wrong wherever it was read.
    logger.info(
        "Access token in %s expires in %.0fs; asking the CLI to renew it",
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
                    detail=f"Access token renewed (`claude {' '.join(rung)}`).",
                )
        logger.debug("`claude %s` did not move the token expiry", " ".join(rung))

    # Every rung ran and the expiry never moved, which is two different
    # states wearing one face. Ask the only question that separates them:
    # is the token usable now? Re-read rather than reasoned about, because
    # the user's own terminal may have refreshed it while the ladder ran.
    after = read_expiry(credential_path)
    if after is not None and seconds_remaining(after) > 0:
        # Nothing is wrong. The CLI did not consider a refresh due, which is
        # what a token inside *our* margin and outside its own looks like —
        # and the watchdog will ask again before it lapses.
        logger.info(
            "The CLI did not renew the access token in %s and did not need "
            "to: %.0fs of life remain",
            credential_path,
            seconds_remaining(after),
        )
        return RefreshOutcome(ok=True, attempted=True)

    # A token past its own expiry that the ladder could not move. Said
    # plainly, with the remedy that is known to work by hand, because the
    # alternative the user actually gets is the CLI's own opaque line.
    logger.warning(
        "Could not refresh the lapsed access token in %s; turns will fail "
        "with an authentication error until it is renewed",
        credential_path,
    )
    return RefreshOutcome(ok=False, attempted=True, detail=REFRESH_FAILED_DETAIL)


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
