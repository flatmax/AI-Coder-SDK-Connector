"""``ClaudeAgentOptions`` assembly.

One function, called once per connect. It is a separate module from
:mod:`ac_dc.claude_code.session` for one reason: this is the surface most
likely to break on an SDK upgrade, and a pure function that builds a
kwargs dict can be tested without spawning a CLI.

The rules that govern everything here:

- **Null config means omit the option.** A field left out gets the SDK's
  own default, which tracks the CLI. A field passed as ``None`` may mean
  something different — and even where it does not, it pins today's
  default into our code.
- **Some options are never set, deliberately.** ``allowed_tools`` would
  approve calls before ``can_use_tool`` runs, silently ungating gated
  tools. ``agents`` would make subagent definitions AC-DC-only instead of
  shared with the CLI. See ``specs5/3-engine/session.md`` § Session
  Options.
- **Two options are the exceptions that prove the null rule.**
  ``system_prompt`` carries no text of ours, but it must be *set*:
  ``None`` means an empty prompt, not the CLI's (see
  ``CLI_SYSTEM_PROMPT``). ``max_buffer_size`` is set because the SDK's
  own default ends sessions (see ``DEFAULT_MAX_BUFFER_SIZE``). In both
  cases deferring to the dependency is the broken option, which is the
  only argument that earns an exception here.
- **One pair is mutually exclusive.** ``session_store`` and
  ``enable_file_checkpointing`` cannot both be set — the SDK refuses the
  combination outright, at connect. See
  :func:`file_checkpointing_available`.

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
# The flag buys nothing on its own, so the two are set together or not at
# all — see :func:`file_checkpointing_available`.
REPLAY_USER_MESSAGES_ARG = {"replay-user-messages": None}

# `AskUserQuestion` option previews are undocumented until the host asks
# for them.
#
# The per-option ``preview`` field is in the tool's input schema
# unconditionally, described only as "see the tool description for the
# expected content format". This env var is what puts that format there:
# read at startup, accepting exactly ``"markdown"`` and ``"html"``, it
# decides whether the tool's prompt carries the "Preview feature" block —
# what previews are for, which format to author, that the UI turns
# side-by-side, that they are single-select only. Left unset it is on for a
# terminal session and off for every SDK entrypoint, and ours is
# ``sdk-py``.
#
# So this is not an on/off switch for the field, and setting it is not what
# makes a preview possible: a model that knows the field from elsewhere
# fills it in regardless, which is what a live A/B against this CLI showed
# — previews arrived with the var popped from the environment entirely.
# What it fixes is that the format was then nobody's decision. The schema
# points at a description that says nothing, so markdown or HTML is the
# model's guess, and the dialog renders one of those as a mockup and the
# other as a wall of angle brackets. Setting it makes the format ours.
#
# ``markdown`` rather than ``html``: html mode has the model author an HTML
# fragment, and forwarding model-authored HTML into the dialog's shadow DOM
# is not a thing to do for a display nicety. Markdown reaches the same
# artefacts the field is for — ASCII mockups, code snippets, diagram
# variations, configuration examples — through the escaping renderer the
# chat panel and the plan body already trust.
#
# The value is pinned rather than configurable because the browser renders
# markdown. The two sides have to agree on one format, and a knob is a way
# for them to disagree: set to ``html`` here, the dialog would show a user
# the angle brackets of a mockup instead of the mockup.
QUESTION_PREVIEW_FORMAT = "markdown"
QUESTION_PREVIEW_ENV = {"CLAUDE_CODE_QUESTION_PREVIEW_FORMAT": QUESTION_PREVIEW_FORMAT}

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

# How much of one line of CLI stdout the SDK may buffer — 16 MiB.
#
# The second option we always set, because the SDK's default is 1 MiB and
# overflowing it is not a degradation but a session ending. The reader in
# ``_internal/transport/subprocess_cli.py`` accumulates until it has a
# newline; past the limit it raises ``CLIJSONDecodeError``, which leaves
# the anext() loop in ``session.py``'s pump. The conversation is then over
# with its last message half-parsed, and no amount of retrying gets the
# rest of it, because the bytes are gone with the reader.
#
# It is reachable in normal use. One line here is one JSON message, and a
# message can carry a whole tool result: an inline screenshot (2026-08-17)
# did exactly this, base64 crossing 1 MiB on the way to the browser and
# killing the engine mid-turn. A Read of a large file, a wide grep, or a
# build log does the same.
#
# 16 MiB rather than something larger: the number has to bound memory as
# well as failure, and the ceiling is per pending line, per session. On the
# localhost single-user host AC-DC is built for, 16 MiB of pending buffer
# is nothing, while covering a base64 payload of about 12 MB of raw bytes —
# well past any screenshot or source file. Beyond that a caller is
# streaming something that should not be arriving as one JSON line at all,
# and ``engine.json``'s ``max_buffer_size`` is there for the case that
# judgement is wrong.
DEFAULT_MAX_BUFFER_SIZE = 16 * 1024 * 1024


def file_checkpointing_available(session_store: Any) -> bool:
    """Whether this session can offer ``rewind_files()``.

    The SDK refuses ``enable_file_checkpointing`` together with
    ``session_store`` — ``ValueError`` from
    ``_internal/session_store_validation.py`` at connect *and* at query,
    on the grounds that "checkpoints are local-disk only and would diverge
    from the mirrored transcript". So this is not a preference to tune but
    a fork in the road, and the mirror takes it:

    - The mirror carries `.ac-dc4/` history, resume-after-restart, the
      session browser, and the derived search index — everything phase 5
      shipped. Without it a session outlives only the CLI's own retention
      window, which looks like data loss days later rather than a missing
      feature today.
    - Checkpointing carries one control that nothing calls yet
      (``specs5/plan/delivery.md`` § No rewind UI), and git already covers
      most of what it would undo.

    Nobody is refused a session over this: a repoless run has no mirror,
    so it *does* get checkpointing, and a mirrored run loses the undo
    rather than the engine.
    """
    return session_store is None


def build_option_kwargs(
    *,
    repo_root: Path | str,
    config: EngineConfig,
    cli_path: str | None = None,
    can_use_tool: Any = None,
    hooks: Any = None,
    mcp_servers: Any = None,
    session_store: Any = None,
    stderr: Any = None,
    resume: str | None = None,
    fork_session: bool = False,
    permission_mode: str | None = None,
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
    stderr:
        A one-line-at-a-time sink for the CLI's own stderr. Omitted while
        ``None``, and the omission is the meaningful case: registering a
        callback is what makes the SDK *pipe* stderr instead of letting it
        inherit the server's, so a caller that passes one takes on
        surfacing it (see :meth:`EngineSession._note_cli_stderr`).
    resume:
        An SDK session ID to resume. Omitted for a new session.
    fork_session:
        Branch from ``resume`` instead of continuing it. Ignored (with a
        warning) without ``resume``, which is what the SDK would do
        anyway but silently.
    permission_mode:
        The posture to start in, overriding ``engine.json``. The session
        passes its *current* mode, so a posture set before the first
        connect — review mode entered on a cold engine — is the posture
        the CLI comes up in, rather than being silently reverted to the
        configured default by the connect itself.
    """
    kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
        # Not ours — the CLI's. See CLI_SYSTEM_PROMPT for why omitting this
        # deletes the prompt instead of defaulting to it.
        "system_prompt": dict(CLI_SYSTEM_PROMPT),
        # The posture a new session starts in. Live-switchable afterwards
        # via set_permission_mode() without a reconnect.
        "permission_mode": permission_mode or config.effective_permission_mode,
        # Token-level streaming. Without it the UI only updates per block,
        # which reads as a stall on long responses.
        "include_partial_messages": True,
        # Makes hook activity inspectable rather than invisible.
        "include_hook_events": True,
        "setting_sources": list(SETTING_SOURCES),
        # Turns on the per-option previews the question dialog renders.
        # Copied, not shared: the SDK holds the dict for the session's
        # lifetime and one session must not be able to edit another's.
        "env": dict(QUESTION_PREVIEW_ENV),
        # Set unconditionally, against the null rule, because the SDK's
        # default is 1 MiB and one line over it ends the session.
        "max_buffer_size": config.max_buffer_size or DEFAULT_MAX_BUFFER_SIZE,
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
    if stderr is not None:
        kwargs["stderr"] = stderr
    if session_store is not None:
        kwargs["session_store"] = session_store
        # Batched flushing holds a turn's tail until the result message,
        # which makes the repo-local mirror lag the UI by a whole turn.
        kwargs["session_store_flush"] = "eager"

    if file_checkpointing_available(session_store):
        kwargs["enable_file_checkpointing"] = True
        kwargs["extra_args"] = dict(REPLAY_USER_MESSAGES_ARG)
    else:
        logger.info(
            "Mirroring the transcript into the repo, so file checkpointing is "
            "off and rewind_files() is unavailable: the SDK refuses the two "
            "together. Undo file changes with git."
        )

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
