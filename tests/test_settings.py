"""Tests for aic_dc.settings.Settings — Layer 1 (deferred) + 4.4.2.

Covers the RPC surface defined in
specs5/1-foundation/rpc-inventory.md#service-settings-browser--server
plus the collaboration restriction pattern from
specs5/1-foundation/rpc-transport.md.

Strategy mirrors ``test_collab_restrictions.py``:

- Real :class:`ConfigManager` against an isolated user config dir
  (via the ``AIC_DC_CONFIG_HOME`` env var — the documented test hook).
- Stub collab with a configurable ``is_caller_localhost`` return,
  reusing the same pattern from the repo restriction tests.
- Two scenarios per write method — localhost allowed, non-localhost
  rejected with the specific error shape.

Reads are unguarded — we test that they work regardless of collab
state (single-user path AND non-localhost path) because a participant
is explicitly allowed to "browse, search, view."

What the conversion changed here: the whitelist is three JSON files
instead of eight mixed prompt and provider files, ``reload_llm_config``
is gone with the provider it reloaded, ``engine.json`` has no reload RPC
at all, and ``get_config_info`` no longer reports model names — the
model in force is the engine's to report, live.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aic_dc.config import CONFIG_TYPES, ConfigManager
from aic_dc.settings import Settings


# ---------------------------------------------------------------------------
# Stub collab (same shape as test_collab_restrictions.py)
# ---------------------------------------------------------------------------


class _StubCollab:
    """Minimal collab with a configurable localhost flag."""

    def __init__(self, is_localhost: bool = True) -> None:
        self._is_localhost = is_localhost
        self.call_count = 0

    def is_caller_localhost(self) -> bool:
        self.call_count += 1
        return self._is_localhost


class _RaisingCollab:
    """Collab that raises — used to verify fail-closed behaviour."""

    def is_caller_localhost(self) -> bool:
        raise RuntimeError("collab check failed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Redirect user config dir to tmp. Matches test_config.py's fixture."""
    home = tmp_path / "aic-dc-config"
    monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def config(isolated_config_dir):
    """A freshly-installed ConfigManager (triggers first-install flow)."""
    return ConfigManager()


@pytest.fixture
def settings(config):
    """A Settings service with no collab attached (single-user mode)."""
    return Settings(config)


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_restricted(result: Any) -> None:
    """Assert ``result`` matches the restricted-error shape."""
    assert isinstance(result, dict)
    assert result.get("error") == "restricted"
    reason = result.get("reason")
    assert isinstance(reason, str) and reason


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_holds_config_reference(self, config):
        svc = Settings(config)
        assert svc._config is config

    def test_collab_starts_none(self, settings):
        assert settings._collab is None

    def test_construction_takes_only_the_config(self, config):
        """The ``llm_service`` parameter is gone, not merely unused.

        It existed so that saving ``app.json`` could ask the native
        engine to re-assemble its system prompt. There is no prompt to
        re-assemble, so accepting the argument would invite a caller to
        wire something that would never be consulted.
        """
        with pytest.raises(TypeError):
            Settings(config, llm_service=object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


class TestWhitelist:
    def test_all_whitelisted_types_resolve(self, settings):
        for type_key in CONFIG_TYPES.keys():
            assert settings._resolve_filename(type_key) is not None

    def test_unknown_type_returns_none(self, settings):
        assert settings._resolve_filename("bogus") is None

    def test_the_whitelist_is_the_two_live_files(self, settings):
        assert set(CONFIG_TYPES) == {"engine", "app"}

    def test_retired_types_no_longer_resolve(self, settings):
        """The five types that described the native engine are gone.

        A frontend still asking for one of these gets an error rather
        than an editor onto a file nothing reads. ``litellm`` is the
        important one: ``llm.json`` may still be sitting in a returning
        user's config dir, and offering to edit it would suggest the
        provider settings in it still matter.
        """
        for retired in (
            "litellm",
            "system",
            "system_extra",
            "system_doc",
            "compaction",
            "review",
        ):
            assert settings._resolve_filename(retired) is None, retired

    def test_internal_prompt_not_whitelisted(self, settings):
        # commit.md is loaded internally but not exposed for UI editing.
        assert settings._resolve_filename("commit") is None


# ---------------------------------------------------------------------------
# get_config_content — read, always allowed
# ---------------------------------------------------------------------------


class TestGetConfigContent:
    def test_reads_the_shipped_engine_file(self, settings):
        result = settings.get_config_content("engine")
        assert result["type"] == "engine"
        # Bundled engine.json is a valid JSON object — parse it to
        # confirm we got real content, not a truncated read.
        parsed = json.loads(result["content"])
        assert "cli_path" in parsed

    def test_reads_the_shipped_app_config(self, settings):
        result = settings.get_config_content("app")
        assert result["type"] == "app"
        assert json.loads(result["content"])["doc_index"]["keywords_enabled"]

    def test_unknown_type_returns_error(self, settings):
        result = settings.get_config_content("bogus")
        assert "error" in result
        assert "bogus" in result["error"]

    def test_commit_prompt_not_readable_via_rpc(self, settings):
        # commit.md is intentionally not in the whitelist — even
        # though the file exists, the RPC can't reach it.
        result = settings.get_config_content("commit")
        assert "error" in result

    def test_missing_user_file_returns_empty_content(
        self, settings, isolated_config_dir, config
    ):
        # Delete the file from the user dir AND suppress the
        # upgrade re-copy by seeding the version marker.
        (isolated_config_dir / "engine.json").unlink(missing_ok=True)
        (isolated_config_dir / ".bundled_version").write_text(
            "seeded", encoding="utf-8"
        )
        # Rebuild ConfigManager against the marker so upgrade is a
        # no-op — the file stays missing.
        from unittest.mock import patch
        with patch("aic_dc.config._bundled_version", return_value="seeded"):
            fresh_config = ConfigManager()
        fresh_settings = Settings(fresh_config)
        result = fresh_settings.get_config_content("engine")
        # Empty content — not an error. The Settings UI opens a
        # blank editor for this case; a next save creates the
        # file, a next startup re-copies the bundle default.
        assert result == {"type": "engine", "content": ""}

    def test_read_allowed_for_non_localhost(self, settings):
        # Reads are always allowed, regardless of collab state.
        settings._collab = _StubCollab(is_localhost=False)
        result = settings.get_config_content("engine")
        assert "error" not in result or result.get("error") != "restricted"

    def test_read_allowed_when_collab_raises(self, settings):
        # Reads don't call _check_localhost_only, so even a raising
        # collab doesn't affect them.
        settings._collab = _RaisingCollab()
        result = settings.get_config_content("app")
        assert "content" in result


# ---------------------------------------------------------------------------
# get_config_info — read, always allowed
# ---------------------------------------------------------------------------


class TestGetConfigInfo:
    def test_returns_the_config_dir_and_nothing_else(
        self, settings, isolated_config_dir
    ):
        """One key. The model names it used to carry belong to the engine.

        They described the native engine's primary/smaller pair, and the
        model actually in force can be changed mid-session through
        ``ClaudeCodeService.set_model`` — so reading it from a config
        file here would show the user a value that is merely a default.
        """
        assert settings.get_config_info() == {
            "config_dir": str(isolated_config_dir)
        }

    def test_allowed_for_non_localhost(self, settings):
        settings._collab = _StubCollab(is_localhost=False)
        info = settings.get_config_info()
        assert "config_dir" in info  # Not restricted.


# ---------------------------------------------------------------------------
# save_config_content — localhost-only
# ---------------------------------------------------------------------------


class TestSaveConfigContent:
    def test_save_overwrites_file(self, settings, isolated_config_dir):
        new_content = json.dumps({"model": "claude-opus-5"}, indent=2)
        result = settings.save_config_content("engine", new_content)
        assert result == {"status": "ok", "type": "engine"}
        on_disk = (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        )
        assert on_disk == new_content

    def test_save_creates_directory_if_missing(
        self, settings, isolated_config_dir
    ):
        # Simulate a vanished config dir (rare but possible — manual
        # rm, filesystem corruption). The save path should re-create.
        import shutil
        shutil.rmtree(isolated_config_dir)
        result = settings.save_config_content("engine", "{}")
        assert result["status"] == "ok"
        assert (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        ) == "{}"

    def test_save_unknown_type_rejected(self, settings):
        result = settings.save_config_content("bogus", "content")
        assert "error" in result
        assert result["error"] != "restricted"
        assert "bogus" in result["error"]

    def test_save_commit_prompt_rejected(self, settings):
        # commit.md not in whitelist — save refuses.
        result = settings.save_config_content("commit", "content")
        assert "error" in result

    def test_save_valid_json_no_warning(self, settings, isolated_config_dir):
        content = json.dumps({"model": "claude-sonnet-5", "cli_path": None})
        result = settings.save_config_content("engine", content)
        assert result == {"status": "ok", "type": "engine"}

    def test_save_invalid_json_warns_but_writes(
        self, settings, isolated_config_dir
    ):
        broken = "{not valid json"
        result = settings.save_config_content("engine", broken)
        # File was written despite the parse error.
        assert result["status"] == "ok"
        assert "warning" in result
        assert "JSON" in result["warning"]
        on_disk = (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        )
        assert on_disk == broken

    def test_every_whitelisted_file_gets_json_validation(self, settings):
        """All three are ``.json`` now, so the advisory check always runs.

        The old whitelist was mostly markdown prompts, where a save had
        no syntax to get wrong. Every remaining editable file is parsed
        by something at startup, so a malformed save that reported plain
        success would surface as a broken app on the next run instead.
        """
        assert all(name.endswith(".json") for name in CONFIG_TYPES.values())
        for type_key in CONFIG_TYPES:
            result = settings.save_config_content(type_key, "{oops")
            assert "warning" in result, type_key

    def test_save_localhost_allowed(self, settings):
        settings._collab = _StubCollab(is_localhost=True)
        result = settings.save_config_content("engine", "{}")
        assert result["status"] == "ok"

    def test_save_non_localhost_rejected(self, settings, isolated_config_dir):
        original = (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        )
        settings._collab = _StubCollab(is_localhost=False)
        result = settings.save_config_content("engine", '{"model": "sneaky"}')
        _assert_restricted(result)
        # File content unchanged — the write never fired.
        assert (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        ) == original

    def test_save_collab_raises_fails_closed(
        self, settings, isolated_config_dir
    ):
        original = (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        )
        settings._collab = _RaisingCollab()
        result = settings.save_config_content("engine", "{}")
        _assert_restricted(result)
        # File unchanged.
        assert (isolated_config_dir / "engine.json").read_text(
            encoding="utf-8"
        ) == original


# ---------------------------------------------------------------------------
# reload_app_config — localhost-only, and the only reload left
# ---------------------------------------------------------------------------


class TestReloadAppConfig:
    def test_reload_picks_up_on_disk_changes(
        self, settings, isolated_config_dir, config
    ):
        original = config.doc_index_config["keywords_top_n"]
        app_json = isolated_config_dir / "app.json"
        data = json.loads(app_json.read_text(encoding="utf-8"))
        data["doc_index"]["keywords_top_n"] = 55
        app_json.write_text(json.dumps(data), encoding="utf-8")
        assert config.doc_index_config["keywords_top_n"] == original
        result = settings.reload_app_config()
        assert result == {"status": "ok"}
        assert config.doc_index_config["keywords_top_n"] == 55

    def test_reload_localhost_allowed(self, settings):
        settings._collab = _StubCollab(is_localhost=True)
        assert settings.reload_app_config() == {"status": "ok"}

    def test_reload_non_localhost_rejected(self, settings):
        settings._collab = _StubCollab(is_localhost=False)
        _assert_restricted(settings.reload_app_config())

    def test_reload_collab_raises_fails_closed(self, settings):
        settings._collab = _RaisingCollab()
        _assert_restricted(settings.reload_app_config())

    def test_reload_reports_a_failure_rather_than_raising(
        self, settings, config, monkeypatch
    ):
        """A broken reload is an error dict, not an RPC exception.

        An exception crossing the RPC boundary reaches the browser as a
        generic transport failure, which tells the user nothing about
        the file they just edited.
        """
        def _boom():
            raise RuntimeError("simulated reload failure")

        monkeypatch.setattr(config, "reload_app_config", _boom)
        result = settings.reload_app_config()
        assert "simulated reload failure" in result["error"]


class TestNoEngineReload:
    """``engine.json`` deliberately has no reload RPC.

    Session options are assembled once, at connect time. A reload call
    would clear a cache nobody reads and report success for a change
    that has not happened — so the honest surface is no method at all,
    and the Settings tab offers a restart instead.
    """

    def test_reload_llm_config_is_gone(self, settings):
        assert not hasattr(settings, "reload_llm_config")

    def test_there_is_no_engine_reload_either(self, settings):
        assert not hasattr(settings, "reload_engine_config")

    def test_engine_is_not_reloadable(self):
        assert Settings.is_reloadable("engine") is False


# ---------------------------------------------------------------------------
# is_reloadable — static helper
# ---------------------------------------------------------------------------


class TestIsReloadable:
    def test_only_app_is_reloadable(self):
        assert Settings.is_reloadable("app") is True
        assert Settings.is_reloadable("engine") is False

    def test_every_whitelisted_type_has_an_answer(self):
        """No type is silently absent from the reload decision."""
        for type_key in CONFIG_TYPES:
            assert isinstance(Settings.is_reloadable(type_key), bool)

    def test_unknown_type_not_reloadable(self):
        assert Settings.is_reloadable("bogus") is False
