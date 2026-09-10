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
import shutil
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
from aic_dc.config import (
    CONFIG_TYPES,
    RETIRED_FILES,
    ConfigManager,
    _bundled_config_dir,
    _bundled_version,
    _merge_json,
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

# ---------------------------------------------------------------------------
# Retired files — left alone, and now reported
# ---------------------------------------------------------------------------


def test_a_fresh_install_has_no_retired_files(isolated_config_dir):
    """Nothing bundled is retired, so a first install starts clean.

    This is what lets the Settings tab stay silent for a new user: the
    note explains cards that disappeared, and for this install they
    never existed.
    """
    manager = ConfigManager()
    assert manager.retired_files_present() == []


def test_retired_files_on_disk_are_reported(isolated_config_dir):
    manager = ConfigManager()
    (isolated_config_dir / "system_extra.md").write_text("mine\n")
    (isolated_config_dir / "llm.json").write_text("{}\n")

    # RETIRED_FILES order, not alphabetical and not disk order — the
    # note reads llm.json first because startup already mentions it.
    assert manager.retired_files_present() == ["llm.json", "system_extra.md"]


def test_live_config_files_are_not_reported_as_retired(isolated_config_dir):
    """The two sets must not overlap, or the note would tell a user
    their working config had been abandoned."""
    manager = ConfigManager()
    assert not set(CONFIG_TYPES.values()) & set(RETIRED_FILES)
    assert manager.retired_files_present() == []


def test_a_directory_of_the_right_name_is_not_a_retired_file(
    isolated_config_dir,
):
    """`is_file`, not `exists`. A directory called `review.md` is not a
    prompt anybody wrote, and offering to explain it would be wrong."""
    manager = ConfigManager()
    (isolated_config_dir / "review.md").mkdir()
    assert manager.retired_files_present() == []


def test_an_upgrade_leaves_retired_files_alone(isolated_config_dir):
    """The rule the report depends on, pinned.

    Retired files are never migrated and never deleted, because
    `system_extra.md` may hold real user work and removing it would be
    irreversible and pointless. If an upgrade ever started cleaning them
    up, the note would quietly stop having anything to say — so the
    preservation and the reporting are tested together.
    """
    ConfigManager()
    kept = isolated_config_dir / "system_extra.md"
    kept.write_text("months of work\n")

    with patch("aic_dc.config._bundled_version", return_value="9.9.9"):
        manager = ConfigManager()

    assert kept.is_file()
    assert kept.read_text() == "months of work\n"
    assert manager.retired_files_present() == ["system_extra.md"]

# ---------------------------------------------------------------------------
# app.json is merged, not overwritten
# ---------------------------------------------------------------------------
#
# The finding that put this section here: `app.json` was a managed file,
# so an upgrade copied the bundle over it — and `app.json` is also the
# file the Settings tab writes and the file AG-17's `engines.enabled`
# policy lives in. A Claude-only deployment came back two-provider after
# a version bump, silently. See specs5/plan-ag/risks.md § AG-R-13.
#
# These tests use a *fake* bundle rather than the shipped one, because
# what they assert is how a value that changed between two releases is
# resolved, and the shipped bundle only ever has one version at a time.


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    """A bundled config dir whose contents the test controls.

    Yields a writer: ``bundle(app={...})`` rewrites the bundled
    ``app.json`` and returns the dir, so a test can ship one version,
    install, then ship the next.
    """
    bundled = tmp_path / "bundled-config"
    bundled.mkdir()
    (bundled / "commit.md").write_text("bundled commit prompt\n", encoding="utf-8")
    (bundled / "engine.json").write_text('{"model": "bundled"}\n', encoding="utf-8")
    monkeypatch.setattr("aic_dc.config._bundled_config_dir", lambda: bundled)

    def bundle(app: dict) -> Path:
        (bundled / "app.json").write_text(
            json.dumps(app, indent=2) + "\n", encoding="utf-8"
        )
        return bundled

    bundle({"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 3}})
    yield bundle


def _install(version: str) -> ConfigManager:
    """Construct a manager as release ``version`` would."""
    with patch("aic_dc.config._bundled_version", return_value=version):
        return ConfigManager()


def test_an_upgrade_keeps_the_engine_policy(isolated_config_dir, fake_bundle):
    """The finding, as a regression test.

    `engines.enabled` is an organisation's policy. Nothing about
    upgrading the application says they stopped being Claude-only.
    """
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps({"engines": {"master": "claude", "enabled": ["claude"]}}),
        encoding="utf-8",
    )

    fake_bundle({"engines": {"master": "claude"}})
    _install("v2")

    assert json.loads(app.read_text())["engines"]["enabled"] == ["claude"]


def test_an_upgrade_delivers_a_changed_default_nobody_touched(
    isolated_config_dir, fake_bundle
):
    """The other half of the contract, and the reason this is a merge
    rather than a promotion to `_USER_FILES`.

    A value the user never edited still belongs to the bundle, so a
    release that changes it reaches an existing install.
    """
    _install("v1")
    app = isolated_config_dir / "app.json"
    assert json.loads(app.read_text())["history"]["mirror_gap_tolerance"] == 3

    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 5}}
    )
    _install("v2")

    assert json.loads(app.read_text())["history"]["mirror_gap_tolerance"] == 5


def test_a_user_edit_outranks_a_changed_default(isolated_config_dir, fake_bundle):
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps(
            {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 9}}
        ),
        encoding="utf-8",
    )

    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 5}}
    )
    _install("v2")

    assert json.loads(app.read_text())["history"]["mirror_gap_tolerance"] == 9


def test_an_upgrade_adds_a_key_the_release_introduced(
    isolated_config_dir, fake_bundle
):
    """Merging must not freeze the file at its install-time shape — a
    section a release adds has to arrive, or the file stops documenting
    what can be set."""
    _install("v1")
    app = isolated_config_dir / "app.json"

    fake_bundle(
        {
            "engines": {"master": "claude"},
            "history": {"mirror_gap_tolerance": 3},
            "doc_convert": {"enabled": True},
        }
    )
    _install("v2")

    assert json.loads(app.read_text())["doc_convert"] == {"enabled": True}


def test_a_nested_policy_survives_its_neighbour_upgrading(
    isolated_config_dir, fake_bundle
):
    """One key of `engines` is the user's and one is the bundle's. The
    merge is per key, not per section, or the two would fight."""
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps({"engines": {"master": "claude", "enabled": ["claude"]}}),
        encoding="utf-8",
    )

    fake_bundle({"engines": {"master": "agy"}})
    _install("v2")

    engines = json.loads(app.read_text())["engines"]
    assert engines == {"master": "agy", "enabled": ["claude"]}


def test_a_key_the_bundle_dropped_is_left_on_disk(isolated_config_dir, fake_bundle):
    """Same reasoning as retired *files*: deleting a user's text is
    irreversible, and an unread key costs bytes."""
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps({"engines": {"master": "claude"}, "url_cache": {"ttl": 60}}),
        encoding="utf-8",
    )

    fake_bundle({"engines": {"master": "claude"}})
    _install("v2")

    assert json.loads(app.read_text())["url_cache"] == {"ttl": 60}


def test_an_install_with_no_pristine_copy_keeps_every_value(
    isolated_config_dir, fake_bundle
):
    """The upgrade *into* this change, which is every existing install.

    With no ancestor on disk there is no evidence a value came from us,
    so every one of them is treated as the user's. New keys still land.
    """
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps(
            {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 3}}
        ),
        encoding="utf-8",
    )
    # Simulate a pre-merge install: the file is here, the ancestor isn't.
    shutil.rmtree(isolated_config_dir / ".pristine")

    fake_bundle(
        {
            "engines": {"master": "claude"},
            "history": {"mirror_gap_tolerance": 5},
            "doc_convert": {"enabled": True},
        }
    )
    _install("v2")

    merged = json.loads(app.read_text())
    assert merged["history"]["mirror_gap_tolerance"] == 3  # kept, not upgraded
    assert merged["doc_convert"] == {"enabled": True}  # still added


def test_the_pristine_copy_tracks_the_shipped_bundle(
    isolated_config_dir, fake_bundle
):
    """Recorded on install and refreshed on upgrade, so the next release
    compares against what this one shipped rather than against v1."""
    _install("v1")
    pristine = isolated_config_dir / ".pristine" / "app.json"
    assert json.loads(pristine.read_text())["history"]["mirror_gap_tolerance"] == 3

    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 5}}
    )
    _install("v2")
    assert json.loads(pristine.read_text())["history"]["mirror_gap_tolerance"] == 5

    # And the refresh is what makes a *third* release land: the user's 5
    # came from v2's bundle, not from them.
    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 7}}
    )
    _install("v3")
    app = json.loads((isolated_config_dir / "app.json").read_text())
    assert app["history"]["mirror_gap_tolerance"] == 7


def test_a_merge_that_changes_nothing_leaves_no_backup(
    isolated_config_dir, fake_bundle
):
    """Backups exist so a user can recover a value the upgrade took. An
    upgrade that took nothing has nothing to hand back, and a directory
    of identical copies is just noise."""
    _install("v1")
    fake_bundle({"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 3}})
    _install("v2")

    assert list(isolated_config_dir.glob("app.json.*")) == []


def test_a_merge_that_changes_something_backs_up_what_it_replaced(
    isolated_config_dir, fake_bundle
):
    _install("v1")
    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 5}}
    )
    _install("v2")

    backups = list(isolated_config_dir.glob("app.json.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["history"]["mirror_gap_tolerance"] == 3
    assert "v1" in backups[0].name


def test_an_unparseable_app_json_is_left_for_the_user_to_fix(
    isolated_config_dir, fake_bundle, caplog
):
    """Their file, unreadable. Overwriting replaces text we cannot read
    with text they did not write; every accessor already defaults, so the
    application starts either way."""
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text("{ not json at all", encoding="utf-8")

    fake_bundle({"engines": {"master": "claude"}})
    with caplog.at_level("WARNING"):
        _install("v2")

    assert app.read_text() == "{ not json at all"
    assert any("app.json" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# One unwritable file does not cost the others their upgrade
# ---------------------------------------------------------------------------


def test_a_read_only_app_json_holds_and_does_not_repeat(
    isolated_config_dir, fake_bundle, caplog
):
    """The second finding, as a regression test.

    A workplace that pins the policy by shipping the file read-only used
    to get: the OSError aborted the whole pass, the marker was never
    written, and every subsequent start retried it — another backup each
    time, and `commit.md` never upgraded at all.
    """
    _install("v1")
    app = isolated_config_dir / "app.json"
    app.write_text(
        json.dumps({"engines": {"master": "claude", "enabled": ["claude"]}}),
        encoding="utf-8",
    )
    app.chmod(0o444)
    commit = isolated_config_dir / "commit.md"
    commit.write_text("stale\n", encoding="utf-8")

    fake_bundle(
        {"engines": {"master": "claude"}, "history": {"mirror_gap_tolerance": 5}}
    )
    with caplog.at_level("WARNING"):
        _install("v2")

    # The policy held.
    assert json.loads(app.read_text())["engines"]["enabled"] == ["claude"]
    # The file it could write, it wrote.
    assert commit.read_text() == "bundled commit prompt\n"
    # The marker moved, so the pass is over.
    assert (isolated_config_dir / ".bundled_version").read_text().strip() == "v2"
    # It said so, naming the file and how to retry.
    warnings = [
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ]
    assert any("app.json" in w and ".bundled_version" in w for w in warnings)

    # A third start is a no-op, and left no litter behind on the second.
    _install("v2")
    assert list(isolated_config_dir.glob("app.json.*")) == []


def test_the_marker_is_written_even_when_a_file_is_left_alone(
    isolated_config_dir, fake_bundle
):
    """Stated separately from the read-only case because it is the
    decision, not the symptom: a pass that cannot finish still records
    that it ran, or it runs forever."""
    _install("v1")
    (isolated_config_dir / "app.json").write_text("{ broken", encoding="utf-8")

    fake_bundle({"engines": {"master": "claude"}})
    _install("v2")

    assert (isolated_config_dir / ".bundled_version").read_text().strip() == "v2"


# ---------------------------------------------------------------------------
# _merge_json, directly
# ---------------------------------------------------------------------------


def test_merge_distinguishes_a_null_ancestor_from_no_ancestor():
    """`pristine.get(key)` cannot tell these apart and they mean opposite
    things: a recorded null is an ancestor the user's value may match, an
    absent key is no ancestor at all."""
    # Recorded as null, user still has null → the bundle's new value.
    assert _merge_json({"k": None}, {"k": None}, {"k": 5}) == {"k": 5}
    # Not recorded → the user's null is theirs.
    assert _merge_json({"k": None}, {}, {"k": 5}) == {"k": None}


def test_merge_leaves_its_inputs_alone():
    """The pass reads the pristine copy once and writes the result;
    mutating either argument would be a silent second writer."""
    user = {"engines": {"enabled": ["claude"]}}
    bundled = {"engines": {"master": "claude"}}
    _merge_json(user, {}, bundled)
    assert user == {"engines": {"enabled": ["claude"]}}
    assert bundled == {"engines": {"master": "claude"}}


# ---------------------------------------------------------------------------
# AG-R-15 — the consultant's model, and the engine's persisted one
# ---------------------------------------------------------------------------


class TestEngineModels:
    def test_the_consultant_is_pinned_by_default(self, isolated_config_dir):
        """AG-R-15: unpinned inherits an account setting that can make a
        second opinion the same vendor asked twice."""
        from aic_dc.agy.consultant import DEFAULT_MODEL

        assert ConfigManager().consultant_model == DEFAULT_MODEL

    def test_auto_hands_the_choice_back_to_the_account(self, isolated_config_dir):
        cfg = ConfigManager()
        cfg.set_engine_option("consultant_model", "auto")
        assert ConfigManager().consultant_model is None

    def test_a_nonsense_value_costs_a_preference_not_the_app(
        self, isolated_config_dir, caplog
    ):
        from aic_dc.agy.consultant import DEFAULT_MODEL

        cfg = ConfigManager()
        cfg.set_engine_option("consultant_model", 17)
        assert ConfigManager().consultant_model == DEFAULT_MODEL

    def test_the_engine_is_not_pinned(self, isolated_config_dir):
        """Deliberately asymmetric with the consultant: a master engine is
        the one the user drives all day, so their own tier choice stands."""
        assert ConfigManager().agy_model is None

    def test_writing_one_key_leaves_the_others_alone(self, isolated_config_dir):
        """`set_engine_option` is a read-modify-write of a file the Settings
        editor also writes, so a blind overwrite would drop a user's edits."""
        cfg = ConfigManager()
        cfg.set_engine_option("master", "claude")
        cfg.set_engine_option("consultant_model", "gemini-3.8-flash-low")
        written = json.loads((isolated_config_dir / "app.json").read_text())
        assert written["engines"]["master"] == "claude"
        assert written["engines"]["consultant_model"] == "gemini-3.8-flash-low"

    def test_it_does_not_write_from_a_stale_cache(self, isolated_config_dir):
        """The manager's app_config may be older than an edit the user just
        saved in the Settings tab; writing from it would revert them."""
        cfg = ConfigManager()
        _ = cfg.app_config  # prime the cache
        path = isolated_config_dir / "app.json"
        on_disk = json.loads(path.read_text()) if path.exists() else {}
        on_disk["typed_by_the_user"] = "keep me"
        path.write_text(json.dumps(on_disk))
        cfg.set_engine_option("consultant_model", "gemini-3.8-flash-low")
        assert json.loads(path.read_text())["typed_by_the_user"] == "keep me"
