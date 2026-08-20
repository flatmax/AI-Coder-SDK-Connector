"""Commit and reset — the two git writes AC⚡DC still performs itself.

The agent has its own ``Bash`` and can run ``git commit`` when asked to.
These two entry points exist for the buttons in the shell's git menu,
which are the user's own hands rather than the agent's, and they keep
working exactly as they did before the conversion.

What changed is where the commit message comes from. It used to be a
blocking provider call against a separately-configured "smaller model".
Now it is a **stateless one-shot** through :func:`claude_agent_sdk.query`
— a second, short-lived CLI process with no tools, no settings sources,
no thinking and one turn, which cannot touch the repository and cannot
see the chat session. Its one loan from the environment is
``settings.json``, handed over as a file rather than a source, because
that is where the machine names its provider and a CLI without one cannot
answer at all (:func:`_one_shot_options`). The smaller model came back
too, as ``engine.json``'s ``commit_model``: the call is still an auxiliary
one, and a diff-to-paragraph turn does not need what a conversation
needs. It is deliberately not routed through the live
:class:`~ac_dc.claude_code.session.EngineSession`: sending a diff to the
chat session would put the whole staged diff in the conversation the user
is having, and would deadlock behind a turn in flight.

Three entry points:

- :func:`commit_all` — the RPC. Gates, guards, and returns
  ``{"status": "started"}`` immediately; the work runs as a background
  task and reports through ``commitResult``.
- :func:`commit_all_background` — the pipeline: stage, generate, commit,
  broadcast.
- :func:`reset_to_head` — no model call, and the one write here that
  destroys work rather than recording it.

Both leave a line in ``.ac-dc4/events.jsonl``. Neither belongs in the
engine's transcript — the CLI never hears about a commit — so this is the
half of history AC⚡DC owns, interleaved back into the browsed conversation
by timestamp (``specs5/3-engine/history.md``).

Governing spec: ``specs5/3-engine/session.md``; the message prompt itself
is ``config/commit.md``, unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ac_dc.claude_code.events_log import commit_content, reset_content
from ac_dc.claude_code.health import user_settings_file
from ac_dc.claude_code.messages import Event

if TYPE_CHECKING:
    from ac_dc.claude_code.service import ClaudeCodeService

logger = logging.getLogger(__name__)

# A staged diff larger than this is not sent to the model. The ceiling is
# in characters because we no longer have a tokenizer to count with, and
# the point is to fail with a sentence the user can act on rather than
# after a 30-second round trip that returns a context-length error.
MAX_DIFF_CHARS = 400_000

# How much of a failure's own words the toast carries. A reason is either
# the CLI's one-line refusal or an exception string, and the second can be
# a paragraph; the server log has the whole of it either way.
MAX_REASON_CHARS = 200

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
        # Which files the commit will contain, read while they are still
        # staged — after the commit the index is clean and the answer is
        # gone. The reader is named for the review because that is what it
        # was written for; what it actually reports is `git diff --cached`,
        # which is this commit.
        staged = [str(entry.get("path") or "") for entry in _staged_files(repo)]
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

        message, reason = await generate_commit_message(service, diff)
        if message is None:
            # No fallback message. The user asked for a generated one, and
            # committing "chore: update files" instead hides the failure
            # inside permanent history.
            #
            # The reason is carried into the toast because the one-shot is a
            # second CLI process the health panel does not describe: when it
            # answered "Not logged in · Please run /login", the panel — which
            # reads the *live* session's credentials — had nothing wrong to
            # show, and the generic sentence sent the user to look at it
            # anyway.
            detail = f": {reason}" if reason else ""
            await service._broadcast(
                Event(
                    "commitResult",
                    {
                        "error": (
                            f"Could not generate a commit message{detail}. The "
                            "staged changes are untouched — try again, or "
                            "commit with a message of your own."
                        )
                    },
                    turn_scoped=False,
                )
            )
            return

        result = repo.commit(message)
        event_text = commit_content(result["sha"][:7], result["message"])
        # The archive, not the transcript. A commit is ours: the engine's
        # transcript never hears about one, so it goes in `events.jsonl` and
        # the browser interleaves it back by timestamp
        # (``specs5/3-engine/history.md`` § One Store, One Index, One Events
        # Log). One wording for both the live toast and the archived line —
        # `commit_content` — because a user comparing the two is entitled to
        # find the same sentence.
        await service._record_event(
            "commit",
            event_text,
            payload={
                "sha": result["sha"],
                "message": result["message"],
                "files": staged,
            },
        )
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
) -> tuple[str | None, str | None]:
    """Ask a throwaway one-shot session for a commit message.

    Returns ``(message, reason)`` with exactly one of them set. A ``None``
    message is a hard error to the caller: there is no fallback message,
    by design. The reason is for the user, not the log — a one-line
    account of what went wrong, which the caller puts in the toast.

    The option set is minimal on purpose, and each omission is load-bearing:

    - ``tools=[]`` — no tools at all, so nothing can read or write the
      repository. There is no permission gate on this session and it does
      not need one.
    - ``setting_sources=[]`` — no ``CLAUDE.md``, no skills, no agents, no
      MCP servers. The message format is ours (``config/commit.md``) and a
      project instruction file should not silently redefine it, nor should
      a diff paid for by the user drag a plugin's startup cost along. The
      provider selection those sources also carry is handed over
      separately — see :func:`_one_shot_options`.
    - ``max_turns=1`` — one response. With no tools there is nothing to
      iterate on.
    """
    prompt = service._config.get_commit_prompt()
    options = _one_shot_options(service, prompt)
    if options is None:
        return None, "the SDK surface moved; see the server log"

    try:
        from claude_agent_sdk import AssistantMessage, TextBlock, query
    except ImportError as exc:  # pragma: no cover - the SDK is a hard dep
        logger.error("claude-agent-sdk is not importable: %s", exc)
        return None, "the claude-agent-sdk is not importable"

    parts: list[str] = []
    # What the CLI said when it answered with an error rather than a
    # message. Kept apart from `parts`, because that answer is a diagnosis
    # and committing it would put "Not logged in · Please run /login" in
    # git history — the CLI marks such a turn with `error` and a
    # `<synthetic>` model, and the text reads like prose either way.
    refusal: str | None = None
    try:
        async with asyncio.timeout(GENERATE_TIMEOUT_SECONDS):
            async for message in query(prompt=diff, options=options):
                if not isinstance(message, AssistantMessage):
                    continue
                text = "".join(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
                # `getattr` because the field arrived in a recent SDK and
                # an older wheel would otherwise turn a failed turn into
                # an AttributeError here.
                error = getattr(message, "error", None)
                if error:
                    logger.warning(
                        "The commit-message one-shot answered with an error "
                        "(%s): %s",
                        error,
                        text.strip() or "(no text)",
                    )
                    refusal = text.strip() or str(error)
                    continue
                parts.append(text)
    except TimeoutError:
        logger.warning(
            "Commit-message generation timed out after %.0fs",
            GENERATE_TIMEOUT_SECONDS,
        )
        return None, f"it timed out after {GENERATE_TIMEOUT_SECONDS:.0f}s"
    except Exception as exc:
        logger.warning("Commit-message generation failed: %s", exc)
        # The refusal first: when the CLI has explained itself, the
        # exception that follows is the SDK reporting the same failure in
        # its own vocabulary ("returned an error result"), which names
        # nothing the user can act on.
        return None, _one_line(refusal or str(exc))

    message = "".join(parts).strip()
    if not message:
        logger.warning("Commit-message generation returned no text")
        return None, _one_line(refusal) if refusal else "it returned no text"
    return _strip_fence(message), None


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
        # `default`, not `plan`, and the empty tool list is why.
        #
        # Plan mode was here as a second lock on a session that already has
        # no tools to unlock. It is not free: the CLI injects a plan-mode
        # reminder telling the session not to act yet but to write a plan,
        # and that instruction competes with `commit.md` for the one turn we
        # get. A large model shrugs it off. A small one obeys it — asked for
        # a commit message in plan mode, Haiku 4.5 answered with the opening
        # of a plan file and burned 4478 output tokens on it, where the same
        # diff in `default` mode came back as one commit message in a
        # tenth of that. `tools=[]` is the lock that actually holds; this
        # was the one that cost us the answer.
        "permission_mode": "default",
        # Nothing to reason about, so nothing to reason with. The task is a
        # transcription — a diff in, a paragraph out, one turn, no choices
        # to weigh — and the CLI enables thinking by default. On a small
        # model that showed up as most of the latency and most of the
        # output tokens for no visible gain in the message. Disabling it
        # also detaches this call from the user's `effortLevel`, which
        # arrives with `settings.json` below and is set for conversations
        # rather than for this.
        "thinking": {"type": "disabled"},
    }
    # The provider, and only the provider.
    #
    # `setting_sources=[]` above is what keeps CLAUDE.md, skills, agents and
    # plugins out — and it also withheld the one thing in `settings.json`
    # this session cannot do without. Its `env` block is where a machine
    # says *which provider to talk to*: `CLAUDE_CODE_USE_BEDROCK` and a
    # region, a Vertex project, a gateway base URL. Nothing puts those in
    # the server's own environment for the CLI to inherit, so the one-shot
    # was launched with no provider at all, fell back to first-party auth it
    # had no credentials for, and answered "Not logged in · Please run
    # /login" — while the live session, which loads all three sources, was
    # working. Passing the file explicitly restores the provider without
    # restoring any of the instruction sources.
    #
    # User scope only: `settings` takes one file, and a provider is a fact
    # about the machine rather than the checkout. A repo that selects a
    # provider in `.claude/settings.json` alone is not covered here, and
    # merging the scopes ourselves would mean reimplementing the CLI's
    # precedence rules against a diff nobody would see them applied to.
    settings = user_settings_file()
    if settings is not None:
        kwargs["settings"] = str(settings)
    # The same binary the live session version-checked, when there is one.
    cli_path = service.session.health.cli_path or service.engine_config.cli_path
    if cli_path:
        kwargs["cli_path"] = cli_path
    # A smaller model than the conversation's, when `engine.json` names one.
    # Writing a commit message is not the work the session model is chosen
    # for: measured against a 19k-char diff on this machine's Bedrock
    # config, Haiku 4.5 answered in 8.2s for $0.015 where Opus 5 took 10.6s
    # for $0.109 — and the messages were of a kind. Null falls back to the
    # session's model, because a default we picked would be a full model id
    # and a full model id is provider-specific.
    model = service.engine_config.commit_model or service.engine_config.model
    if model:
        kwargs["model"] = model
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


def _one_line(reason: str | None) -> str:
    """A failure's own words, cut down to something a toast can hold.

    First line only, because the tail of a multi-line exception is stack
    context that means nothing in a toast and the server log has all of it.
    """
    text = " ".join((reason or "").split("\n")[0].split()).strip()
    if not text:
        return "no reason given"
    if len(text) > MAX_REASON_CHARS:
        return text[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return text


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


async def reset_to_head(service: ClaudeCodeService) -> dict[str, Any]:
    """Discard every uncommitted change. **Localhost only.**

    No model call, so the git work is over as soon as it returns.
    Broadcasts ``filesModified`` because every modified, staged and
    untracked file changes state at once.

    Awaits one thing: the record of what it destroyed. This is the only
    action in AC⚡DC that throws away work with no way back, so the list of
    files goes into ``events.jsonl`` *before* the reset — afterwards there
    is nothing left to ask. Recording it inline rather than as a
    fire-and-forget task means the answer is on disk before the caller is
    told the reset happened.
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
    doomed = _files_a_reset_discards(service._repo)
    try:
        service._repo.reset_hard()
    except Exception as exc:
        return {"error": str(exc)}

    event_text = reset_content()
    await service._record_event(
        "reset", event_text, payload={"to": "HEAD", "files": doomed}
    )
    service._broadcast_soon(Event("filesModified", [], turn_scoped=False))
    return {"status": "ok", "system_event_message": event_text}


def _staged_files(repo: Any) -> list[dict[str, Any]]:
    """The staged file list, or empty if git would not say.

    Never raises: a commit that succeeded must not be reported as failed
    over the file list in its history record.
    """
    try:
        return list(repo.get_review_changed_files())
    except Exception:
        logger.warning("Could not list the staged files for the commit record")
        return []


def _files_a_reset_discards(repo: Any) -> list[str]:
    """What ``git reset --hard HEAD`` is about to throw away.

    Modified, staged and deleted — **not** untracked, which a hard reset
    leaves alone. Naming untracked files as discarded would be a permanent
    record of a deletion that never happened, and this record is the only
    trace the work leaves.
    """
    try:
        status = repo.get_file_tree()
    except Exception:
        logger.warning("Could not list what the reset discards")
        return []
    files: set[str] = set()
    for key in ("modified", "staged", "deleted"):
        entries = status.get(key) or []
        files.update(str(path) for path in entries)
    return sorted(files)
