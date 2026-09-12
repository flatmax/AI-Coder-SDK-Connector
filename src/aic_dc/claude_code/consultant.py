"""Claude as a one-shot consultant, so [AG-1]'s asymmetry closes.

``agy`` and ``antigravity`` could each be master *and* consultant. ``claude``
could only ever be master, which meant a session driven by Google's engine had
no second opinion to reach for at all. This module is the missing half: one
question in, prose out, no history, no second turn, no tools.

The contract is [AG-16]'s, unchanged — a consultation is not a session, and
the reason it is not is that a consultant which can browse the repository is
no longer independent. Two agents disagreeing about a diff is information;
one agent given a second chance to read the same files is not. So ``context``
is how the caller supplies what it already read, and there is no other way in.

**What makes that true here is two mechanisms, not one**, and
[AG-25](../../../specs5/plan-ag/decisions.md#ag-25) is the record of why.
``tools=[]`` removes the tool registry — and note that it is ``tools``, not
``allowed_tools``: the latter with an empty list emits no flag at all
(``subprocess_cli.py:597`` guards it with ``if effective_allowed_tools:``) and
leaves the CLI's defaults intact, which is a consultant that looks gated and
is not. ``setting_sources=[]`` is the documented control for settings files
and ``CLAUDE.md``. Measured, it also suppresses the git environment block —
but that is *undocumented* behaviour on one SDK version, so the sterile
working directory below is a second, independent mechanism rather than
belt-and-braces. Neither is load-bearing alone, because this directory's
recurring defect is the one where something quietly stops working and nothing
says so.

**What still crosses the boundary**, measured rather than assumed: the
consultant is told the account's email address and today's date. That is not
git-derived — it survives a sterile ``cwd`` with ``GIT_CONFIG_GLOBAL`` and
``GIT_CONFIG_SYSTEM`` pointed at ``/dev/null`` — so no environment hygiene
reaches it. It is recorded rather than mitigated.

Reaching this from an ``agy`` master is a separate deliverable: it needs the
authenticated HTTP MCP listener of
[AG-22](../../../specs5/plan-ag/decisions.md#ag-22), and the per-turn quota of
[AG-R-24](../../../specs5/plan-ag/risks.md#ag-r-24). Rendering it inline in
the browser needs a translator, which is a third. This module is the
consultant itself and is usable without either.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where scratch working directories live, under the config directory this
#: app already owns. The name matters more than it looks: the directory must
#: be **outside any repository tree**, because ``cwd`` alone does not stop
#: git — it walks up until it finds a ``.git``, and a scratch directory
#: created inside the repository is found immediately. ``GIT_CEILING_DIRECTORIES``
#: was measured as no help here: with the ceiling set to a subdirectory of a
#: repository, ``git rev-parse --show-toplevel`` still resolved the parent.
SCRATCH_DIR = "claude-consultations"

#: Model left to the CLI's own default. The consultant is prose-only and the
#: master's model is the user's choice, not ours to mirror.
DEFAULT_MODEL: str | None = None

#: Long enough for the longest consultation measured (20.3s on a 2700-character
#: answer) with room for a slow one, and **short enough to land inside the
#: 3m0s deadline ``agy``'s MCP client enforces** — measured exactly, see
#: [AG-24]. ``agy`` adds roughly 11s of its own turn overhead around a tool
#: call, so 150s leaves the error path time to return a real answer rather
#: than having the client give up first. A timeout we raise says what
#: happened; one the client raises says only that we were slow.
DEFAULT_TIMEOUT_SECONDS = 150.0

#: What the consultant is told about its own situation.
#:
#: It exists for framing, not for safety. A reviewer held that ``tools=[]``
#: would leave the CLI's base prompt commanding file inspection, so the model
#: would refuse with *"I don't have access to tools to read the codebase"*
#: unless our prompt completely overwrote it. Measured both ways and refuted:
#: with **no system prompt at all** the model still answered in full prose in
#: one turn. So this text buys answer quality — it tells the model to judge
#: what it was given instead of asking for more — and nothing else. It is not
#: a mitigation and must not be relied on as one.
SYSTEM_PROMPT = (
    "You are a second opinion inside another agent's session, not a session "
    "of your own. This is one question and one answer: there is no next turn "
    "to ask a follow-up in, and you have no tools, no repository and no way "
    "to read a file. Everything you are going to know is in the message.\n\n"
    "Judge what you were given. Where it is enough to answer, answer "
    "directly and commit to a position — the caller has its own view already "
    "and wants yours to compare, so agreement stated plainly is as useful as "
    "disagreement. Where it is genuinely not enough, say which specific thing "
    "you would need and what you would conclude either way, rather than "
    "declining. Prose, no preamble."
)


class ConsultationError(RuntimeError):
    """A consultation that could not produce an answer.

    Prose rather than a code, for the same reason the ``agy`` consultant's is:
    it is read by a model deciding what to do next, and the thing we want it
    to do next is carry on without the second opinion rather than retry.
    """


@contextlib.contextmanager
def sterile_cwd(config_dir: Path | str, *, prefix: str = "c-") -> Iterator[Path]:
    """An empty directory for the length of one consultation.

    Empty is the whole point, and so is *where* it is empty. The reviewer who
    first asked for this argued it on latency and on leaked paths; both were
    measured and neither survived — a real repository costs five extra ``git``
    invocations, all of them sub-10ms on a 1860-commit tree, and a consultant
    run inside one with ``setting_sources=[]`` answered ``NO GIT CONTEXT`` to
    a question about its own branch, with a nonce in ``CLAUDE.md`` it never
    quoted.

    It is here because ``setting_sources`` is documented to govern settings
    files and ``CLAUDE.md`` and says nothing about the git environment block.
    Suppressing that block too is emergent behaviour on one SDK version, free
    to change in a patch release, and if it changes the symptom is a
    consultant quietly receiving this repository's state.

    Removed in a ``finally``. There is nothing in it to lose: the consultant
    cannot write, so anything found here afterwards came from the CLI itself.
    """
    parent = Path(config_dir) / SCRATCH_DIR
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield root
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            logger.warning("Could not remove the consultation scratch %s: %s", root, exc)


def sweep_scratch(config_dir: Path | str) -> int:
    """Remove scratch directories a previous process left behind.

    :func:`sterile_cwd` removes its own, so the only way one survives is a
    process that never ran the ``finally`` — ``SIGKILL``, a power cut, an
    ``os._exit``. Nothing else collects those. Unfiltered by age deliberately:
    age is not the question, ownership is, and every directory under here
    belongs to a consultation that is over by definition once this process is
    the one doing the sweeping.
    """
    parent = Path(config_dir) / SCRATCH_DIR
    if not parent.is_dir():
        return 0
    removed = 0
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            logger.warning("Could not sweep the consultation scratch %s: %s", child, exc)
        else:
            removed += 1
    return removed


def isolation_env() -> dict[str, str]:
    """The environment overrides that keep git out of the consultation.

    **Overrides, not deletions**, and that is a property of the SDK rather
    than a choice: ``subprocess_cli.py`` builds ``inherited_env`` from
    ``os.environ`` and spreads ``options.env`` across it, so a variable here
    can be given a new value but cannot be removed. ``GIT_DIR=""`` is enough —
    measured, git reports ``fatal: not a git repository: ''`` rather than
    falling back to a search, so an empty value neutralises an inherited one
    and costs nothing when there was none.

    ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM`` do **not** remove the
    account email from the consultant's context — that was measured and comes
    from the authenticated profile, not from git. They are set because the
    identity in a *repository-local* config is a different one, and a sterile
    ``cwd`` is what actually keeps that out of reach.
    """
    return {
        "GIT_DIR": "",
        "GIT_WORK_TREE": "",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


class ClaudeConsultant:
    """One-shot ``second_opinion`` on Claude, through ``claude_agent_sdk.query()``.

    ``query()`` rather than ``ClaudeSDKClient`` because a consultation is one
    process for one call. A client exists to hold a conversation open across
    turns, and holding one open here would create exactly the thing
    [AG-16] says a consultation is not.
    """

    def __init__(
        self,
        config_dir: Path | str | None = None,
        *,
        model: str | None = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cli_path: Path | str | None = None,
    ) -> None:
        self._config_dir = Path(config_dir or Path.home() / ".config" / "aic-dc")
        self._model = model
        self._timeout = float(timeout)
        # The engine's CLI, not whatever is first on ``PATH``. This app
        # bundles one, and a consultant answering from a different build
        # than the master is a difference nobody would think to look for.
        self._cli_path = Path(cli_path) if cli_path else None
        self._task: asyncio.Task[Any] | None = None
        self._cancelled = False
        # Litter from a process that was killed mid-consultation, taken out
        # here for the reason the ``agy`` consultant takes its own out here:
        # this is the first place in the program that knows where it lives.
        sweep_scratch(self._config_dir)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Whether a consultation could run at all.

        The SDK is imported lazily here for the reason the rest of this
        package does it: these modules must stay importable, and unit
        testable, on a machine where the CLI is not installed.
        """
        return self._unavailable_reason() is None

    def _unavailable_reason(self) -> str | None:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return (
                "The Claude Agent SDK is not installed, so this install "
                "offers no Claude second opinion."
            )
        if self._resolve_cli() is None:
            return (
                "The Claude CLI could not be found, so this install offers "
                "no Claude second opinion. It is the same binary the engine "
                "runs; if the engine works, this is a packaging fault rather "
                "than a missing credential."
            )
        return None

    def _resolve_cli(self) -> Path | None:
        """The CLI this consultation would run, or ``None`` if there is none.

        Explicit path first, then the bundled one, then ``PATH`` — the same
        order the engine resolves in, so the two cannot disagree quietly.
        """
        if self._cli_path is not None:
            return self._cli_path if self._cli_path.exists() else None
        from aic_dc.claude_code.cli_surface import bundled_cli_path

        bundled = bundled_cli_path()
        if bundled is not None and Path(bundled).exists():
            return Path(bundled)
        found = shutil.which("claude")
        return Path(found) if found else None

    # ------------------------------------------------------------------
    # The consultation
    # ------------------------------------------------------------------

    async def second_opinion(
        self,
        question: str,
        context: str = "",
        observer: Callable[[str], Any] | None = None,
    ) -> str:
        """Ask Claude one question and return its answer as text.

        ``observer`` is called with each text chunk as it arrives, and is
        exactly that — an observer. *No sink may be load-bearing; every sink
        is an observer, and the result never depends on one.* An exception
        raised by one is logged and swallowed here, because a browser tab
        that went away must not be able to fail a consultation the caller is
        still waiting on.
        """
        question = (question or "").strip()
        if not question:
            raise ConsultationError("A second opinion needs a question to answer.")

        reason = self._unavailable_reason()
        if reason is not None:
            raise ConsultationError(reason)

        prompt = question if not context.strip() else f"{question}\n\n{context.strip()}"

        self._cancelled = False
        self._task = asyncio.current_task()
        try:
            return await asyncio.wait_for(self._run(prompt, observer), self._timeout)
        except TimeoutError as exc:
            raise ConsultationError(
                f"The Claude consultation did not finish within "
                f"{self._timeout:.0f}s and was stopped."
            ) from exc
        except asyncio.CancelledError:
            if self._cancelled:
                raise ConsultationError("The consultation was stopped.") from None
            raise
        finally:
            self._task = None

    async def _run(self, prompt: str, observer: Callable[[str], Any] | None) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        chunks: list[str] = []
        terminal: str | None = None
        turns: int | None = None

        with sterile_cwd(self._config_dir) as scratch:
            options = ClaudeAgentOptions(
                # AG-25: `tools`, never `allowed_tools`. An empty
                # `allowed_tools` emits no flag and gates nothing.
                tools=[],
                setting_sources=[],
                cwd=str(scratch),
                env=isolation_env(),
                system_prompt=SYSTEM_PROMPT,
                # Measured at 1 without it. Set anyway, because the thing
                # that would raise it is the model reaching for a tool it
                # does not have, and that is the case where we would rather
                # stop than pay for a second lap.
                max_turns=1,
                **({"model": self._model} if self._model else {}),
                **({"cli_path": str(cli)} if (cli := self._resolve_cli()) else {}),
            )
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            _notify(observer, block.text)
                elif isinstance(message, ResultMessage):
                    terminal = getattr(message, "terminal_reason", None) or getattr(
                        message, "stop_reason", None
                    )
                    turns = getattr(message, "num_turns", None)

        answer = "".join(chunks).strip()
        if not answer:
            raise ConsultationError(
                "Claude returned an empty answer. The consultation ran and "
                "produced no prose "
                f"(terminal reason: {terminal!r}, turns: {turns}), which "
                "usually means the turn was spent reaching for tools it does "
                "not have here."
            )
        return answer

    async def cancel(self) -> bool:
        """Stop a running consultation. Safe when there is none.

        Killing rather than starving, for the reason the ``agy`` consultant
        gives: there is no session to lose, and starving is useless against
        a consultant that is prose by construction and already allowed
        nothing. Cancelling the awaiting task closes the ``query()``
        generator, which tears down the CLI subprocess with it.
        """
        task = self._task
        if task is None or task.done():
            return False
        self._cancelled = True
        task.cancel()
        return True


def _notify(observer: Callable[[str], Any] | None, text: str) -> None:
    """Hand a chunk to the observer, and never let it break the consultation."""
    if observer is None:
        return
    try:
        observer(text)
    except Exception:  # noqa: BLE001 - an observer is never load-bearing
        logger.exception("A consultation observer raised; the answer is unaffected")
