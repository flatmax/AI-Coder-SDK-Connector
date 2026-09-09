"""One long-lived ``agy`` process, and the turns driven through it.

The last piece of [AG-14](../../../specs5/plan-ag/decisions.md#ag-14):
spawn, read ``init``, claim the conversation, then feed prompts and pump
the stream. The handshake is not a design choice — it is forced, and the
order is proved in ``scripts/probe_agy_gate.py``:

    spawn → read `init` → claim → prompt

A conversation id is unknown before ``init``, and a tool call cannot
precede the first prompt, so there is exactly one window in which to take
ownership. Claim late and the first tool call is waved through as somebody
else's session; claim early is impossible.

Cancellation is where this transport is genuinely weaker
========================================================
**There is no halt frame.** The input protocol accepts one event —
``user`` — and the binary answers anything else with *"unsupported stream
input message event"*. The SDK transport has ``conversation.cancel()``,
which sends a real ``halt_request``; there is no counterpart here.

What there *is* instead is the gate. Every tool call on this transport
passes through :mod:`~aic_dc.agy.gate_server`, so ⏹ can
**starve** a turn: from the moment it is pressed, every call is refused
with a reason naming the user's stop. The agent reads those refusals and
winds down, which is the same mechanism the Claude adapter already relies
on — ``cancel_streaming`` denies the turn's open permissions *before* it
interrupts, precisely because a released dialog is what makes an interrupt
actionable.

It is weaker in one stated way and the limit is worth knowing rather than
discovering: **a turn producing only prose cannot be starved**, because it
is asking permission for nothing. That turn runs to its own end.
Terminating the process would stop it and would also end the session, so
this does not do that silently — :meth:`cancel` starves, and
:meth:`close` is the separate, explicit act of ending the session.

Governing spec: ``specs5/plan-ag/`` — AG-14; ``sdk-surface.md``
§ *The stream, measured in bidirectional mode*.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.agy.steps import AgyTranslator, subagent_entries, unwrap
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

#: How long to wait for the ``init`` frame before giving up. Generous:
#: ``agy`` authenticates from the keyring and may refresh a token first,
#: and the failure this guards against is a hang, not a slow start.
INIT_TIMEOUT_SECONDS = 120.0

#: Passed to ``--print-timeout``. It bounds the whole turn, so it must
#: outlast any permission dialog — the default is 5m, which a user reading
#: a diff can exceed without trying.
PRINT_TIMEOUT = "12h"


class AgyNotInstalledError(RuntimeError):
    """``agy`` is not on PATH. A named failure rather than a traceback."""


class TurnInProgressError(RuntimeError):
    """One turn at a time, and the second one is refused rather than queued."""


class PromptNotSentError(RuntimeError):
    """The process would not take the prompt, so the turn never started.

    Distinct from a turn that failed: nothing ran, and nothing was
    written. :meth:`AgySession.stream_turn` converts it into the system
    event a browser can render; a Python caller catches it.
    """


class AgySession:
    """A conversation held open across turns.

    One process, not one per turn: ``agy`` reads newline-delimited prompts
    from stdin and keeps its context between them, which is the whole
    reason for driving it bidirectionally rather than with ``-p``.
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        gate: AgyGateServer,
        model: str | None = None,
        executable: str = "agy",
        resume: str | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._gate = gate
        self._model = model
        self._executable = executable
        self._resume = resume or None
        self._proc: Any = None
        self._conversation_id: str | None = None
        self._turn_active = False
        self._cancelled = False

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def read_only(self) -> bool:
        """Whether this session can change the working tree.

        Part of the session contract ``AntigravityService`` reads —
        ``get_current_state`` and ``get_engine_status`` both do
        ``session.read_only``, and ``AgyService`` inherits both. Without
        this property **the whole app-state load failed** for an `agy`
        session with ``'AgySession' object has no attribute 'read_only'``,
        which is not a degraded panel but a browser that cannot render the
        engine at all. Reported from a live run on 2026-09-05.

        The answer is the gate, which is this transport's counterpart to
        the SDK's decide hook: ``agy`` runs with
        ``--dangerously-skip-permissions``, so its own headless layer is
        not gating anything and :class:`~aic_dc.agy.gate_server.AgyGateServer`
        is the only thing between the model and the tree. No gate would
        mean no way to review a write, and the honest answer then is that
        this session must not make them.
        """
        return self._gate is None

    def _argv(self) -> list[str]:
        argv = [
            self._executable,
            # `--print` takes a value, so the `=` form is required: the
            # bare flag silently swallows the next argument as the prompt.
            "--print=",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            # **The repository, as the agent's own working directory.**
            #
            # Not decoration, and `cwd=` on the subprocess is not a
            # substitute for it. Measured 2026-09-05 with everything else
            # held constant — same parent, same `git init`, same process
            # cwd — the tools run in different places:
            #
            #     with --add-dir : pwd -> /tmp/temp/wstest
            #     without        : pwd -> ~/.gemini/antigravity-cli/scratch
            #                      git rev-parse -> not a git repository
            #
            # So without this the agent is not in the user's repo at all.
            # It is in `agy`'s own scratch directory, with no git and no
            # project — and `agy`'s system prompt tells it that when it
            # needs somewhere to write, that scratch directory is the
            # place, and to suggest the user adopt it as their workspace.
            # Which is exactly what it did: asked to "create a helloworld
            # script" it wrote one to `scratch/hello_world/hello.py` and
            # reported success with a file:// link, because that *was* its
            # working directory.
            #
            # That is [AG-R-3](../../../specs5/plan-ag/risks.md#ag-r-3),
            # whose cause had been guessed at four times — trustedWorkspaces,
            # git-ness, emptiness, concurrency — and disproven four times.
            # Nothing was ever diverted. The agent wrote where it was, and
            # nobody had told it where to be.
            #
            # One directory, never a list: AG-10 is one repo root and one
            # working tree, and a second would give the diff viewer and
            # the file tree paths they cannot resolve.
            "--add-dir", str(self._repo_root),
            # The gate is the only gate on this transport. agy's own
            # headless layer auto-denies rather than asking, and would
            # refuse the turn before our hook ever ran. This removes a gate
            # that cannot ask, in favour of one that can.
            "--dangerously-skip-permissions",
            "--print-timeout", PRINT_TIMEOUT,
        ]
        if self._model:
            argv += ["--model", self._model]
        if self._resume:
            # Phase 5. `agy` restores the conversation's context itself,
            # from its own store — nothing here replays our mirror into a
            # prompt. The id is the one the `init` frame gave us when the
            # conversation was created, which is the same id the mirror
            # filed the transcript under.
            argv += ["--conversation", self._resume]
        return argv

    async def start(self) -> str:
        """Spawn, read ``init``, and claim the conversation. Returns its id.

        Everything up to the claim happens before any prompt is sent, which
        is what makes the gate correct: by the time a tool call can exist,
        the hook already knows the conversation is ours.
        """
        if self._proc is not None:
            return self._conversation_id or ""

        await self._gate.start()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                cwd=str(self._repo_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgyNotInstalledError(
                f"{self._executable!r} is not on PATH, so the agy transport "
                "cannot start. Install the Antigravity CLI or choose another "
                "engine."
            ) from exc

        # stderr is drained continuously rather than read at the end: a
        # full pipe blocks the child, and agy is chatty enough on stderr to
        # fill one. The output is logged, never parsed.
        asyncio.ensure_future(self._drain_stderr())

        try:
            async with asyncio.timeout(INIT_TIMEOUT_SECONDS):
                while True:
                    frame = await self._read_frame()
                    if frame is None:
                        raise RuntimeError(
                            "agy exited before sending its init frame"
                        )
                    init = unwrap(frame, "init")
                    if init is not None or frame.get("event") == "init":
                        self._conversation_id = str(
                            frame.get("conversation_id") or ""
                        )
                        break
        except TimeoutError as exc:
            await self.close()
            raise RuntimeError(
                f"agy sent no init frame within {INIT_TIMEOUT_SECONDS:.0f}s"
            ) from exc

        if not self._conversation_id:
            await self.close()
            raise RuntimeError("agy's init frame carried no conversation id")

        if self._resume and self._conversation_id != self._resume:
            # A resume that quietly became a new conversation is the one
            # failure worth refusing to start over: the user asked to
            # continue, the context is gone, and nothing downstream would
            # say so — the turn would simply behave as though the agent had
            # forgotten everything. Reported as an error the caller can
            # show, with the id agy actually opened, since that is the
            # session the work would otherwise have gone into.
            opened = self._conversation_id
            await self.close()
            raise RuntimeError(
                f"agy was asked to resume conversation {self._resume} and "
                f"opened {opened} instead, so its context is not the one "
                f"that was asked for."
            )

        # The one window. After this the hook recognises our calls; before
        # it, it would pass them through as a stranger's.
        self._gate.claim(self._conversation_id)
        return self._conversation_id

    async def _read_frame(self) -> dict[str, Any] | None:
        """One NDJSON frame, or ``None`` at end of stream.

        A line that will not parse is skipped rather than fatal: this is a
        CLI that prints, and one stray non-JSON line should not end a
        conversation.
        """
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None
            try:
                frame = json.loads(line.decode("utf-8"))
            except ValueError:
                logger.debug("Skipping a non-JSON line from agy: %r", line[:200])
                continue
            if isinstance(frame, dict):
                return frame

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                logger.debug("agy stderr: %s", line.decode("utf-8").rstrip())
        except Exception:  # noqa: BLE001 - draining must not kill a turn
            logger.debug("agy stderr drain ended", exc_info=True)

    async def stream_frames(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        """Send one prompt and yield its raw frames until ``result``.

        The reader, with no rendering in it. :meth:`stream_turn` is this
        plus a translator and the turn's close; the consultant
        (:mod:`aic_dc.agy.consultant`, AG-16) is this plus a translator
        and *no* close, because a consultation is not a turn — emitting a
        ``streamComplete`` for one would end the user's Claude turn in the
        browser, which is holding the tool call the consultation is
        answering.

        One reader rather than two: the frame loop, the turn latch and the
        gate's ``resume`` are the parts that must not diverge between the
        two callers, and a second copy of them is how the SDK transport's
        two fetch paths quietly stopped being copies.

        Raises rather than reporting when the prompt cannot be sent. The
        event-shaped version of that failure belongs to :meth:`stream_turn`,
        whose caller is a browser waiting on a stream; a consultation's
        caller is a Python ``await`` that can be told with an exception.
        """
        if self._proc is None:
            raise RuntimeError("the agy session has not been started")
        if self._turn_active:
            raise TurnInProgressError(
                "A turn is already running on this session. Stop it before "
                "sending another."
            )
        self._turn_active = True
        self._cancelled = False
        self._gate.resume()
        try:
            try:
                self._proc.stdin.write(
                    json.dumps(
                        {
                            "event": "user",
                            "message": {"role": "user", "content": prompt},
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                await self._proc.stdin.drain()
            except (
                BrokenPipeError,
                ConnectionResetError,
                RuntimeError,
                OSError,
            ) as exc:
                logger.warning("agy would not accept a prompt; the process is gone")
                raise PromptNotSentError(
                    "The agy process is no longer running, so the turn was "
                    "not sent. Restart the session."
                ) from exc

            while True:
                frame = await self._read_frame()
                if frame is None:
                    logger.warning("agy's stream ended mid-turn")
                    break
                # Before the yield, and before the translator: this is the
                # earliest instant this process can act on the frame, and
                # what it buys is the gate closing over the subagent
                # sooner. See `_gate_subagents`.
                self._gate_subagents(frame)
                yield frame
                if frame.get("event") == "result":
                    break
        finally:
            self._turn_active = False

    def _gate_subagents(self, frame: dict[str, Any]) -> None:
        """Claim a subagent's conversation, so its tool calls reach the gate.

        **The hole this closes.** ``agy`` runs under
        ``--dangerously-skip-permissions``, so this host's gate is the only
        thing between the model and the tree — and the hook routes to it by
        *conversation id*, passing through everything unclaimed, which is
        what keeps a second session of the user's own out of our dialog
        (``probe_agy_isolation.py``). A subagent is given a conversation of
        its own. So until this existed, a delegation was a route around the
        permission dialog: ``invoke_subagent`` raised a dialog, the user
        approved *the spawn*, and every call the subagent then made — reads,
        commands and writes into the repository — was passed through
        unreviewed. Measured on 2026-09-09 by
        ``scripts/probe_agy_subagent_gate.py``, which denied everything but
        the delegation and watched the subagent's edit land anyway.

        AG-5's table puts the spawners in the *still asks* column because "a
        subagent inherits the tool set". It does inherit it; what it did not
        inherit was the gate.

        **Claimed on any announcement, and never released here.** The first
        cut read ``state`` the obvious way — claim on ``ACTIVE``, release on
        ``DONE`` — and the live re-run failed exactly as the unfixed code
        had. ``DONE`` on a ``subagent`` step means **the launch finished**,
        not the subagent: measured at ``duration_seconds: 0.10`` on a
        delegation whose work then ran for six more seconds, over frames
        the parent spent waiting on it. Some runs announce ``ACTIVE`` and
        ``DONE``, some only ``DONE``, so a release keyed on that word
        un-gates a subagent that is still working — and on the run that
        never says ``ACTIVE``, releases a claim that was never made.

        The subagent therefore stays claimed for the life of the session
        and is released by :meth:`AgyGateServer.stop`. Holding a claim
        costs one registry file; dropping one early costs the review.

        **The residue, stated rather than papered over:** ``agy`` spawns
        the child before it announces it, so a tool call made in that
        window reaches the hook before the claim is on disk and passes
        through. This side cannot close it — ``invoke_subagent``'s own
        arguments carry no conversation id, because the child does not
        exist when the dialog for the spawn is answered — and the honest
        mitigations are upstream: a parent id on the hook payload, or a
        way to spawn pre-claimed.
        """
        step = unwrap(frame, "step_update")
        if step is None or str(step.get("step_type") or "") != "subagent":
            return
        for entry in subagent_entries(step):
            child = str(entry.get("conversation_id") or "")
            if not child or child == self._conversation_id:
                continue
            try:
                self._gate.claim(child)
            except Exception:  # noqa: BLE001 - a turn must not die on this
                # Logged loudly: a claim that failed is a subagent running
                # ungated, which is the condition this method exists to
                # prevent and must not be silent.
                logger.exception(
                    "Could not claim the subagent conversation %s; its tool "
                    "calls will not reach the permission gate",
                    child,
                )

    async def stream_turn(
        self, prompt: str, *, translator: AgyTranslator
    ) -> AsyncIterator[Event]:
        """Send one prompt and yield its events until the ``result`` frame.

        Ends on ``result``, or on end-of-stream if the process died. Both
        close the turn out through the translator, because with no RPC
        reply left to carry a failure the event stream is the only channel
        there is — the lesson the SDK transport learned the hard way.
        """
        frames = self.stream_frames(prompt)
        try:
            async for frame in frames:
                for event in translator.translate(frame):
                    yield event
        except PromptNotSentError as exc:
            # Reported as an event rather than raised, and then closed out
            # below: the browser is waiting on this stream and an exception
            # here would leave it spinning with no explanation.
            yield Event(
                "systemEvent",
                {
                    "subtype": "engine_error",
                    "data": {"message": str(exc)},
                },
            )
        finally:
            # **Closed explicitly, and this is not tidiness.** A caller
            # that stops early — ⏹ is exactly that — closes *this*
            # generator, which raises `GeneratorExit` at the yield above
            # and leaves the inner one suspended, its `finally` waiting on
            # a garbage collection that has not happened. The turn latch it
            # clears would still be set, so the next `stream_turn` would be
            # refused with `TurnInProgressError` on a session with no turn
            # running. Found by the test that stops a turn and starts
            # another; before the reader was split there was one generator
            # and no inner one to forget.
            await frames.aclose()

        for event in translator.stream_complete():
            yield event

    async def cancel(self) -> None:
        """Stop the turn by starving it. See the module docstring.

        There is no halt frame on this transport, so ⏹ refuses every
        subsequent tool call with a reason naming the user's stop. The
        agent reads those and winds down. A turn producing only prose
        cannot be starved and runs to its own end; that is stated rather
        than papered over, and it is why this does not kill the process —
        doing so would end the whole session to stop one turn.
        """
        if not self._turn_active:
            return
        self._cancelled = True
        self._gate.refuse_all(
            "The user stopped this turn in AIC-DC. Do not continue, and do "
            "not try another way of making this change."
        )

    async def close(self) -> None:
        """End the session. Never raises."""
        proc, self._proc = self._proc, None
        self._turn_active = False
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("agy stdin would not close", exc_info=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (TimeoutError, Exception):  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    logger.debug("agy would not terminate", exc_info=True)
        # The gate goes last, and releases ownership before closing its
        # socket — see AgyGateServer.stop.
        await self._gate.stop()
