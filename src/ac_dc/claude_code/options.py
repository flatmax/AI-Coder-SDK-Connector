"""``ClaudeAgentOptions`` assembly.

One function, called once per connect. It is a separate module from
:mod:`ac_dc.claude_code.session` for one reason: this is the surface most
likely to break on an SDK upgrade, and a pure function that builds a
kwargs dict can be tested without spawning a CLI.

Two rules govern everything here:

- **Null config means omit the option.** A field left out gets the SDK's
  own default, which tracks the CLI. A field passed as ``None`` may mean
  something different — and even where it does not, it pins today's
  default into our code.
- **Some options are never set, deliberately.** ``allowed_tools`` would
  approve calls before ``can_use_tool`` runs, silently ungating gated
  tools. ``agents`` would make subagent definitions AC-DC-only instead of
  shared with the CLI. See ``specs5/3-engine/session.md`` § Session
  Options.
- **``system_prompt`` is the exception that proves the null rule.** It
  carries no text of ours, but it must be *set*: ``None`` means an empty
  prompt, not the CLI's. See ``CLI_SYSTEM_PROMPT``.

Governing spec: ``specs5/3-engine/session.md``.
Reference: ``specs-reference/3-engine/session.md`` § Options assembly.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ac_dc.claude_code.health import EngineStartupError

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

    from ac_dc.claude_code.engine_config import EngineConfig

logger = logging.getLogger(__name__)


# The repo's own settings apply: CLAUDE.md, .claude/settings.json,
# .claude/agents/, .claude/skills/, and custom slash commands. A session
# in AC-DC therefore behaves like a session in the CLI in the same repo
# (specs5/plan/decisions.md CC-11).
SETTING_SOURCES = ["user", "project", "local"]

# ``enable_file_checkpointing`` alone is not enough for ``rewind_files()``
# — rewinding is keyed on a user-message ID that only exists when user
# messages are replayed back to the SDK. A value of None emits a bare
# ``--replay-user-messages`` flag. Missing it fails at rewind time rather
# than connect time, which is why it is pinned here next to its partner.
REPLAY_USER_MESSAGES_ARG = {"replay-user-messages": None}

# Options AC-DC must never set, with the reason, so a future reader who
# is tempted gets the argument rather than a bare prohibition.
NEVER_SET = {
    "allowed_tools": "an allow rule approves a call before can_use_tool runs, "
    "which would silently ungate a gated tool; tool allowances belong in "
    "project settings where the user can see them",
    "agents": "subagent definitions come from .claude/agents/ so they are "
    "shared with the CLI rather than being AC-DC-only",
}

# The CLI's own system prompt, kept as the CLI's.
#
# Omitting ``system_prompt`` does **not** mean "use the default". The SDK
# emits ``--system-prompt ""`` when it is ``None`` (verified in
# ``_internal/transport/subprocess_cli.py``), which deletes the prompt
# outright — including the dynamic sections that carry the working
# directory, the git status and the platform. Observed consequence: asked
# to edit ``greet.py`` in a repo at ``/tmp/ac-dc-live``, the agent tried to
# read ``/home/flatmax/greet.py``, because nothing had told it where it was.
#
# The preset form with no ``append`` emits no flag at all, which leaves the
# CLI's prompt exactly as a terminal session in the same repo would have
# it. That is the point: prompt *customisation* is still CLAUDE.md's job
# (our own prompt text would fork behaviour between AC-DC and the CLI in a
# file the user does not know exists), and this sets no text of ours.
CLI_SYSTEM_PROMPT: dict[str, str] = {"type": "preset", "preset": "claude_code"}


def build_option_kwargs(
    *,
    repo_root: Path | str,
    config: EngineConfig,
    cli_path: str | None = None,
    can_use_tool: Any = None,
    hooks: Any = None,
    mcp_servers: Any = None,
    session_store: Any = None,
    resume: str | None = None,
    fork_session: bool = False,
) -> dict[str, Any]:
    """Build the kwargs for :class:`ClaudeAgentOptions`.

    Split out from :func:`build_options` so tests can assert on what we
    set — including what we deliberately leave unset — without needing
    the SDK's dataclass to accept it first.

    Parameters
    ----------
    repo_root:
        Becomes ``cwd``. Every tool path the agent produces is then
        repo-relative, so the diff viewer and file tree resolve it
        without translation.
    config:
        Parsed ``engine.json``. Null fields are omitted.
    cli_path:
        An already-resolved CLI path, which wins over ``config.cli_path``.
        The session passes the binary it version-checked, so the probed
        binary and the run binary are the same one.
    can_use_tool, hooks, mcp_servers, session_store:
        Injected collaborators, each landing in a later conversion phase.
        Omitted while ``None`` so the spike runs without them.
    resume:
        An SDK session ID to resume. Omitted for a new session.
    fork_session:
        Branch from ``resume`` instead of continuing it. Ignored (with a
        warning) without ``resume``, which is what the SDK would do
        anyway but silently.
    """
    kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
        # Not ours — the CLI's. See CLI_SYSTEM_PROMPT for why omitting this
        # deletes the prompt instead of defaulting to it.
        "system_prompt": dict(CLI_SYSTEM_PROMPT),
        # The posture a new session starts in. Live-switchable afterwards
        # via set_permission_mode() without a reconnect.
        "permission_mode": config.effective_permission_mode,
        # Token-level streaming. Without it the UI only updates per block,
        # which reads as a stall on long responses.
        "include_partial_messages": True,
        # Makes hook activity inspectable rather than invisible.
        "include_hook_events": True,
        "setting_sources": list(SETTING_SOURCES),
        "enable_file_checkpointing": True,
        "extra_args": dict(REPLAY_USER_MESSAGES_ARG),
    }

    resolved_cli = cli_path or config.cli_path
    if resolved_cli:
        # Bypasses SDK discovery, which prefers the binary bundled in the
        # wheel over one on PATH. Setting it explicitly also removes the
        # gap between the binary health.py probed and the one that runs:
        # our resolution has a fallback the SDK's does not.
        kwargs["cli_path"] = resolved_cli
    if config.model:
        kwargs["model"] = config.model
    if config.max_budget_usd is not None:
        kwargs["max_budget_usd"] = config.max_budget_usd
    if config.effort:
        kwargs["effort"] = config.effort
    if config.thinking_display:
        # The SDK models thinking as a TypedDict union, not a class with a
        # `display` argument. "adaptive" leaves the budget to the model,
        # which is the CLI's own default posture; `display` is the part the
        # user asked for.
        kwargs["thinking"] = {
            "type": "adaptive",
            "display": config.thinking_display,
        }

    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool
    if hooks is not None:
        kwargs["hooks"] = hooks
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if session_store is not None:
        kwargs["session_store"] = session_store
        # Batched flushing holds a turn's tail until the result message,
        # which makes the repo-local mirror lag the UI by a whole turn.
        kwargs["session_store_flush"] = "eager"

    if resume:
        kwargs["resume"] = resume
        if fork_session:
            kwargs["fork_session"] = True
    elif fork_session:
        logger.warning(
            "fork_session requested without a session to resume; ignoring. "
            "Forking branches an existing session and needs one to branch from."
        )

    return kwargs


def build_options(**kwargs: Any) -> ClaudeAgentOptions:
    """Construct :class:`ClaudeAgentOptions` from :func:`build_option_kwargs`.

    Raises
    ------
    EngineStartupError
        When the installed SDK has no field for something we set. The SDK
        is a dependency rather than a contract we own, so a removed field
        is a real possibility; failing here names the fields instead of
        surfacing a bare ``TypeError`` from a dataclass constructor.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    option_kwargs = build_option_kwargs(**kwargs)
    known = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    unknown = sorted(set(option_kwargs) - known)
    if unknown:
        raise EngineStartupError(
            "The installed claude-agent-sdk has no ClaudeAgentOptions "
            f"field(s) named: {', '.join(unknown)}. The SDK surface moved; "
            "re-read it and update ac_dc.claude_code.options "
            "(see specs5/plan/sdk-surface.md)."
        )
    return ClaudeAgentOptions(**option_kwargs)
