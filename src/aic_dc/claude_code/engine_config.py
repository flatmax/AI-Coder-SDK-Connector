"""Engine configuration — the ``engine.json`` reader.

Holds the session options for the one ``ClaudeSDKClient`` this process
owns — and one field that belongs to the commit-message one-shot instead
(``commit_model``), because that call is the only other place AIC-DC names
a model. Deliberately tiny: the engine's own configuration is not ours to
manage (specs5/1-foundation/configuration.md), so this file carries the
handful of knobs AIC-DC needs to hand the SDK and nothing else.

**Null means "omit the option"**, not "substitute our own default" — a
null model lets the CLI pick, a null effort lets the CLI pick. The
distinction is load-bearing: passing ``None`` explicitly to
``ClaudeAgentOptions`` is not always the same as leaving the field at its
dataclass default, and the CLI's defaults move with the CLI rather than
with us. See :mod:`aic_dc.claude_code.options` for the call site.

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

# The floor for ``max_buffer_size``, and the reason it needs one: the field
# raises a ceiling that ends sessions, so a *small* value is not a mild
# misconfiguration. One line over the limit raises ``CLIJSONDecodeError``
# inside the SDK's reader and the message pump for that session is over —
# see :data:`aic_dc.claude_code.options.DEFAULT_MAX_BUFFER_SIZE`. This is the
# SDK's own default (``subprocess_cli._DEFAULT_MAX_BUFFER_SIZE``), so a
# value below it could only make the failure arrive sooner than doing
# nothing would have. Dropped with a warning like every other bad value.
MIN_MAX_BUFFER_SIZE = 1024 * 1024

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
    #: The model for the commit-message one-shot, when it should not be the
    #: session's.
    #:
    #: Generating a commit message is the one auxiliary model call left
    #: (:mod:`aic_dc.claude_code.commit`) — one turn, no tools, a diff in and
    #: a paragraph out — and it does not need the model the conversation
    #: needs. Naming a small one here is worth roughly 7× on the cost of a
    #: commit and a few seconds on the wait.
    #:
    #: Null falls back to :attr:`model`, so an unset value keeps the
    #: previous behaviour rather than picking a model on the user's behalf.
    #: There is no portable way to *default* this: a full id is
    #: provider-specific (a first-party id is a 400 from Bedrock), and the
    #: CLI's tier aliases resolve against per-tier defaults that a
    #: third-party provider only has if something wrote them — asking for
    #: ``haiku`` on this machine's Bedrock config resolved to Sonnet 4.5.
    #: So the id is the user's to write, in the dialect their provider
    #: speaks.
    commit_model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None
    thinking_display: str | None = None
    max_budget_usd: float | None = None
    cli_path: str | None = None
    #: Bytes the SDK may buffer for one line of CLI stdout.
    #:
    #: The one field where null does **not** mean "let the CLI decide": the
    #: SDK's default is known to end sessions, so :mod:`options` substitutes
    #: a ceiling of its own. Present here for the pathological case the
    #: chosen number does not cover.
    max_buffer_size: int | None = None

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
            commit_model=_clean_str(raw, "commit_model"),
            permission_mode=_clean_choice(raw, "permission_mode", PERMISSION_MODES),
            effort=_clean_choice(raw, "effort", EFFORT_LEVELS),
            thinking_display=_clean_choice(
                raw, "thinking_display", THINKING_DISPLAYS
            ),
            max_budget_usd=_clean_positive_float(raw, "max_budget_usd"),
            cli_path=_clean_str(raw, "cli_path"),
            max_buffer_size=_clean_buffer_size(raw, "max_buffer_size"),
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
            "commit_model": self.commit_model,
            "permission_mode": self.permission_mode,
            "effort": self.effort,
            "thinking_display": self.thinking_display,
            "max_budget_usd": self.max_budget_usd,
            "cli_path": self.cli_path,
            "max_buffer_size": self.max_buffer_size,
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


def _clean_buffer_size(raw: dict[str, Any], key: str) -> int | None:
    """A byte count, floored at :data:`MIN_MAX_BUFFER_SIZE`.

    Stricter than the other cleaners in one way and looser in none: a
    *float* is rejected rather than truncated, because a byte count written
    with a decimal point is a units mistake (``1.5`` meaning megabytes) and
    reading it as one and a half bytes would be the worst of the readings.
    """
    value = raw.get(key)
    if value is None:
        return None
    # bool is an int subclass, and `true` here is not a size.
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning(
            "engine.json: %s must be a whole number of bytes (got %r); ignoring",
            key,
            value,
        )
        return None
    if value < MIN_MAX_BUFFER_SIZE:
        logger.warning(
            "engine.json: %s=%r is below the SDK's own %d-byte default, so it "
            "would lower the ceiling rather than raise it and end sessions "
            "sooner; ignoring",
            key,
            value,
            MIN_MAX_BUFFER_SIZE,
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
