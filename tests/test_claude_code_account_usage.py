"""Tests for aic_dc.claude_code.account_usage.

The module answers the question ``RateLimitEvent`` cannot — *where do I
stand* — by making the same REST call the CLI makes for its own ``/usage``
panel. Two facts drive almost every test here:

1. **The units are not the event channel's.** ``utilization`` is a percent
   on this endpoint and a fraction on the SDK's, and ``resets_at`` is an
   ISO string here and Unix seconds there. Both conversions happen in
   :func:`normalise`, and both have a wrong answer that looks plausible —
   0.48% instead of 48%, a reset in 1970 instead of this afternoon — so
   they are asserted with real numbers rather than round ones.
2. **No credential is ever written and no token is ever refreshed.**
   Rotating the refresh token would invalidate the CLI's own copy. Every
   unhappy path here therefore ends in a *reason*, and the tests check the
   reason is specific enough to act on.

The payloads below are trimmed from a real 2026-08-29 response, including
its null windows and its codename keys.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from aic_dc.claude_code import account_usage
from aic_dc.claude_code.account_usage import (
    REASON_EXPIRED,
    REASON_FAILED,
    REASON_NO_CREDENTIALS,
    REASON_REDIRECTED,
    REASON_UNAUTHORIZED,
    AccountUsage,
    normalise,
)

# A real response, trimmed. Note `utilization` at 48.0 for a window the
# `limits` array reports as `percent: 48` — the two agree, which is why
# `normalise` reads one and not both.
LIVE = {
    "five_hour": {
        "utilization": 48.0,
        "resets_at": "2026-08-29T12:20:00.010711+00:00",
        "locked_reason": None,
    },
    "seven_day": {
        "utilization": 11.0,
        "resets_at": "2026-09-02T18:00:00.010729+00:00",
        "locked_reason": None,
    },
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "cinder_cove": None,
    "extra_usage": {"is_enabled": False, "disabled_reason": None},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 48,
            "severity": "normal",
            "resets_at": "2026-08-29T12:20:00.010711+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 11,
            "severity": "normal",
            "resets_at": "2026-09-02T18:00:00.010729+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 0,
            "severity": "normal",
            "resets_at": None,
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
}


def by_key(windows):
    return {w["key"]: w for w in windows}


class TestNormalise:
    """The endpoint's answer, in the units the browser already reads."""

    def test_percent_becomes_a_fraction(self):
        """48 percent is 0.48, not 48 and not 0.0048.

        The wrong answers are the interesting part: passing the percent
        through unchanged renders as 4800% against ``utilizationPercent``,
        which at least *looks* broken, while dividing twice renders as
        0.48% — a plausible figure nobody would question.
        """
        windows = by_key(normalise(LIVE))
        assert windows["session"]["utilization"] == pytest.approx(0.48)
        assert windows["weekly_all"]["utilization"] == pytest.approx(0.11)

    def test_iso_becomes_unix_seconds(self):
        """`resets_at` crosses the wire in the event channel's units."""
        session = by_key(normalise(LIVE))["session"]
        assert session["resets_at"] == 1788006000
        assert isinstance(session["resets_at"], int)

    def test_the_per_model_weekly_window_is_named_after_its_model(self):
        """**The row this whole module exists for.**

        A scoped weekly cap is the only place a per-model window appears,
        and its name is read from ``scope.model.display_name`` rather than
        mapped from an enum — which is why a model the enum has never
        heard of arrives labelled anyway.
        """
        windows = by_key(normalise(LIVE))
        assert "weekly_scoped:Fable" in windows
        assert windows["weekly_scoped:Fable"]["label"] == "Current week (Fable)"

    def test_a_window_that_has_not_started_is_kept(self):
        """0% with no reset is an answer, and the answer someone came for."""
        fable = by_key(normalise(LIVE))["weekly_scoped:Fable"]
        assert fable["utilization"] == 0.0
        assert fable["resets_at"] is None

    def test_the_five_hour_window_is_not_drawn_twice(self):
        """``limits`` wins outright; the flat keys are a fallback, not a merge."""
        keys = [w["key"] for w in normalise(LIVE)]
        assert len(keys) == len(set(keys))
        assert "five_hour" not in keys

    def test_the_flat_keys_answer_when_the_array_is_missing(self):
        """An older response shape still produces windows."""
        payload = {k: v for k, v in LIVE.items() if k != "limits"}
        windows = by_key(normalise(payload))
        assert windows["five_hour"]["utilization"] == pytest.approx(0.48)
        assert windows["five_hour"]["label"] == "5-hour"
        assert windows["seven_day"]["resets_at"] == 1788372000

    def test_an_unknown_kind_is_named_rather_than_dropped(self):
        """A window this build has never heard of is still a window."""
        payload = {"limits": [{"kind": "lunar_month", "percent": 5}]}
        window = normalise(payload)[0]
        assert window["label"] == "lunar month"
        assert window["utilization"] == pytest.approx(0.05)

    def test_an_entry_with_no_figure_is_dropped(self):
        """No figure is not a figure of zero — the rule this surface exists for."""
        payload = {"limits": [{"kind": "session", "percent": None}]}
        assert normalise(payload) == []

    def test_rubbish_is_survived(self):
        assert normalise(None) == []
        assert normalise([]) == []
        assert normalise({"limits": "soon"}) == []
        assert normalise({"limits": [{"kind": "session", "percent": True}]}) == []
        assert normalise({"limits": [{"kind": "session", "percent": -3}]}) == []

    def test_an_unparsable_reset_is_not_a_reset(self):
        payload = {"limits": [{"kind": "session", "percent": 1, "resets_at": "soon"}]}
        assert normalise(payload)[0]["resets_at"] is None


class TestCredentials:
    """Read-only, and specific about why not."""

    @pytest.fixture(autouse=True)
    def no_redirect(self, monkeypatch):
        monkeypatch.setattr(account_usage, "credential_redirect", lambda: None)

    def credentials(self, monkeypatch, tmp_path, payload):
        path = tmp_path / ".credentials.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(
            account_usage, "subscription_credential_path", lambda: path
        )
        return path

    def test_no_login_file_is_named_as_such(self, monkeypatch):
        monkeypatch.setattr(account_usage, "subscription_credential_path", lambda: None)
        token, reason, _ = account_usage._access_token()
        assert token is None
        assert reason == REASON_NO_CREDENTIALS

    def test_an_api_key_login_has_no_windows(self, monkeypatch, tmp_path):
        """A file with no OAuth block is not a failure to read the file."""
        self.credentials(monkeypatch, tmp_path, {"somethingElse": {}})
        token, reason, detail = account_usage._access_token()
        assert token is None
        assert reason == REASON_NO_CREDENTIALS
        assert "API-key" in detail

    def test_an_expired_token_is_reported_not_refreshed(self, monkeypatch, tmp_path):
        """**The refresh token is never used.**

        Rotating it would invalidate the copy the CLI holds and could lock
        the user out of their own editor, so expiry is a sentence rather
        than a repair — and the sentence names the thing that fixes it.
        """
        path = self.credentials(
            monkeypatch,
            tmp_path,
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "refreshToken": "refresh-me-not",
                    "expiresAt": 1_000_000,
                }
            },
        )
        token, reason, detail = account_usage._access_token()
        assert token is None
        assert reason == REASON_EXPIRED
        assert "Running a turn" in detail
        # And the file is untouched, refresh token and all.
        assert json.loads(path.read_text())["claudeAiOauth"]["refreshToken"] == (
            "refresh-me-not"
        )

    def test_a_live_token_is_returned(self, monkeypatch, tmp_path):
        self.credentials(
            monkeypatch,
            tmp_path,
            {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 99_999_999_999_999}},
        )
        token, reason, _ = account_usage._access_token()
        assert (token, reason) == ("tok", "")

    def test_a_token_with_no_expiry_is_tried(self, monkeypatch, tmp_path):
        """Absent is not expired — let the endpoint be the judge."""
        self.credentials(monkeypatch, tmp_path, {"claudeAiOauth": {"accessToken": "t"}})
        assert account_usage._access_token()[0] == "t"

    def test_unreadable_json_is_not_a_crash(self, monkeypatch, tmp_path):
        path = tmp_path / ".credentials.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(
            account_usage, "subscription_credential_path", lambda: path
        )
        assert account_usage._access_token()[1] == REASON_NO_CREDENTIALS


class TestSnapshot:
    """The cached reader, and every way it declines to guess."""

    @pytest.fixture(autouse=True)
    def token(self, monkeypatch):
        monkeypatch.setattr(account_usage, "credential_redirect", lambda: None)
        monkeypatch.setattr(
            account_usage, "_access_token", lambda *a, **k: ("tok", "", "")
        )

    def fetches(self, monkeypatch, *answers):
        """Install a fetch that returns (or raises) each answer in turn."""
        calls = []

        def fake(token, url=account_usage.USAGE_URL):
            calls.append(token)
            answer = answers[min(len(calls) - 1, len(answers) - 1)]
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(account_usage, "_fetch", fake)
        return calls

    async def test_a_reading_arrives_in_the_browsers_units(self, monkeypatch):
        self.fetches(monkeypatch, LIVE)
        answer = await AccountUsage().snapshot()
        assert answer["ok"] is True
        assert [w["label"] for w in answer["windows"]] == [
            "5-hour",
            "Current week",
            "Current week (Fable)",
        ]
        assert answer["fetched_at"]

    async def test_a_second_read_inside_the_ttl_is_the_cache(self, monkeypatch):
        calls = self.fetches(monkeypatch, LIVE)
        reader = AccountUsage()
        await reader.snapshot()
        await reader.snapshot()
        assert len(calls) == 1

    async def test_force_goes_back_to_the_endpoint(self, monkeypatch):
        calls = self.fetches(monkeypatch, LIVE)
        reader = AccountUsage()
        await reader.snapshot()
        await reader.snapshot(force=True)
        assert len(calls) == 2

    async def test_an_expired_cache_is_re_read(self, monkeypatch):
        calls = self.fetches(monkeypatch, LIVE)
        reader = AccountUsage(ttl=0)
        await reader.snapshot()
        await reader.snapshot()
        assert len(calls) == 2

    async def test_a_failure_serves_the_last_good_reading_marked_stale(
        self, monkeypatch
    ):
        """A dropped packet does not mean "you have no limits".

        Emptying the section on one failed read is the same confident
        wrong answer as painting an absent figure green, pointing the
        other way — so the last reading stands, labelled.
        """
        self.fetches(monkeypatch, LIVE, OSError("network down"))
        reader = AccountUsage(ttl=0)
        await reader.snapshot()
        answer = await reader.snapshot()
        assert answer["ok"] is True
        assert answer["stale"] is True
        assert answer["stale_reason"] == REASON_FAILED
        assert len(answer["windows"]) == 3

    async def test_a_first_failure_has_nothing_to_fall_back_on(self, monkeypatch):
        self.fetches(monkeypatch, OSError("network down"))
        answer = await AccountUsage().snapshot()
        assert answer["ok"] is False
        assert answer["reason"] == REASON_FAILED
        assert "network down" in answer["detail"]

    async def test_a_refused_token_says_so(self, monkeypatch):
        """401 is not "the network failed" — it names what to do about it."""
        self.fetches(
            monkeypatch,
            urllib.error.HTTPError(account_usage.USAGE_URL, 401, "no", {}, None),
        )
        answer = await AccountUsage().snapshot()
        assert answer["reason"] == REASON_UNAUTHORIZED
        assert "Running a turn" in answer["detail"]

    async def test_a_server_error_is_not_an_auth_error(self, monkeypatch):
        self.fetches(
            monkeypatch,
            urllib.error.HTTPError(account_usage.USAGE_URL, 503, "no", {}, None),
        )
        answer = await AccountUsage().snapshot()
        assert answer["reason"] == REASON_FAILED
        assert "503" in answer["detail"]

    async def test_a_redirected_engine_is_never_asked(self, monkeypatch):
        """**These windows would be a real figure about another account.**

        With the engine pointed at Bedrock, Vertex or an API key, turns do
        not bill to the subscription this endpoint describes (R-9). The
        reader is told which variable did it, and no request goes out.
        """
        calls = self.fetches(monkeypatch, LIVE)
        monkeypatch.setattr(
            account_usage,
            "credential_redirect",
            lambda: "$ANTHROPIC_API_KEY is set, so turns bill to that key.",
        )
        answer = await AccountUsage().snapshot()
        assert answer["ok"] is False
        assert answer["reason"] == REASON_REDIRECTED
        assert "ANTHROPIC_API_KEY" in answer["detail"]
        assert calls == []

    async def test_no_credentials_never_reaches_the_network(self, monkeypatch):
        calls = self.fetches(monkeypatch, LIVE)
        monkeypatch.setattr(
            account_usage,
            "_access_token",
            lambda *a, **k: (None, REASON_NO_CREDENTIALS, "nothing to read"),
        )
        answer = await AccountUsage().snapshot()
        assert answer["reason"] == REASON_NO_CREDENTIALS
        assert calls == []


class TestRequest:
    """What actually goes on the wire."""

    def test_the_request_is_the_cli_s(self, monkeypatch):
        """Same URL, same beta header, bearer token — and nothing else.

        The beta header is a contract about the response shape, so it is
        pinned: :func:`normalise` reads the shape observed under this
        value.
        """
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(LIVE).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["headers"] = dict(request.headers)
            seen["timeout"] = timeout
            return FakeResponse()

        monkeypatch.setattr(account_usage.urllib.request, "urlopen", fake_urlopen)
        payload = account_usage._fetch("tok")

        assert payload == LIVE
        assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
        assert seen["headers"]["Authorization"] == "Bearer tok"
        assert seen["headers"]["Anthropic-beta"] == account_usage.OAUTH_BETA
        assert seen["timeout"] == account_usage.REQUEST_TIMEOUT
