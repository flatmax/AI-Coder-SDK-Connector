"""Engine health — CLI discovery, version skew, credential source.

Three facts about the engine that are invisible unless we go looking, and
each of which produces a support question when it is wrong:

1. **Which ``claude`` binary are we running?** The SDK resolves one of
   four candidates, and the one it picks is not the one most people
   assume (see :func:`resolve_cli`).
2. **Is its version compatible?** The SDK enforces a floor by *logging a
   warning* and continuing. AC-DC fails startup instead, because a
   below-floor CLI produces malformed streams rather than a clean error.
3. **Which credentials will it use?** The CLI authenticates through its
   own config. Nothing here exports a credential into the environment;
   this module only reads back what the CLI will resolve, so the user can
   see which account a turn bills to.

Governing spec: ``specs5/3-engine/session.md`` § Errors and Degradation.
Reference: ``specs-reference/3-engine/session.md`` § ``EngineHealth`` and
§ Dependency quirks (CLI discovery and version skew).
Risks: ``specs5/plan/risks.md`` R-8 (version skew), R-9 (auth conflict).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# How long to wait for ``claude --version``. The bundled binary is ~295 MB
# and its first exec on a cold page cache is the slow case; 15 s is
# generous for a version print and still bounded well inside the 60 s
# connect budget.
CLI_VERSION_PROBE_TIMEOUT = 15.0

# Failed mirror appends tolerated before the health banner calls the mirror
# broken rather than unlucky. The SDK retries a batch before reporting a gap
# at all, so one is bad luck; a fourth is a pattern. `app.json`'s `history`
# section owns the running value and this is its default — the same
# relationship `DISK_WARNING_BYTES` has with the threshold beside it.
DEFAULT_MIRROR_GAP_TOLERANCE = 3

# The floor the SDK itself enforces. Read from the SDK when available so
# an upgraded floor is honoured without a code change here.
_FALLBACK_MINIMUM_CLI_VERSION = "2.0.0"

_VERSION_RE = re.compile(r"([0-9]+\.[0-9]+\.[0-9]+)")

# Environment variables that redirect the CLI's credentials or endpoint.
# Read only — never written. See § Credential resolution must not be
# polluted in the session reference.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_GATEWAY_VARS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
_ENDPOINT_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
)


class EngineStartupError(RuntimeError):
    """The engine cannot start, and no degraded mode is meaningful.

    Raised for a missing CLI and for a CLI below the SDK's floor. Both
    are actionable by the user and neither has a fallback: without a
    working CLI nothing in the new engine works at all.
    """


# ---------------------------------------------------------------------------
# SDK version facts
# ---------------------------------------------------------------------------


def sdk_version() -> str:
    """The installed ``claude-agent-sdk`` version, or ``"unknown"``."""
    try:
        from claude_agent_sdk import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - import-shape guard
        try:
            import importlib.metadata as md

            return md.version("claude-agent-sdk")
        except Exception:
            return "unknown"


def sdk_cli_pin() -> str:
    """The CLI version the SDK wheel bundles, or ``"unknown"``.

    Lives in the private ``claude_agent_sdk._cli_version`` module rather
    than the public namespace, so this read is a deliberate coupling to
    an SDK internal. It is wrapped because a private name can move in a
    patch release, and a skew *warning* is not worth failing startup for.
    """
    try:
        from claude_agent_sdk import _cli_version

        return str(_cli_version.__cli_version__)
    except Exception:
        logger.debug("SDK does not expose _cli_version.__cli_version__")
        return "unknown"


def minimum_cli_version() -> str:
    """The CLI version floor the SDK enforces."""
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli

        return str(subprocess_cli.MINIMUM_CLAUDE_CODE_VERSION)
    except Exception:
        return _FALLBACK_MINIMUM_CLI_VERSION


def _version_tuple(version: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


# ---------------------------------------------------------------------------
# CLI discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliResolution:
    """Which binary was selected, where it came from, and its version."""

    path: str
    source: str
    version: str
    version_warning: str | None = None


def _bundled_cli_path() -> Path | None:
    """The CLI shipped inside the SDK wheel, if this wheel has one.

    Mirrors ``SubprocessCLITransport._find_bundled_cli``. Only used when
    the SDK's own resolver is unavailable; the resolver is preferred so
    the reported path is the path the SDK will actually spawn.
    """
    try:
        import claude_agent_sdk

        name = "claude.exe" if platform.system() == "Windows" else "claude"
        candidate = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        return candidate if candidate.is_file() else None
    except Exception:  # pragma: no cover - import-shape guard
        return None


def _classify_source(path: str) -> str:
    """Human-readable account of where ``path`` came from."""
    bundled = _bundled_cli_path()
    if bundled is not None and Path(path) == bundled:
        return "bundled with the claude-agent-sdk wheel"
    on_path = shutil.which("claude")
    if on_path is not None and Path(path) == Path(on_path):
        return "found on PATH"
    return "found by SDK discovery"


def _sdk_find_cli() -> str:
    """Ask the SDK's own resolver which binary it would spawn.

    The resolution order is **not** the intuitive one, and not the one an
    earlier draft of the spec recorded: the bundled binary wins over a
    ``claude`` on ``PATH``. A machine with a newer system CLI still runs
    the wheel's copy unless ``engine.json``'s ``cli_path`` says otherwise.
    That is why this is reported rather than assumed.

    Uses a private transport method. The constructor is pure assignment,
    so instantiating one purely to resolve a path spawns nothing.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    probe = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())
    return str(probe._find_cli())


def _fallback_find_cli() -> str | None:
    """Replicate the SDK's search order when the private path is gone."""
    bundled = _bundled_cli_path()
    if bundled is not None:
        return str(bundled)
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = Path.home()
    for candidate in (
        home / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        home / ".local/bin/claude",
        home / "node_modules/.bin/claude",
        home / ".yarn/bin/claude",
        home / ".claude/local/claude",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def probe_cli_version(cli_path: str) -> str:
    """Run ``<cli> --version`` and return the parsed ``X.Y.Z``.

    Returns ``"unknown"`` rather than raising when the probe fails: an
    unparseable version is a reporting problem, and the SDK will produce
    a far better diagnostic at connect time if the binary is genuinely
    broken.
    """
    try:
        completed = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=CLI_VERSION_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not run %s --version: %s", cli_path, exc)
        return "unknown"
    match = _VERSION_RE.search((completed.stdout or completed.stderr or "").strip())
    return match.group(1) if match else "unknown"


def resolve_cli(cli_path_override: str | None = None) -> CliResolution:
    """Resolve the ``claude`` binary, probe it, and check for skew.

    Parameters
    ----------
    cli_path_override:
        ``engine.json``'s ``cli_path``. Bypasses discovery entirely —
        present precisely because discovery prefers the bundled binary.

    Raises
    ------
    EngineStartupError
        When no CLI can be found, when an explicitly configured path does
        not exist, or when the resolved CLI is below the SDK's floor.
        None of the three has a degraded mode: the spec's rule is that a
        missing or too-old CLI fails startup with both versions named.
    """
    if cli_path_override:
        path = str(Path(cli_path_override).expanduser())
        if not Path(path).is_file():
            raise EngineStartupError(
                f"engine.json sets cli_path={cli_path_override!r} but no file "
                f"exists there. Fix the path or set it to null to use SDK "
                f"discovery (which prefers the CLI bundled with the wheel)."
            )
        source = "engine.json cli_path"
    else:
        try:
            path = _sdk_find_cli()
            source = _classify_source(path)
        except Exception as exc:
            # CLINotFoundError carries the SDK's own install instructions,
            # which are better than anything we would write; anything else
            # means the private resolver moved and we fall back.
            if type(exc).__name__ == "CLINotFoundError":
                raise EngineStartupError(str(exc)) from exc
            logger.warning(
                "SDK CLI discovery unavailable (%s); falling back to our own search",
                exc,
            )
            found = _fallback_find_cli()
            if found is None:
                raise EngineStartupError(
                    "Claude Code CLI not found. Searched: the copy bundled with "
                    "claude-agent-sdk, `claude` on PATH, ~/.npm-global/bin, "
                    "/usr/local/bin, ~/.local/bin, ~/node_modules/.bin, "
                    "~/.yarn/bin and ~/.claude/local. Install it with "
                    "`npm install -g @anthropic-ai/claude-code`, or set "
                    "cli_path in engine.json."
                ) from exc
            path = found
            source = _classify_source(found)

    version = probe_cli_version(path)
    warning = _version_warning(path, version)
    logger.info(
        "Claude Code CLI: %s (%s), version %s, SDK pin %s",
        path,
        source,
        version,
        sdk_cli_pin(),
    )
    return CliResolution(path=path, source=source, version=version, version_warning=warning)


def _version_warning(path: str, version: str) -> str | None:
    """Compare the resolved CLI against the floor and the SDK's pin.

    Below the floor raises; above the floor but different from the pin
    warns and continues, per the spec's three-way table.
    """
    floor = minimum_cli_version()
    pin = sdk_cli_pin()
    resolved = _version_tuple(version)
    if resolved is None:
        return (
            f"Could not determine the version of the Claude Code CLI at {path}. "
            f"The SDK expects {pin} and requires at least {floor}."
        )
    floor_parts = _version_tuple(floor)
    if floor_parts is not None and resolved < floor_parts:
        raise EngineStartupError(
            f"Claude Code CLI at {path} is version {version}; the installed "
            f"claude-agent-sdk requires at least {floor}. Upgrade the CLI "
            f"(`npm install -g @anthropic-ai/claude-code@latest`) or point "
            f"engine.json's cli_path at a newer binary."
        )
    if pin != "unknown" and version != pin:
        return (
            f"CLI version {version} differs from the version the SDK was built "
            f"against ({pin}). Above the {floor} floor, so the session will "
            f"start; behaviour differences are possible."
        )
    return None


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def detect_credentials() -> tuple[str, str | None]:
    """Report which credential source the CLI will resolve.

    Read-only by contract: this function never sets an environment
    variable, and nothing in the new engine's config path does either.
    Returns ``(source, warning)`` where a non-null warning describes a
    combination that will surprise the user — most importantly an API key
    in the environment when a subscription login also exists, which
    silently bills a different account (R-9).
    """
    env = os.environ
    gateway = next((var for var in _GATEWAY_VARS if _truthy(env.get(var))), None)
    api_key_var = next((var for var in _API_KEY_VARS if env.get(var)), None)
    subscription = _subscription_credential_path()
    endpoint_overrides = [var for var in _ENDPOINT_VARS if env.get(var)]

    if gateway:
        provider = "Amazon Bedrock" if "BEDROCK" in gateway else "Google Vertex AI"
        source = f"{provider} (via {gateway})"
    elif api_key_var:
        source = f"API key from ${api_key_var}"
    elif subscription is not None:
        source = f"subscription login ({_tilde(subscription)})"
    else:
        source = "unknown — the CLI will prompt for login"

    warnings: list[str] = []
    if api_key_var and subscription is not None:
        warnings.append(
            f"${api_key_var} is set and a subscription login exists at "
            f"{_tilde(subscription)}. The CLI will use the API key, so turns "
            f"bill to that key rather than the subscription."
        )
    if gateway and subscription is not None:
        warnings.append(
            f"${gateway} redirects the CLI to a gateway even though a "
            f"subscription login exists at {_tilde(subscription)}."
        )
    if endpoint_overrides:
        warnings.append(
            "Endpoint override(s) in the environment: "
            + ", ".join(f"${var}" for var in endpoint_overrides)
            + ". The CLI will talk to that endpoint, not the default API."
        )
    return source, ("; ".join(warnings) if warnings else None)


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in ("0", "false", "no", "")


def _subscription_credential_path() -> Path | None:
    """Locate the CLI's own credential file, honouring CLAUDE_CONFIG_DIR."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    for name in (".credentials.json", "credentials.json"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def _tilde(path: Path) -> str:
    """Render ``path`` with ``$HOME`` collapsed to ``~`` for display."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# EngineHealth
# ---------------------------------------------------------------------------


@dataclass
class EngineHealth:
    """The engine-health record broadcast to the browser.

    Mutable and long-lived: one instance per session, updated as facts
    arrive (a mirror gap mid-turn, an MCP server dropping out). Shape
    fixed by ``specs-reference/3-engine/session.md`` § ``EngineHealth``.
    """

    connected: bool = False
    cli_path: str = ""
    cli_version: str = "unknown"
    cli_source: str = ""
    sdk_version: str = field(default_factory=sdk_version)
    sdk_cli_pin: str = field(default_factory=sdk_cli_pin)
    version_warning: str | None = None
    credential_source: str = "unknown"
    auth_warning: str | None = None
    mcp: list[dict[str, Any]] = field(default_factory=list)
    mirror_gaps: int = 0
    last_error: str | None = None
    #: Capabilities the session started *without*, one sentence each.
    #:
    #: The bridge and the post-write hook both degrade to None rather than
    #: refusing to construct, which keeps the editor alive and used to keep
    #: the loss to a log line nobody reads —
    #: ``specs5/3-engine/mcp-bridge.md`` § Availability and Degradation asks
    #: for a banner instead. Sentences rather than flags because the browser
    #: is told what was lost and what the agent will do instead, for the
    #: reason it is told the disk warning's sentence: one owner of the words.
    degradations: list[str] = field(default_factory=list)
    #: How many failed mirror appends the browser's health banner treats as
    #: bad luck before it treats them as a broken mirror. A callable, not a
    #: number, because it comes from ``app.json`` — which reloads without a
    #: restart, so reading it once here would pin the value the server
    #: started with. The default answers for a session built without a
    #: config manager, which is every unit test that does not care.
    mirror_gap_tolerance: Callable[[], int] = lambda: DEFAULT_MIRROR_GAP_TOLERANCE

    def apply_cli(self, resolution: CliResolution) -> None:
        """Record a :func:`resolve_cli` result."""
        self.cli_path = resolution.path
        self.cli_version = resolution.version
        self.cli_source = resolution.source
        self.version_warning = resolution.version_warning

    def apply_credentials(self) -> None:
        """Re-read the credential source and warning."""
        self.credential_source, self.auth_warning = detect_credentials()

    def note_mirror_gap(self) -> None:
        """Count a ``MirrorErrorMessage``; the repo-local copy has a hole."""
        self.mirror_gaps += 1

    def note_degradation(self, sentence: str) -> None:
        """Record a capability this session is running without.

        Deduplicated on the text: this is a standing condition rather than
        an event, so a second report of the same loss is not new information
        — and the banner keys its dismissal on what it showed, which a
        growing list of identical sentences would defeat.
        """
        text = str(sentence).strip()
        if text and text not in self.degradations:
            self.degradations.append(text)

    def _escalated(self) -> bool:
        """Whether the gap count has passed what is tolerated.

        The rule lives here rather than in the browser, for the reason the
        disk warning's one-shot does: one owner. A second copy of it in the
        panel could only disagree, and would have to be handed the
        threshold to disagree about.

        A tolerance that raises or answers with nonsense is read as the
        default rather than as "never escalate" — a broken config key must
        not be a way to silence a broken mirror.
        """
        try:
            tolerated = int(self.mirror_gap_tolerance())
        except Exception:  # noqa: BLE001 - a config read, not a control path
            tolerated = DEFAULT_MIRROR_GAP_TOLERANCE
        if tolerated < 0:
            tolerated = DEFAULT_MIRROR_GAP_TOLERANCE
        return self.mirror_gaps > tolerated

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "cli_path": self.cli_path,
            "cli_version": self.cli_version,
            "cli_source": self.cli_source,
            "sdk_version": self.sdk_version,
            "sdk_cli_pin": self.sdk_cli_pin,
            "version_warning": self.version_warning,
            "credential_source": self.credential_source,
            "auth_warning": self.auth_warning,
            "mcp": list(self.mcp),
            "mirror_gaps": self.mirror_gaps,
            "mirror_gaps_escalated": self._escalated(),
            "last_error": self.last_error,
            "degradations": list(self.degradations),
        }
