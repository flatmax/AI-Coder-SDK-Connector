"""Tests for AG-17 — a Claude-only install, and the switch that makes one.

The requirement is a workplace's, not a preference: *"some workplaces are
only allowed to use Claude."* So the assertions here are about what a
configured policy **prevents**, and the one that matters most is not the
engine selector — it is that a Claude turn is not offered
``second_opinion`` and ``generate_image``, which are the surfaces that
reach Google from inside a session nobody switched.

Two failure directions, and they are not symmetrical
====================================================
A policy that is read too *narrowly* costs a user a feature they are
entitled to. A policy read too *widely* mounts a provider a workplace
forbids. So the tests below pin the direction of every degenerate case:
malformed configuration keeps every engine (the state before anybody
wrote the key), while a well-formed one that names Claude alone removes
everything else — including the consultant, which is the one that is easy
to forget because it is not an engine anybody picks.

The tripwire ``TestEveryMountPointConsults`` is the entry's own: the
defect AG-17 was written about is a mount condition that answered
``shutil.which("agy")`` and nothing else — locally correct, and wrong the
day the product met a workplace.

Offline. No engines are started; what is asserted is what mounts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aic_dc import capabilities
from aic_dc.agy.consultant import choose_consultant
from aic_dc.config import ConfigManager
from aic_dc.engine_router import build_router


class Config(ConfigManager):
    """The real property, over an ``app.json`` a test supplies.

    Subclassed rather than constructed, because ``ConfigManager``'s
    constructor reads the bundled and user config directories and creates
    a per-repo ``.aic-dc/`` — none of which this decision touches, and all
    of which would make these tests depend on the machine they run on.
    What is under test is :meth:`ConfigManager.enabled_engines`, and it
    reads exactly one thing.
    """

    def __init__(self, app_config: dict) -> None:  # noqa: D107 - see class
        self._app = app_config

    @property
    def app_config(self) -> dict:
        return self._app


def config_with(engines) -> Config:
    """A config whose ``app.json`` holds ``engines`` (or has no such key)."""
    return Config({} if engines is None else {"engines": engines})


# ----------------------------------------------------------------------
# Reading the policy
# ----------------------------------------------------------------------


class TestReadingThePolicy:
    def test_no_key_means_every_engine(self):
        """The default is what every install had before this key existed."""
        config = config_with(None)
        assert set(config.enabled_engines) == set(capabilities.ENGINES)

    def test_claude_only_removes_both_antigravity_engines(self):
        config = config_with({"enabled": ["claude"]})
        assert config.enabled_engines == (capabilities.CLAUDE,)

    def test_one_antigravity_transport_can_be_allowed_alone(self):
        """A workplace may permit the subscription and not the API key."""
        config = config_with({"enabled": ["claude", "agy"]})
        assert set(config.enabled_engines) == {capabilities.CLAUDE, capabilities.AGY}

    def test_claude_is_added_back_when_omitted(self):
        """An install with no engine is not a configuration this app can run."""
        config = config_with({"enabled": ["agy"]})
        assert capabilities.CLAUDE in config.enabled_engines

    def test_an_unknown_name_is_dropped_not_fatal(self):
        config = config_with({"enabled": ["claude", "gpt-5"]})
        assert config.enabled_engines == (capabilities.CLAUDE,)

    def test_a_malformed_policy_keeps_every_engine(self):
        """The safe direction for *this* mistake, and it is the wide one.

        A policy that will not parse says nothing about what is
        permitted. Reading it as a restriction would let a typo silently
        remove a feature the user is entitled to, while reading it as
        "no policy" leaves them where they were before they wrote it —
        and the Settings panel shows what is in force either way.
        """
        for broken in ("claude", 7, {"claude": True}):
            config = config_with({"enabled": broken})
            assert set(config.enabled_engines) == set(capabilities.ENGINES)

    def test_the_order_is_canonical_not_the_file_s(self):
        """Two configs enabling the same engines compare equal."""
        a = config_with({"enabled": ["agy", "claude"]})
        b = config_with({"enabled": ["claude", "agy"]})
        assert a.enabled_engines == b.enabled_engines


# ----------------------------------------------------------------------
# The consultant, which is the surface a workplace actually meets
# ----------------------------------------------------------------------


class TestTheConsultantFollowsThePolicy:
    """The tools reach Google from inside a *Claude* turn (AG-R-13)."""

    def test_claude_only_means_no_consultant_at_all(self, tmp_path, monkeypatch):
        """Even where both transports would otherwise be usable."""
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant, why = choose_consultant(tmp_path, enabled=("claude",))
        assert consultant is None
        assert "policy" in why

    def test_the_reason_does_not_blame_a_missing_credential(
        self, tmp_path, monkeypatch
    ):
        """An administrator sent to install a wheel is sent to the wrong place."""
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: None)
        _consultant, why = choose_consultant(tmp_path, enabled=("claude",))
        assert "engines.enabled" in why
        assert "not on PATH" not in why

    def test_a_permitted_transport_still_mounts(self, tmp_path, monkeypatch):
        from aic_dc.agy.consultant import AgyConsultant

        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant, _why = choose_consultant(
            tmp_path, enabled=("claude", capabilities.AGY)
        )
        assert isinstance(consultant, AgyConsultant)

    def test_a_preference_cannot_widen_the_policy(self, tmp_path, monkeypatch):
        """`engines.consultant` names *which*; the policy says *whether*.

        The other ordering would make the preference an override, and a
        policy a preference can override is not a policy.
        """
        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        consultant, why = choose_consultant(
            tmp_path, transport="agy", enabled=("claude",)
        )
        assert consultant is None
        assert "policy" in why

    def test_excluding_one_transport_falls_through_to_the_other(
        self, tmp_path, monkeypatch
    ):
        """Permitting the SDK and not the CLI is a coherent workplace rule."""
        from aic_dc.antigravity.consultant import Consultant

        monkeypatch.setattr(
            "aic_dc.agy.consultant.install.status",
            lambda *_a, **_k: {"state": "current", "path": "x"},
        )
        monkeypatch.setattr("aic_dc.agy.consultant.shutil.which", lambda _n: "/bin/agy")
        monkeypatch.setattr(Consultant, "available", property(lambda _s: True))
        consultant, why = choose_consultant(
            tmp_path, enabled=("claude", capabilities.ANTIGRAVITY)
        )
        assert isinstance(consultant, Consultant)
        assert "does not permit the agy transport" in why


# ----------------------------------------------------------------------
# The router: what is offered, and how a refusal reads
# ----------------------------------------------------------------------


class Adapter:
    """A stand-in with the core surface, enough to mount.

    ``_check_localhost_only`` is here because the router borrows the
    master's rather than owning one, and an adapter without it fails
    closed — which would make every switch below refuse for a reason
    that is not the one under test.
    """

    def __init__(self):
        self.session = None

    def _check_localhost_only(self):
        return None

    async def get_current_state(self):
        return {}


def router(enabled=None, alternates=None):
    return build_router(
        Adapter(),
        engine=capabilities.CLAUDE,
        alternates=alternates or {},
        require_full_surface=False,
        enabled=enabled,
    )


class TestTheSelectorAndTheRefusal:
    def test_a_disabled_engine_is_not_offered(self):
        """Not merely unmountable: not on the list the selector reads."""
        listed = router(enabled=(capabilities.CLAUDE,)).list_engines()
        assert listed["available"] == [capabilities.CLAUDE]
        assert listed["enabled"] == [capabilities.CLAUDE]

    def test_no_policy_offers_everything(self):
        """A router built without one has not been given a restriction."""
        listed = router().list_engines()
        assert listed["available"] == list(capabilities.ENGINES)

    @pytest.mark.asyncio
    async def test_switching_to_a_disabled_engine_names_the_policy(self):
        """`not_mountable` would send an administrator to install something."""
        result = await router(enabled=(capabilities.CLAUDE,)).switch_engine(
            capabilities.AGY
        )
        assert result["reason"] == "engine_disabled"
        assert "engines.enabled" in result["error"]
        assert "credential" in result["error"]  # ...saying it is *not* that

    @pytest.mark.asyncio
    async def test_an_enabled_but_unmounted_engine_still_says_so(self):
        """The old message survives for the case it was written about."""
        result = await router().switch_engine(capabilities.AGY)
        assert result["reason"] == "not_mountable"


# ----------------------------------------------------------------------
# The tripwire AG-17 asks for
# ----------------------------------------------------------------------


class TestEveryMountPointConsults:
    """A surface added beside these must not inherit the old default.

    The defect this entry was written about is a mount condition that
    answered ``shutil.which("agy")`` and nothing else — correct on the
    day it was written, and wrong the day the product met a workplace
    that is only allowed Claude. Asserted against the source, because
    what is being checked is that the *condition exists*, and a test
    that started servers would only ever exercise this machine's own
    configuration.
    """

    def test_both_engine_mounts_consult_the_policy(self):
        source = Path("src/aic_dc/main.py").read_text(encoding="utf-8")
        assert "capabilities.ANTIGRAVITY not in enabled_engines" in source
        assert "capabilities.AGY not in enabled_engines" in source

    def test_the_router_is_given_the_policy(self):
        source = Path("src/aic_dc/main.py").read_text(encoding="utf-8")
        assert "enabled=enabled_engines" in source

    def test_the_consultant_is_given_the_policy(self):
        source = Path("src/aic_dc/claude_code/service.py").read_text(encoding="utf-8")
        assert "enabled=getattr(self._config, \"enabled_engines\", None)" in source

    def test_the_policy_has_one_reader(self):
        """`engines.enabled` is parsed in exactly one place.

        Two readers of one key is how a policy comes to mean two
        different things — the convergence this suite keeps choosing.
        Everything else takes the answer as an argument.
        """
        parsers = []
        for path in Path("src/aic_dc").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if 'section.get("enabled")' in text:
                parsers.append(path.name)
        assert parsers == ["config.py"], parsers
