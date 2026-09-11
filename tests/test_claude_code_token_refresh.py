"""Tests for aic_dc.claude_code.token_refresh.

The bug under test is a *silent* one: a resumed session's CLI child gets a
``.credentials.json`` with ``refreshToken`` stripped by the SDK, so it holds
an access token it cannot renew and dies with "OAuth session expired and
could not be refreshed" the moment ``expiresAt`` passes. Nothing in AIC⚡DC
was renewing it, because the only CLI that could was never the one being
spawned.

Two properties matter more than any individual assertion here, and both are
asserted explicitly at the bottom:

- **A refresh never leaks a refresh token downhill.** :func:`propagate`
  writes the child's file, and the whole reason the SDK redacts that field
  is that a token spent under a redirected config dir revokes the parent's.
- **A failed refresh is never fatal.** The pre-flight runs before connect;
  refusing a session that would have worked is worse than the expiry it
  guards against.

Every test builds its own credential files in ``tmp_path``. Nothing here
reads the developer's real ``~/.claude``, and no test spawns a real CLI —
the ladder is driven through a fake ``create_subprocess_exec``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aic_dc.claude_code import token_refresh
from aic_dc.claude_code.token_refresh import (
    LOGIN_REQUIRED_DETAIL,
    REFRESH_FAILED_DETAIL,
    REFRESH_MARGIN_SECONDS,
    ensure_fresh,
    needs_refresh,
    propagate,
    read_expiry,
    read_refresh_expiry,
    seconds_remaining,
)

NOW_MS = 1_788_700_000_000


def _creds(
    expires_at: int,
    *,
    refresh_token: str | None = "refresh-secret",
    refresh_expires_at: int | None = None,
) -> str:
    oauth: dict[str, object] = {
        "accessToken": f"access-for-{expires_at}",
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
    if refresh_token is not None:
        oauth["refreshToken"] = refresh_token
    if refresh_expires_at is not None:
        oauth["refreshTokenExpiresAt"] = refresh_expires_at
    return json.dumps({"claudeAiOauth": oauth})


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading the expiry
# ---------------------------------------------------------------------------


def test_read_expiry_returns_the_oauth_timestamp(tmp_path: Path) -> None:
    path = _write(tmp_path / ".credentials.json", _creds(NOW_MS + 8 * 3600_000))
    assert read_expiry(path) == NOW_MS + 8 * 3600_000


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json at all",
        json.dumps({"somethingElse": {"expiresAt": 1}}),
        json.dumps({"claudeAiOauth": {"expiresAt": "not-a-number"}}),
        json.dumps(["a", "list"]),
    ],
    ids=["empty", "garbage", "no-oauth-block", "wrong-type", "not-an-object"],
)
def test_read_expiry_tolerates_every_shape_that_is_not_ours(
    tmp_path: Path, content: str
) -> None:
    """None, not an exception. An API-key machine has no OAuth block, and a
    pre-flight check must not turn that legitimate state into a failure."""
    assert read_expiry(_write(tmp_path / ".credentials.json", content)) is None


def test_read_expiry_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert read_expiry(tmp_path / "nope.json") is None


def test_read_refresh_expiry_returns_the_refresh_tokens_own_timestamp(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / ".credentials.json",
        _creds(NOW_MS, refresh_expires_at=NOW_MS + 30 * 86_400_000),
    )
    assert read_refresh_expiry(path) == NOW_MS + 30 * 86_400_000


def test_a_credential_without_the_refresh_expiry_reads_as_cannot_tell(
    tmp_path: Path,
) -> None:
    """Absent is not lapsed. A file written by an older CLI need not carry
    the field, and inventing an expired login from a missing one would be
    the same false alarm this distinction exists to remove."""
    path = _write(tmp_path / ".credentials.json", _creds(NOW_MS))
    assert read_refresh_expiry(path) is None


@pytest.mark.parametrize(
    "content",
    ["", "not json", json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "soon"}})],
    ids=["empty", "garbage", "wrong-type"],
)
def test_read_refresh_expiry_tolerates_the_shapes_read_expiry_does(
    tmp_path: Path, content: str
) -> None:
    assert read_refresh_expiry(_write(tmp_path / ".credentials.json", content)) is None


# ---------------------------------------------------------------------------
# Deciding whether to refresh
# ---------------------------------------------------------------------------


def test_a_token_with_hours_left_is_not_refreshed() -> None:
    assert needs_refresh(NOW_MS + 8 * 3600_000, now_ms=NOW_MS) is False


def test_a_token_inside_the_margin_is_refreshed() -> None:
    inside = NOW_MS + int((REFRESH_MARGIN_SECONDS - 60) * 1000)
    assert needs_refresh(inside, now_ms=NOW_MS) is True


def test_an_already_expired_token_is_refreshed() -> None:
    assert needs_refresh(NOW_MS - 3600_000, now_ms=NOW_MS) is True


def test_an_unknown_expiry_is_not_a_reason_to_refresh() -> None:
    """A machine on Bedrock or an API key has no expiry to read. Spawning a
    CLI at it would be a subprocess spent to learn nothing."""
    assert needs_refresh(None, now_ms=NOW_MS) is False


def test_seconds_remaining_goes_negative_after_expiry() -> None:
    assert seconds_remaining(NOW_MS - 5000, now_ms=NOW_MS) == pytest.approx(-5.0)


# ---------------------------------------------------------------------------
# The refresh ladder
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, returncode: int) -> None:
        self._returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True


def _spy_exec(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_call,
    returncode: int = 0,
    yield_inside: bool = False,
):
    """Replace subprocess spawning with a recorder. Returns the call list.

    ``yield_inside`` hands the loop back while the fake "subprocess" is in
    flight, and before ``on_call`` has had a chance to rewrite the
    credential. Only the concurrency tests need it, and they need it
    badly: these fakes otherwise complete without ever suspending, so a
    second caller would never get to run and a test for serialisation
    would pass whether or not anything was serialised.
    """
    calls: list[dict[str, object]] = []

    async def fake_exec(program, *args, env=None, cwd=None, **kwargs):
        calls.append({"program": program, "args": args, "env": env or {}, "cwd": cwd})
        if yield_inside:
            await asyncio.sleep(0)
        on_call(len(calls))
        return _FakeProcess(returncode)

    monkeypatch.setattr(
        token_refresh.asyncio, "create_subprocess_exec", fake_exec, raising=True
    )
    return calls


async def test_a_healthy_token_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / ".credentials.json", _creds(_far_future()))
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is True
    assert outcome.attempted is False
    assert calls == []


async def test_the_first_rung_wins_when_it_moves_the_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude auth status` costs no tokens, so if it refreshes, the ladder
    stops there and the expensive rung never runs."""
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))

    def refresh_on_first_call(n: int) -> None:
        _write(path, _creds(_far_future()))

    calls = _spy_exec(monkeypatch, on_call=refresh_on_first_call)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is True
    assert outcome.attempted is True
    assert len(calls) == 1
    assert calls[0]["args"] == ("auth", "status", "--json")


async def test_the_ladder_escalates_when_the_cheap_rung_does_not_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which invocation refreshes is not a documented contract, so a rung
    that exits 0 without moving `expiresAt` must not be believed."""
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))

    def refresh_on_second_call(n: int) -> None:
        if n == 2:
            _write(path, _creds(_far_future()))

    calls = _spy_exec(monkeypatch, on_call=refresh_on_second_call)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is True
    assert [call["args"] for call in calls] == list(token_refresh.REFRESH_LADDER)


async def test_a_ladder_that_moved_nothing_is_not_a_failure_while_the_token_lives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect reported on 2026-09-10, as a test.

    Measurement says neither rung renews a token the CLI does not consider
    due, and our margin is wider than the CLI's. So every connect landing in
    the gap between the two ran the whole ladder, saw nothing move, and told
    the user to go fix a credential with minutes of life still on it.
    "Did not move" is not "could not refresh": the criterion is whether the
    token is usable, and this one is.
    """
    path = _write(tmp_path / ".credentials.json", _creds(_inside_margin()))
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is True
    # The ladder did run — this is not the healthy-token early return.
    assert outcome.attempted is True
    assert len(calls) == len(token_refresh.REFRESH_LADDER)
    # And nothing reaches the banner. A log line, not a wolf.
    assert outcome.detail is None


async def test_a_lapsed_token_the_ladder_cannot_move_reports_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that survives the fix above: past its own expiry, so the
    next turn fails with the CLI's opaque authentication line."""
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))
    _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is False
    assert outcome.attempted is True
    assert outcome.detail == REFRESH_FAILED_DETAIL
    # The sentence names the thing that is known to work by hand.
    assert "claude" in REFRESH_FAILED_DETAIL
    assert "restart" in REFRESH_FAILED_DETAIL.lower()


async def test_a_lapsed_refresh_token_asks_for_a_login_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure here that no rung could ever repair, so no rung runs.

    It is decided from the file, which is why it is the one degradation that
    reports ``attempted=False``.
    """
    path = _write(
        tmp_path / ".credentials.json",
        _creds(_expired(), refresh_expires_at=_expired()),
    )
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is False
    assert outcome.attempted is False
    assert calls == []
    assert outcome.detail == LOGIN_REQUIRED_DETAIL


async def test_a_refresh_token_with_life_left_still_runs_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The near case, which must not be read as the lapsed one: a refresh
    token good for another month is the ordinary state of a working login."""
    path = _write(
        tmp_path / ".credentials.json",
        _creds(_expired(), refresh_expires_at=_far_future()),
    )
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert calls, "a live refresh token is not a reason to skip the ladder"
    assert outcome.detail == REFRESH_FAILED_DETAIL


def test_the_two_degradations_do_not_share_a_sentence() -> None:
    """Why they are separated at all: the banner is the only thing the user
    sees, and one of the two remedies does not work for the other. A restart
    cannot recover a lapsed refresh token, and a re-login is not needed for
    a CLI that merely declined to renew."""
    assert REFRESH_FAILED_DETAIL != LOGIN_REQUIRED_DETAIL
    assert "auth login" in LOGIN_REQUIRED_DETAIL
    assert "auth login" not in REFRESH_FAILED_DETAIL
    # Published as a set, because the caller withdraws them by name.
    assert set(token_refresh.DEGRADATION_SENTENCES) == {
        REFRESH_FAILED_DETAIL,
        LOGIN_REQUIRED_DETAIL,
    }


async def test_the_probe_pins_the_real_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one invariant that keeps a refresh from destroying the login: a
    server running under a redirected CLAUDE_CONFIG_DIR must still refresh
    into the real file, or it spends the parent's single-use refresh token
    on a scratch copy."""
    real = tmp_path / "real"
    real.mkdir()
    path = _write(real / ".credentials.json", _creds(_expired()))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "scratch"))
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    await ensure_fresh(credential_path=path, config_dir=real, cli_path="/bin/claude")

    assert calls, "expected the ladder to run"
    for call in calls:
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(real)


async def test_no_credential_file_means_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_exec(monkeypatch, on_call=lambda n: None)

    outcome = await ensure_fresh(
        credential_path=None, config_dir=tmp_path, cli_path="/bin/claude"
    )

    assert outcome.ok is True
    assert outcome.attempted is False
    assert calls == []


async def test_a_cli_that_cannot_be_spawned_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing binary, a permissions error — reported, never raised."""
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))

    async def boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(
        token_refresh.asyncio, "create_subprocess_exec", boom, raising=True
    )

    outcome = await ensure_fresh(
        credential_path=path, config_dir=tmp_path, cli_path="/nope/claude"
    )

    assert outcome.ok is False


# ---------------------------------------------------------------------------
# Serialising concurrent refreshes
# ---------------------------------------------------------------------------
#
# R-14, as tests. The refresh token is single-use and the callers are per
# *session*: a connect-time pre-flight and a watchdog each call
# `ensure_fresh`, sessions are per repo, and every watchdog wakes the same
# margin before the same expiry. Two of them inside that margin used to
# read the same `expiresAt`, both decide a refresh was due, and the second
# spend a token the first had already redeemed.


async def test_two_concurrent_refreshes_spawn_one_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-14's tripwire: one rung, not two.

    The second caller does not merely lose a race — it never runs a
    ladder at all. It wakes to the file the first one refreshed, and
    `attempted is False` is how we know the deciding read happened inside
    the lock rather than before it.
    """
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))

    def refresh_on_first_call(n: int) -> None:
        _write(path, _creds(_far_future()))

    calls = _spy_exec(monkeypatch, on_call=refresh_on_first_call, yield_inside=True)

    first, second = await asyncio.gather(
        ensure_fresh(credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"),
        ensure_fresh(credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"),
    )

    assert len(calls) == 1
    assert (first.ok, first.attempted) == (True, True)
    assert (second.ok, second.attempted) == (True, False)


async def test_many_converging_watchdogs_still_spawn_one_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape the risk is actually written about: *n* open repos give
    2*n* callers on one credential file, and their watchdogs converge
    because they all wake the same margin before the same expiry."""
    path = _write(tmp_path / ".credentials.json", _creds(_inside_margin()))

    def refresh_on_first_call(n: int) -> None:
        _write(path, _creds(_far_future()))

    calls = _spy_exec(monkeypatch, on_call=refresh_on_first_call, yield_inside=True)

    outcomes = await asyncio.gather(
        *(
            ensure_fresh(
                credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
            )
            for _ in range(8)
        )
    )

    assert len(calls) == 1
    assert all(outcome.ok for outcome in outcomes)
    assert [outcome.attempted for outcome in outcomes].count(True) == 1


async def test_a_ladder_that_cannot_fix_it_is_re_run_by_each_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded price of the mitigation, pinned rather than glossed.

    When the ladder *works*, the lock turns 2*n* refreshes into one. When
    it cannot — a lapsed token no rung will move — each caller still runs
    its own ladder, serially, so the last one waits for the sum of the
    others rather than for the longest. The subprocess count is no worse
    than it was without a lock; only the latency is, and the alternative
    is caching a failure the user's own terminal may have just fixed.
    """
    path = _write(tmp_path / ".credentials.json", _creds(_expired()))
    calls = _spy_exec(monkeypatch, on_call=lambda n: None, yield_inside=True)

    outcomes = await asyncio.gather(
        *(
            ensure_fresh(
                credential_path=path, config_dir=tmp_path, cli_path="/bin/claude"
            )
            for _ in range(2)
        )
    )

    assert [outcome.detail for outcome in outcomes] == [REFRESH_FAILED_DETAIL] * 2
    assert len(calls) == 2 * len(token_refresh.REFRESH_LADDER)


def test_the_refresh_mutex_rebinds_to_each_event_loop() -> None:
    """Why the mutex is built on demand instead of at import.

    An `asyncio.Lock` binds itself to a loop the first time it is
    contended and refuses every loop after that, so a module-level
    instance would serialise the first batch of callers and then raise
    `bound to a different event loop` for the rest of the process. One
    loop per server run makes that invisible in production and certain in
    the test suite, which gets a fresh loop per test — this is the only
    test that would notice, so it contends deliberately.
    """

    async def contend() -> None:
        async def hold() -> None:
            async with token_refresh._refresh_mutex():
                await asyncio.sleep(0)

        await asyncio.gather(hold(), hold())

    asyncio.run(contend())
    asyncio.run(contend())


# ---------------------------------------------------------------------------
# Propagating into a live session
# ---------------------------------------------------------------------------


async def test_propagate_writes_the_newer_access_token(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "home", tmp_path / "resume"
    parent_dir.mkdir()
    child_dir.mkdir()
    # Pinned rather than recomputed: `_far_future()` reads the clock, so
    # calling it again in the assertion compares against a millisecond that
    # has already moved on.
    fresh = _far_future()
    parent = _write(parent_dir / ".credentials.json", _creds(fresh))
    _write(child_dir / ".credentials.json", _creds(_expired(), refresh_token=None))

    assert propagate(credential_path=parent, materialized_dir=child_dir) is True

    child = json.loads((child_dir / ".credentials.json").read_text())
    assert child["claudeAiOauth"]["accessToken"] == f"access-for-{fresh}"
    assert child["claudeAiOauth"]["expiresAt"] == fresh


async def test_propagate_never_writes_a_refresh_token(tmp_path: Path) -> None:
    """The invariant `_write_redacted_credentials` exists to hold. A refresh
    token under a redirected config dir can revoke the parent's credentials,
    so this file must never gain one — only ever a short-lived access token,
    and only ever downhill from the authority."""
    parent_dir, child_dir = tmp_path / "home", tmp_path / "resume"
    parent_dir.mkdir()
    child_dir.mkdir()
    parent = _write(parent_dir / ".credentials.json", _creds(_far_future()))
    _write(child_dir / ".credentials.json", _creds(_expired(), refresh_token=None))

    propagate(credential_path=parent, materialized_dir=child_dir)

    child_text = (child_dir / ".credentials.json").read_text()
    assert "refreshToken" not in child_text
    assert "refresh-secret" not in child_text
    # And the parent kept its own.
    assert "refreshToken" in parent.read_text()


async def test_propagate_leaves_the_file_owner_only(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "home", tmp_path / "resume"
    parent_dir.mkdir()
    child_dir.mkdir()
    parent = _write(parent_dir / ".credentials.json", _creds(_far_future()))
    child_path = _write(
        child_dir / ".credentials.json", _creds(_expired(), refresh_token=None)
    )

    propagate(credential_path=parent, materialized_dir=child_dir)

    assert child_path.stat().st_mode & 0o077 == 0


async def test_propagate_does_not_move_a_token_backwards(tmp_path: Path) -> None:
    """A child that is already fresher than the parent is left alone, so a
    watchdog tick cannot downgrade a session that just reconnected."""
    parent_dir, child_dir = tmp_path / "home", tmp_path / "resume"
    parent_dir.mkdir()
    child_dir.mkdir()
    parent = _write(parent_dir / ".credentials.json", _creds(_expired()))
    child_path = _write(
        child_dir / ".credentials.json", _creds(_far_future(), refresh_token=None)
    )
    before = child_path.read_text()

    assert propagate(credential_path=parent, materialized_dir=child_dir) is False
    assert child_path.read_text() == before


async def test_propagate_without_a_materialized_dir_is_a_no_op(tmp_path: Path) -> None:
    """A session that was not a resume has no temp config dir; the CLI reads
    the real file and renews itself."""
    parent = _write(tmp_path / ".credentials.json", _creds(_far_future()))
    assert propagate(credential_path=parent, materialized_dir=None) is False


async def test_propagate_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "home", tmp_path / "resume"
    parent_dir.mkdir()
    child_dir.mkdir()
    parent = _write(parent_dir / ".credentials.json", _creds(_far_future()))
    _write(child_dir / ".credentials.json", _creds(_expired(), refresh_token=None))

    propagate(credential_path=parent, materialized_dir=child_dir)

    assert [p.name for p in child_dir.iterdir()] == [".credentials.json"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _far_future() -> int:
    return token_refresh._now_ms() + 8 * 3600_000


def _expired() -> int:
    return token_refresh._now_ms() - 3600_000


def _inside_margin() -> int:
    """Alive, and close enough to expiry that a refresh is attempted.

    The gap the reported false alarm lived in: inside AIC⚡DC's margin, so
    the ladder runs, and outside the CLI's own narrower one, so no rung
    renews anything.
    """
    return token_refresh._now_ms() + int((REFRESH_MARGIN_SECONDS - 60) * 1000)
