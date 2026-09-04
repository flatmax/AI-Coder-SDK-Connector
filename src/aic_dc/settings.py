"""Settings RPC service — Layer 1 (deferred) / Layer 4.4.2 (restriction).

The Settings service exposes a narrow, whitelisted surface for reading
and writing user-editable config files. It's registered alongside
:class:`Repo` and :class:`ClaudeCodeService` via
``server.add_class(settings)`` so the browser can call
``Settings.get_config_content(...)`` etc.

Scope pinned by specs5/1-foundation/configuration.md#settings-service
and specs5/1-foundation/rpc-inventory.md:

- **Whitelisted config types only.** The :data:`CONFIG_TYPES` map in
  :mod:`aic_dc.config` is the authoritative allow-list — two entries
  now (``"engine"`` and ``"app"``), down from eight.
  Callers pass a type key; the service maps that to a real filename and
  reads / writes inside the user config directory. Arbitrary paths are
  rejected — a caller that asks for ``"commit"`` (loaded internally but
  not exposed for UI editing) gets an error, not the file content.

- **Reload is type-aware, and narrower than it looks.** Editing
  ``app.json`` invalidates the doc-convert / doc-index caches, and
  nothing in it reaches the engine, so the new values apply on the next
  use. ``engine.json`` has **no reload RPC at all**: session options are
  assembled once, at connect time, so most of that file only takes
  effect on a new session and a reload call would be a lie. The
  Settings tab says so and offers a restart instead — an offer it makes
  from the per-field disposition every save returns
  (:func:`_save_disposition`), so it names the fields that are waiting
  rather than warning about the file in general.

- **Localhost-only for writes and reloads.** The collaboration policy
  treats config edits as mutation-class operations. Read methods
  (``get_config_content``, ``get_config_info``) are always allowed; write and reload methods
  check :meth:`_check_localhost_only` and return the restricted-error
  shape when a non-localhost participant calls them. Matches the
  pattern established in :class:`Repo` and
  :class:`ClaudeCodeService`.

- **No caller-side file discovery.** The browser asks for a config
  type; the service maps it to the disk path. Arbitrary paths
  never cross the RPC boundary — makes it impossible for a
  misbehaving client to read or write outside the user config
  dir.

- **Direct file I/O, not going through private ConfigManager
  methods.** :class:`ConfigManager` has an internal
  ``_read_user_file`` that falls back to the bundle when the user
  file is absent. That fallback is wrong for Settings — we want to
  present the user's edited content, not silently show the bundle
  when the user file is missing. Read directly from
  ``config.config_dir / filename`` so the UI reflects the actual
  on-disk state.

Design notes:

- **JSON validation on write is advisory.** The service tries
  ``json.loads`` on ``.json``-suffixed content after a successful
  write and emits a warning to the returned dict when parse fails.
  The file is still written — users may save a broken file
  intentionally (mid-edit) and reload it later. Rejecting malformed
  JSON at the write boundary would force users into a third-party
  editor to fix syntax errors.

- **Reload is a separate RPC, not implicit on save.** The spec says
  "For reloadable configs, save automatically triggers the
  corresponding reload RPC" — but the frontend controls that
  dispatch, not the service. The service exposes `reload_app_config`
  as a distinct call so the UI can decide (e.g., after a save
  succeeded AND passed JSON validation). A separate Reload button is
  also available for the user-edited-on-disk case.

- **`get_config_info` returns a snapshot.** Just the config directory
  now: the model names it used to carry described the native engine's
  two-model setup, and the model in force is the engine's own — it
  comes from ``ClaudeCodeService.get_current_state()``, live, rather
  than from a config file the CLI may have overridden. Still a dict so
  the browser can render the whole banner from one RPC call.

Governing spec: ``specs5/1-foundation/configuration.md#settings-service``.
Restriction pattern: ``specs5/1-foundation/rpc-transport.md``.
"""

from __future__ import annotations

import json
import logging
import shutil

from aic_dc import capabilities
from typing import TYPE_CHECKING, Any

from aic_dc.claude_code.engine_config import LIVE_CONTROLS
from aic_dc.config import CONFIG_TYPES

if TYPE_CHECKING:
    from aic_dc.config import ConfigManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reload-trigger map
# ---------------------------------------------------------------------------
#
# Which config types cause a reload call into ConfigManager on save.
#
# "app"    → reload_app_config (invalidates the doc-convert and
#            doc-index caches)
# "engine" → deliberately NOT reloadable. The session's options are
#            built once at connect time, so a reload would clear a
#            cache nobody reads and report success for a change that
#            hasn't happened. A new session is the honest answer.

_RELOADABLE_TYPES = frozenset({"app"})

#: The config type whose fields :data:`LIVE_CONTROLS` describes.
_ENGINE_TYPE = "engine"


# ---------------------------------------------------------------------------
# Per-field save disposition
# ---------------------------------------------------------------------------


def _parsed_object(content: str) -> dict[str, Any] | None:
    """``content`` as a JSON object, or ``None`` if it is not one."""
    try:
        parsed = json.loads(content)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _changed_fields(
    before: dict[str, Any] | None, after: dict[str, Any]
) -> list[str]:
    """Top-level keys whose effective value differs between two configs.

    A key that was *removed* counts as changed: dropping ``effort`` hands
    the decision back to the CLI, which changes what the next session runs.

    Absent and explicitly ``null`` are the same value here, because they are
    the same value in both files — ``engine.json``'s null means "omit the
    option" and ``app.json``'s consumers read through accessors with
    defaults. Distinguishing them would offer a restart to apply a save that
    changed only how "unset" was spelled.

    ``before is None`` means the old content could not be read or parsed, so
    every key present now is reported. See :func:`_save_disposition`.
    """
    if before is None:
        return sorted(after)
    return sorted(
        key
        for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    )


def _save_disposition(
    type_key: str, content: str, previous: str | None
) -> dict[str, Any] | None:
    """Where each field this save changed can take effect.

    ``None`` when ``content`` is not a JSON object: there is nothing to
    diff, and the save is already reporting the parse error as a warning.
    Distinct from an empty ``changed`` list, which is a real answer — the
    user saved a file that says what it said before.

    ``compared`` is False when the previous content was missing or would not
    parse. Every key in the new content is then reported as changed, because
    "nothing changed" is a claim this call cannot support and it would be
    wrong in the direction that drops a field from the summary.

    ``live`` is a disposition, not a receipt. The reload that applies those
    fields is a separate RPC the caller makes next, so the caller is the one
    that knows whether it succeeded; reporting them as applied from here
    would claim a reload that has not happened yet.
    """
    after = _parsed_object(content)
    if after is None:
        return None
    before = _parsed_object(previous) if previous is not None else None
    changed = _changed_fields(before, after)
    reloadable = type_key in _RELOADABLE_TYPES
    live_control: dict[str, str] = {}
    if type_key == _ENGINE_TYPE:
        live_control = {
            field: LIVE_CONTROLS[field] for field in changed if field in LIVE_CONTROLS
        }
    return {
        "compared": before is not None,
        "changed": changed,
        "live": changed if reloadable else [],
        "next_session": [] if reloadable else changed,
        "live_control": live_control,
    }


class Settings:
    """Config-editing RPC service.

    Construct once per backend process and register via
    ``server.add_class(settings)``. Holds a reference to the
    :class:`ConfigManager` — the authoritative owner of config
    state — and delegates all heavy lifting to it. The Settings
    service is just the RPC surface.

    Thread-safety — reads and writes run on the event loop thread.
    No internal state beyond the config manager reference and the
    collab reference (for localhost checks).
    """

    def __init__(self, config: "ConfigManager") -> None:
        """Construct against an existing ConfigManager.

        Parameters
        ----------
        config:
            The central config manager. Settings never replaces
            the instance; saves write to disk and then (optionally)
            ask the config manager to reload.

        The optional ``llm_service`` parameter is gone. It existed so
        that saving ``app.json`` could ask the native engine to
        re-assemble its system prompt — the ``agents.enabled`` toggle
        changed the prompt, and the context manager had cached it.
        There is no prompt to re-assemble now, and nothing in
        ``app.json`` reaches the engine at all.
        """
        self._config = config
        # Collaboration reference — set by main.py when collab mode
        # is active, None otherwise. When None, every caller is
        # treated as localhost. Matches the pattern on Repo and
        # ClaudeCodeService.
        self._collab: Any = None

    # ------------------------------------------------------------------
    # Localhost-only guard
    # ------------------------------------------------------------------

    def _check_localhost_only(self) -> dict[str, Any] | None:
        """Return a restricted-error dict when the caller is non-localhost.

        Same contract as :meth:`Repo._check_localhost_only` and
        :meth:`ClaudeCodeService._check_localhost_only` — returns None
        for allowed callers (no collab attached, or localhost), returns
        the mandated shape otherwise. Fails closed on collab
        check exceptions — better to deny a legitimate call than to
        let an unauthenticated caller mutate config.
        """
        if self._collab is None:
            return None
        try:
            is_local = self._collab.is_caller_localhost()
        except Exception as exc:
            logger.warning(
                "Collab localhost check raised: %s; denying",
                exc,
            )
            return {
                "error": "restricted",
                "reason": (
                    "Internal error checking caller identity"
                ),
            }
        if is_local:
            return None
        return {
            "error": "restricted",
            "reason": (
                "Participants cannot perform this action"
            ),
        }

    # ------------------------------------------------------------------
    # Whitelist helpers
    # ------------------------------------------------------------------

    def _resolve_filename(self, type_key: str) -> str | None:
        """Map a whitelisted type key to its filename, or None.

        Returns None for unknown types — caller produces the error
        dict with a consistent message.
        """
        return CONFIG_TYPES.get(type_key)

    # ------------------------------------------------------------------
    # Read operations (always allowed)
    # ------------------------------------------------------------------

    def get_config_content(self, type_key: str) -> dict[str, Any]:
        """Return the raw content of a whitelisted config file.

        Reads directly from the user config directory — NOT via
        :meth:`ConfigManager._read_user_file`, which falls back to
        the bundle when the user file is missing. For the Settings
        UI we want to present the user's actual on-disk state, so
        a missing user file returns an empty string (the bundle's
        defaults will be copied on the next startup anyway).

        Parameters
        ----------
        type_key:
            One of the whitelisted types from :data:`CONFIG_TYPES`.
            Unknown keys return an error dict.

        Returns
        -------
        dict
            ``{"type": type_key, "content": "..."}`` on success.
            ``{"error": "..."}`` on unknown type or read failure.
            Missing user files succeed with empty content — the
            Settings UI opens a fresh editor.
        """
        filename = self._resolve_filename(type_key)
        if filename is None:
            return {"error": f"Unknown config type: {type_key!r}"}
        path = self._config.config_dir / filename
        if not path.is_file():
            # Missing file — return empty content. The next save
            # will create the file; startup will repopulate from
            # the bundle.
            return {"type": type_key, "content": ""}
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to read config %s (%s): %s",
                type_key, filename, exc,
            )
            return {"error": f"Read failed: {exc}"}
        return {"type": type_key, "content": content}

    def get_config_info(self) -> dict[str, Any]:
        """Return a snapshot of current config state for the UI banner.

        Used by the Settings tab's info banner to show where the config
        files live. The two model names it used to carry are gone: they
        described the native engine's primary/smaller pair, and the
        model actually in force belongs to the engine, which reports it
        live through ``ClaudeCodeService.get_current_state()``. Reading
        it from a config file here would go stale the moment the user
        switched model mid-session.

        It also carries ``retired_files``: the names of retired config
        files this install still has on disk, empty on a fresh one. A
        field on the banner's existing RPC rather than an RPC of its
        own, because it is one more fact about the same config
        directory and a second call would be a second round trip for a
        list that is usually empty. The Settings tab renders it as the
        note explaining the cards that disappeared — see
        ``specs5/5-webapp/settings.md`` § Deleted cards.
        """
        return {
            "config_dir": str(self._config.config_dir),
            "retired_files": self._config.retired_files_present(),
        }

    # ------------------------------------------------------------------
    # Write operations (localhost-only)
    # ------------------------------------------------------------------

    def save_config_content(
        self,
        type_key: str,
        content: str,
    ) -> dict[str, Any]:
        """Write content to a whitelisted config file.

        JSON content is validated advisorily — a parse failure
        emits a warning in the result but the file is still
        written. Users may save a partially-edited file and
        continue editing.

        Carries a **per-field disposition** for JSON types: which top-level
        keys the edit changed, and whether each one reaches anything already
        running. Without it a save can only report "saved", and a save that
        moved ``effort`` — which nothing will pick up until a new session —
        shows the same unqualified success as one that moved a field the
        running session does pick up. See
        ``specs5/5-webapp/settings.md`` § The Applies Column Is Load-Bearing.

        Parameters
        ----------
        type_key:
            Whitelisted config type.
        content:
            New file content as a string.

        Returns
        -------
        dict
            ``{"status": "ok", "type": type_key, "disposition": {...}}`` on
            success; ``disposition`` is null when the content is not a JSON
            object (:func:`_save_disposition`).
            ``{"status": "ok", ..., "warning": "..."}``
            when JSON validation failed but the write succeeded.
            ``{"error": "..."}`` on unknown type or write failure.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        filename = self._resolve_filename(type_key)
        if filename is None:
            return {"error": f"Unknown config type: {type_key!r}"}
        path = self._config.config_dir / filename
        # Before the write, or there is nothing left to diff against. A read
        # failure is not a save failure: it costs the comparison, which the
        # disposition then reports as uncompared, and the user's edit still
        # lands.
        previous: str | None = None
        try:
            previous = path.read_text(encoding="utf-8")
        except OSError:
            logger.debug(
                "No readable previous %s; reporting every field as changed", filename
            )
        try:
            # Ensure the directory exists. The config manager
            # creates it at construction, but a later directory
            # deletion (rare but possible) shouldn't wedge the
            # save path.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to write config %s (%s): %s",
                type_key, filename, exc,
            )
            return {"error": f"Write failed: {exc}"}

        # Advisory JSON validation for .json-suffixed files.
        # The write has already succeeded — we're just telling
        # the user they've got a parse error.
        result: dict[str, Any] = {"status": "ok", "type": type_key}
        if filename.endswith(".json"):
            result["disposition"] = _save_disposition(type_key, content, previous)
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                result["warning"] = f"JSON parse error: {exc}"
        return result

    def reload_app_config(self) -> dict[str, Any]:
        """Re-read ``app.json``.

        Called by the Settings UI after a successful save of
        ``app.json``. Downstream consumers (the document converter, the
        doc index) read values through accessor methods, so
        hot-reloaded values take effect on the next access.

        There is no prompt to refresh afterwards, and nothing in
        ``app.json`` reaches the engine's session options — so unlike
        ``engine.json``, this reload really does apply everything the
        user just saved.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        try:
            self._config.reload_app_config()
        except Exception as exc:
            logger.warning(
                "App config reload failed: %s", exc
            )
            return {"error": f"Reload failed: {exc}"}
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Introspection helper (for diagnostics, not RPC-exposed)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # The `agy` permission gate — a machine setting, not an engine one
    # ------------------------------------------------------------------
    #
    # These live here rather than on the Antigravity adapter because what
    # they change is the *user's own* `agy` configuration, not a property
    # of a running session. A user is entitled to ask whether the gate is
    # installed, and to remove it, without an engine running at all — and
    # putting them on an engine would make that impossible on the engine
    # they are not currently using. See AG-14 and `aic_dc/agy/install.py`.

    def get_agy_gate(self) -> dict[str, Any]:
        """Whether the gate is in the user's ``agy`` configuration."""
        from aic_dc.agy import install

        report = install.status(self._config.config_dir)
        report["agy_present"] = shutil.which("agy") is not None
        # Which engine this gate is *for*, named by the server so the
        # browser can ask "does the engine I am switching to need this?"
        # without a hard-coded engine name of its own — AG-R-4.
        report["needed_by"] = capabilities.AGY
        return report

    def install_agy_gate(self) -> dict[str, Any]:
        """Add it. **Localhost only** — this writes outside the repository.

        Never called automatically. While it is installed, *every* tool call
        in *every* ``agy`` session on this machine runs our hook, including
        sessions that have nothing to do with this app. That is a thing to
        be asked about rather than a startup side effect, which is why there
        is a button and no default.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        from aic_dc.agy import install

        try:
            report = install.install(self._config.config_dir)
        except RuntimeError as exc:
            return {"error": "unwritable", "message": str(exc)}
        report["agy_present"] = shutil.which("agy") is not None
        return report

    def uninstall_agy_gate(self) -> dict[str, Any]:
        """Remove it. **Localhost only.**

        Also called on shutdown, so the cost is paid only while it buys
        something and a machine whose AIC⚡DC is closed has an untouched
        ``agy``.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        from aic_dc.agy import install

        return {
            "status": "ok",
            "removed": install.uninstall(),
            "gate": self.get_agy_gate(),
        }

    @staticmethod
    def is_reloadable(type_key: str) -> bool:
        """Return True when saving ``type_key`` should trigger a reload.

        Helper for callers (tests, a future UI-side dispatch layer)
        that want to know whether a save on this type warrants a
        reload RPC. Underscore-prefixed so it's not auto-exposed
        by jrpc-oo's ``add_class`` introspection — actually wait,
        jrpc-oo exposes everything non-underscored. This IS a
        public method. That's fine — it's a pure query with no
        state, safe to expose.
        """
        return type_key in _RELOADABLE_TYPES