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
from aic_dc.cli import main


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