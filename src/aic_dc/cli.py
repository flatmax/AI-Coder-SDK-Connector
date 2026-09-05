"""Command-line entry point for ``aic-dc``.

Parses arguments, prints the startup banner, and hands off to
:func:`aic_dc.main.run` for startup orchestration (port selection,
WebSocket server, webapp serving, deferred indexing) per
specs5/6-deployment/startup.md.

``--check-engine`` is the one flag that does not launch anything: it
resolves the ``claude`` binary, reports it, and exits. See
:func:`_check_engine`.

Exposed via the ``aic-dc`` console script declared in pyproject.toml.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aic_dc import __version__
from aic_dc.logging_setup import configure as configure_logging

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser.

    The flag set matches specs5/6-deployment/startup.md#cli-arguments so that
    flags are stable from day one.
    """
    parser = argparse.ArgumentParser(
        prog="aic-dc",
        description="AIC-DC — AI-Coder-SDK-Connector. A browser UI over AI coding-agent SDKs.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"aic-dc {__version__}",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=18080,
        help="RPC WebSocket server port (default: 18080)",
    )
    parser.add_argument(
        "--webapp-port",
        type=int,
        default=18999,
        help="Webapp static/dev server port (default: 18999)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the browser on startup",
    )
    parser.add_argument(
        "--repo-path",
        default=".",
        help="Path to the git repository to operate on (default: current directory)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Run the Vite dev server instead of the bundled webapp",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Build and run the Vite preview server instead of the bundled webapp",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    parser.add_argument(
        "--collab",
        action="store_true",
        help="Enable collaboration mode (bind all interfaces, admission-gated)",
    )
    parser.add_argument(
        "--check-engine",
        action="store_true",
        help=(
            "Report which claude binary would be used and exit without "
            "starting a server. Exit 0 when the engine is resolvable and "
            "answers --version, 1 when no engine can be resolved, 2 when one "
            "was found but could not be run"
        ),
    )
    parser.add_argument(
        "--agy-hook",
        metavar="CONFIG_DIR",
        default=None,
        # Suppressed: this is not a thing a user runs. `agy` runs it, once
        # per tool call, from the command `aic_dc.agy.install` wrote into
        # the user's hooks file — and listing it here would invite someone
        # to invoke a permission gate by hand.
        help=argparse.SUPPRESS,
    )
    return parser


#: ``--check-engine`` exit codes. The two failures are distinct because they
#: need different fixes: nothing to run, versus something that will not run.
CHECK_ENGINE_OK = 0
CHECK_ENGINE_UNRESOLVED = 1
CHECK_ENGINE_UNRUNNABLE = 2


def _check_engine() -> int:
    """Resolve the engine, report what was found, and exit without launching.

    The runtime half of the packaging tripwire
    (specs5/6-deployment/build.md § The Engine Is Not Bundleable). The
    build-time assertion can only prove the bundled CLI is *in* the archive;
    it cannot prove the process that unpacks it can find and spawn it. That
    gap is not hypothetical — the collected engine is a data file, and data
    files carry no permission bits, so "present but not executable" is the
    exact shape this has to rule out.

    Reports rather than predicts. Every line is something already resolved:
    the path the SDK would spawn, where it came from, what it answered, and
    which credential source is visible. Nothing here starts a session, so
    it needs no credentials to be useful and does not fail without them —
    a fresh container has no login, and a check that required one could
    never run there.

    Exiting non-zero does not contradict specs5/6-deployment/startup.md
    § Engine Health in the Overlay, which requires the *launch* path to
    degrade to a working editor with a health banner. This is a diagnostic,
    and a diagnostic that cannot fail reports nothing.

    Deliberately does not take ``--repo-path``: ``cli_path`` lives in the
    user config directory, not a per-repo one, and a diagnostic should not
    create ``.aic-dc/`` or edit ``.gitignore`` in whatever directory it was
    run from.

    Returns
    -------
    int
        :data:`CHECK_ENGINE_OK`, :data:`CHECK_ENGINE_UNRESOLVED` or
        :data:`CHECK_ENGINE_UNRUNNABLE`.
    """
    import shutil

    from aic_dc.claude_code.engine_config import EngineConfig
    from aic_dc.claude_code.health import (
        EngineStartupError,
        detect_credentials,
        minimum_cli_version,
        resolve_cli,
        sdk_cli_pin,
        sdk_version,
    )
    from aic_dc.config import ConfigManager

    print(f"aic-dc:      {__version__}")
    print(
        f"sdk:         claude-agent-sdk {sdk_version()} "
        f"(pins CLI {sdk_cli_pin()}, floor {minimum_cli_version()})"
    )

    config = EngineConfig.load(ConfigManager(repo_root=None).config_dir)
    try:
        resolution = resolve_cli(config.cli_path)
    except EngineStartupError as exc:
        print("engine:      NOT RESOLVED")
        print(f"\n{exc}", file=sys.stderr)
        return CHECK_ENGINE_UNRESOLVED

    print(f"engine:      {resolution.path}")
    print(f"source:      {resolution.source}")
    print(f"version:     {resolution.version}")

    # The resolution order surprises people, and it surprised this spec
    # twice: the wheel's copy wins over a `claude` on PATH. Say so only
    # when both exist, which is the one case where the answer looks wrong.
    on_path = shutil.which("claude")
    if on_path and str(Path(on_path)) != str(Path(resolution.path)):
        print(
            f"note:        `claude` is also on PATH at {on_path}, which is NOT "
            f"what will run. Set cli_path in engine.json to prefer it."
        )

    credentials, credential_warning = detect_credentials()
    print(f"credentials: {credentials}")
    if credential_warning:
        print(f"warning:     {credential_warning}")
    if resolution.version_warning:
        print(f"warning:     {resolution.version_warning}")

    if resolution.version == "unknown":
        # Found, and did not run. For the launch path this is a warning and
        # the session still tries; for a packaging check it is the failure
        # the check exists to catch.
        print(
            f"\nThe engine at {resolution.path} did not answer `--version`. It "
            f"was found but could not be run — check that it is executable and "
            f"that its dynamic libraries are present.",
            file=sys.stderr,
        )
        return CHECK_ENGINE_UNRUNNABLE
    return CHECK_ENGINE_OK


def _print_banner() -> None:
    """Print the startup banner to stderr.

    Uses ASCII only so it works over ssh and in plain terminals. Version
    reads the baked VERSION file via ``__version__``. Release builds have
    the VERSION file baked with a real timestamp+SHA; source installs show
    "dev".
    """
    banner = f"""
  AIC-DC  —  AI-Coder-SDK-Connector
  version {__version__}
"""
    print(banner, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Parameters
    ----------
    argv:
        Optional argument list for testing. When ``None`` the default
        ``sys.argv[1:]`` is used.

    Returns
    -------
    int
        Process exit code. 0 on success.
    """
    parser = _build_parser()
    # Parse (and validate) arguments — argparse will exit on --help/--version
    # or on parse errors.
    args = parser.parse_args(argv)

    # The permission gate, before logging is configured and before the
    # banner. This process is `agy` asking whether one tool call may run:
    # it reads a payload on stdin, prints one JSON decision on stdout, and
    # anything else this entry point would print is noise in the middle of
    # that protocol.
    #
    # **It exists because `python -m aic_dc.agy.hook` is not available in
    # every install.** Under PyInstaller `sys.executable` is the frozen
    # binary, which does not honour `-m`, so the installed hook command
    # exited 2 and `agy` took the `|| printf '{"decision":"allow"}'`
    # fallback — every tool call auto-approved, while Settings reported
    # the gate as installed and current because it compares command
    # strings. An ungated agent that says it is gated is the one outcome
    # AG-5 rules out, so the frozen build needs a command it can actually
    # run. See `aic_dc.agy.install.hook_command`.
    if args.agy_hook is not None:
        from aic_dc.agy.hook import main as agy_hook_main

        return agy_hook_main([args.agy_hook])

    # Install logging before anything else that might want to log.
    configure_logging(verbose=args.verbose)
    logger.debug("aic-dc invoked with args=%s", args)

    # Before the banner: the report is the output, and a banner above it is
    # noise in a CI log and in a pasted bug report.
    if args.check_engine:
        return _check_engine()

    _print_banner()

    # Launch the full startup orchestrator.
    import asyncio
    from aic_dc.main import run

    asyncio.run(run(
        repo_path=args.repo_path,
        server_port=args.server_port,
        webapp_port=args.webapp_port,
        no_browser=args.no_browser,
        dev=args.dev,
        preview=args.preview,
        verbose=args.verbose,
        collab=args.collab,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())