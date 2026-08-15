"""Commit and reset — the two git writes AC⚡DC still performs itself.

The agent has its own ``Bash`` and can run ``git commit`` when asked to.
These two entry points exist for the buttons in the shell's git menu,
which are the user's own hands rather than the agent's, and they keep
working exactly as they did before the conversion.

What changed is where the commit message comes from. It used to be a
blocking provider call against a separately-configured "smaller model".
Now it is a **stateless one-shot** through :func:`claude_agent_sdk.query`
— a second, short-lived CLI process with no tools, no settings sources
and one turn, which cannot touch the repository and cannot see the chat
session. It is deliberately not routed through the live
:class:`~ac_dc.claude_code.session.EngineSession`: sending a diff to the
chat session would put the whole staged diff in the conversation the user
is having, and would deadlock behind a turn in flight.

Three entry points:

- :func:`commit_all` — the RPC. Gates, guards, and returns
  ``{"status": "started"}`` immediately; the work runs as a background
  task and reports through ``commitResult``.
- :func:`commit_all_background` — the pipeline: stage, generate, commit,
  broadcast.
- :func:`reset_to_head` — synchronous, no model call.

Governing spec: ``specs5/3-engine/session.md``; the message prompt itself
is ``config/commit.md``, unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ac_dc.claude_code.messages import Event

if TYPE_CHECKING:
    from ac_dc.claude_code.service import ClaudeCodeService

logger = logging.getLogger(__name__)

# A staged diff larger than this is not sent to the model. The ceiling is
# in characters because we no longer have a tokenizer to count with, and
# the point is to fail with a sentence the user can act on rather than
# after a 30-second round trip that returns a context-length error.
MAX_DIFF_CHARS = 400_000

# How long the one-shot may take before we give up on it. Generous: a
# large diff on a slow link is a normal case, and the user is watching a
# spinner they can ignore. Short enough that a wedged subprocess does not
# leave the commit button disabled for the rest of the session.
GENERATE_TIMEOUT_SECONDS = 180.0


async def commit_all(service: ClaudeCodeService) -> dict[str, Any]:
    """Stage everything, generate a message, and commit. **Localhost only.**

    Returns ``{"status": "started"}`` as soon as the work is launched;
    the outcome arrives as a ``commitResult`` broadcast, so every client
    sees the same result rather than only the one that clicked.

    Refused during a review: HEAD sits at the merge-base, so a commit
    there would record the whole reviewed branch as one new commit on a
    detached HEAD (``specs5/4-features/code-review.md`` § Read-Only Mode).
    """
    restricted = service._check_localhost_only()
    if restricted is not None:
        return restricted
    if service._repo is None:
        return {"error": "No repository attached"}
    if service.review.active:
        return {
            "error": (
                "Commit is disabled during a review. HEAD is at the review's "
                "merge-base, so committing here would rewrite the branch you "
                "are reviewing. Exit the review first."
            )
        }
    if service._committing:
        return {"error": "A commit is already in progress"}

    service._committing = True
    task = asyncio.create_task(commit_all_background(service), name="cc-commit")
    service._turn_tasks.add(task)
    task.add_done_callback(service._turn_tasks.discard)
    return {"status": "started"}


async def commit_all_background(service: ClaudeCodeService) -> None:
    """Stage, generate, commit, broadcast. Never raises.

    Every exit path broadcasts exactly one ``commitResult`` and clears
    the in-progress flag, because the button stays disabled until it
    hears back.
    """
    repo = service._repo
    assert repo is not None
    try:
        repo.stage_all()
        diff = repo.get_staged_diff()
        if not diff.strip():
            await service._broadcast(
                Event(
                    "commitResult",
                    {"error": "No staged changes to commit"},
                    turn_scoped=False,
                )
            )
            return
        if len(diff) > MAX_DIFF_CHARS:
            await service._broadcast(
                Event(
                    "commitResult",
                    {
                        "error": (
                            f"The staged diff is {len(diff) // 1000}k characters, "
                            f"over the {MAX_DIFF_CHARS // 1000}k ceiling for "
                            "message generation. Commit in smaller pieces, or "
                            "write the message yourself with `git commit`. Your "
                            "staged changes are unchanged."
                        )
                    },
                    turn_scoped=False,
                )
            )
            return

        message = await generate_commit_message(service, diff)
        if message is None:
            # No fallback message. The user asked for a generated one, and
            # committing "chore: update files" instead hides the failure
            # inside permanent history.
            await service._broadcast(
                Event(
                    "commitResult",
                    {
                        "error": (
                            "Could not generate a commit message. The staged "
                            "changes are untouched — check the engine health "
                            "panel, then try again."
                        )
                    },
                    turn_scoped=False,
                )
            )
            return

        result = repo.commit(message)
        event_text = (
            f"**Committed** `{result['sha'][:7]}`\n\n"
            f"```\n{result['message']}\n```"
        )
        # Broadcast only. The transcript this belongs in is the engine's,
        # and we do not write to it — mirroring lands in phase 5
        # (specs5/plan/README.md), at which point this event joins it.
        await service._broadcast(
            Event(
                "commitResult",
                {
                    "sha": result["sha"],
                    "short_sha": result["sha"][:7],
                    "message": result["message"],
                    "system_event_message": event_text,
                },
                turn_scoped=False,
            )
        )
        # A commit changes no file content but flips every staged file's
        # status badge, so the picker has to reload.
        await service._broadcast(Event("filesModified", [], turn_scoped=False))
    except Exception as exc:
        logger.exception("Commit failed: %s", exc)
        await service._broadcast(
            Event("commitResult", {"error": str(exc)}, turn_scoped=False)
        )
    finally:
        service._committing = False


async def generate_commit_message(
    service: ClaudeCodeService, diff: str
) -> str | None:
    """Ask a throwaway one-shot session for a commit message.

    Returns the message, or ``None`` when the call failed or produced
    nothing usable. ``None`` is a hard error to the caller: there is no
    fallback message, by design.

    The option set is minimal on purpose, and each omission is load-bearing:

    - ``tools=[]`` — no tools at all, so nothing can read or write the
      repository. There is no permission gate on this session and it does
      not need one.
    - ``setting_sources=[]`` — no ``CLAUDE.md``, no skills, no agents, no
      MCP servers. The message format is ours (``config/commit.md``) and a
      project instruction file should not silently redefine it, nor should
      a diff paid for by the user drag a plugin's startup cost along.
    - ``max_turns=1`` — one response. With no tools there is nothing to
      iterate on.
    """
    prompt = service._config.get_commit_prompt()
    options = _one_shot_options(service, prompt)
    if options is None:
        return None

    try:
        from claude_agent_sdk import AssistantMessage, TextBlock, query
    except ImportError as exc:  # pragma: no cover - the SDK is a hard dep
        logger.error("claude-agent-sdk is not importable: %s", exc)
        return None

    parts: list[str] = []
    try:
        async with asyncio.timeout(GENERATE_TIMEOUT_SECONDS):
            async for message in query(prompt=diff, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
    except TimeoutError:
        logger.warning(
            "Commit-message generation timed out after %.0fs",
            GENERATE_TIMEOUT_SECONDS,
        )
        return None
    except Exception as exc:
        logger.warning("Commit-message generation failed: %s", exc)
        return None

    message = "".join(parts).strip()
    if not message:
        logger.warning("Commit-message generation returned no text")
        return None
    return _strip_fence(message)


def _one_shot_options(service: ClaudeCodeService, prompt: str) -> Any:
    """Build the options for the throwaway session, or ``None``.

    Returns ``None`` rather than raising when the installed SDK has no
    field for something here: a commit that cannot generate its message
    reports that and leaves the tree staged, which is recoverable. Killing
    the RPC with a ``TypeError`` from a dataclass constructor is not.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ImportError as exc:  # pragma: no cover - the SDK is a hard dep
        logger.error("claude-agent-sdk is not importable: %s", exc)
        return None

    kwargs: dict[str, Any] = {
        "system_prompt": prompt,
        "cwd": str(service._repo_root),
        "tools": [],
        "setting_sources": [],
        "max_turns": 1,
        "permission_mode": "plan",
    }
    # The same binary the live session version-checked, when there is one.
    cli_path = service.session.health.cli_path or service.engine_config.cli_path
    if cli_path:
        kwargs["cli_path"] = cli_path
    if service.engine_config.model:
        kwargs["model"] = service.engine_config.model
    try:
        return ClaudeAgentOptions(**kwargs)
    except TypeError as exc:
        logger.error(
            "Could not build options for commit-message generation (%s). The "
            "SDK surface moved; re-read it and update "
            "ac_dc.claude_code.commit._one_shot_options.",
            exc,
        )
        return None


def _strip_fence(message: str) -> str:
    """Drop a wrapping code fence, if the model added one.

    ``config/commit.md`` asks for the bare message, and the model usually
    complies — but a fenced answer is a formatting slip, not a failure,
    and committing the backticks verbatim would put them in git history.
    """
    lines = message.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return message


def reset_to_head(service: ClaudeCodeService) -> dict[str, Any]:
    """Discard every uncommitted change. **Localhost only.**

    Synchronous — no model call. Broadcasts ``filesModified`` because
    every modified, staged and untracked file changes state at once.
    """
    restricted = service._check_localhost_only()
    if restricted is not None:
        return restricted
    if service._repo is None:
        return {"error": "No repository attached"}
    if service.review.active:
        return {
            "error": (
                "Reset is disabled during a review. The reviewed changes are "
                "staged against the merge-base, so a hard reset would discard "
                "the branch's content from your working tree. Exit the review "
                "first."
            )
        }
    try:
        service._repo.reset_hard()
    except Exception as exc:
        return {"error": str(exc)}

    event_text = (
        "**Reset to HEAD** — all uncommitted changes have been discarded."
    )
    service._broadcast_soon(Event("filesModified", [], turn_scoped=False))
    return {"status": "ok", "system_event_message": event_text}
