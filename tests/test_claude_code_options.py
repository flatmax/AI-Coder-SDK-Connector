"""Tests for ac_dc.claude_code.options — conversion phase 1.

This is the SDK-drift tripwire. ``build_options`` is the only place
AC⚡DC's assumptions about ``ClaudeAgentOptions`` are written down, so
these tests are as much a contract test against the installed wheel as a
test of our own logic:

- Every option we always set is set, with the value the spec names
- Null config fields are **omitted**, not passed as ``None``
- ``enable_file_checkpointing`` and ``--replay-user-messages`` ship
  together — checkpointing alone fails at rewind time, not connect time
- ``allowed_tools``, ``agents``, and ``system_prompt`` are never set
- ``fork_session`` without ``resume`` is refused rather than silently kept
- Every key we produce exists on the installed dataclass

Offline: no client is constructed and no CLI is spawned.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from ac_dc.claude_code.engine_config import EngineConfig
from ac_dc.claude_code.health import EngineStartupError
from ac_dc.claude_code.options import (
    NEVER_SET,
    REPLAY_USER_MESSAGES_ARG,
    SETTING_SOURCES,
    build_option_kwargs,
    build_options,
)


@pytest.fixture
def kwargs(tmp_path):
    """Kwargs for a default config — the null-everywhere baseline."""
    return build_option_kwargs(repo_root=tmp_path, config=EngineConfig())


# ---------------------------------------------------------------------------
# Always set
# ---------------------------------------------------------------------------


class TestAlwaysSet:
    def test_cwd_is_the_repo_root(self, tmp_path, kwargs):
        """So every tool path the agent produces is repo-relative."""
        assert kwargs["cwd"] == str(tmp_path)

    def test_permission_mode_is_always_present(self, kwargs):
        """A named posture, because the UI has to display one."""
        assert kwargs["permission_mode"] == "default"

    def test_partial_messages_are_on(self, kwargs):
        """Without them the UI updates per block, which reads as a stall."""
        assert kwargs["include_partial_messages"] is True

    def test_hook_events_are_on(self, kwargs):
        assert kwargs["include_hook_events"] is True

    def test_setting_sources_are_user_project_local(self, kwargs):
        """CC-11: a session here behaves like a CLI session in the repo."""
        assert kwargs["setting_sources"] == ["user", "project", "local"]

    def test_setting_sources_are_copied_not_shared(self, kwargs):
        """A caller mutating the result must not corrupt the constant."""
        kwargs["setting_sources"].append("nonsense")
        assert SETTING_SOURCES == ["user", "project", "local"]

    def test_checkpointing_and_replay_ship_together(self, kwargs):
        """rewind_files() needs both; checkpointing alone fails at call time."""
        assert kwargs["enable_file_checkpointing"] is True
        assert kwargs["extra_args"] == REPLAY_USER_MESSAGES_ARG

    def test_replay_flag_is_a_bare_flag(self):
        """A None value emits `--replay-user-messages` with no argument."""
        assert REPLAY_USER_MESSAGES_ARG == {"replay-user-messages": None}

    def test_extra_args_is_copied_not_shared(self, kwargs):
        kwargs["extra_args"]["something"] = "else"
        assert REPLAY_USER_MESSAGES_ARG == {"replay-user-messages": None}


# ---------------------------------------------------------------------------
# Null means omit
# ---------------------------------------------------------------------------


class TestNullMeansOmit:
    @pytest.mark.parametrize(
        "key", ["model", "effort", "max_budget_usd", "thinking", "cli_path"]
    )
    def test_null_config_omits_the_key(self, kwargs, key):
        """Passing None would pin today's CLI default into our code."""
        assert key not in kwargs

    def test_collaborators_are_omitted_when_absent(self, kwargs):
        """The spike runs before permissions, hooks, MCP, and the mirror."""
        for key in ("can_use_tool", "hooks", "mcp_servers", "session_store"):
            assert key not in kwargs

    def test_resume_and_fork_are_omitted_for_a_new_session(self, kwargs):
        assert "resume" not in kwargs
        assert "fork_session" not in kwargs


# ---------------------------------------------------------------------------
# Populated config
# ---------------------------------------------------------------------------


class TestPopulatedConfig:
    def test_every_field_lands(self, tmp_path):
        config = EngineConfig(
            model="claude-opus-5",
            permission_mode="acceptEdits",
            effort="high",
            thinking_display="summarized",
            max_budget_usd=3.5,
            cli_path="/opt/claude",
        )
        kwargs = build_option_kwargs(repo_root=tmp_path, config=config)
        assert kwargs["model"] == "claude-opus-5"
        assert kwargs["permission_mode"] == "acceptEdits"
        assert kwargs["effort"] == "high"
        assert kwargs["max_budget_usd"] == 3.5
        assert kwargs["cli_path"] == "/opt/claude"

    def test_thinking_is_the_typed_dict_shape(self, tmp_path):
        """Not ThinkingConfig(display=…) — the SDK models this as a union."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(thinking_display="omitted")
        )
        assert kwargs["thinking"] == {"type": "adaptive", "display": "omitted"}

    def test_a_resolved_cli_path_lands_when_config_pins_nothing(self, tmp_path):
        """The session passes the binary it version-checked, so that one runs."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(), cli_path="/opt/resolved/claude"
        )
        assert kwargs["cli_path"] == "/opt/resolved/claude"

    def test_a_resolved_cli_path_wins_over_the_config(self, tmp_path):
        """resolve_cli() already honoured config.cli_path, so it is the same
        path — unless the config named something unusable, in which case
        resolution failed and we never got here."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(cli_path="/from/config"),
            cli_path="/from/resolution",
        )
        assert kwargs["cli_path"] == "/from/resolution"

    def test_session_store_forces_eager_flush(self, tmp_path):
        """Batched flushing would make the mirror lag the UI by a whole turn."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(), session_store=object()
        )
        assert kwargs["session_store_flush"] == "eager"

    def test_empty_mcp_servers_is_omitted(self, tmp_path):
        """An empty dict is nothing to register, so do not register it."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(), mcp_servers={}
        )
        assert "mcp_servers" not in kwargs

    def test_collaborators_land_when_supplied(self, tmp_path):
        gate, hooks, servers, store = object(), object(), {"ac-dc": object()}, object()
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(),
            can_use_tool=gate,
            hooks=hooks,
            mcp_servers=servers,
            session_store=store,
        )
        assert kwargs["can_use_tool"] is gate
        assert kwargs["hooks"] is hooks
        assert kwargs["mcp_servers"] is servers
        assert kwargs["session_store"] is store


# ---------------------------------------------------------------------------
# Resume and fork
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_lands(self, tmp_path):
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(), resume="abc-123"
        )
        assert kwargs["resume"] == "abc-123"
        assert "fork_session" not in kwargs

    def test_fork_needs_resume(self, tmp_path):
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(),
            resume="abc-123",
            fork_session=True,
        )
        assert kwargs["fork_session"] is True

    def test_fork_without_resume_warns_and_is_dropped(self, tmp_path, caplog):
        """Forking branches an existing session and needs one to branch from."""
        with caplog.at_level(logging.WARNING):
            kwargs = build_option_kwargs(
                repo_root=tmp_path, config=EngineConfig(), fork_session=True
            )
        assert "fork_session" not in kwargs
        assert "fork" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Never set
# ---------------------------------------------------------------------------


class TestNeverSet:
    @pytest.mark.parametrize("key", sorted(NEVER_SET))
    def test_option_is_never_set(self, tmp_path, key):
        """Each of the three has a reason recorded next to the prohibition."""
        config = EngineConfig(
            model="x",
            permission_mode="plan",
            effort="max",
            thinking_display="summarized",
            max_budget_usd=1.0,
            cli_path="/opt/claude",
        )
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=config,
            can_use_tool=object(),
            hooks=object(),
            mcp_servers={"ac-dc": object()},
            session_store=object(),
            resume="abc",
            fork_session=True,
        )
        assert key not in kwargs

    def test_the_prohibited_set_is_the_three_from_the_spec(self):
        assert set(NEVER_SET) == {"allowed_tools", "agents", "system_prompt"}

    def test_each_prohibition_records_a_reason(self):
        """So a future reader gets the argument, not a bare rule."""
        assert all(len(reason) > 40 for reason in NEVER_SET.values())


# ---------------------------------------------------------------------------
# SDK contract
# ---------------------------------------------------------------------------


class TestSdkContract:
    def test_every_key_exists_on_the_installed_dataclass(self, tmp_path):
        """The drift tripwire: an SDK that drops a field fails here."""
        from claude_agent_sdk import ClaudeAgentOptions

        known = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(
                model="x",
                effort="high",
                thinking_display="summarized",
                max_budget_usd=1.0,
                cli_path="/opt/claude",
            ),
            can_use_tool=object(),
            hooks={},
            mcp_servers={"ac-dc": object()},
            session_store=object(),
            resume="abc",
            fork_session=True,
        )
        assert set(kwargs) <= known

    def test_build_options_constructs_the_dataclass(self, tmp_path):
        options = build_options(repo_root=tmp_path, config=EngineConfig())
        assert options.cwd == str(tmp_path)
        assert options.include_partial_messages is True
        assert options.setting_sources == ["user", "project", "local"]

    def test_build_options_names_missing_fields(self, tmp_path, monkeypatch):
        """A removed SDK field must produce a diagnosis, not a TypeError."""
        import ac_dc.claude_code.options as options_module

        def _extra(**_: object) -> dict[str, object]:
            return {"cwd": str(tmp_path), "invented_field": True}

        monkeypatch.setattr(options_module, "build_option_kwargs", _extra)
        with pytest.raises(EngineStartupError, match="invented_field"):
            build_options(repo_root=tmp_path, config=EngineConfig())
