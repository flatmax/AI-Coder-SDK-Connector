"""Tests for ac_dc.claude_code.engine_config — conversion phase 1.

Scope:

- Null-means-omit: every field defaults to None and stays None
- Validation drops bad values to null rather than raising, because the
  user cannot fix ``engine.json`` if the app refuses to start
- ``effective_permission_mode`` is the one substituted default
- The valid value sets track the SDK's own ``Literal`` aliases
- ``load()`` survives a missing, unreadable, or non-object file

Offline by construction: nothing here connects, and nothing needs the
``claude`` CLI.
"""

from __future__ import annotations

import json
import logging

from ac_dc.claude_code.engine_config import (
    EFFORT_LEVELS,
    MIN_MAX_BUFFER_SIZE,
    PERMISSION_MODES,
    THINKING_DISPLAYS,
    EngineConfig,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_every_field_defaults_to_null(self):
        """A default config sets nothing, so the CLI decides everything."""
        config = EngineConfig()
        assert config.to_dict() == {
            "model": None,
            "permission_mode": None,
            "effort": None,
            "thinking_display": None,
            "max_budget_usd": None,
            "cli_path": None,
            "max_buffer_size": None,
        }

    def test_effective_permission_mode_substitutes_default(self):
        """The one field with a default: the UI has to name the posture."""
        assert EngineConfig().effective_permission_mode == "default"
        assert (
            EngineConfig(permission_mode="plan").effective_permission_mode == "plan"
        )

    def test_bundled_engine_json_is_all_null(self):
        """The shipped default must not pin any CLI-owned default."""
        from ac_dc.config import _bundled_config_dir

        raw = json.loads(
            (_bundled_config_dir() / "engine.json").read_text(encoding="utf-8")
        )
        assert set(raw) == set(EngineConfig().to_dict())
        assert all(value is None for value in raw.values())


# ---------------------------------------------------------------------------
# Valid value sets
# ---------------------------------------------------------------------------


class TestValueSets:
    def test_permission_modes_match_the_sdk(self):
        """Probed from the SDK, so a seventh mode is not rejected here."""
        import typing

        from claude_agent_sdk import types as sdk_types

        assert set(PERMISSION_MODES) == set(typing.get_args(sdk_types.PermissionMode))

    def test_effort_levels_match_the_sdk(self):
        import typing

        from claude_agent_sdk import types as sdk_types

        assert set(EFFORT_LEVELS) == set(typing.get_args(sdk_types.EffortLevel))

    def test_thinking_displays_match_the_sdk(self):
        """No alias to probe, so this is the drift check for the hardcode."""
        import typing

        from claude_agent_sdk.types import ThinkingConfigAdaptive

        # The field is `NotRequired[Literal[...]]`, so unwrap one layer.
        display = ThinkingConfigAdaptive.__annotations__["display"]
        args = typing.get_args(display)
        if len(args) == 1 and typing.get_origin(args[0]) is typing.Literal:
            args = typing.get_args(args[0])
        assert set(THINKING_DISPLAYS) == set(args)


# ---------------------------------------------------------------------------
# from_dict validation
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_reads_valid_values(self):
        config = EngineConfig.from_dict(
            {
                "model": "claude-opus-5",
                "permission_mode": "acceptEdits",
                "effort": "high",
                "thinking_display": "summarized",
                "max_budget_usd": 5,
                "cli_path": "/usr/local/bin/claude",
            }
        )
        assert config.model == "claude-opus-5"
        assert config.permission_mode == "acceptEdits"
        assert config.effort == "high"
        assert config.thinking_display == "summarized"
        assert config.max_budget_usd == 5.0
        assert config.cli_path == "/usr/local/bin/claude"

    def test_unknown_keys_are_ignored(self):
        """Forward compatibility with a newer bundled config."""
        config = EngineConfig.from_dict({"model": "x", "something_new": 1})
        assert config.model == "x"

    def test_strings_are_stripped(self):
        assert EngineConfig.from_dict({"model": "  sonnet  "}).model == "sonnet"

    def test_bad_choice_drops_to_null_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            config = EngineConfig.from_dict({"permission_mode": "yolo"})
        assert config.permission_mode is None
        assert "yolo" in caplog.text

    def test_empty_string_drops_to_null(self):
        assert EngineConfig.from_dict({"model": "   "}).model is None

    def test_non_string_drops_to_null(self):
        assert EngineConfig.from_dict({"model": 42}).model is None

    def test_budget_must_be_a_number(self):
        assert EngineConfig.from_dict({"max_budget_usd": "5"}).max_budget_usd is None

    def test_budget_rejects_bool(self):
        """`true` is an int subclass in Python, and is not a budget."""
        assert EngineConfig.from_dict({"max_budget_usd": True}).max_budget_usd is None

    def test_budget_must_be_positive(self):
        assert EngineConfig.from_dict({"max_budget_usd": 0}).max_budget_usd is None
        assert EngineConfig.from_dict({"max_budget_usd": -1}).max_budget_usd is None

    def test_reads_a_buffer_size(self):
        raw = {"max_buffer_size": 32 * 1024 * 1024}
        assert EngineConfig.from_dict(raw).max_buffer_size == 32 * 1024 * 1024

    def test_buffer_size_must_be_a_whole_number(self):
        """A float here is a units mistake, not one and a half bytes."""
        for value in ("8388608", 1.5, 8388608.0, None):
            assert EngineConfig.from_dict({"max_buffer_size": value}).max_buffer_size is None

    def test_buffer_size_rejects_bool(self):
        assert EngineConfig.from_dict({"max_buffer_size": True}).max_buffer_size is None

    def test_buffer_size_below_the_sdk_default_drops_with_a_warning(self, caplog):
        """A lower ceiling than doing nothing would give is not a setting.

        The field exists to raise the SDK's 1 MiB limit. Below that it
        could only make the session-ending overflow arrive sooner, so it is
        read as the misconfiguration it is.
        """
        with caplog.at_level(logging.WARNING):
            config = EngineConfig.from_dict({"max_buffer_size": 4096})
        assert config.max_buffer_size is None
        assert "max_buffer_size" in caplog.text

    def test_buffer_size_accepts_exactly_the_sdk_default(self):
        floor = MIN_MAX_BUFFER_SIZE
        assert EngineConfig.from_dict({"max_buffer_size": floor}).max_buffer_size == floor

    def test_the_floor_is_the_sdk_s_own_default(self):
        """Not a number of ours: the SDK's, so the comparison stays true.

        A wheel that raises its own default leaves the floor here too low
        rather than wrong, which is why this reads the private constant
        instead of asserting on 1 MiB.
        """
        from claude_agent_sdk._internal.transport import subprocess_cli

        assert MIN_MAX_BUFFER_SIZE == subprocess_cli._DEFAULT_MAX_BUFFER_SIZE

    def test_one_bad_field_does_not_lose_the_others(self):
        """The whole point of dropping rather than raising."""
        config = EngineConfig.from_dict(
            {"model": "sonnet", "effort": "turbo", "permission_mode": "plan"}
        )
        assert config.model == "sonnet"
        assert config.permission_mode == "plan"
        assert config.effort is None


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


class TestLoad:
    def test_none_config_dir_yields_defaults(self):
        assert EngineConfig.load(None) == EngineConfig()

    def test_missing_file_yields_defaults(self, tmp_path):
        assert EngineConfig.load(tmp_path) == EngineConfig()

    def test_reads_a_real_file(self, tmp_path):
        (tmp_path / "engine.json").write_text(json.dumps({"effort": "max"}))
        assert EngineConfig.load(tmp_path).effort == "max"

    def test_malformed_json_yields_defaults_with_a_warning(self, tmp_path, caplog):
        (tmp_path / "engine.json").write_text("{not json")
        with caplog.at_level(logging.WARNING):
            config = EngineConfig.load(tmp_path)
        assert config == EngineConfig()
        assert "engine.json" in caplog.text

    def test_non_object_json_yields_defaults(self, tmp_path, caplog):
        (tmp_path / "engine.json").write_text("[1, 2, 3]")
        with caplog.at_level(logging.WARNING):
            config = EngineConfig.load(tmp_path)
        assert config == EngineConfig()
        assert "not a JSON object" in caplog.text

    def test_round_trips_through_to_dict(self, tmp_path):
        original = EngineConfig(
            model="sonnet", permission_mode="plan", max_budget_usd=2.5
        )
        (tmp_path / "engine.json").write_text(json.dumps(original.to_dict()))
        assert EngineConfig.load(tmp_path) == original
