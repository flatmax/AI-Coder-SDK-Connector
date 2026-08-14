"""Engine configuration — the ``engine.json`` reader.

Holds the session options for the one ``ClaudeSDKClient`` this process
owns. Deliberately tiny: the engine's own configuration is not ours to
manage (specs5/1-foundation/configuration.md), so this file carries the
six knobs AC-DC needs to hand the SDK and nothing else.

**Null means "omit the option"**, not "substitute our own default" — a
null model lets the CLI pick, a null effort lets the CLI pick. The
distinction is load-bearing: passing ``None`` explicitly to
``ClaudeAgentOptions`` is not always the same as leaving the field at its
dataclass default, and the CLI's defaults move with the CLI rather than
with us. See :mod:`ac_dc.claude_code.options` for the call site.

Governing spec: ``specs5/1-foundation/configuration.md`` § Engine Config.
Schema: ``specs-reference/1-foundation/configuration.md`` § ``engine.json``.
"""

from __future__ import annotations

import json
import logging
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Valid value sets
# ---------------------------------------------------------------------------
#
# Read out of the SDK's own Literal aliases rather than hardcoded, so an
# SDK that gains a seventh permission mode does not silently reject it
# here. The implementation guide's rule — "attribute-probe rather than
# import-and-hope" — applies: a renamed alias degrades to the fallback
# tuple instead of failing at import.


def _literal_values(alias_name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Extract a ``Literal``'s members from a public SDK type alias."""
    try:
        from claude_agent_sdk import types as sdk_types

        alias = getattr(sdk_types, alias_name, None)
        values = tuple(str(v) for v in typing.get_args(alias))
        if values:
            return values
        logger.debug(
            "SDK alias %s yielded no Literal members; using built-in fallback",
            alias_name,
        )
    except Exception as exc:  # pragma: no cover - import-shape guard
        logger.debug("Could not read SDK alias %s (%s); using fallback", alias_name, exc)
    return fallback


PERMISSION_MODES = _literal_values(
    "PermissionMode",
    ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"),
)

EFFORT_LEVELS = _literal_values(
    "EffortLevel",
    ("low", "medium", "high", "xhigh", "max"),
)

# The SDK models thinking as a TypedDict union rather than a class with a
# ``display`` argument, so there is no alias to probe. Both members come
# from ``ThinkingConfigAdaptive.__annotations__["display"]`` in 0.2.137.
THINKING_DISPLAYS = ("summarized", "omitted")

ENGINE_CONFIG_FILENAME = "engine.json"


@dataclass(frozen=True)
class EngineConfig:
    """The contents of ``engine.json``, validated.

    Every field is nullable and null means "let the CLI decide". Invalid
    values are dropped to null with a warning rather than raising: a typo
    in one field should not stop the engine from starting, because the
    user cannot fix the file if the app will not run.
    """

    model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None
    thinking_display: str | None = None
    max_budget_usd: float | None = None
    cli_path: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EngineConfig:
        """Build from a parsed ``engine.json`` mapping.

        Unknown keys are ignored (forward compatibility with a newer
        bundled config); known keys with out-of-range values are logged
        and dropped.
        """
        return cls(
            model=_clean_str(raw, "model"),
            permission_mode=_clean_choice(raw, "permission_mode", PERMISSION_MODES),
            effort=_clean_choice(raw, "effort", EFFORT_LEVELS),
            thinking_display=_clean_choice(
                raw, "thinking_display", THINKING_DISPLAYS
            ),
            max_budget_usd=_clean_positive_float(raw, "max_budget_usd"),
            cli_path=_clean_str(raw, "cli_path"),
        )

    @classmethod
    def load(cls, config_dir: Path | str | None) -> EngineConfig:
        """Read ``engine.json`` from ``config_dir``.

        A missing or unreadable file yields an all-null config, which is
        a working configuration — every option falls through to the CLI.
        """
        if config_dir is None:
            return cls()
        path = Path(config_dir) / ENGINE_CONFIG_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.debug("No %s at %s; using CLI defaults", ENGINE_CONFIG_FILENAME, path)
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read %s (%s); using CLI defaults for every engine option",
                path,
                exc,
            )
            return cls()
        if not isinstance(raw, dict):
            logger.warning(
                "%s is not a JSON object (got %s); using CLI defaults",
                path,
                type(raw).__name__,
            )
            return cls()
        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Round-trippable mapping, nulls included, for the Settings tab."""
        return {
            "model": self.model,
            "permission_mode": self.permission_mode,
            "effort": self.effort,
            "thinking_display": self.thinking_display,
            "max_budget_usd": self.max_budget_usd,
            "cli_path": self.cli_path,
        }

    @property
    def effective_permission_mode(self) -> str:
        """The mode a new session starts in; ``"default"`` when unset.

        The one field with a substituted default rather than an omission,
        because the permission posture is user-visible state that the UI
        has to name, and "whatever the CLI picked" is not nameable.
        """
        return self.permission_mode or "default"


# ---------------------------------------------------------------------------
# Field cleaning
# ---------------------------------------------------------------------------


def _clean_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        logger.warning(
            "engine.json: %s must be a non-empty string (got %r); ignoring", key, value
        )
        return None
    return value.strip()


def _clean_choice(
    raw: dict[str, Any], key: str, allowed: tuple[str, ...]
) -> str | None:
    value = _clean_str(raw, key)
    if value is None:
        return None
    if value not in allowed:
        logger.warning(
            "engine.json: %s=%r is not one of %s; ignoring",
            key,
            value,
            ", ".join(allowed),
        )
        return None
    return value


def _clean_positive_float(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    # bool is an int subclass; a JSON `true` here is a mistake, not a budget.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            "engine.json: %s must be a number (got %r); ignoring", key, value
        )
        return None
    if value <= 0:
        logger.warning(
            "engine.json: %s must be greater than zero (got %r); ignoring", key, value
        )
        return None
    return float(value)
