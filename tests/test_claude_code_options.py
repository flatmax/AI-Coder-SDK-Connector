"""Tests for ac_dc.claude_code.options — conversion phase 1.

This is the SDK-drift tripwire. ``build_options`` is the only place
AC⚡DC's assumptions about ``ClaudeAgentOptions`` are written down, so
these tests are as much a contract test against the installed wheel as a
test of our own logic:

- Every option we always set is set, with the value the spec names
- Null config fields are **omitted**, not passed as ``None``
- ``enable_file_checkpointing`` and ``--replay-user-messages`` ship
  together — checkpointing alone fails at rewind time, not connect time
- neither ships alongside ``session_store``: the SDK refuses that pair at
  connect, so the whole engine fails to start
- ``allowed_tools`` and ``agents`` are never set, and ``system_prompt``
  carries no text of ours
- the two exceptions to null-means-omit are set anyway, because for both
  the SDK's default is the broken option: ``system_prompt`` (omitting it
  deletes the CLI's prompt) and ``max_buffer_size`` (one line over the
  SDK's 1 MiB ends the session)
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
    DEFAULT_MAX_BUFFER_SIZE,
    NEVER_SET,
    QUESTION_PREVIEW_ENV,
    QUESTION_PREVIEW_FORMAT,
    REPLAY_USER_MESSAGES_ARG,
    SETTING_SOURCES,
    build_option_kwargs,
    build_options,
    file_checkpointing_available,
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

    def test_question_previews_are_asked_for(self, kwargs):
        """Undocumented by default for an SDK entrypoint, and ours is ``sdk-py``.

        The per-option ``preview`` field is in the tool's schema either way,
        and a model sends one without being told. What is missing while this
        is unset is the *format*: the schema defers it to a tool description
        that, for an SDK entrypoint, does not mention previews at all — so
        markdown or an HTML fragment is the model's guess, and the dialog
        renders one of those as a mockup and the other as angle brackets.
        """
        assert kwargs["env"]["CLAUDE_CODE_QUESTION_PREVIEW_FORMAT"] == "markdown"

    def test_the_format_is_the_one_the_browser_renders(self):
        """``html`` is the other value the CLI takes, and it must not be it.

        The dialog renders the preview through the markdown renderer. Flip
        this and the user is shown the angle brackets of a mockup instead of
        the mockup — and the alternative fix, model-authored HTML in the
        dialog's shadow DOM, is the one this choice exists to avoid.
        """
        assert QUESTION_PREVIEW_FORMAT == "markdown"

    def test_the_env_is_copied_not_shared(self, kwargs):
        """The SDK holds it for the session; one session must not edit another's."""
        kwargs["env"]["CLAUDE_CODE_QUESTION_PREVIEW_FORMAT"] = "html"
        assert QUESTION_PREVIEW_ENV == {
            "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT": "markdown",
        }

    def test_the_buffer_ceiling_is_set_even_with_a_null_config(self, kwargs):
        """The second exception to null-means-omit, and the reason for it.

        The SDK's own default is 1 MiB per line of CLI stdout, and one line
        over it raises inside the reader and ends the session's message
        pump. Deferring to the dependency is the broken option here, which
        is what earns the exception.
        """
        assert kwargs["max_buffer_size"] == DEFAULT_MAX_BUFFER_SIZE

    def test_the_ceiling_is_higher_than_the_sdk_would_have_used(self):
        """Read from the wheel, so an SDK that raises its own stays covered."""
        from claude_agent_sdk._internal.transport import subprocess_cli

        assert DEFAULT_MAX_BUFFER_SIZE > subprocess_cli._DEFAULT_MAX_BUFFER_SIZE

    def test_the_ceiling_covers_an_inline_screenshot(self):
        """The payload that actually killed a session, on 2026-08-17.

        A base64 image arrives as one JSON line. 8 MB of raw bytes is a
        large screenshot and encodes to about 10.7 MB, which has to fit
        with the surrounding message rather than only just fit.
        """
        assert DEFAULT_MAX_BUFFER_SIZE > 8 * 1024 * 1024 * 4 / 3

    def test_a_configured_ceiling_wins(self, tmp_path):
        """The escape hatch for the case the chosen number does not cover."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(max_buffer_size=64 * 1024 * 1024),
        )
        assert kwargs["max_buffer_size"] == 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# File checkpointing — and the mirror it cannot share a session with
# ---------------------------------------------------------------------------


class TestFileCheckpointing:
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

    def test_a_mirrored_session_gets_neither(self, tmp_path):
        """The SDK refuses the pair alongside a store — at *connect*, so
        asking for both anyway costs every session, not only undo."""
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(), session_store=object()
        )
        assert "enable_file_checkpointing" not in kwargs
        assert "extra_args" not in kwargs

    def test_availability_is_the_absence_of_a_store(self):
        """One predicate, so the options and the RPC refusal cannot drift."""
        assert file_checkpointing_available(None) is True
        assert file_checkpointing_available(object()) is False

    def test_the_lost_undo_is_logged(self, tmp_path, caplog):
        """A capability that vanished silently would be read as a bug."""
        with caplog.at_level(logging.INFO):
            build_option_kwargs(
                repo_root=tmp_path, config=EngineConfig(), session_store=object()
            )
        assert "rewind_files" in caplog.text


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

    def test_stderr_is_omitted_when_nobody_will_read_it(self, kwargs):
        """Not a cosmetic omission: registering a callback *pipes* stderr.

        Unset, the CLI inherits the server's stderr and its diagnostics
        reach the terminal. Set to something that drops them, the terminal
        would lose what it has today — so absence has to mean absence.
        """
        assert "stderr" not in kwargs

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
        sink = object()
        kwargs = build_option_kwargs(
            repo_root=tmp_path,
            config=EngineConfig(),
            can_use_tool=gate,
            hooks=hooks,
            mcp_servers=servers,
            session_store=store,
            stderr=sink,
        )
        assert kwargs["can_use_tool"] is gate
        assert kwargs["hooks"] is hooks
        assert kwargs["mcp_servers"] is servers
        assert kwargs["session_store"] is store
        assert kwargs["stderr"] is sink


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

    def test_the_prohibited_set_is_the_two_that_change_behaviour(self):
        assert set(NEVER_SET) == {"allowed_tools", "agents"}

    def test_each_prohibition_records_a_reason(self):
        """So a future reader gets the argument, not a bare rule."""
        assert all(len(reason) > 40 for reason in NEVER_SET.values())

    def test_the_system_prompt_is_the_clis_own_and_is_set(self, tmp_path):
        """`None` is an *empty* prompt, not the CLI's.

        The SDK emits `--system-prompt ""` for `None`, which strips the
        dynamic sections carrying the working directory, the git status and
        the platform. Observed: an agent asked to edit `greet.py` in a repo
        at `/tmp/ac-dc-live` reached for `/home/flatmax/greet.py`, because
        nothing had told it where it was. The preset form emits no flag,
        leaving the CLI's own prompt in place.
        """
        kwargs = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig(permission_mode="default")
        )
        assert kwargs["system_prompt"] == {"type": "preset", "preset": "claude_code"}
        # No `append`: an appended string is prompt text of ours, which is
        # the thing CLAUDE.md is for.
        assert "append" not in kwargs["system_prompt"]

    def test_the_system_prompt_default_is_not_shared_between_sessions(self, tmp_path):
        config = EngineConfig(permission_mode="default")
        first = build_option_kwargs(repo_root=tmp_path, config=config)
        first["system_prompt"]["append"] = "leaked"
        second = build_option_kwargs(repo_root=tmp_path, config=config)
        assert "append" not in second["system_prompt"]


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

    def test_a_mirrored_session_passes_the_sdks_own_validation(self, tmp_path):
        """The regression guard for a whole engine that would not start.

        The SDK validates this pair inside `connect()` *and* `query()`, so
        the mirror plus checkpointing surfaced as "Could not start a Claude
        Code session" with a ValueError about local-disk divergence — with
        no session, no history and no way to ask for one.
        """
        validation = pytest.importorskip(
            "claude_agent_sdk._internal.session_store_validation"
        )
        options = build_options(
            repo_root=tmp_path, config=EngineConfig(), session_store=object()
        )
        validation.validate_session_store_options(options)

    def test_the_sdk_still_refuses_the_pair(self, tmp_path):
        """The tripwire on the constraint itself, not on our compliance:
        an SDK that starts allowing both fails here, and undo can come back."""
        validation = pytest.importorskip(
            "claude_agent_sdk._internal.session_store_validation"
        )
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            session_store=object(), enable_file_checkpointing=True
        )
        with pytest.raises(ValueError, match="checkpointing"):
            validation.validate_session_store_options(options)

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
