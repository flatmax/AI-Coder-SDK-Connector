"""Account usage — the windows the CLI's ``/usage`` panel actually draws.

**Why this module exists.** ``specs5/5-webapp/viewers-hud.md`` § *The
Rate-Limit Channel Is An Alarm, Not A Usage Panel* establishes that
``RateLimitEvent`` cannot answer "where do I stand": it is a transition
notice, it carries one window, and the record measured 2026-08-29 carried
no ``utilization`` at all while the CLI's own panel showed 37% for that
same window and two further windows beside it. That section closes by
saying the way to reopen the question is **the SDK growing a usage query**.
It has not. The CLI does not use one either.

**What the CLI actually does**, read out of the shipped binary
(``@anthropic-ai/claude-code`` 2.x, ``bin/claude.exe``): it calls
``GET /api/oauth/usage`` on ``api.anthropic.com`` itself, with the OAuth
credentials it stores in ``~/.claude/.credentials.json``, and renders the
result. Its own ``fetchUtilization`` is that request and nothing more. The
figures were never on the SDK's wire because the CLI does not put them
there — they are a REST call away, on a channel that runs beside the
engine entirely. So this module makes the same call.

**The units are not the event channel's, and this is the trap.** The same
two field *names* mean different things on the two channels:

===================  =======================  ==========================
field                ``RateLimitEvent``       ``/api/oauth/usage``
===================  =======================  ==========================
``utilization``      fraction, 0.0–1.0        **percent, 0–100**
``resets_at``        **Unix seconds**         **ISO 8601 string**
===================  =======================  ==========================

A record from here rendered by a reader expecting the other reads as 4800%
used and a window that reset in 1970 — or, worse, as 0.48% used, which is
a plausible number and would never be questioned. :func:`normalise`
therefore converts **here, once**, into the event channel's convention, so
that ``webapp/src/rate-limit.js`` keeps one definition of what a window
record is and the browser cannot pick the wrong one
(``specs5/next.md`` § C3).

**Nothing is written, and the refresh token is never used.** The CLI
refreshes its own OAuth token on a 401 and rewrites the credential file.
Doing that from here would rotate the token out from under the CLI copy
and could lock the user out of their own editor, which is a far worse
outcome than a missing gauge. This module reads the access token, and an
expired one is reported as :data:`REASON_EXPIRED` rather than repaired.
That costs nothing in practice: AIC-DC *spawns* the CLI, so the engine
running a turn is what keeps the file fresh.

**A redirected CLI is not this account.** When the environment points the
CLI at Bedrock, Vertex, an API key, or a different base URL, turns bill
somewhere that ``api.anthropic.com/api/oauth/usage`` knows nothing about
(``specs5/plan/risks.md`` R-9). Answering with subscription windows then
would put a confident, irrelevant figure on screen — § B5's rule again.
Those cases report a reason instead.

Governing spec: ``specs5/5-webapp/viewers-hud.md`` § *The Rate-Limit
Channel Is An Alarm, Not A Usage Panel*.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .health import credential_redirect, subscription_credential_path

logger = logging.getLogger(__name__)


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# The CLI sends this on every OAuth-authenticated call. Kept identical
# because a beta header is a contract about the response shape, and the
# shape :func:`normalise` reads is the one observed under this value.
OAUTH_BETA = "oauth-2025-04-20"

# Seconds. The CLI gives its own fetch 5 s; this is a background read for a
# tab that already waits 3–14 s on the context breakdown beside it, so a
# little more patience costs nothing and a slow network is not an error.
REQUEST_TIMEOUT = 10.0

# How long a snapshot is served before the next read goes out. The windows
# move on the scale of a turn, and the panel is opened by hand — a minute
# is far inside the resolution anyone reads these at, and it keeps a
# reader mashing Refresh from becoming a request per click.
CACHE_TTL = 60.0

# Reasons, named rather than spelled out at each raise, because the browser
# switches on them: each is a different sentence to the reader and two of
# them are not failures at all.
REASON_NO_CREDENTIALS = "no-credentials"
REASON_EXPIRED = "expired"
REASON_REDIRECTED = "redirected"
REASON_UNAUTHORIZED = "unauthorized"
REASON_FAILED = "failed"


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------


def _fraction(percent: Any) -> float | None:
    """A 0–100 percent as the 0.0–1.0 fraction the browser expects.

    ``None`` for anything non-numeric or negative rather than a zero: an
    absent figure is not a figure of zero, which is the one mistake this
    whole surface exists to avoid.
    """
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    value = float(percent)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value / 100.0


def _reset_seconds(value: Any) -> int | None:
    """An ISO 8601 instant as Unix seconds; ``None`` when there is none.

    A null ``resets_at`` is normal here and means the window is not
    counting yet — the ``weekly_scoped`` entry for a model you have not
    used this week comes through at 0% with no reset. It is served as a
    window without a reset line rather than dropped, because "0% of your
    Fable week" is exactly the reading someone opened the panel for.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.debug("Unparsable resets_at from the usage endpoint: %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


# Labels for the ``limits`` entries. The CLI writes "Current week
# (<model>)" for a scoped one and this follows it, because the two panels
# describing the same window differently is how a user ends up believing
# they are two windows.
_KIND_LABELS = {
    "session": "5-hour",
    "weekly_all": "Current week",
    "weekly_scoped": "Current week",
}

# The flat keys, for the fallback below. ``seven_day_opus`` and its
# siblings are per-model weekly caps under their old names.
_FLAT_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "Current week",
    "seven_day_opus": "Current week (Opus)",
    "seven_day_sonnet": "Current week (Sonnet)",
    "seven_day_oauth_apps": "Current week (OAuth apps)",
}


def _limit_window(entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    """One ``limits[]`` entry as a window record, or ``None`` if it says nothing."""
    utilization = _fraction(entry.get("percent"))
    if utilization is None:
        return None
    kind = entry.get("kind")
    label = _KIND_LABELS.get(kind) if isinstance(kind, str) else None
    if label is None:
        # An unknown kind is still a window the account is limited by, and
        # naming it badly beats not naming it — the rule `limitTypeLabel`
        # already follows one layer out.
        label = str(kind).replace("_", " ") if kind else f"Window {index + 1}"
    key = str(kind) if kind else f"limit-{index}"

    # **The only place Fable appears.** Per-model weekly caps arrive as
    # `weekly_scoped` entries carrying a display name, not as members of a
    # fixed enum — which is why no amount of work on `RateLimitType` could
    # ever have produced this row, and why the name is read rather than
    # mapped. A model shipped tomorrow arrives here already labelled.
    scope = entry.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        name = model.get("display_name") if isinstance(model, dict) else None
        if isinstance(name, str) and name:
            label = f"{label} ({name})"
            key = f"{key}:{name}"

    window: dict[str, Any] = {
        "key": key,
        "label": label,
        "utilization": utilization,
        "resets_at": _reset_seconds(entry.get("resets_at")),
    }
    severity = entry.get("severity")
    if isinstance(severity, str) and severity:
        window["severity"] = severity
    return window


def normalise(payload: Any) -> list[dict[str, Any]]:
    """The endpoint's answer as window records the browser can render.

    Reads ``limits`` in preference to the flat ``five_hour`` /
    ``seven_day`` keys: it is the array the CLI's own panel is built from,
    it carries the scoped per-model rows the flat keys have no place for,
    and the two agree where they overlap (``five_hour.utilization`` and the
    ``session`` entry's ``percent`` were both 48 in the measured payload).
    The flat keys are the fallback for an account whose response predates
    the array, not a second source to merge — merging them would draw the
    five-hour window twice.
    """
    if not isinstance(payload, dict):
        return []

    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        windows = [
            window
            for index, entry in enumerate(limits)
            if isinstance(entry, dict)
            for window in (_limit_window(entry, index),)
            if window is not None
        ]
        if windows:
            return windows

    windows = []
    for key, label in _FLAT_LABELS.items():
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        # Flat-key utilisation is a percent here too, despite the name it
        # shares with the SDK's fraction. Measured, not assumed.
        utilization = _fraction(entry.get("utilization"))
        if utilization is None:
            continue
        windows.append(
            {
                "key": key,
                "label": label,
                "utilization": utilization,
                "resets_at": _reset_seconds(entry.get("resets_at")),
            }
        )
    return windows


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _access_token(now: float | None = None) -> tuple[str | None, str, str]:
    """``(token, reason, detail)`` — the OAuth access token, or why not.

    Reads the same file :func:`health.detect_credentials` reports, and
    honours ``$CLAUDE_CONFIG_DIR`` through it, so the panel and the health
    banner cannot disagree about which login is in play.
    """
    path: Path | None = subscription_credential_path()
    if path is None:
        return None, REASON_NO_CREDENTIALS, "No subscription login file to read."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, REASON_NO_CREDENTIALS, f"Could not read {path.name}: {exc}"

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return (
            None,
            REASON_NO_CREDENTIALS,
            "The login file holds no OAuth access token — an API-key login "
            "has no usage windows to read.",
        )

    # Milliseconds, matching the file. Checked before the request rather
    # than after a 401 so the reader is told "your login expired" instead
    # of "the server refused us", which sends them somewhere else.
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        if expires_at / 1000.0 <= (time.time() if now is None else now):
            return (
                None,
                REASON_EXPIRED,
                "The stored OAuth token has expired. Running a turn refreshes it.",
            )
    return token, "", ""


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def _fetch(token: str, url: str = USAGE_URL) -> Any:
    """Blocking GET. Called in a thread; raises for the caller to classify."""
    request = urllib.request.Request(  # noqa: S310 - constant https URL
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": "aic-dc",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


class AccountUsage:
    """Cached reader for the account's rate-limit windows.

    One instance per service. The cache exists because the panel is
    re-read on every tab entry and every Refresh, while the figures move
    on the scale of a turn; :data:`CACHE_TTL` is the whole policy.

    **A failed read falls back to the last good one, marked stale.** The
    alternative is a section that empties itself on one dropped packet,
    which reads as "you have no limits" — the same confident wrong answer
    in the other direction. The staleness and its reason are both carried,
    so the browser can say *when* the figure is from.
    """

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """The windows, from cache when fresh enough."""
        async with self._lock:
            age = time.monotonic() - self._cached_at
            if self._cached is not None and not force and age < self._ttl:
                return dict(self._cached)
            result = await self._read()
            if result.get("ok"):
                self._cached = result
                self._cached_at = time.monotonic()
                return dict(result)
            # Keep serving the last good answer, aged and labelled.
            if self._cached is not None:
                stale = dict(self._cached)
                stale["stale"] = True
                stale["stale_reason"] = result.get("reason", REASON_FAILED)
                stale["stale_detail"] = result.get("detail", "")
                return stale
            return result

    async def _read(self) -> dict[str, Any]:
        redirect = credential_redirect()
        if redirect:
            return {"ok": False, "reason": REASON_REDIRECTED, "detail": redirect}

        token, reason, detail = _access_token()
        if token is None:
            return {"ok": False, "reason": reason, "detail": detail}

        try:
            payload = await asyncio.to_thread(_fetch, token)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {
                    "ok": False,
                    "reason": REASON_UNAUTHORIZED,
                    "detail": (
                        f"The usage endpoint refused the stored token (HTTP "
                        f"{exc.code}). Running a turn refreshes it."
                    ),
                }
            logger.debug("Usage endpoint returned HTTP %s", exc.code)
            return {
                "ok": False,
                "reason": REASON_FAILED,
                "detail": f"The usage endpoint returned HTTP {exc.code}.",
            }
        except Exception as exc:  # network, DNS, timeout, malformed JSON
            logger.debug("Usage endpoint read failed: %s", exc)
            return {
                "ok": False,
                "reason": REASON_FAILED,
                "detail": f"Could not read account usage: {exc}",
            }

        return {
            "ok": True,
            "windows": normalise(payload),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": USAGE_URL,
        }
