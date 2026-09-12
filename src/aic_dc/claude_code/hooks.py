"""Hooks — the three events AIC⚡DC subscribes to, all purely observational.

``PostToolUse`` is the substantial one: what to do after the agent writes a
file. ``PreCompact`` is a single broadcast, and it is here because it is the
one fact about a session that the message stream cannot carry in time — see
:func:`build_pre_compact_hook`. ``PreToolUse`` is narrower still: it fires on
the two Antigravity tools and copies down their ``tool_use_id``, which is
the only fact in this file that exists *nowhere else* — an in-process MCP
handler is called with its arguments and nothing about the call. See
:func:`build_consultation_anchor_hook`.

**``PostToolUse``.** Three things, all of them bookkeeping:

1. **Tell the browsers.** A ``filesModified`` push makes the files tab
   reload the tree, so a file the agent created appears and git-status
   badges refresh. ``Repo``'s post-write callback covers the *user's*
   edits; the CLI's ``Write`` and ``Edit`` go straight to disk and never
   pass through the repo layer, so without this hook the tree silently
   drifts from the disk for the rest of the session.
2. **Re-index it.** The symbol and doc indexes are what the MCP bridge
   serves, and an index that predates the agent's own edit is worse than
   no index: it describes code that no longer exists, confidently.
3. **Nothing else.**

``Bash`` is registered too, on a second matcher, and it is the odd one
out: it does no work at all. A shell command's ``tool_input`` is a
command line, not a file list, so the hook records only that one ran and
:meth:`Reindexer._sweep_after_shell` later asks the *disk* which known
files moved. That is CC-18's answer — the mtime the cache already keeps
per file turns out to be the attribution the tool input could not
give.

That third point is the invariant. **These hooks are observational.** They
never return a ``permissionDecision``, ``decision``, ``continue: False``,
or any other control field — an empty dict, always. The reason is
specific and easy to trip over: a ``PreToolUse`` hook that returns a
decision *shadows* ``can_use_tool``, so the CLI stops asking us and our
permission dialog silently never appears again. A hook that returned
"allow" to be helpful would ungate every gated tool in the session
without a single error message. See ``specs5/plan/sdk-surface.md``.

Since AG-28 there *is* a ``PreToolUse`` registration, which moves that
invariant from theoretical to load-bearing. It is scoped to
:data:`CONSULT_TOOL_MATCHER` precisely so the pair it could ungate is the
pair a silent ungating would cost the most: ``generate_image`` writes to
the repository and ``second_opinion`` bills a separate provider.

The re-index is debounced, because a turn that edits eight files fires
eight hooks in a few seconds and each re-index ends with two whole-index
passes (:meth:`~aic_dc.symbol_index.index.SymbolIndex.reindex_files`).
Debouncing turns that into one pass over the batch. The debounce is then
*flushable*: any MCP tool that reads an index awaits :meth:`Reindexer.flush`
first, so the agent can write a file and immediately ask for the symbol
map without a race between its own edit and its own query.

Governing spec: ``specs5/3-engine/mcp-bridge.md`` § Freshness.
Decision: ``specs5/plan/decisions.md`` § CC-7.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The tools that put bytes on disk at a path we can name.
WRITE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"

# The tool that puts bytes on disk at a path we cannot name. `PostToolUse`
# hands us the command line, not the files it touched, and guessing paths
# out of a shell line would be wrong more often than right — so this hook
# does not try. It sets a flag, and the *disk* is asked later. See
# `Reindexer.note_shell_ran` and CC-18.
SHELL_TOOL_MATCHER = "Bash"

# The consultation tools. Matched only to *read the id off the call* —
# `PreToolUse` is the one place the `tool_use_id` of an in-process MCP tool
# is visible to us, because the handler itself is handed nothing but its
# own `args` dict. See `ConsultantBridge.note_tool_use` for what the id
# then buys, and AG-28 for why the alternatives were heuristics.
CONSULT_TOOL_MATCHER = (
    "mcp__aic-dc-antigravity__second_opinion"
    "|mcp__aic-dc-antigravity__generate_image"
)

# Where each of those tools keeps the path. NotebookEdit is the odd one out,
# which is the whole reason this is a table and not a constant.
PATH_KEYS = ("file_path", "notebook_path")

# Long enough to coalesce the writes of a single multi-file edit, short
# enough that a user watching the file tree does not notice the lag. The
# flush path means a *tool* never waits this long — only a browser does.
DEBOUNCE_SECONDS = 0.6

# A flush drains the queue, then drains again for writes that landed while
# it was draining. Two rounds, not "until empty": under a stream of writes
# an unbounded loop would keep a tool call waiting indefinitely, and one
# round of staleness is a better failure than a hung turn.
MAX_FLUSH_ROUNDS = 2


# ---------------------------------------------------------------------------
# Reindexer
# ---------------------------------------------------------------------------


class Reindexer:
    """Coalesces post-write re-indexing, and can be forced to finish.

    Parameters
    ----------
    symbol_index:
        ``() -> SymbolIndex | None``, called per drain. A callable rather
        than the object because the index does not exist yet when the
        service is constructed, and the first writes may land while it is
        still being built.
    doc_builder:
        The :class:`~aic_dc.doc_index.background.DocIndexBuilder`. Its
        ``note_file_written`` is already the right entry point — extension
        gate, cache drop, re-extract, re-enqueue enrichment — and it must
        be called from the event-loop thread, because it schedules the
        enrichment task on the running loop.
    broadcast:
        ``async (Event) -> None``.
    repo_root:
        For turning the absolute paths the CLI reports into the
        repo-relative keys every index and the browser use.
    executor:
        Where the parse runs. Defaults to the loop's default pool.
    """

    def __init__(
        self,
        *,
        symbol_index: Callable[[], Any] | None = None,
        doc_builder: Any = None,
        broadcast: Callable[[Event], Awaitable[None]] | None = None,
        repo_root: str | Path | None = None,
        debounce: float = DEBOUNCE_SECONDS,
        executor: Any = None,
    ) -> None:
        self._symbol_index = symbol_index or (lambda: None)
        self._doc_builder = doc_builder
        self._broadcast = broadcast
        self._repo_root = Path(repo_root) if repo_root is not None else None
        self._debounce = debounce
        self._executor = executor

        self._pending: set[str] = set()
        # What we actually refreshed since anyone last asked. Drained by
        # `take_reindexed` into `postResponseComplete.files_reindexed`,
        # which is the frontend's only evidence that the agent's edits
        # reached the indexes.
        self._reindexed: set[str] = set()
        # Set by the `Bash` hook, cleared by the sweep in `flush`. A bool
        # rather than a queue because there is nothing to queue: the point
        # of the shell path is that we do not know what it touched.
        self._shell_ran = False
        self._timer: asyncio.Task[None] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        # Serialises drains against each other. Without it a flush racing
        # the debounce timer runs two whole-index rebuilds concurrently
        # over the same mutable index.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    def note_writes(self, paths: Iterable[str]) -> list[str]:
        """Queue repo-relative paths for re-indexing; return what stuck.

        Paths outside the repo are dropped: the indexes are keyed on
        repo-relative paths and the browser's file tree only shows the
        repo, so a write to ``/tmp`` has nothing to update. It is not an
        error — the agent is allowed to write there.
        """
        accepted = [rel for rel in (self._relative(p) for p in paths) if rel]
        if not accepted:
            return []
        self._pending.update(accepted)
        self._arm()
        return accepted

    def note_shell_ran(self) -> None:
        """Record that a shell command ran, without saying what it did.

        Deliberately not a re-index and deliberately not armed on the
        debounce timer. The objection to hooking ``Bash`` was that it
        would re-index after every ``ls`` — so an ``ls`` sets a bool and
        nothing else happens. The cost is only paid if something then
        *reads* an index, and even then only for files that really
        changed: :meth:`flush` sweeps, and the sweep asks the disk.

        Fires when a command *starts* if it was backgrounded, not when
        it finishes, so a background build's later writes belong to
        whichever shell command comes next. Recorded in
        ``specs5/2-indexing/symbol-index.md``.
        """
        self._shell_ran = True

    def _relative(self, path: str) -> str | None:
        """Repo-relative form of ``path``, or None if it is outside."""
        if not isinstance(path, str) or not path:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            # Already relative: the CLI reports absolute paths, so this is
            # a test or a caller of our own. Take it as given.
            return str(candidate).replace("\\", "/").strip("/")
        if self._repo_root is None:
            return None
        try:
            return candidate.relative_to(self._repo_root).as_posix()
        except ValueError:
            logger.debug("Write outside the repo, not re-indexed: %s", path)
            return None

    def _arm(self) -> None:
        """(Re)start the debounce timer, if there is a loop to run it on."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a synchronous caller, or a test. The queue stays
            # armed and the next flush drains it, so nothing is lost.
            return
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = loop.create_task(self._after_debounce(), name="aic-dc-reindex")

    async def _after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        # Spawned, not awaited here. `flush` and `_arm` both cancel this
        # task, and a drain awaited inside it would be cancelled along with
        # it — after it had already taken its batch off the queue, so the
        # batch would be lost and the flush would return believing the
        # index was fresh.
        self._spawn_drain()

    def _spawn_drain(self) -> None:
        """Run a drain as a task of its own, outliving the timer that armed it."""
        loop = asyncio.get_running_loop()
        # Held on the instance so it is not garbage-collected mid-flight;
        # `flush` joins on the lock rather than on this task, because a
        # drain started by an earlier timer is just as much "in flight".
        self._drain_task = loop.create_task(
            self._drain_quietly(), name="aic-dc-reindex-drain"
        )

    async def _drain_quietly(self) -> None:
        """A drain nobody awaits, so nobody would see it raise.

        On the flush path an exception belongs to the caller; here it would
        surface as asyncio's "Task exception was never retrieved" long after
        the fact, naming neither the write nor the turn.
        """
        try:
            await self._drain()
        except Exception as exc:
            logger.warning("Debounced re-index failed: %s", exc)

    # ------------------------------------------------------------------
    # Draining
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Finish any pending re-indexing before the caller reads an index.

        Cheap and safe to call on every tool invocation: with an empty
        queue it takes the lock and returns.
        """
        if self._timer is not None and not self._timer.done():
            # Safe to cancel because the timer only sleeps: a drain it
            # already started is a task of its own, and the lock below is
            # what waits for that.
            self._timer.cancel()
        # Before the queue is examined, not after: the sweep's whole job
        # is to *add* to that queue, and a flush that drained first would
        # answer from the index it was about to discover was stale.
        await self._sweep_after_shell()
        for _ in range(MAX_FLUSH_ROUNDS):
            if not self._pending:
                # An in-flight drain still has to finish — it holds the
                # index half-rebuilt — so take the lock even when the
                # queue looks empty.
                async with self._lock:
                    pass
                return
            await self._drain()
        if self._pending:
            logger.info(
                "Re-index queue still has %d file(s) after %d flush rounds; "
                "answering from a slightly stale index",
                len(self._pending),
                MAX_FLUSH_ROUNDS,
            )

    async def _sweep_after_shell(self) -> None:
        """Ask the disk what a shell command changed, and queue it.

        The other half of :meth:`note_shell_ran`, and the whole of
        CC-18's answer. ``SymbolIndex.find_stale_files`` compares each
        known file's mtime against the one the cache recorded, so no
        attribution from the command line is needed — a ``sed -i``, a
        ``git checkout``, a formatter and an ``mv`` away all look the
        same to it, which is the point.

        What it cannot see is a file the command *created*: it is in
        neither the symbol map nor the cache, so nothing holds an mtime
        to disagree with. Catching those needs a repo re-walk per sweep,
        which is the cost this approach was chosen to avoid; the gap is
        recorded in ``specs5/2-indexing/symbol-index.md``.

        Runs under the drain lock and in the executor: it is a ``stat``
        per known file, which is blocking I/O, and it reads the two
        structures ``reindex_files`` mutates.
        """
        if not self._shell_ran:
            return
        index = self._symbol_index()
        if index is None:
            # The index is still being built, and that build reads the
            # disk as it is now. Keep the flag: the first flush after it
            # exists is the one that should sweep.
            return
        # Cleared before the scan, not after. A command landing mid-scan
        # re-arms the flag and earns one redundant sweep later; clearing
        # afterwards would swallow it, and a missed change outlives the
        # session.
        self._shell_ran = False
        try:
            async with self._lock:
                loop = asyncio.get_running_loop()
                stale = await loop.run_in_executor(
                    self._executor, index.find_stale_files
                )
        except Exception as exc:
            # Same bargain as the drain: a failed sweep costs freshness,
            # and raising here would surface inside a tool call that has
            # nothing to do with indexing.
            logger.warning("Post-shell staleness sweep failed: %s", exc)
            return
        if stale:
            logger.debug(
                "Shell command changed %d indexed file(s): %s",
                len(stale),
                stale,
            )
            self._pending.update(stale)

    async def _drain(self) -> None:
        """Re-index everything queued, then hand the docs to the builder."""
        async with self._lock:
            batch = sorted(self._pending)
            self._pending.clear()
            if not batch:
                return

            index = self._symbol_index()
            if index is not None:
                try:
                    loop = asyncio.get_running_loop()
                    done = await loop.run_in_executor(
                        self._executor, lambda: index.reindex_files(batch)
                    )
                    self._reindexed.update(done or ())
                except Exception as exc:
                    # The index keeps its previous content, so the failure
                    # costs freshness rather than availability. Swallowed
                    # because the alternative is an exception surfacing
                    # inside a hook, which the CLI reports to the agent as
                    # a tool failure for a write that actually succeeded.
                    logger.warning("Post-write symbol re-index failed: %s", exc)

            if self._doc_builder is not None:
                for rel in batch:
                    # On the loop thread on purpose: note_file_written
                    # schedules its own enrichment task on the running
                    # loop, and from an executor thread it would find no
                    # loop and silently defer the keywords.
                    if self._doc_builder.note_file_written(rel):
                        self._reindexed.add(rel)

    def take_reindexed(self) -> list[str]:
        """Paths refreshed since the last call, and reset the tally.

        Take-and-clear rather than a growing list: the turn footer reports
        the writes of *that* turn, and a list that accumulated across a
        session would claim every earlier turn's files again.
        """
        done = sorted(self._reindexed)
        self._reindexed.clear()
        return done


# ---------------------------------------------------------------------------
# Hook construction
# ---------------------------------------------------------------------------


def extract_written_paths(tool_input: Any) -> list[str]:
    """The path(s) a write tool's input names, in order, deduplicated.

    Tolerant by design: this reads a dict that came from the CLI over a
    wire, and a shape we did not expect is a reason to re-index nothing,
    not to raise inside a hook.
    """
    if not isinstance(tool_input, dict):
        return []
    found: list[str] = []
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value and value not in found:
            found.append(value)
    return found


def build_post_tool_use_hook(
    reindexer: Reindexer,
    broadcast: Callable[[Event], Awaitable[None]] | None = None,
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """The ``PostToolUse`` callback: broadcast the write, queue the re-index.

    Returns ``{}`` on every path, including every failure path. A hook
    that raises is reported to the agent as a hook error against a tool
    call that succeeded, which would have it retry a write that already
    landed.
    """

    async def post_tool_use(
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        try:
            tool_name = (input_data or {}).get("tool_name")
            paths = extract_written_paths((input_data or {}).get("tool_input"))
            if not paths:
                return {}

            accepted = reindexer.note_writes(paths)
            logger.debug(
                "PostToolUse %s wrote %s; queued %s for re-index",
                tool_name,
                paths,
                accepted,
            )
            if accepted and broadcast is not None:
                # Session-wide, not turn-scoped: the tree is the same tree
                # for every watching browser, including ones that did not
                # send this turn.
                await broadcast(
                    Event("filesModified", accepted, turn_scoped=False)
                )
        except Exception as exc:
            logger.warning("PostToolUse re-index hook failed: %s", exc)
        return {}

    return post_tool_use


def build_post_shell_hook(
    reindexer: Reindexer,
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """The ``PostToolUse`` callback for ``Bash``: set a flag, do no work.

    It reads nothing out of ``tool_input`` on purpose. The command line
    is right there and parsing it is the option CC-18 rejected — every
    heuristic that recovers paths from a shell string is wrong for
    pipelines, redirects, ``xargs``, subshells and anything behind a
    script. So this hook records only *that* a command ran, and
    :meth:`Reindexer._sweep_after_shell` asks the filesystem what that
    meant.

    Takes no ``broadcast``, unlike its write-tool neighbour: it has no
    file list to push, so there is nothing the browser could be told
    beyond "something may have happened", which is not worth a reload of
    the tree. A shell command that changes the tree therefore still
    leaves the file panel stale until something else refreshes it — a
    narrower gap than the index one, and untouched here.
    """

    async def post_shell(
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        try:
            reindexer.note_shell_ran()
        except Exception as exc:
            logger.warning("PostToolUse shell hook failed: %s", exc)
        return {}

    return post_shell


def build_pre_compact_hook(
    broadcast: Callable[[Event], Awaitable[None]] | None = None,
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """The ``PreCompact`` callback: say that compaction *may* be starting.

    The only reason this is a hook and not a translated message: timing.
    Compaction re-summarises the whole conversation, which on a long
    session is tens of seconds during which the engine emits nothing.
    ``compact_boundary`` arrives when compaction has *finished*, so the
    only thing it can explain is a stall the user has already sat through
    and read as a hang. This hook fires first.

    It fires *too* often, though, and the browser has to know that. The
    CLI also compacts speculatively — a summary precomputed in the
    background well ahead of the threshold, discarded if the session ends
    or the context moves on — and it runs this same hook, with the same
    ``trigger="auto"``, for that. Nothing in the payload distinguishes the
    two. So this broadcast is a *maybe*, and the engine's own
    ``status`` frames (``messages._status_event``) are what confirm it;
    ``webapp/src/compaction-progress.js`` holds one back for the other.

    Observational like its neighbour: returns ``{}``, and a ``PreCompact``
    hook has nothing it could usefully return anyway — the compaction is
    happening either way.
    """

    async def pre_compact(
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        try:
            data = input_data if isinstance(input_data, dict) else {}
            if broadcast is not None:
                await broadcast(
                    Event(
                        "systemEvent",
                        {
                            "subtype": "pre_compact",
                            "data": {
                                # "auto" (the context window filled) or
                                # "manual" (/compact). Read with .get(): this
                                # is a CLI-owned dict off a wire, and a
                                # missing trigger is not a reason to skip
                                # telling the user about the pause.
                                "trigger": data.get("trigger"),
                                "custom_instructions": data.get(
                                    "custom_instructions"
                                ),
                            },
                        },
                    )
                )
        except Exception as exc:
            logger.warning("PreCompact hook failed: %s", exc)
        return {}

    return pre_compact


def build_consultation_anchor_hook(
    bridge: Callable[[], Any],
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """The ``PreToolUse`` callback for the consultation tools: note the id.

    **It decides nothing.** It returns ``{}`` on every path, like its
    neighbours and for a sharper reason than theirs: this is the module's
    only ``PreToolUse`` registration, and a ``PreToolUse`` hook that
    returns a decision *shadows* ``can_use_tool``. One stray
    ``permissionDecision`` here and the permission dialog stops appearing
    for these two tools — the pair that bill a separate account and write
    files. So it reads, it records, and it answers with nothing.

    What it records is the one fact only this event carries. An in-process
    MCP handler is called with its ``args`` dict and no context object, so
    a consultation cannot otherwise learn the ``tool_use_id`` of the card
    that invoked it, and its row has to be minted an identity of its own
    and rendered detached from that card (the cost AG-13 accepted). The
    hook fires just before the handler, with both the arguments and the
    id, which is exactly the join.

    ``bridge`` is a callable rather than the bridge itself. The service
    mounts the consultant before it builds the matchers, so one could be
    passed directly — but ``service.consultant_bridge`` is the single
    place that answers "is there a consultant", and a hook holding its own
    reference would be a second one to keep in agreement. Reading it late
    also makes ``None`` an ordinary answer rather than a wiring bug.
    """

    async def pre_tool_use(
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        try:
            target = bridge()
            if target is None:
                return {}
            data = input_data or {}
            target.note_tool_use(
                data.get("tool_name") or "",
                data.get("tool_input") or {},
                # The payload's own id first: the callback argument is
                # typed optional, while `PreToolUseHookInput.tool_use_id`
                # is required.
                data.get("tool_use_id") or tool_use_id,
            )
        except Exception as exc:  # noqa: BLE001 - an anchor is worth less
            # Losing the id costs the consultation its nesting, nothing
            # more: `_tab` falls back to the minted identity and the row
            # renders where it always did.
            logger.warning("Could not note the consultation's tool id: %s", exc)
        return {}

    return pre_tool_use


def build_hook_matchers(
    reindexer: Reindexer,
    broadcast: Callable[[Event], Awaitable[None]] | None = None,
    consultant_bridge: Callable[[], Any] | None = None,
) -> dict[str, list[Any]]:
    """The ``hooks=`` mapping for ``ClaudeAgentOptions``.

    Three events, and all of them only watch. Every other hook event AIC⚡DC
    could subscribe to is either already covered by the message pump (which
    sees the same facts in the stream, in time to be useful) or is a
    permission decision we must not make here.

    ``PreCompact`` registers without a ``matcher``: the field filters on a
    tool name and this event has none, so a matcher string here would be a
    pattern tested against nothing.

    ``PreToolUse`` registers *only* when a consultant bridge is passed, and
    only against :data:`CONSULT_TOOL_MATCHER`. The narrowness is the point:
    this event is the one that can shadow ``can_use_tool``, so the fewer
    tools reach it the smaller the blast radius of a future edit that
    forgets the rule. Sessions without Antigravity register none at all.
    """
    from claude_agent_sdk import HookMatcher

    matchers: dict[str, list[Any]] = {
        "PostToolUse": [
            HookMatcher(
                matcher=WRITE_TOOL_MATCHER,
                hooks=[build_post_tool_use_hook(reindexer, broadcast)],
            ),
            # Two matchers rather than one alternation, because the two
            # do different things: one knows the paths, the other only
            # knows that it does not.
            HookMatcher(
                matcher=SHELL_TOOL_MATCHER,
                hooks=[build_post_shell_hook(reindexer)],
            ),
        ],
        "PreCompact": [HookMatcher(hooks=[build_pre_compact_hook(broadcast)])],
    }
    if consultant_bridge is not None:
        matchers["PreToolUse"] = [
            HookMatcher(
                matcher=CONSULT_TOOL_MATCHER,
                hooks=[build_consultation_anchor_hook(consultant_bridge)],
            ),
        ]
    return matchers
