"""Tests for aic_dc.config.ConfigManager.
Layer 1 scope — covers:
- Config directory resolution (AIC_DC_CONFIG_HOME override,
  platform-specific paths)
- Version-aware upgrade (first install, upgrade with backup, same-version
  no-op, user file preservation)
- Accessor read-through (hot-reload changes are observed without
  reconstruction)
- Per-repo working directory creation and .gitignore wiring
- The commit prompt, which is the one prompt file the config layer
  still loads
Uses tmp_path + AIC_DC_CONFIG_HOME env var to redirect config to
isolated temp dirs. Avoids monkeypatching sys.platform etc. — the
override env var is the designated test hook.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from aic_dc.config import (
    CONFIG_TYPES,
    ConfigManager,
    _bundled_config_dir,
    _bundled_version,
    _user_config_dir,
)
# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Redirect the user config dir to an isolated tmp path.
    Uses the AIC_DC_CONFIG_HOME env var — the documented test hook —
    rather than patching platform detection. Yields the dir path so
    tests can inspect its contents.
    """
    config_home = tmp_path / "aic-dc-config"
    monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(config_home))
    yield config_home
@pytest.fixture
def repo_root(tmp_path):
    """A fresh tmp dir acting as a git repo root.
    No actual git init — ConfigManager doesn't care. The .gitignore
    wiring is driven purely by file presence/content.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo
# ---------------------------------------------------------------------------
# _user_config_dir resolution
# ---------------------------------------------------------------------------
def test_user_config_dir_respects_override_env(tmp_path, monkeypatch):
    """AIC_DC_CONFIG_HOME overrides platform detection."""
    override = tmp_path / "override"
    monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(override))
    assert _user_config_dir() == override
def test_user_config_dir_linux(monkeypatch):
    """Linux path honours XDG_CONFIG_HOME, then falls back to ~/.config."""
    monkeypatch.delenv("AIC_DC_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    # With XDG_CONFIG_HOME set.
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    assert _user_config_dir() == Path("/custom/xdg/aic-dc")
    # Without it, falls back to ~/.config.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert _user_config_dir() == Path.home() / ".config" / "aic-dc"
def test_user_config_dir_macos(monkeypatch):
    """macOS path is under ~/Library/Application Support."""
    monkeypatch.delenv("AIC_DC_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = Path.home() / "Library" / "Application Support" / "aic-dc"
    assert _user_config_dir() == expected
def test_user_config_dir_windows(monkeypatch):
    """Windows path is under %APPDATA%."""
    monkeypatch.delenv("AIC_DC_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\test\\AppData\\Roaming")
    result = _user_config_dir()
    assert result.name == "aic-dc"
    assert "Roaming" in str(result)
# ---------------------------------------------------------------------------
# _bundled_version
# ---------------------------------------------------------------------------
def test_bundled_version_reads_version_file():
    """_bundled_version reads the shipped VERSION file."""
    version = _bundled_version()
    # Source tree ships 'dev'; release builds bake a timestamp+SHA.
    # Either way, it's a non-None string.
    assert isinstance(version, str)
# ---------------------------------------------------------------------------
# First-install upgrade flow
# ---------------------------------------------------------------------------
def test_first_install_copies_all_files(isolated_config_dir):
    """On first install, all bundled files are copied to user dir."""
    assert not isolated_config_dir.exists()
    ConfigManager()
    assert isolated_config_dir.is_dir()
    # Every managed + user file is present. Five prompt files and
    # llm.json left this list with the native engine; the ones that
    # remain are the ones something still reads.
    for filename in (
        "commit.md",
        "app.json",
        "engine.json",
    ):
        assert (isolated_config_dir / filename).is_file(), f"missing {filename}"
def test_first_install_writes_version_marker_for_release_builds(
    isolated_config_dir,
):
    """Release builds write a .bundled_version marker on first install."""
    # Simulate a release build by patching _bundled_version.
    with patch("aic_dc.config._bundled_version", return_value="2025.01.15-a1b2c3d4"):
        ConfigManager()
    marker = isolated_config_dir / ".bundled_version"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == "2025.01.15-a1b2c3d4"
def test_first_install_writes_dev_marker(isolated_config_dir):
    """Source installs with version='dev' write 'dev' as the marker.
    The code writes any truthy version. A dev install records 'dev',
    and the next real release mismatches it and triggers upgrade.
    """
    with patch("aic_dc.config._bundled_version", return_value="dev"):
        ConfigManager()
    marker = isolated_config_dir / ".bundled_version"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == "dev"
def test_first_install_skips_marker_when_version_empty(isolated_config_dir):
    """Empty version (VERSION file unreadable) skips marker write.
    We can't record a version we don't know, so the next run treats
    everything as new again.
    """
    with patch("aic_dc.config._bundled_version", return_value=""):
        ConfigManager()
    marker = isolated_config_dir / ".bundled_version"
    assert not marker.exists()
# ---------------------------------------------------------------------------
# Same-version no-op
# ---------------------------------------------------------------------------
def test_same_version_startup_is_noop(isolated_config_dir):
    """Second startup with matching version doesn't modify files."""
    version = "2025.01.15-a1b2c3d4"
    with patch("aic_dc.config._bundled_version", return_value=version):
        # First install.
        ConfigManager()
        # User modifies a managed file.
        commit_md = isolated_config_dir / "commit.md"
        commit_md.write_text("user-edited content", encoding="utf-8")
        # Second startup — same version.
        ConfigManager()
        # User edit preserved.
        assert commit_md.read_text(encoding="utf-8") == "user-edited content"
# ---------------------------------------------------------------------------
# Upgrade flow
# ---------------------------------------------------------------------------
def test_upgrade_backs_up_and_overwrites_managed_files(isolated_config_dir):
    """On version bump, managed files are backed up and overwritten."""
    # Install at version A.
    with patch("aic_dc.config._bundled_version", return_value="2025.01.01-aaaaaaaa"):
        ConfigManager()
    # User customises a managed file.
    commit_md = isolated_config_dir / "commit.md"
    commit_md.write_text("user-hacked commit prompt", encoding="utf-8")
    # Startup at version B — triggers upgrade.
    with patch("aic_dc.config._bundled_version", return_value="2025.02.01-bbbbbbbb"):
        ConfigManager()
    # Original content was backed up somewhere.
    backups = list(isolated_config_dir.glob("commit.md.*"))
    assert len(backups) == 1
    assert "user-hacked commit prompt" in backups[0].read_text(encoding="utf-8")
    # Managed file was overwritten with the bundled version.
    current = commit_md.read_text(encoding="utf-8")
    assert "user-hacked" not in current
    assert "commit message" in current

    # Marker updated to the new version.
    marker = isolated_config_dir / ".bundled_version"
    assert marker.read_text(encoding="utf-8").strip() == "2025.02.01-bbbbbbbb"
def test_upgrade_preserves_user_files(isolated_config_dir):
    """``engine.json`` is a user file and is never overwritten.

    It holds the CLI path, the model and the permission mode — a
    machine-specific answer the shipped default cannot know. An upgrade
    that reset it would point the app at a ``claude`` binary that isn't
    there.
    """
    # Install at version A.
    with patch("aic_dc.config._bundled_version", return_value="2025.01.01-aaaaaaaa"):
        ConfigManager()
    engine_json = isolated_config_dir / "engine.json"
    custom = {
        "cli_path": "/opt/claude/bin/claude",
        "model": "claude-opus-5",
    }
    engine_json.write_text(json.dumps(custom), encoding="utf-8")
    # Upgrade to version B.
    with patch("aic_dc.config._bundled_version", return_value="2025.02.01-bbbbbbbb"):
        ConfigManager()
    # User file preserved exactly.
    preserved = json.loads(engine_json.read_text(encoding="utf-8"))
    assert preserved["cli_path"] == "/opt/claude/bin/claude"
    assert preserved["model"] == "claude-opus-5"
    # And no backup file was created for user files.
    assert list(isolated_config_dir.glob("engine.json.*")) == []
def test_backup_name_with_version(isolated_config_dir):
    """Backup filename includes the OLD installed version."""
    with patch("aic_dc.config._bundled_version", return_value="2025.01.01-aaaaaaaa"):
        ConfigManager()
    # Modify a managed file so it gets backed up.
    (isolated_config_dir / "commit.md").write_text("v1 content", encoding="utf-8")
    with patch("aic_dc.config._bundled_version", return_value="2025.02.01-bbbbbbbb"):
        ConfigManager()
    backups = list(isolated_config_dir.glob("commit.md.*"))
    assert len(backups) == 1
    # Backup name contains the OLD version, not the new one.
    assert "2025.01.01-aaaaaaaa" in backups[0].name
    assert "2025.02.01-bbbbbbbb" not in backups[0].name
def test_backup_name_without_version(isolated_config_dir):
    """Backup filename falls back to timestamp-only when no installed version."""
    # First install with empty version — no marker written.
    with patch("aic_dc.config._bundled_version", return_value=""):
        ConfigManager()
    # User customises a managed file.
    (isolated_config_dir / "commit.md").write_text("custom", encoding="utf-8")
    # Upgrade to a real version — no installed version to stamp into backup.
    with patch("aic_dc.config._bundled_version", return_value="2025.02.01-bbbbbbbb"):
        ConfigManager()
    backups = list(isolated_config_dir.glob("commit.md.*"))
    assert len(backups) == 1
    # Backup name has a timestamp but no trailing -sha.
    # Format: commit.md.YYYY.MM.DD-HH.MM
    import re
    assert re.match(
        r"^commit\.md\.\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}$",
        backups[0].name,
    ), f"unexpected backup name: {backups[0].name}"
# ---------------------------------------------------------------------------
# App config accessors
# ---------------------------------------------------------------------------
def test_doc_convert_config_defaults(isolated_config_dir):
    """doc_convert_config returns extensions list and size limit."""
    cfg = ConfigManager()
    dcc = cfg.doc_convert_config
    assert dcc["enabled"] is True
    assert ".docx" in dcc["extensions"]
    assert ".pdf" in dcc["extensions"]
    assert dcc["max_source_size_mb"] > 0
def test_doc_index_config_defaults(isolated_config_dir):
    """doc_index_config returns all keyword-enricher fields."""
    cfg = ConfigManager()
    dic = cfg.doc_index_config
    assert isinstance(dic["keyword_model"], str)
    assert dic["keyword_model"]
    assert dic["keywords_enabled"] is True
    assert dic["keywords_top_n"] > 0
    assert dic["keywords_ngram_range"] == [1, 2]
    assert 0.0 <= dic["keywords_min_score"] <= 1.0
    assert 0.0 <= dic["keywords_diversity"] <= 1.0
    assert 0.0 <= dic["keywords_max_doc_freq"] <= 1.0


















def test_history_config_defaults(isolated_config_dir):
    """history_config returns the two mirror thresholds."""
    from aic_dc.claude_code.health import DEFAULT_MIRROR_GAP_TOLERANCE
    from aic_dc.claude_code.session_store import DISK_WARNING_BYTES

    cfg = ConfigManager()
    hc = cfg.history_config
    assert hc["session_dir_warning_bytes"] == DISK_WARNING_BYTES
    assert hc["mirror_gap_tolerance"] == DEFAULT_MIRROR_GAP_TOLERANCE


def test_history_config_honours_edits(isolated_config_dir):
    """Both keys are read from the file when the file says something."""
    ConfigManager()  # installs the bundled app.json
    app_json = isolated_config_dir / "app.json"
    data = json.loads(app_json.read_text(encoding="utf-8"))
    data["history"] = {
        "session_dir_warning_bytes": 5000,
        "mirror_gap_tolerance": 0,
    }
    app_json.write_text(json.dumps(data), encoding="utf-8")
    hc = ConfigManager().history_config
    assert hc["session_dir_warning_bytes"] == 5000
    # Zero is a real answer for this one: "tell me about the first gap".
    assert hc["mirror_gap_tolerance"] == 0


def test_history_config_rejects_a_silencing_threshold(isolated_config_dir):
    """A zero, a negative or a typo falls back rather than muting the check."""
    from aic_dc.claude_code.health import DEFAULT_MIRROR_GAP_TOLERANCE
    from aic_dc.claude_code.session_store import DISK_WARNING_BYTES

    ConfigManager()  # installs the bundled app.json
    app_json = isolated_config_dir / "app.json"
    for bad_bytes, bad_gaps in (
        (0, -1),
        (-1, "three"),
        ("lots", None),
        (None, {}),
    ):
        data = json.loads(app_json.read_text(encoding="utf-8"))
        data["history"] = {
            "session_dir_warning_bytes": bad_bytes,
            "mirror_gap_tolerance": bad_gaps,
        }
        app_json.write_text(json.dumps(data), encoding="utf-8")
        hc = ConfigManager().history_config
        assert hc["session_dir_warning_bytes"] == DISK_WARNING_BYTES, bad_bytes
        assert hc["mirror_gap_tolerance"] == DEFAULT_MIRROR_GAP_TOLERANCE, bad_gaps


def test_history_config_survives_a_non_dict_section(isolated_config_dir):
    """A section written as something other than an object is ignored."""
    from aic_dc.claude_code.session_store import DISK_WARNING_BYTES

    ConfigManager()  # installs the bundled app.json
    app_json = isolated_config_dir / "app.json"
    data = json.loads(app_json.read_text(encoding="utf-8"))
    data["history"] = ["1073741824", 3]
    app_json.write_text(json.dumps(data), encoding="utf-8")
    hc = ConfigManager().history_config
    assert hc["session_dir_warning_bytes"] == DISK_WARNING_BYTES


def test_app_config_hot_reload(isolated_config_dir):
    """Editing app.json and calling reload_app_config reflects changes."""
    cfg = ConfigManager()
    original = cfg.doc_index_config["keywords_top_n"]
    # User edits app.json on disk.
    app_json = isolated_config_dir / "app.json"
    data = json.loads(app_json.read_text(encoding="utf-8"))
    data["doc_index"]["keywords_top_n"] = 99
    app_json.write_text(json.dumps(data), encoding="utf-8")
    # Before reload — cached.
    assert cfg.doc_index_config["keywords_top_n"] == original
    # After reload — new value.
    cfg.reload_app_config()
    assert cfg.doc_index_config["keywords_top_n"] == 99
# ---------------------------------------------------------------------------
# Corrupt-config resilience
# ---------------------------------------------------------------------------
def test_non_dict_json_root_falls_back(isolated_config_dir):
    """A JSON root that's not an object logs and falls back."""
    ConfigManager()
    (isolated_config_dir / "app.json").write_text("[]", encoding="utf-8")
    cfg = ConfigManager()
    # Accessors return their defaults despite the broken file.
    assert cfg.app_config == {}
    assert cfg.doc_index_config["keyword_model"]
    assert cfg.doc_convert_config["max_source_size_mb"] > 0
# ---------------------------------------------------------------------------
# Per-repo .aic-dc/ working directory
# ---------------------------------------------------------------------------
def test_aic_dc_dir_not_created_without_repo(isolated_config_dir):
    """No repo_root argument → no per-repo directory created."""
    cfg = ConfigManager()
    assert cfg.aic_dc_dir is None
    assert cfg.repo_root is None
def test_aic_dc_dir_created_with_repo(isolated_config_dir, repo_root):
    """When repo_root is given, .aic-dc/ is created — and nothing inside it."""
    cfg = ConfigManager(repo_root=repo_root)
    assert cfg.repo_root == repo_root
    assert cfg.aic_dc_dir == repo_root / ".aic-dc"
    assert cfg.aic_dc_dir.is_dir()
    # Subdirectories belong to whoever writes them. `images/` in particular
    # is retired: images live in the transcript now, so an empty one would
    # only look like a place data should be.
    assert list(cfg.aic_dc_dir.iterdir()) == []
def test_aic_dc_dir_creation_is_idempotent(isolated_config_dir, repo_root):
    """Calling ConfigManager twice doesn't fail if .aic-dc/ already exists."""
    ConfigManager(repo_root=repo_root)
    # Add a file inside to prove it isn't re-created (which would delete it).
    marker = repo_root / ".aic-dc" / "marker.txt"
    marker.write_text("preserve me", encoding="utf-8")
    ConfigManager(repo_root=repo_root)
    assert marker.read_text(encoding="utf-8") == "preserve me"
def test_gitignore_created_when_absent(isolated_config_dir, repo_root):
    """A fresh repo gets a .gitignore containing the .aic-dc/ entry."""
    assert not (repo_root / ".gitignore").exists()
    ConfigManager(repo_root=repo_root)
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".aic-dc/" in gitignore
def test_gitignore_entry_appended_when_present(isolated_config_dir, repo_root):
    """Existing .gitignore gets the entry appended, existing content preserved."""
    gitignore = repo_root / ".gitignore"
    gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
    ConfigManager(repo_root=repo_root)
    content = gitignore.read_text(encoding="utf-8")
    assert "*.pyc" in content
    assert "__pycache__/" in content
    assert ".aic-dc/" in content
def test_gitignore_not_duplicated(isolated_config_dir, repo_root):
    """Running twice doesn't append .aic-dc/ twice."""
    ConfigManager(repo_root=repo_root)
    ConfigManager(repo_root=repo_root)
    content = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".aic-dc/") == 1
def test_gitignore_recognises_trailing_slashless_entry(
    isolated_config_dir, repo_root
):
    """An existing '.aic-dc' entry (no slash) is recognised and not duplicated."""
    gitignore = repo_root / ".gitignore"
    gitignore.write_text(".aic-dc\n", encoding="utf-8")
    ConfigManager(repo_root=repo_root)
    content = gitignore.read_text(encoding="utf-8")
    # The original '.aic-dc' line remains; no new '.aic-dc/' line added.
    assert ".aic-dc\n" in content
    assert ".aic-dc/" not in content
def test_gitignore_handles_missing_trailing_newline(
    isolated_config_dir, repo_root
):
    """Appends correctly even when existing .gitignore has no trailing newline."""
    gitignore = repo_root / ".gitignore"
    gitignore.write_text("*.pyc", encoding="utf-8")  # no trailing \n
    ConfigManager(repo_root=repo_root)
    content = gitignore.read_text(encoding="utf-8")
    # Appended entry is on its own line.
    lines = content.splitlines()
    assert "*.pyc" in lines
    assert ".aic-dc/" in lines
# ---------------------------------------------------------------------------
# The commit prompt
# ---------------------------------------------------------------------------
def test_get_commit_prompt_loads_as_is(isolated_config_dir):
    """The commit prompt is the file, verbatim.

    It used to be one of several prompt files the config layer
    concatenated. There is no assembly left — the system prompt is the
    CLI's now — so this reads ``commit.md`` and nothing else, and the
    one-shot in ``aic_dc.claude_code.commit`` gets exactly what the user
    sees in the settings editor.
    """
    cfg = ConfigManager()
    prompt = cfg.get_commit_prompt()
    assert prompt == (isolated_config_dir / "commit.md").read_text(
        encoding="utf-8"
    )
    assert "conventional" in prompt.lower() or "imperative" in prompt.lower()




# ---------------------------------------------------------------------------
# CONFIG_TYPES whitelist
# ---------------------------------------------------------------------------


def test_config_types_covers_editable_files():
    """CONFIG_TYPES is exactly the three files the Settings UI edits.

    ``specs5/1-foundation/configuration.md`` § The Whitelist names these
    two and no others. The list was eight entries while the native
    engine owned prompt assembly; five of them described a prompt or a
    provider knob that no longer has a reader, and a settings editor
    offering an edit that changes nothing is worse than not offering it.
    ``snippets`` left with the snippet mechanism itself (CC-22).
    """
    assert set(CONFIG_TYPES.keys()) == {"engine", "app"}


def test_config_types_excludes_the_commit_prompt():
    """commit.md is loaded internally and not offered for UI editing.

    It has a rigid output contract — the generated text goes straight
    into git history — and an edit that breaks the format is discovered
    at commit time. Users who want to change it can edit the file on
    disk, where the upgrade pass will treat it as managed and back it
    up before overwriting.
    """
    assert "commit.md" not in set(CONFIG_TYPES.values())


def test_config_types_values_are_real_files(isolated_config_dir):
    """Every whitelisted type maps to a real shipped file.

    ``engine.json`` included: it is a user file the upgrade pass never
    overwrites, but it is still shipped on first install so the settings
    editor opens something with the defaults in it rather than a blank
    page.
    """
    ConfigManager()  # Trigger install so files exist in user dir.
    for type_name, filename in CONFIG_TYPES.items():
        path = isolated_config_dir / filename
        assert path.is_file(), f"{type_name!r} → {filename!r} not installed"


# ---------------------------------------------------------------------------
# _bundled_config_dir resolution
# ---------------------------------------------------------------------------


def test_bundled_config_dir_uses_module_relative_path():
    """Outside PyInstaller, config dir is next to the aic_dc module."""
    # sys._MEIPASS is unset in normal test runs.
    if hasattr(sys, "_MEIPASS"):
        pytest.skip("running inside PyInstaller bundle")
    bundled = _bundled_config_dir()
    assert bundled.is_dir()
    assert bundled.name == "config"
    # Parent should be the aic_dc package dir.
    assert bundled.parent.name == "aic_dc"


def test_bundled_config_dir_prefers_meipass_when_present(monkeypatch, tmp_path):
    """Inside a PyInstaller bundle, _MEIPASS takes precedence.

    We simulate the bundle layout by creating ``<meipass>/aic_dc/config/``
    and setting sys._MEIPASS to point at it.
    """
    fake_meipass = tmp_path / "meipass"
    fake_config = fake_meipass / "aic_dc" / "config"
    fake_config.mkdir(parents=True)
    # Drop a sentinel file so we can verify we actually read this dir
    # (not the real one next to the module).
    (fake_config / "sentinel.txt").write_text("from meipass", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)
    resolved = _bundled_config_dir()
    assert resolved == fake_config
    assert (resolved / "sentinel.txt").read_text(encoding="utf-8") == "from meipass"


def test_bundled_config_dir_falls_back_when_meipass_missing_config(
    monkeypatch, tmp_path, caplog
):
    """If _MEIPASS is set but doesn't contain aic_dc/config, fall back.

    Pathological-but-real case — a malformed PyInstaller bundle or a
    misconfigured test harness setting _MEIPASS incorrectly. We log a
    warning and use the module-relative path so the app still works.
    """
    empty_meipass = tmp_path / "empty-meipass"
    empty_meipass.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(empty_meipass), raising=False)

    with caplog.at_level("WARNING", logger="aic_dc.config"):
        resolved = _bundled_config_dir()
    # Fell back to the module-relative dir.
    assert resolved.parent.name == "aic_dc"
    assert resolved.is_dir()
    # Logged the fallback so operators can see why.
    assert any("MEIPASS" in r.message for r in caplog.records)