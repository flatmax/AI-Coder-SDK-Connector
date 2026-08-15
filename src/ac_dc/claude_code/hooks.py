"""``PostToolUse`` — what AC⚡DC does after the agent writes a file.

Three things, all of them bookkeeping:

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

That third point is the invariant. **These hooks are observational.** They
never return a ``permissionDecision``, ``decision``, ``continue: False``,
or any other control field — an empty dict, always. The reason is
specific and easy to trip over: a ``PreToolUse`` hook that returns a
decision *shadows* ``can_use_tool``, so the CLI stops asking us and our
permission dialog silently never appears again. A hook that returned
"allow" to be helpful would ungate every gated tool in the session
without a single error message. See ``specs5/plan/sdk-surface.md``.

The re-index is debounced, because a turn that edits eight files fires
eight hooks in a few seconds and each re-index ends with two whole-index
passes (:meth:`~ac_dc.symbol_index.index.SymbolIndex.reindex_files`).
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

from ac_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The tools that put bytes on disk at a path we can name. Bash is
# deliberately absent: `PostToolUse` hands us the command, not the files it
# touched, and guessing paths out of a shell line would be wrong more often
# than right. A `sed -i` therefore leaves the index stale until the next
# full build — recorded as a known gap rather than papered over with a
# heuristic.
WRITE_TOOL_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"

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
        The :class:`~ac_dc.doc_index.background.DocIndexBuilder`. Its
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
        self._timer = loop.create_task(self._after_debounce(), name="ac-dc-reindex")

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
            self._drain_quietly(), name="ac-dc-reindex-drain"
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


def build_hook_matchers(
    reindexer: Reindexer,
    broadcast: Callable[[Event], Awaitable[None]] | None = None,
) -> dict[str, list[Any]]:
    """The ``hooks=`` mapping for ``ClaudeAgentOptions``.

    One event, one matcher. Every other hook event AC⚡DC could subscribe
    to is either already covered by the message pump (which sees the same
    facts in the stream) or is a permission decision we must not make
    here.
    """
    from claude_agent_sdk import HookMatcher

    return {
        "PostToolUse": [
            HookMatcher(
                matcher=WRITE_TOOL_MATCHER,
                hooks=[build_post_tool_use_hook(reindexer, broadcast)],
            )
        ]
    }
