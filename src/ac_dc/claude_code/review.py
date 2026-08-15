"""Review mode — the git state machine, and nothing else.

A review is an arrangement of the repository: disk at the branch tip, HEAD
at the merge-base, every change staged. Once that arrangement exists, the
file picker, the diff viewer and every git tool the agent has are looking
at the review without being told about it.

What the conversion removed from this module is everything that used to
*describe* the review to a model: the review system prompt, the
re-injected review context block, and the pre-change symbol map held in
memory. The agent reaches the pre-change state the way a human reviewer
does — ``git show``, ``git diff``, ``git log``
(``specs5/4-features/code-review.md`` § What is no longer injected).

What it added is enforcement. The old read-only guarantee was structural:
edits reached disk only through AC⚡DC's apply step, and review skipped
that step. The agent writes to disk itself now, so review entry switches
the permission posture to ``plan`` — the CLI's own "read and reason, no
writes, no commands" state — and remembers the previous mode for exit.
That enforcement covers ``Bash``, which no rule list of ours would have.

Governing spec: ``specs5/4-features/code-review.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ac_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

# The posture a review runs in. `plan` is the platform's own read-only
# state; see specs5/3-engine/permissions.md § Permission Mode.
REVIEW_PERMISSION_MODE = "plan"


def empty_review_state() -> dict[str, Any]:
    """The not-in-review shape.

    Every field the active shape has, so the frontend never has to branch
    on a key's absence — only on ``active``.
    """
    return {
        "active": False,
        "branch": None,
        "branch_tip": None,
        "base_commit": None,
        "parent_commit": None,
        "original_branch": None,
        "commits": [],
        "changed_files": [],
        "stats": {},
        "permission_mode_at_entry": None,
    }


def compute_review_stats(
    commits: list[dict[str, Any]],
    changed_files: list[dict[str, Any]],
) -> dict[str, int]:
    """Aggregate counts for the banner and the status bar."""
    additions = sum(int(f.get("additions", 0) or 0) for f in changed_files)
    deletions = sum(int(f.get("deletions", 0) or 0) for f in changed_files)
    return {
        "commit_count": len(commits),
        "files_changed": len(changed_files),
        "additions": additions,
        "deletions": deletions,
    }


class ReviewMode:
    """The review's in-memory record, and the operations that move it.

    Parameters
    ----------
    repo:
        The ``Repo`` service. Owns every git operation here; this class
        owns the ordering and the rollback.
    broadcast:
        ``async (Event) -> None``. ``reviewStarted`` / ``reviewEnded`` go
        to every client, because a frontend must never infer entry or exit
        from one RPC's return value.
    set_permission_mode:
        ``async (str) -> str | None`` — applies a posture to the session,
        live if it is connected and at next connect otherwise. Returns the
        mode actually in force, or ``None`` when it could not be applied.
    current_permission_mode:
        ``() -> str | None`` — the posture right now, read at entry so exit
        has something to put back.
    restricted:
        The localhost gate: ``() -> dict | None``.
    on_selection_cleared:
        ``async () -> None``, called after entry clears the selection.
        Separate from ``broadcast`` because clearing the selection is the
        service's state, not the review's.

    ``symbol_index`` and ``doc_builder`` are attached after construction —
    both are built by the startup path long after this object exists, and
    a review that starts before they land re-indexes nothing rather than
    failing.

    State is never persisted. A server restart comes up not-in-review,
    which matches the fact that the git arrangement is not restored either.
    """

    def __init__(
        self,
        *,
        repo: Any = None,
        broadcast: Callable[[Event], Awaitable[None]] | None = None,
        set_permission_mode: Callable[[str], Awaitable[str | None]] | None = None,
        current_permission_mode: Callable[[], str | None] | None = None,
        restricted: Callable[[], dict[str, Any] | None] | None = None,
        on_selection_cleared: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._repo = repo
        self._broadcast = broadcast
        self._set_permission_mode = set_permission_mode
        self._current_permission_mode = current_permission_mode
        self._restricted = restricted
        self._on_selection_cleared = on_selection_cleared
        self.symbol_index: Any = None
        self.doc_builder: Any = None
        self._state = empty_review_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._state.get("active"))

    def state(self) -> dict[str, Any]:
        """A defensive copy, safe to hand to the RPC layer."""
        state = dict(self._state)
        state["commits"] = list(state.get("commits") or [])
        state["changed_files"] = list(state.get("changed_files") or [])
        state["stats"] = dict(state.get("stats") or {})
        return state

    # ------------------------------------------------------------------
    # Readiness and graph queries
    # ------------------------------------------------------------------

    def check_ready(self) -> dict[str, Any]:
        """Whether the working tree is clean enough to enter a review."""
        if self._repo is None:
            return {"clean": False, "message": "No repository attached."}
        if self._repo.is_clean():
            return {"clean": True}
        return {
            "clean": False,
            "message": (
                "Working tree has uncommitted changes. Commit, stash, or "
                "discard them before entering review mode."
            ),
        }

    def commit_graph(
        self,
        limit: int = 100,
        offset: int = 0,
        include_remote: bool = False,
    ) -> dict[str, Any]:
        """Thin delegation to the repo, for the review selector's graph."""
        if self._repo is None:
            return {"commits": [], "branches": [], "has_more": False}
        return self._repo.get_commit_graph(
            limit=limit, offset=offset, include_remote=include_remote
        )

    def file_diff(self, path: str) -> dict[str, Any]:
        """The forward diff (base..tip) for one file in the review."""
        if not self.active:
            return {"error": "Review mode is not active."}
        if self._repo is None:
            return {"error": "No repository attached."}
        try:
            return self._repo.get_review_file_diff(
                path,
                base_commit=self._state.get("parent_commit"),
                head_commit=self._state.get("branch_tip"),
            )
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def start(self, branch: str, base_commit: str) -> dict[str, Any]:
        """Arrange the repository as a review of ``branch``.

        Ordered per ``specs5/4-features/code-review.md`` § Entry Sequence.
        Any failure after the git state has moved rolls back through
        :meth:`Repo.exit_review_mode` before returning the error.
        """
        gate = self._restricted() if self._restricted is not None else None
        if gate is not None:
            return gate
        if self._repo is None:
            return {"error": "No repository attached."}
        if self.active:
            return {
                "error": (
                    "Review mode is already active. Exit the current review "
                    "first."
                )
            }

        clean = self.check_ready()
        if not clean["clean"]:
            return {"error": clean.get("message", "Tree not clean")}

        parent_result = self._repo.checkout_review_parent(branch, base_commit)
        if "error" in parent_result:
            return {"error": parent_result["error"]}
        branch_tip = parent_result["branch_tip"]
        parent_commit = parent_result["parent_commit"]
        original_branch = parent_result["original_branch"]

        reset_result = self._repo.setup_review_soft_reset(
            branch_tip, parent_commit
        )
        if "error" in reset_result:
            self._repo.exit_review_mode(branch_tip, original_branch)
            return {"error": reset_result["error"]}

        try:
            commits = self._repo.get_commit_log(
                base=parent_commit, head=branch_tip, limit=100
            )
            changed_files = self._repo.get_review_changed_files()
            stats = compute_review_stats(commits, changed_files)
        except Exception as exc:
            logger.exception("Failed to gather review metadata: %s", exc)
            self._repo.exit_review_mode(branch_tip, original_branch)
            return {"error": f"Review setup failed: {exc}"}

        # Disk moved, so both indexes now describe the wrong tree. Neither
        # rebuild is allowed to fail the entry: an unindexed review is
        # navigable, a half-entered one is not.
        await self._reindex("review entry")

        # The read-only posture. Recorded before it changes so exit can put
        # it back, and reported in the return value even when it could not
        # be applied — a review the agent can still write in is a fact the
        # user needs, not a detail to swallow.
        posture = await self._apply_review_posture()

        self._state = {
            "active": True,
            "branch": branch,
            "branch_tip": branch_tip,
            "base_commit": base_commit,
            "parent_commit": parent_commit,
            "original_branch": original_branch,
            "commits": commits,
            "changed_files": changed_files,
            "stats": stats,
            "permission_mode_at_entry": posture["previous"],
        }

        if self._on_selection_cleared is not None:
            await self._on_selection_cleared()
        await self._emit("reviewStarted", self.state())

        result = {
            "status": "review_active",
            "branch": branch,
            "base_commit": base_commit,
            "commits": commits,
            "changed_files": changed_files,
            "stats": stats,
            "permission_mode": posture["applied"],
            "system_event_message": (
                f"Entered review mode for `{branch}` ({len(commits)} commits, "
                f"{stats.get('files_changed', 0)} files changed)."
            ),
        }
        if posture["warning"]:
            result["warning"] = posture["warning"]
        return result

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    async def end(self) -> dict[str, Any]:
        """Put the repository, the indexes and the posture back.

        The posture is restored even when the git restore fails. A
        half-exited review that leaves the agent read-only is recoverable;
        one that re-arms writing against a detached HEAD is not.
        """
        gate = self._restricted() if self._restricted is not None else None
        if gate is not None:
            return gate
        if not self.active:
            return {"error": "Review mode is not active."}
        if self._repo is None:
            return {"error": "No repository attached."}

        branch_tip = self._state["branch_tip"]
        original_branch = self._state["original_branch"]
        entry_mode = self._state.get("permission_mode_at_entry")

        exit_result = self._repo.exit_review_mode(branch_tip, original_branch)
        exit_error = exit_result.get("error")

        await self._reindex("review exit")

        restored_mode: str | None = None
        if entry_mode and self._set_permission_mode is not None:
            try:
                restored_mode = await self._set_permission_mode(entry_mode)
            except Exception as exc:
                logger.warning(
                    "Could not restore the permission mode to %r after "
                    "review: %s",
                    entry_mode,
                    exc,
                )

        self._state = empty_review_state()
        await self._emit("reviewEnded", self.state())

        event_text = (
            f"Exited review mode with issues: {exit_error}"
            if exit_error
            else "Exited review mode."
        )
        if exit_error:
            return {
                "error": exit_error,
                "status": "partial",
                "permission_mode": restored_mode,
                "system_event_message": event_text,
            }
        return {
            "status": "restored",
            "permission_mode": restored_mode,
            "system_event_message": event_text,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _apply_review_posture(self) -> dict[str, Any]:
        """Switch to ``plan``, reporting what actually happened."""
        previous: str | None = None
        if self._current_permission_mode is not None:
            previous = self._current_permission_mode()
        if self._set_permission_mode is None:
            return {"previous": previous, "applied": None, "warning": None}

        applied: str | None = None
        warning: str | None = None
        try:
            applied = await self._set_permission_mode(REVIEW_PERMISSION_MODE)
        except Exception as exc:
            logger.warning("Could not switch to the review posture: %s", exc)
            applied = None
        if applied != REVIEW_PERMISSION_MODE:
            warning = (
                "The review is arranged, but the read-only posture could not "
                "be applied — the agent can still edit files. Set the "
                "permission mode to plan yourself, or exit and retry."
            )
        return {"previous": previous, "applied": applied, "warning": warning}

    async def _reindex(self, when: str) -> None:
        """Rebuild both indexes against what is now on disk.

        Best-effort and off the event loop: the symbol pass over a large
        repo takes seconds, and freezing the loop for it would stall the
        WebSocket the progress has to travel on. Serving a hover from a
        half-rebuilt index for those seconds is the lesser fault.
        """
        if self._repo is None:
            return
        index = self.symbol_index
        if index is not None:
            try:
                flat = self._repo.get_flat_file_list()
                file_list = [f for f in flat.split("\n") if f]
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, lambda: index.index_repo(file_list)
                )
            except Exception as exc:
                logger.warning("Symbol reindex on %s failed: %s", when, exc)
        builder = self.doc_builder
        if builder is not None:
            try:
                builder.schedule(force=True)
            except Exception as exc:
                logger.warning("Doc reindex on %s failed: %s", when, exc)

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self._broadcast is None:
            return
        await self._broadcast(Event(name, payload, turn_scoped=False))
