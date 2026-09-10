"""Configuration layer.

Single class :class:`ConfigManager` owns:

- Resolution of the config directory (dev tree vs packaged install,
  platform-specific user directory).
- Version-aware upgrade — bundled managed files get overwritten on
  upgrade (with a timestamped backup); user files are never touched.
- Cached but hot-reloadable access to the app config and the one
  remaining prompt (the commit-message one-shot's).
- Per-repo ``.aic-dc/`` working directory creation and gitignore wiring.

Most of what this module used to hold went with the native engine. The
conversation's system prompt, its document and review variants, the
per-turn reminder, the compaction prompt, every provider knob (models,
timeouts, retries, cache minimums, credential env vars) and the tiering
parameters all described an engine AIC⚡DC no longer runs — the CLI owns
its own prompt, its own model selection and its own context management.
What is left is what AIC⚡DC still decides for itself: how it converts
documents, how it indexes them, and how it asks a throwaway session for
a commit message. The engine's own
handful of settings live in ``engine.json``, read by
:class:`aic_dc.claude_code.engine_config.EngineConfig` rather than here.

Governing specs: ``specs5/1-foundation/configuration.md`` and
``specs4/6-deployment/packaging.md``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Safe to import from the config layer, unlike an engine package: this
# module is a frozen table of surface names with no dependencies of its
# own. The rule it must not break is the one the `history` defaults are
# duplicated for — the config layer answers without importing an engine.
from aic_dc import capabilities

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File category constants
# ---------------------------------------------------------------------------
#
# Managed files — safe to overwrite on upgrade. The bundled copy is
# the source of truth; user customisations to these should be applied
# via git patches to the source tree.
#
# User files — expected to be user-edited. Created from the bundle on
# first install, then never touched. Upgrading the app never clobbers
# the user's engine settings.
#
# Eight files left these sets with the native engine: llm.json,
# system.md, system_doc.md, system_agentic_appendix.md, system_extra.md,
# review.md, compaction.md and system_reminder.md. The upgrade pass
# iterates the union of the two sets, so a user who still has those
# files on disk keeps them: they may hold a customised prompt, that text
# is real work, and deleting it would be both irreversible and pointless
# (nothing reads the file either way). Ignoring them costs a few
# kilobytes. See specs5/1-foundation/configuration.md § Retired files
# are ignored, not deleted.

_MANAGED_FILES = frozenset({
    "commit.md",
    "app.json",
})

# Managed files upgraded key-by-key rather than wholesale. `app.json` is
# the only one, and it is here because "managed" and "written by the
# Settings tab" were both true of it: `CONFIG_TYPES` exposes `app` for
# editing, `engines.master` moves when a session switches engines, and
# AG-17's `engines.enabled` is an organisation's *policy* — so the
# upgrade that overwrote this file reverted every one of those, and
# quietly turned a Claude-only deployment back into a two-provider one
# (plan-ag AG-R-13). Overwriting is still right for `commit.md`: it is
# prose the bundle owns, a user who edits it is patching a prompt, and
# the backup is how they get their text back.
#
# Merging needs a third leg, because a user's file is a *copy of the
# bundle* taken at install: comparing it against the new bundle cannot
# tell "I chose this" from "this was the default". `_PRISTINE_DIR` holds
# the bundled file as it was last installed, so the comparison is the
# familiar three-way one — see `_merge_json`.
_MERGED_MANAGED_FILES = frozenset({
    "app.json",
})

_USER_FILES = frozenset({
    "engine.json",
})

# The eight the comment above names, as a set rather than as prose,
# because the Settings tab has to *say* which of them a given install
# still has. Deliberately in neither file set: the upgrade iterator
# walks their union and must keep skipping these.
#
# Order is the order the note lists them, so it is the reading order and
# not alphabetical — the provider file first because a leftover
# ``llm.json`` is the one startup already mentions, then the prompt
# files in the order they used to compose.
RETIRED_FILES: tuple[str, ...] = (
    "llm.json",
    "system.md",
    "system_extra.md",
    "system_doc.md",
    "system_agentic_appendix.md",
    "system_reminder.md",
    "review.md",
    "compaction.md",
)

# Version marker filename inside the user config dir. Hidden (leading
# dot), not in either file set so the upgrade iterator skips it.
_VERSION_MARKER = ".bundled_version"

# Where the pristine copies live: a hidden directory inside the user
# config dir holding each merged managed file exactly as the bundle last
# shipped it. Hidden and a directory, so neither the upgrade iterator
# (which walks filenames) nor the retired-file scan (which looks for
# files by name) sees it. An install that predates this directory has no
# pristine copy, and the merge reads that as "every value on disk is the
# user's" — the safe direction, since the alternative reverts an edit we
# cannot prove was ours.
_PRISTINE_DIR = ".pristine"

# Per-repo working directory name. Created under the repo root on
# first run; added to .gitignore.
_AIC_DC_DIR = ".aic-dc"

# Working directory names this project used before it was called
# AIC⚡DC. Nothing reads them — they exist so the directory walkers can
# keep skipping stale state a shared checkout may still hold.
_LEGACY_DIRS = (".ac-dc4", ".ac-dc")

# Defaults for the `history` section of app.json. Duplicated as a
# fallback rather than imported from `claude_code.session_store`, which
# holds the same number as its own default: the config layer must answer
# without the engine package, and a config manager that imports the
# engine to read a threshold inverts the dependency.
_DISK_WARNING_BYTES = 1024 * 1024 * 1024
_MIRROR_GAP_TOLERANCE = 3


def _int_at_least(value: Any, default: int, minimum: int) -> int:
    """``value`` as an int no smaller than ``minimum``, or ``default``.

    Anything unparseable or below the floor falls back. The floor differs
    per key and is not decoration: a zero-byte size warning fires on
    every check, while a zero gap tolerance honestly means "tell me about
    the first one".
    """
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


# ---------------------------------------------------------------------------
# Config type whitelist (for the Settings RPC service)
# ---------------------------------------------------------------------------
#
# Only these names can be read/written through the Settings service.
# ``commit.md`` is deliberately absent — it's loaded internally but not
# exposed for UI editing.
#
# Three entries, down from eight. The five prompt entries went with the
# prompt files, and the provider entry (``llm.json``) went with the
# engine that read it; ``engine`` took its place. Note that most of
# ``engine.json`` takes effect only on a new session — the Settings tab
# says so rather than appearing to apply live. See
# specs5/1-foundation/configuration.md § What a config change can and
# cannot do live.

CONFIG_TYPES: dict[str, str] = {
    "engine": "engine.json",
    "app": "app.json",
}


# ---------------------------------------------------------------------------
# Config directory resolution
# ---------------------------------------------------------------------------


def _bundled_config_dir() -> Path:
    """Locate the bundled (source-of-truth) config directory.

    Under PyInstaller, ``sys._MEIPASS`` points at the unpacked bundle
    root. Under a normal install, config lives next to this module.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        bundled = Path(meipass) / "aic_dc" / "config"
        if bundled.is_dir():
            return bundled
        logger.warning(
            "_MEIPASS set but aic_dc/config not found; "
            "falling back to module-relative lookup"
        )
    return Path(__file__).parent / "config"


def _user_config_dir(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Platform-appropriate user config directory.

    Per specs4/6-deployment/packaging.md:

    - Linux / BSD → ``~/.config/aic-dc/`` (honours XDG_CONFIG_HOME)
    - macOS      → ``~/Library/Application Support/aic-dc/``
    - Windows    → ``%APPDATA%/aic-dc/``

    ``AIC_DC_CONFIG_HOME`` environment variable overrides everything —
    tests use it to redirect to tmp paths without monkeypatching.

    ``env`` and ``home`` are injectable so a caller that must resolve a
    config path hermetically — :mod:`aic_dc.antigravity.credentials`,
    which resolves a secret and may not read the developer's own shell —
    gets this platform rule rather than a second copy of it.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    override = env.get("AIC_DC_CONFIG_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        appdata = env.get("APPDATA")
        if appdata:
            return Path(appdata) / "aic-dc"
        return home / "AppData" / "Roaming" / "aic-dc"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "aic-dc"
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "aic-dc"
    return home / ".config" / "aic-dc"


def _bundled_version() -> str:
    """Read the baked VERSION string.

    Returns an empty string on any read failure — callers treat
    empty-version installs as "never upgraded" and write a marker
    on first run.
    """
    version_file = Path(__file__).parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _backup_name(original: Path, installed_version: str) -> Path:
    """Return the backup path for a managed file being overwritten.

    - With known version: ``system.md.2025.06.15-14.32-a1b2c3d4``
    - Without: ``system.md.2025.06.15-14.32`` (UTC timestamp only)
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y.%m.%d-%H.%M")
    if installed_version:
        suffix = f".{timestamp}-{installed_version}"
    else:
        suffix = f".{timestamp}"
    return original.with_name(original.name + suffix)


# Distinguishes "the pristine copy records null for this key" from "the
# pristine copy does not mention this key". ``.get(key)`` cannot: both
# answer None, and they mean opposite things here.
_ABSENT = object()


def _merge_json(
    user: Mapping[str, Any],
    pristine: Mapping[str, Any],
    bundled: Mapping[str, Any],
) -> dict[str, Any]:
    """Three-way merge of one managed JSON file, key by key.

    ``pristine`` is the bundled file as it was last installed — the
    common ancestor. Per key:

    - Absent from ``user`` → take the bundled value. This is how a key
      a release *adds* reaches an existing install.
    - Present in all three and ``user == pristine`` → take the bundled
      value. The user never touched it, so a changed default lands.
    - ``user != pristine`` → keep the user's. Their edit is the answer,
      and an upgrade is not the place to overrule it.
    - No pristine record for the key → keep the user's, for the same
      reason as above: without an ancestor there is no evidence the
      value came from us.
    - Present in ``user`` but not in ``bundled`` → keep it. A key the
      bundle dropped is left on disk unread, exactly as a retired *file*
      is (see § Retired files are ignored, not deleted).

    Nested objects recurse, so ``engines.enabled`` survives a release
    that changes ``engines.master``'s default. Lists are compared whole:
    a user who edits ``doc_convert.extensions`` owns the list, and there
    is no per-element ancestry to merge against.
    """
    merged = dict(user)
    for key, bundled_value in bundled.items():
        if key not in merged:
            merged[key] = bundled_value
            continue
        user_value = merged[key]
        pristine_value = pristine.get(key, _ABSENT)
        if isinstance(user_value, Mapping) and isinstance(bundled_value, Mapping):
            merged[key] = _merge_json(
                user_value,
                pristine_value if isinstance(pristine_value, Mapping) else {},
                bundled_value,
            )
            continue
        if pristine_value is not _ABSENT and user_value == pristine_value:
            merged[key] = bundled_value
    return merged


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Owns the user config directory and exposes cached accessors.

    Construction performs, in order:

    1. Resolve the bundled and user config directories.
    2. Run the version-aware upgrade pass — copy new files, back up
       and overwrite managed files on version mismatch, leave user
       files alone.
    3. Lazily load config files on first property access. Hot-reload
       methods clear the cache to force re-read.

    Accessor properties are read-through — they consult the cached
    dict on every access rather than snapshotting values at
    construction time. Downstream consumers that hold a long-lived
    ConfigManager reference see hot-reloaded values on the next
    access without being re-constructed.
    """

    def __init__(self, repo_root: Path | str | None = None) -> None:
        """Initialise the config manager.

        Parameters
        ----------
        repo_root:
            Path to the git repository. When provided, the per-repo
            ``.aic-dc/`` working directory is created and added to
            ``.gitignore``. When ``None``, per-repo operations are
            skipped — useful for tests and for pre-repo tooling.
        """
        self._bundled_dir = _bundled_config_dir()
        self._user_dir = _user_config_dir()
        self._repo_root: Path | None = (
            Path(repo_root) if repo_root is not None else None
        )

        # Lazily-loaded cache. None means "not yet loaded"; a dict
        # means "loaded, use this value". The hot-reload method sets
        # it back to None to force a re-read.
        self._app_config: dict[str, Any] | None = None

        # Run the upgrade pass. Failure here is non-fatal — if the
        # user config directory itself can't be created (permissions,
        # etc.) we log and fall back to reading the bundle directly.
        # Per-*file* failures inside the pass no longer reach this
        # handler: they are reported and skipped there, so one
        # unwritable file cannot cost the others their upgrade.
        try:
            self._ensure_user_dir()
            self._run_upgrade()
        except OSError as exc:
            logger.warning(
                "Failed to initialise user config dir at %s: %s. "
                "Falling back to bundled config.",
                self._user_dir,
                exc,
            )

        # Per-repo working directory (if a repo was supplied).
        if self._repo_root is not None:
            try:
                self._init_aic_dc_dir()
            except OSError as exc:
                logger.warning(
                    "Failed to create .aic-dc dir at %s: %s",
                    self._repo_root,
                    exc,
                )

    # ------------------------------------------------------------------
    # Directory management
    # ------------------------------------------------------------------

    def _ensure_user_dir(self) -> None:
        """Create the user config directory if it doesn't exist."""
        self._user_dir.mkdir(parents=True, exist_ok=True)

    def _read_installed_version(self) -> str:
        """Read the version marker from the user config dir.

        Empty string means "no marker" — either a first install or
        a pre-tracking version. Either way, the upgrade pass will
        treat all files as new.
        """
        marker = self._user_dir / _VERSION_MARKER
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write_installed_version(self, version: str) -> None:
        """Write the version marker after a successful upgrade pass."""
        marker = self._user_dir / _VERSION_MARKER
        marker.write_text(version, encoding="utf-8")

    def _pristine_path(self, filename: str) -> Path:
        """Path of the last-installed bundled copy of ``filename``."""
        return self._user_dir / _PRISTINE_DIR / filename

    def _record_pristine(self, filename: str, bundled_path: Path) -> None:
        """Snapshot the bundled file as the ancestor for the next merge.

        Written on install and after every upgrade of a merged managed
        file, so the next release compares against what *this* release
        shipped rather than against the original install.
        """
        target = self._pristine_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_path, target)

    def _read_json_path(self, path: Path) -> dict[str, Any] | None:
        """Parse a JSON object at ``path``, or None if it isn't one.

        Distinct from :meth:`_read_user_json`, which answers ``{}`` for
        both "absent" and "unparseable" because its callers want a dict
        to ``.get()`` from. The merge cannot use that: an empty dict
        would read as "the user set nothing" and hand the whole bundled
        file back, which is the overwrite this method exists to avoid.
        """
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _run_upgrade(self) -> None:
        """Version-aware upgrade pass.

        Compares the bundled version against the installed marker:

        - Match → no action (fast path)
        - Mismatch or first install → copy new files, merge or overwrite
          managed files, leave user files alone

        Files not in either category set (the marker itself, the
        pristine directory, any stray files users may have added) are
        skipped.

        Each file is upgraded inside its own error handling. A file the
        pass cannot write — a read-only ``app.json`` shipped by
        configuration management is the case that happens — is reported
        and skipped, and the remaining files still upgrade. The marker is
        written even then, deliberately: the alternative left every
        subsequent startup repeating the whole pass, dropping another
        backup each time, and never upgrading the files it *could*
        write.
        """
        bundled_version = _bundled_version()
        installed_version = self._read_installed_version()

        if bundled_version and bundled_version == installed_version:
            logger.debug(
                "Config at version %s; no upgrade needed", bundled_version
            )
            return

        logger.info(
            "Config upgrade: installed=%r bundled=%r",
            installed_version or "(none)",
            bundled_version or "(none)",
        )

        left_alone: list[str] = []
        for filename in sorted(_MANAGED_FILES | _USER_FILES):
            try:
                reason = self._upgrade_one(filename, installed_version)
            except OSError as exc:
                reason = str(exc)
            if reason is not None:
                left_alone.append(f"{filename} ({reason})")

        if left_alone:
            logger.warning(
                "Config upgrade left %s as found: %s. The version marker "
                "is written anyway so startup does not retry on every run "
                "— resolve the cause and delete %s to run the pass again.",
                "one file" if len(left_alone) == 1 else "files",
                ", ".join(left_alone),
                self._user_dir / _VERSION_MARKER,
            )

        # Only write the marker if we actually have a bundled
        # version to record. Source installs (VERSION == "dev" or
        # empty) skip the marker so the next real release still
        # triggers an upgrade.
        if bundled_version:
            self._write_installed_version(bundled_version)

    def _upgrade_one(self, filename: str, installed_version: str) -> str | None:
        """Bring one config file up to the bundled version.

        Returns None when the file needed nothing or was brought up to
        date, and a short reason when it was deliberately left as found.
        Raises :class:`OSError` for the write failures the caller
        reports per file.
        """
        bundled_path = self._bundled_dir / filename
        user_path = self._user_dir / filename

        if not bundled_path.is_file():
            # Missing from bundle — nothing to copy. Not an
            # error (some files may be optional in future).
            return None

        if not user_path.exists():
            # New file — copy from bundle regardless of category.
            logger.info("Config install: %s", filename)
            shutil.copy2(bundled_path, user_path)
            if filename in _MERGED_MANAGED_FILES:
                self._record_pristine(filename, bundled_path)
            return None

        if filename in _USER_FILES:
            # User file already exists — never touch.
            return None

        if filename in _MERGED_MANAGED_FILES:
            return self._merge_one(
                filename, bundled_path, user_path, installed_version
            )

        # Managed prose — back up then overwrite.
        backup = _backup_name(user_path, installed_version)
        logger.info("Config upgrade: %s → backup %s", filename, backup.name)
        shutil.copy2(user_path, backup)
        shutil.copy2(bundled_path, user_path)
        return None

    def _merge_one(
        self,
        filename: str,
        bundled_path: Path,
        user_path: Path,
        installed_version: str,
    ) -> str | None:
        """Three-way merge one managed JSON file in place.

        The user's file is backed up and rewritten only when the merge
        actually changes something, so an install that has diverged from
        no default collects no backups. The pristine copy is refreshed
        either way — it records what the bundle shipped, not what we
        wrote.

        Note that the merged file is re-serialised: key order follows the
        user's file with bundled additions appended, and the bundle's
        hand-wrapped arrays come back expanded. JSON carries no comments,
        so there is nothing else to lose.
        """
        user_data = self._read_json_path(user_path)
        bundled_data = self._read_json_path(bundled_path)
        if user_data is None:
            # Their file, unreadable or not an object. Overwriting would
            # replace text we cannot read with text they did not write;
            # every accessor already falls back to its own default, so
            # the application runs either way and the user keeps the file
            # they have to fix.
            return "unparseable JSON, left for the user to fix"
        if bundled_data is None:
            return "bundled copy is not a JSON object"

        pristine_data = self._read_json_path(self._pristine_path(filename)) or {}
        merged = _merge_json(user_data, pristine_data, bundled_data)

        if merged != user_data:
            backup = _backup_name(user_path, installed_version)
            logger.info(
                "Config merge: %s → backup %s", filename, backup.name
            )
            shutil.copy2(user_path, backup)
            try:
                user_path.write_text(
                    json.dumps(merged, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                # A read-only file is a workplace pinning it, not a
                # fault. Take the backup back out: a copy of a file we
                # failed to change is litter, and littering once per
                # startup is what the old pass did.
                backup.unlink(missing_ok=True)
                raise
        else:
            logger.info("Config merge: %s already current", filename)

        self._record_pristine(filename, bundled_path)
        return None

    def _init_aic_dc_dir(self) -> None:
        """Create the per-repo ``.aic-dc/`` working directory.

        Idempotent — safe to call on every startup. Ensures the
        directory exists and that it appears in the repo's
        ``.gitignore``.

        No subdirectories are created here. The session store makes its
        own on first write, which keeps "this directory exists" and "a
        session was mirrored" from being the same signal. The
        ``images/`` directory this used to create is retired with the
        native engine: images now live in the transcript as the content
        blocks they were sent as, so there is nothing to put in it
        ([CC-19](../../specs5/plan/decisions.md#cc-19)). An existing one
        left by the native engine is ignored, not read and not migrated.
        """
        assert self._repo_root is not None  # guarded by caller
        aic_dc_path = self._repo_root / _AIC_DC_DIR
        aic_dc_path.mkdir(exist_ok=True)
        self._ensure_gitignore_entry()

    def _ensure_gitignore_entry(self) -> None:
        """Add ``.aic-dc/`` to the repo's ``.gitignore`` if absent.

        Idempotent — checks for an existing entry before appending.
        Creates ``.gitignore`` if it doesn't exist. If the repo
        doesn't have a git directory (not actually a git repo), we
        still write the entry because the config manager can't tell
        the difference and the file is harmless in a non-git dir.
        """
        assert self._repo_root is not None
        gitignore = self._repo_root / ".gitignore"
        entry = f"{_AIC_DC_DIR}/"

        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8")
            # Match either exact ".aic-dc/" or ".aic-dc" on its own line
            # — some users write the entry without the trailing slash.
            for line in existing.splitlines():
                stripped = line.strip()
                if stripped in (entry, _AIC_DC_DIR):
                    return  # already present
            # Not present — append with a leading newline if needed.
            suffix = "" if existing.endswith("\n") else "\n"
            gitignore.write_text(
                existing + suffix + entry + "\n",
                encoding="utf-8",
            )
        else:
            gitignore.write_text(entry + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # File-reading helpers
    # ------------------------------------------------------------------

    def _read_user_file(self, filename: str) -> str:
        """Read a file from the user config directory.

        Falls back to the bundled copy when the user file is absent
        — happens when the user-dir initialisation failed during
        construction. Returns an empty string if neither exists.
        """
        user_path = self._user_dir / filename
        try:
            return user_path.read_text(encoding="utf-8")
        except OSError:
            bundled_path = self._bundled_dir / filename
            try:
                return bundled_path.read_text(encoding="utf-8")
            except OSError:
                return ""

    def _read_user_json(self, filename: str) -> dict[str, Any]:
        """Read and parse a JSON file from the user config directory.

        Returns an empty dict on any read or parse failure and logs
        a warning — corrupt JSON should never crash construction.
        Callers that need a required field should use ``.get()``
        with a default, never index directly.
        """
        raw = self._read_user_file(filename)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse %s: %s. Using empty config.",
                filename,
                exc,
            )
            return {}
        if not isinstance(parsed, dict):
            logger.warning(
                "%s root is not an object (got %s). Using empty config.",
                filename,
                type(parsed).__name__,
            )
            return {}
        return parsed

    # ------------------------------------------------------------------
    # App config accessors
    # ------------------------------------------------------------------

    @property
    def app_config(self) -> dict[str, Any]:
        """Full app config dict, lazily loaded."""
        if self._app_config is None:
            self._app_config = self._read_user_json("app.json")
        return self._app_config

    @property
    def doc_convert_config(self) -> dict[str, Any]:
        """Document conversion section with defaults filled in."""
        section = self.app_config.get("doc_convert", {})
        if not isinstance(section, dict):
            section = {}
        extensions = section.get(
            "extensions",
            [".docx", ".pdf", ".pptx", ".xlsx", ".csv", ".rtf", ".odt", ".odp"],
        )
        if not isinstance(extensions, list):
            extensions = []
        return {
            "enabled": bool(section.get("enabled", True)),
            "extensions": [str(e) for e in extensions],
            "max_source_size_mb": int(section.get("max_source_size_mb", 50)),
        }

    @property
    def doc_index_config(self) -> dict[str, Any]:
        """Document index section with defaults filled in.

        Consumed by Layer 2's keyword enricher. Ranges and thresholds
        follow specs4/2-indexing/keyword-enrichment.md.
        """
        section = self.app_config.get("doc_index", {})
        if not isinstance(section, dict):
            section = {}
        ngram = section.get("keywords_ngram_range", [1, 2])
        if not isinstance(ngram, list) or len(ngram) != 2:
            ngram = [1, 2]
        return {
            "keyword_model": str(
                section.get("keyword_model", "BAAI/bge-small-en-v1.5")
            ),
            "keywords_enabled": bool(section.get("keywords_enabled", True)),
            "keywords_top_n": int(section.get("keywords_top_n", 3)),
            "keywords_ngram_range": [int(ngram[0]), int(ngram[1])],
            "keywords_min_section_chars": int(
                section.get("keywords_min_section_chars", 50)
            ),
            "keywords_min_score": float(section.get("keywords_min_score", 0.3)),
            "keywords_diversity": float(section.get("keywords_diversity", 0.5)),
            "keywords_tfidf_fallback_chars": int(
                section.get("keywords_tfidf_fallback_chars", 150)
            ),
            "keywords_max_doc_freq": float(
                section.get("keywords_max_doc_freq", 0.6)
            ),
        }

    @property
    def master_engine(self) -> str:
        """Which engine a session starts on (``specs5/plan-ag`` AG-1).

        Lives in ``app.json`` rather than in ``engine.json`` despite the
        second file's name. Every key in ``engine.json`` is a *Claude
        session option* — model, effort, permission mode — read by
        ``claude_code.engine_config``; which engine is master is a fact
        about the application, and putting it in one engine's option file
        would make the second engine's existence conditional on the
        first's config.

        It does not break :meth:`reload_app_config`'s promise that nothing
        in ``app.json`` reaches the engine's session options. This is read
        at startup and at an explicit switch, never mid-session, so a
        reload changes what the *next* session starts on and invalidates
        no running context.

        An unknown name falls back to Claude with a warning rather than
        raising. A typo here should cost the user the second engine, not
        the ability to start the application — and the fallback is the
        engine that is always mountable.
        """
        section = self.app_config.get("engines", {})
        if not isinstance(section, dict):
            section = {}
        chosen = section.get("master")
        if chosen is None:
            return capabilities.CLAUDE
        if chosen not in capabilities.ENGINES:
            logger.warning(
                "app.json engines.master is %r, which is not one of %s. "
                "Starting on %s.",
                chosen,
                ", ".join(capabilities.ENGINES),
                capabilities.CLAUDE,
            )
            return capabilities.CLAUDE
        return chosen

    @property
    def enabled_engines(self) -> tuple[str, ...]:
        """Which engines this install may mount at all (AG-17).

        `app.json`'s `engines.enabled`, defaulting to every engine. It is
        a *policy* rather than a preference: some workplaces are only
        permitted to use Claude, and until this existed nothing could
        express that — the `agy` adapter mounted on the binary being on
        PATH with no configuration consulted, and `engines.master` names
        which engine starts rather than which may run.

        **The consultant follows this too**, which is the reason it is an
        engine list rather than a set of feature switches. Reaching
        Antigravity from inside a Claude turn is the same question as
        running it as master — one provider, one answer — and two
        switches for one question are two things that can disagree.

        **Claude cannot be removed.** A list that omits it, or names only
        engines this install does not have, would leave the application
        with no engine at all: a config typo costing the user the whole
        product, which is the failure `master_engine`'s fallback already
        exists to prevent. It is added back with a warning.

        Unknown names are dropped with a warning rather than raising, for
        the same reason, and an `engines.enabled` that is not a list is
        ignored entirely — a malformed policy must not read as a
        *narrower* policy than the user wrote, because the direction of
        that mistake decides whether they lose a feature or lose a
        restriction. Ignoring it keeps every engine, which is the state
        they had before writing it, and the Settings panel shows what is
        in force.
        """
        section = self.app_config.get("engines", {})
        if not isinstance(section, dict):
            section = {}
        chosen = section.get("enabled")
        if chosen is None:
            return tuple(capabilities.ENGINES)
        if not isinstance(chosen, list):
            logger.warning(
                "app.json engines.enabled is %r, which is not a list of "
                "engine names. Ignoring it; every engine stays enabled.",
                chosen,
            )
            return tuple(capabilities.ENGINES)

        named = [name for name in chosen if isinstance(name, str)]
        unknown = [name for name in named if name not in capabilities.ENGINES]
        if unknown:
            logger.warning(
                "app.json engines.enabled names %s, which %s not %s. "
                "Ignoring the unknown %s.",
                ", ".join(repr(name) for name in unknown),
                "is" if len(unknown) == 1 else "are",
                " or ".join(capabilities.ENGINES),
                "name" if len(unknown) == 1 else "names",
            )
        enabled = [name for name in named if name in capabilities.ENGINES]
        if capabilities.CLAUDE not in enabled:
            logger.warning(
                "app.json engines.enabled does not name %s. Adding it: an "
                "install with no engine is not a configuration this "
                "application can run.",
                capabilities.CLAUDE,
            )
            enabled.insert(0, capabilities.CLAUDE)
        # Ordered by ENGINES rather than by the file, so two configs that
        # enable the same engines compare equal however they were written.
        return tuple(name for name in capabilities.ENGINES if name in enabled)

    @property
    def consultant_transport(self) -> str:
        """Which Antigravity transport answers ``second_opinion`` (AG-16).

        ``"auto"`` by default, and auto prefers ``agy``: the SDK path
        authenticates with a metered Gemini key whose free tier allows
        twenty agent requests a day and *zero* image generations, while
        ``agy`` reaches the account holder's own subscription. A default
        that picks the transport which cannot generate an image would
        leave AG-1's worked example broken for the reason it has always
        been broken.

        ``"sdk"`` and ``"agy"`` name one explicitly. The first is worth
        having rather than theoretical: a user with a *paid* key may
        prefer it, because it pins the model, and an opinion whose model
        moves is not a second opinion.

        An unknown value falls back to ``"auto"`` with a warning, for the
        reason :meth:`master_engine` does — a typo should cost a
        preference, not the application.
        """
        section = self.app_config.get("engines", {})
        if not isinstance(section, dict):
            section = {}
        chosen = section.get("consultant")
        if chosen is None:
            return "auto"
        if chosen not in ("auto", "agy", "sdk"):
            logger.warning(
                "app.json engines.consultant is %r, which is not one of "
                "auto, agy, sdk. Choosing automatically.",
                chosen,
            )
            return "auto"
        return str(chosen)

    #: What ``engines.consultant_model`` / ``engines.agy_model`` mean when
    #: they hold this word: *do not pass ``--model``*, so ``agy`` uses the
    #: model the account holder selected in its own settings.
    #:
    #: Spelled rather than left to ``null`` because :func:`_changed_fields`
    #: in ``settings.py`` treats absent and explicitly-null as the same
    #: value — deliberately, so a save that only changes how "unset" is
    #: written does not offer a restart. A meaning that needs to survive
    #: that has to be a value, and ``"auto"`` is the vocabulary
    #: :meth:`consultant_transport` already uses for "you choose".
    INHERIT_ACCOUNT_MODEL = "auto"

    @property
    def consultant_model(self) -> str | None:
        """Which model answers ``second_opinion`` on the ``agy`` transport.

        ``None`` means "pass no ``--model`` and let the account's own
        setting decide"; a string is a model id handed to ``agy``.

        **The default is pinned, and it is pinned for a reason that is not
        performance** ([AG-R-15](../../specs5/plan-ag/risks.md#ag-r-15)).
        ``agy models`` offers Claude and GPT-OSS entries beside the Gemini
        ones, so inheriting the account's choice inherits the possibility
        that a *second* opinion is the same vendor asked twice — which is
        the one thing [AG-13](../../specs5/plan-ag/decisions.md#ag-13)
        exists to prevent, arriving through a settings file this app does
        not own. Effort is baked into the model name on this surface, so
        the pin also settles the depth question the README raises about the
        SDK transport: the point of a second opinion is a *capable* one.

        This is not validated against ``agy models`` here, because that is
        a subprocess and this is a property. :meth:`Settings.set_consultant_model`
        validates before writing, and an unknown name reaching ``agy``
        fails loudly at session start rather than silently downgrading.
        """
        from aic_dc.agy.consultant import DEFAULT_MODEL

        section = self.app_config.get("engines", {})
        if not isinstance(section, dict):
            section = {}
        chosen = section.get("consultant_model")
        if chosen is None:
            return DEFAULT_MODEL
        if chosen == self.INHERIT_ACCOUNT_MODEL:
            return None
        if not isinstance(chosen, str) or not chosen.strip():
            logger.warning(
                "app.json engines.consultant_model is %r, which is not a "
                "model id or %r. Using %s.",
                chosen,
                self.INHERIT_ACCOUNT_MODEL,
                DEFAULT_MODEL,
            )
            return DEFAULT_MODEL
        return chosen

    @property
    def agy_model(self) -> str | None:
        """Which model the ``agy`` *engine* runs as master.

        Unpinned by default — ``None``, the account holder's own choice —
        and that asymmetry with :meth:`consultant_model` is the point.
        A master engine is the one the user drives all day and whose cost
        and latency they feel directly, so inheriting the tier they chose
        in ``agy`` is respecting a decision. A consultation is a
        cross-check bought by the answer it gives, and it has a vendor
        requirement the master does not.

        Persisted so a choice survives a restart, which
        :meth:`AgyService.set_model` did not do until 2026-09-09: it
        assigned an instance attribute, so every selection was silently
        forgotten the next time the server started.
        """
        section = self.app_config.get("engines", {})
        if not isinstance(section, dict):
            section = {}
        chosen = section.get("agy_model")
        if chosen is None or chosen == self.INHERIT_ACCOUNT_MODEL:
            return None
        if not isinstance(chosen, str) or not chosen.strip():
            logger.warning(
                "app.json engines.agy_model is %r, which is not a model id "
                "or %r. Letting agy choose.",
                chosen,
                self.INHERIT_ACCOUNT_MODEL,
            )
            return None
        return chosen

    def set_engine_option(self, key: str, value: Any) -> None:
        """Write one key under ``app.json``'s ``engines`` section.

        A read-modify-write of the file rather than of :attr:`app_config`,
        because that cache may be stale against an edit the user made in
        the Settings editor, and writing from it would silently revert
        them. Whole-file rename for the same reason
        :func:`aic_dc.agy.registry.claim` uses one: a reader can arrive at
        any instant and a half-written config parses as no config.

        The in-memory cache is invalidated rather than patched, so the next
        read goes through the same accessors and defaults as a fresh start.
        """
        path = self.config_dir / "app.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {}
        except (OSError, ValueError):
            current = {}
        section = current.get("engines")
        if not isinstance(section, dict):
            section = {}
        section[key] = value
        current["engines"] = section
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        self._app_config = None

    @property
    def history_config(self) -> dict[str, Any]:
        """Transcript-history section with defaults filled in.

        Two thresholds, both about the mirrored transcripts under
        ``.aic-dc/`` (``specs5/1-foundation/configuration.md`` § App
        Config):

        ``session_dir_warning_bytes``
            When the session directory passes this, the user is told once.
            A gigabyte by default. Reached sooner than the native engine's
            history did, because pasted images now live in the transcript
            as the base64 blocks they were sent as.
        ``mirror_gap_tolerance``
            How many failed mirror appends are tolerated before the
            engine-health banner escalates from a warning to a fault.
            Three by default: the SDK retries a batch before reporting a
            gap at all, so one is bad luck and a fourth is a broken
            mirror.

        An unreadable value falls back to the default rather than
        disabling the check. The two floors differ: a size warning of
        zero bytes would fire on every check, which is how a warning
        worth reading becomes one nobody reads, while a tolerance of zero
        honestly means "escalate on the first gap" and is honoured.
        """
        section = self.app_config.get("history", {})
        if not isinstance(section, dict):
            section = {}
        return {
            "session_dir_warning_bytes": _int_at_least(
                section.get("session_dir_warning_bytes"), _DISK_WARNING_BYTES, 1
            ),
            "mirror_gap_tolerance": _int_at_least(
                section.get("mirror_gap_tolerance"), _MIRROR_GAP_TOLERANCE, 0
            ),
        }

    # ------------------------------------------------------------------
    # Directory accessors
    # ------------------------------------------------------------------

    @property
    def repo_root(self) -> Path | None:
        """The git repository root, if one was supplied."""
        return self._repo_root

    @property
    def config_dir(self) -> Path:
        """The resolved user config directory."""
        return self._user_dir

    def retired_files_present(self) -> list[str]:
        """Which retired config files this install still has on disk.

        Retired files are left alone on upgrade — never read, never
        migrated, never deleted — because ``system_extra.md`` may hold
        months of a user's own prompt work and throwing it away would be
        irreversible and pointless (see the module comment on
        :data:`RETIRED_FILES`). The cost of that rule is silence: the
        cards that edited these files are gone from the Settings tab and
        nothing says why.

        This is what lets the tab break that silence, and it is a
        *query about this install* rather than the constant list. A
        fresh install has none of them and must be told nothing — a note
        naming files the user has never had would be noise, and the one
        thing the note has to be is relevant.

        Names only, in :data:`RETIRED_FILES` order, no paths: the
        directory is already on screen in the info banner, and the
        browser has no use for an absolute path it cannot open.
        """
        return [
            name
            for name in RETIRED_FILES
            if (self._user_dir / name).is_file()
        ]

    @property
    def aic_dc_dir(self) -> Path | None:
        """The per-repo ``.aic-dc/`` directory, if a repo was supplied."""
        if self._repo_root is None:
            return None
        return self._repo_root / _AIC_DC_DIR

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    def reload_app_config(self) -> None:
        """Re-read ``app.json``.

        Downstream consumers that access ``doc_convert_config``,
        ``doc_index_config``, etc. through this ConfigManager will
        see the new values on their next access — no need to rebuild
        the converter or the doc index.

        Nothing in ``app.json`` reaches the engine's session options, so
        a reload here never invalidates the CLI's context.
        """
        self._app_config = None

    # ------------------------------------------------------------------
    # The commit prompt
    # ------------------------------------------------------------------
    #
    # Read fresh from disk on every call — no caching. An edit to
    # commit.md takes effect on the next commit without a reload.

    def get_commit_prompt(self) -> str:
        """Commit message generation prompt.

        The only prompt AIC⚡DC still writes. It is no longer a system
        prompt for a separately-configured auxiliary model — it is the
        system prompt of a stateless one-shot ``query()`` against the
        same CLI, which is handed the staged diff and nothing else. See
        :mod:`aic_dc.claude_code.commit`.
        """
        return self._read_user_file("commit.md")
