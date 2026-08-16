"""Configuration layer.

Single class :class:`ConfigManager` owns:

- Resolution of the config directory (dev tree vs packaged install,
  platform-specific user directory).
- Version-aware upgrade — bundled managed files get overwritten on
  upgrade (with a timestamped backup); user files are never touched.
- Cached but hot-reloadable access to the app config, the snippets and
  the one remaining prompt (the commit-message one-shot's).
- Per-repo ``.ac-dc4/`` working directory creation and gitignore wiring.

Most of what this module used to hold went with the native engine. The
conversation's system prompt, its document and review variants, the
per-turn reminder, the compaction prompt, every provider knob (models,
timeouts, retries, cache minimums, credential env vars) and the tiering
parameters all described an engine AC⚡DC no longer runs — the CLI owns
its own prompt, its own model selection and its own context management.
What is left is what AC⚡DC still decides for itself: how it converts
documents, how it indexes them, which snippets the composer offers, and
how it asks a throwaway session for a commit message. The engine's own
handful of settings live in ``engine.json``, read by
:class:`ac_dc.claude_code.engine_config.EngineConfig` rather than here.

Governing specs: ``specs5/1-foundation/configuration.md`` and
``specs4/6-deployment/packaging.md``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    "snippets.json",
})

_USER_FILES = frozenset({
    "engine.json",
})

# Version marker filename inside the user config dir. Hidden (leading
# dot), not in either file set so the upgrade iterator skips it.
_VERSION_MARKER = ".bundled_version"

# Per-repo working directory name. Created under the repo root on
# first run; added to .gitignore. The `4` suffix is deliberate —
# this reimplementation shares repositories with the previous
# `.ac-dc/`-using implementation during the transition, and
# colliding on the same directory name would corrupt both states.
# See IMPLEMENTATION_NOTES.md for the rename rationale.
_AC_DC_DIR = ".ac-dc4"

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
    "snippets": "snippets.json",
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
        bundled = Path(meipass) / "ac_dc" / "config"
        if bundled.is_dir():
            return bundled
        logger.warning(
            "_MEIPASS set but ac_dc/config not found; "
            "falling back to module-relative lookup"
        )
    return Path(__file__).parent / "config"


def _user_config_dir() -> Path:
    """Platform-appropriate user config directory.

    Per specs4/6-deployment/packaging.md:

    - Linux / BSD → ``~/.config/ac-dc/`` (honours XDG_CONFIG_HOME)
    - macOS      → ``~/Library/Application Support/ac-dc/``
    - Windows    → ``%APPDATA%/ac-dc/``

    ``AC_DC_CONFIG_HOME`` environment variable overrides everything —
    tests use it to redirect to tmp paths without monkeypatching.
    """
    override = os.environ.get("AC_DC_CONFIG_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "ac-dc"
        return Path.home() / "AppData" / "Roaming" / "ac-dc"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ac-dc"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "ac-dc"
    return Path.home() / ".config" / "ac-dc"


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
            ``.ac-dc/`` working directory is created and added to
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
        # user config directory can't be created (permissions, etc.)
        # we log and fall back to reading the bundle directly.
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
                self._init_ac_dc_dir()
            except OSError as exc:
                logger.warning(
                    "Failed to create .ac-dc dir at %s: %s",
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

    def _run_upgrade(self) -> None:
        """Version-aware upgrade pass.

        Compares the bundled version against the installed marker:

        - Match → no action (fast path)
        - Mismatch or first install → copy new files, back up and
          overwrite managed files, leave user files alone

        Files not in either category set (the marker itself, any
        stray files users may have added) are skipped.
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

        for filename in sorted(_MANAGED_FILES | _USER_FILES):
            bundled_path = self._bundled_dir / filename
            user_path = self._user_dir / filename

            if not bundled_path.is_file():
                # Missing from bundle — nothing to copy. Not an
                # error (some files may be optional in future).
                continue

            if not user_path.exists():
                # New file — copy from bundle regardless of category.
                logger.info("Config install: %s", filename)
                shutil.copy2(bundled_path, user_path)
                continue

            if filename in _USER_FILES:
                # User file already exists — never touch.
                continue

            if filename in _MANAGED_FILES:
                # Back up then overwrite.
                backup = _backup_name(user_path, installed_version)
                logger.info(
                    "Config upgrade: %s → backup %s",
                    filename,
                    backup.name,
                )
                shutil.copy2(user_path, backup)
                shutil.copy2(bundled_path, user_path)

        # Only write the marker if we actually have a bundled
        # version to record. Source installs (VERSION == "dev" or
        # empty) skip the marker so the next real release still
        # triggers an upgrade.
        if bundled_version:
            self._write_installed_version(bundled_version)

    def _init_ac_dc_dir(self) -> None:
        """Create the per-repo ``.ac-dc/`` working directory.

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
        ac_dc_path = self._repo_root / _AC_DC_DIR
        ac_dc_path.mkdir(exist_ok=True)
        self._ensure_gitignore_entry()

    def _ensure_gitignore_entry(self) -> None:
        """Add ``.ac-dc/`` to the repo's ``.gitignore`` if absent.

        Idempotent — checks for an existing entry before appending.
        Creates ``.gitignore`` if it doesn't exist. If the repo
        doesn't have a git directory (not actually a git repo), we
        still write the entry because the config manager can't tell
        the difference and the file is harmless in a non-git dir.
        """
        assert self._repo_root is not None
        gitignore = self._repo_root / ".gitignore"
        entry = f"{_AC_DC_DIR}/"

        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8")
            # Match either exact ".ac-dc/" or ".ac-dc" on its own line
            # — some users write the entry without the trailing slash.
            for line in existing.splitlines():
                stripped = line.strip()
                if stripped in (entry, _AC_DC_DIR):
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
    def history_config(self) -> dict[str, Any]:
        """Transcript-history section with defaults filled in.

        Two thresholds, both about the mirrored transcripts under
        ``.ac-dc4/`` (``specs5/1-foundation/configuration.md`` § App
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

    @property
    def ac_dc_dir(self) -> Path | None:
        """The per-repo ``.ac-dc/`` directory, if a repo was supplied."""
        if self._repo_root is None:
            return None
        return self._repo_root / _AC_DC_DIR

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

        The only prompt AC⚡DC still writes. It is no longer a system
        prompt for a separately-configured auxiliary model — it is the
        system prompt of a stateless one-shot ``query()`` against the
        same CLI, which is handed the staged diff and nothing else. See
        :mod:`ac_dc.claude_code.commit`.
        """
        return self._read_user_file("commit.md")

    # ------------------------------------------------------------------
    # Snippets
    # ------------------------------------------------------------------

    def get_snippets(self, mode: str = "code") -> list[dict[str, str]]:
        """Load quick-insert snippets for a mode.

        Parameters
        ----------
        mode:
            One of ``"code"`` (default), ``"review"``, or ``"doc"``.

        Resolution order (two-location fallback):

        1. Per-repo override at ``<repo_root>/.ac-dc/snippets.json``
        2. User config directory ``snippets.json``

        The first file that exists and parses is used; further
        fallback is not attempted. Returns an empty list on any
        failure rather than raising — a broken snippets file must
        not break the chat UI.

        Supports both the canonical nested format::

            {"code": [...], "review": [...], "doc": [...]}

        and the legacy flat format::

            {"snippets": [{"mode": "code", ...}, ...]}

        Legacy entries missing a ``mode`` field default to ``code``.
        """
        data = self._load_snippets_data()
        if not data:
            return []

        # Nested format — the mode key maps directly to its list.
        if mode in data and isinstance(data[mode], list):
            return [s for s in data[mode] if isinstance(s, dict)]

        # Legacy flat format — filter by mode field.
        if isinstance(data.get("snippets"), list):
            result: list[dict[str, str]] = []
            for entry in data["snippets"]:
                if not isinstance(entry, dict):
                    continue
                entry_mode = entry.get("mode", "code")
                if entry_mode == mode:
                    result.append(entry)
            return result

        return []

    def _load_snippets_data(self) -> dict[str, Any]:
        """Load and parse the snippets file with two-location fallback.

        Per-repo override takes precedence so users can customise
        snippets for individual repos without editing their global
        config.
        """
        # 1. Per-repo override.
        if self._repo_root is not None:
            repo_override = self._repo_root / _AC_DC_DIR / "snippets.json"
            if repo_override.is_file():
                try:
                    parsed = json.loads(
                        repo_override.read_text(encoding="utf-8")
                    )
                    if isinstance(parsed, dict):
                        return parsed
                    logger.warning(
                        "Per-repo snippets root is not an object; "
                        "falling back to user config"
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "Failed to read per-repo snippets: %s; "
                        "falling back to user config",
                        exc,
                    )

        # 2. User config directory.
        return self._read_user_json("snippets.json")