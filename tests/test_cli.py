"""Tests for aic_dc.cli.

Layer 0 scope — verify the CLI parses arguments, prints the banner, and
exits cleanly. Full startup orchestration is tested in Layer 6.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from aic_dc import __version__
from aic_dc.cli import (
    CHECK_ENGINE_OK,
    CHECK_ENGINE_UNRESOLVED,
    CHECK_ENGINE_UNRUNNABLE,
    main,
)


def test_main_no_args_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Running with no arguments returns 0 and prints the banner to stderr.

    Asserts only the stable identity strings (product name and expansion)
    — not the banner wording itself, which changes as development
    progresses.

    ``main()`` now dispatches to ``aic_dc.main.run`` which launches the
    webapp and RPC servers, opens a browser, and then waits on
    ``asyncio.Event().wait()`` to keep the process alive. Unit tests
    want the argparse + banner behaviour only, so we mock the launcher.
    ``run`` is patched where it's imported inside ``cli.main``
    (``aic_dc.main.run``), not at the import site in ``aic_dc.cli``
    (because ``cli.py`` imports it lazily inside the function).
    """
    with patch("aic_dc.main.run") as mock_run:
        # Replace the coroutine with a no-op async function so
        # asyncio.run can await it without raising.
        async def _noop(**_kwargs):
            return None
        mock_run.side_effect = _noop
        exit_code = main([])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "AIC-DC" in captured.err
    assert "AI-Coder-SDK-Connector" in captured.err


def test_main_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    """--version exits 0 and prints the version string from __init__."""
    # argparse calls sys.exit on --version; SystemExit(code=0) is the expected
    # signal. We catch it so pytest doesn't treat it as a test error.
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_main_help_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """--help exits 0 and prints usage to stdout."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "aic-dc" in captured.out.lower()
    assert "usage" in captured.out.lower()


def test_main_accepts_all_documented_flags() -> None:
    """All flags listed in specs4/6-deployment/startup.md parse without error.

    This test is the contract that the flag set is stable. ``main()``
    now launches real servers via ``aic_dc.main.run``, so we mock that
    launcher — this test's scope is argparse plumbing, not the full
    startup orchestration (covered by Layer 6 tests). Each flag set
    should parse cleanly and reach the launcher; we don't verify what
    the launcher does with the values.
    """
    flag_sets = [
        ["--server-port", "19000"],
        ["--webapp-port", "19001"],
        ["--no-browser"],
        ["--repo-path", "/tmp"],
        ["--dev"],
        ["--preview"],
        ["--verbose"],
        ["--collab"],
        # Composite — several at once.
        ["--server-port", "19000", "--no-browser", "--verbose"],
    ]

    async def _noop(**_kwargs):
        return None

    for flags in flag_sets:
        with patch("aic_dc.main.run") as mock_run:
            mock_run.side_effect = _noop
            assert main(flags) == 0, (
                f"flags {flags!r} did not parse cleanly"
            )
            # Sanity check — the launcher was invoked, which
            # confirms main() actually reached past argparse.
            assert mock_run.called, (
                f"flags {flags!r} never reached the launcher"
            )


def test_main_rejects_unknown_flag() -> None:
    """Unknown flags cause argparse to exit with code 2."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--does-not-exist"])
    assert exc_info.value.code == 2


class TestCheckEngine:
    """``--check-engine`` — the runtime half of the packaging tripwire.

    specs5/6-deployment/build.md § The Engine Is Not Bundleable requires the
    build to assert the bundled CLI is in the archive. That assertion cannot
    tell whether the unpacked copy is *runnable*, which is the thing a user
    on a fresh machine depends on. These tests cover the three answers the
    flag can give, and that giving any of them never starts a server.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config_dir(self, tmp_path, monkeypatch):
        """Keep the check's first-run config population out of the real dir.

        ``--check-engine`` reads ``engine.json`` for ``cli_path``, so it
        constructs a ``ConfigManager``, which installs bundled defaults on
        first run. Pointed at the developer's own config directory that is a
        test with side effects; ``AIC_DC_CONFIG_HOME`` exists for this.
        """
        monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(tmp_path / "config"))

    def test_resolves_the_real_engine(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Against the installed SDK, the check passes and names what it found.

        Not mocked on purpose: this is the assertion that the engine in this
        environment is present *and* spawnable, which is exactly what CI runs
        against a built artefact. If the bundled binary ever ships without
        its executable bit, this is the test that says so.
        """
        assert main(["--check-engine"]) == CHECK_ENGINE_OK
        out = capsys.readouterr().out
        # Stable keys — CI greps these, so they are part of the contract.
        for key in ("aic-dc:", "sdk:", "engine:", "source:", "version:", "credentials:"):
            assert key in out, f"{key!r} missing from the report"
        assert "NOT RESOLVED" not in out

    def test_unresolvable_engine_exits_one_with_install_instructions(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No engine is exit 1, and the message says what to do about it.

        The point of the flag is that this arrives at startup rather than at
        the first prompt, so the message has to carry the fix.
        """
        from aic_dc.claude_code.health import EngineStartupError

        with patch(
            "aic_dc.claude_code.health.resolve_cli",
            side_effect=EngineStartupError(
                "Claude Code CLI not found. Install it with "
                "`npm install -g @anthropic-ai/claude-code`, or set cli_path "
                "in engine.json."
            ),
        ):
            assert main(["--check-engine"]) == CHECK_ENGINE_UNRESOLVED
        captured = capsys.readouterr()
        assert "NOT RESOLVED" in captured.out
        assert "npm install -g @anthropic-ai/claude-code" in captured.err

    def test_present_but_unrunnable_engine_exits_two(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A resolved engine that will not report a version is exit 2.

        The distinct code matters: "install an engine" and "the engine you
        have will not run" have different fixes, and this second one is the
        failure mode collecting the CLI as a data file can produce, since
        data files carry no permission bits.
        """
        from aic_dc.claude_code.health import CliResolution

        with patch(
            "aic_dc.claude_code.health.resolve_cli",
            return_value=CliResolution(
                path="/nonexistent/claude",
                source="bundled with the claude-agent-sdk wheel",
                version="unknown",
            ),
        ):
            assert main(["--check-engine"]) == CHECK_ENGINE_UNRUNNABLE
        captured = capsys.readouterr()
        assert "/nonexistent/claude" in captured.out
        assert "could not be run" in captured.err

    def test_warns_when_a_shadowed_claude_is_on_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A `claude` on PATH that is not the resolved one is called out.

        The SDK prefers the wheel's copy over PATH, which is not the
        intuitive order and has had the spec backwards twice. When both
        exist, silence reads as "PATH won".
        """
        from aic_dc.claude_code.health import CliResolution

        with (
            patch(
                "aic_dc.claude_code.health.resolve_cli",
                return_value=CliResolution(
                    path="/wheel/claude_agent_sdk/_bundled/claude",
                    source="bundled with the claude-agent-sdk wheel",
                    version="2.1.229",
                ),
            ),
            patch("shutil.which", return_value="/usr/local/bin/claude"),
        ):
            assert main(["--check-engine"]) == CHECK_ENGINE_OK
        out = capsys.readouterr().out
        assert "/usr/local/bin/claude" in out
        assert "NOT what will run" in out

    def test_never_starts_a_server(self) -> None:
        """The diagnostic must not reach the launcher.

        A check that boots the app cannot be the first thing someone runs on
        a new binary, and cannot run in CI where no port is free by
        assumption.
        """
        with patch("aic_dc.main.run") as mock_run:
            main(["--check-engine"])
        assert not mock_run.called


def test_module_entrypoint_runs() -> None:
    """`python -m aic_dc` works as an alternative to the aic-dc script.

    Uses a subprocess so we exercise the real __main__ module dispatch.
    """
    result = subprocess.run(
        [sys.executable, "-m", "aic_dc", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_module_entrypoint_propagates_the_exit_code(monkeypatch, tmp_path) -> None:
    """``__main__`` exits with whatever ``main()`` returned.

    ``src/aic_dc/__main__.py`` is the script PyInstaller builds the release
    binary from, so a bare ``main()`` there makes every exit code the CLI
    computes invisible to a shell — the process ends 0 regardless. That was
    harmless while every path returned 0, and became load-bearing with
    ``--check-engine``: a CI step asserting on its exit status would have
    passed whether or not the artefact had a working engine.

    Uses ``runpy`` so the real ``__main__`` guard runs, which is the line
    under test. A subprocess would need a broken engine on disk to provoke
    a non-zero code; mocking the resolver is the same assertion, cheaper.
    """
    import runpy

    from aic_dc.claude_code.health import EngineStartupError

    monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(sys, "argv", ["aic-dc", "--check-engine"])
    with patch(
        "aic_dc.claude_code.health.resolve_cli",
        side_effect=EngineStartupError("no engine"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("aic_dc", run_name="__main__")
    assert exc_info.value.code == CHECK_ENGINE_UNRESOLVED