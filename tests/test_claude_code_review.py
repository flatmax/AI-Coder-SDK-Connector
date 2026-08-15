"""Review mode on the new engine — git arrangement plus read-only posture.

Adapted from the native engine's ``test_llm_service/test_review.py``. The
git state machine is unchanged and still tested against a real repository,
because that is the only way to catch a wrong argument to ``git reset``.

What is new, and what most of this module is about:

- **Entry switches the permission posture to ``plan``, and exit puts it
  back.** Read-only used to be structural — edits reached disk only through
  AC⚡DC's apply step, which review skipped. The agent writes to disk itself
  now, so the guarantee has to be the CLI's own posture.
- **A cold engine still gets the posture.** Nothing connects the CLI until
  the first turn, so a review started before chatting has no live session
  to switch. The request is recorded for the connect instead of dropped.
- **A posture that could not be applied is reported, not swallowed.** The
  difference is whether the agent can edit the branch under review.
- **Exit restores the posture even when the git restore fails.** Leaving a
  user read-only is recoverable; re-arming writes against a detached HEAD
  is not.
- **Commit and reset are refused while a review is active**, because HEAD
  is at the merge-base.

The prompt swap, the injected review context block and the pre-change
symbol map are gone; there are no tests for them because there is nothing
left to test (``specs5/4-features/code-review.md`` § What is no longer
injected).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ac_dc.claude_code.engine_config import EngineConfig
from ac_dc.claude_code.review import ReviewMode, compute_review_stats
from ac_dc.claude_code.service import ClaudeCodeService
from ac_dc.config import ConfigManager
from ac_dc.repo import Repo

from .test_claude_code_service import FakeCollab, FakeSession, Recorder


def run_git(cwd: Path, *args: str) -> str:
    """Run git in a test repo, failing loudly and returning stdout."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


class FakeDocBuilder:
    """Records ``schedule`` calls. Standing in for the real build.

    The real builder would walk the repo and, where KeyBERT is installed,
    load a sentence-transformer — neither of which review mode is about.
    """

    def __init__(self):
        self.schedules: list[bool] = []

    def schedule(self, *, force: bool = False) -> None:
        self.schedules.append(force)

    def status(self) -> dict:
        return {
            "doc_index_ready": False,
            "doc_index_building": False,
            "doc_index_enriched": False,
            "enrichment_status": "pending",
        }


class FakeSymbolIndex:
    """Records reindex passes."""

    def __init__(self):
        self.index_calls: list[list[str]] = []

    def index_repo(self, file_list):
        self.index_calls.append(list(file_list))


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    run_git(d, "init", "-q")
    run_git(d, "config", "user.email", "test@example.com")
    run_git(d, "config", "user.name", "Test")
    run_git(d, "checkout", "-q", "-b", "main")
    (d / "seed.md").write_text("seed\n")
    run_git(d, "add", "seed.md")
    run_git(d, "commit", "-q", "-m", "seed")
    return d


@pytest.fixture
def feature_tip(repo_dir: Path) -> str:
    """A one-commit ``feature`` branch, with ``main`` checked out again."""
    run_git(repo_dir, "checkout", "-q", "-b", "feature")
    (repo_dir / "new.py").write_text("def hello():\n    return 42\n")
    run_git(repo_dir, "add", "new.py")
    run_git(repo_dir, "commit", "-q", "-m", "feat: add hello")
    run_git(repo_dir, "checkout", "-q", "main")
    return run_git(repo_dir, "rev-parse", "feature")


@pytest.fixture
def config(tmp_path: Path, repo_dir: Path, monkeypatch) -> ConfigManager:
    monkeypatch.setenv("AC_DC_CONFIG_HOME", str(tmp_path / "config-home"))
    return ConfigManager(repo_root=repo_dir)


@pytest.fixture
def events() -> Recorder:
    return Recorder()


@pytest.fixture
def service(config, repo_dir, events) -> ClaudeCodeService:
    svc = ClaudeCodeService(
        config,
        repo=Repo(repo_dir),
        event_callback=events,
        engine_config=EngineConfig(),
    )
    svc.session = FakeSession()
    svc.review.doc_builder = FakeDocBuilder()
    return svc


# ---------------------------------------------------------------------------
# Readiness, graph, inactive state
# ---------------------------------------------------------------------------


class TestBeforeEntry:
    async def test_a_clean_tree_is_ready(self, service):
        assert service.check_review_ready() == {"clean": True}

    async def test_a_dirty_tree_says_what_to_do_about_it(self, service, repo_dir):
        (repo_dir / "new.md").write_text("content")
        run_git(repo_dir, "add", "new.md")
        answer = service.check_review_ready()
        assert answer["clean"] is False
        assert "commit" in answer["message"].lower()

    async def test_no_repo_is_not_ready(self, config):
        svc = ClaudeCodeService(config, repo=None, engine_config=EngineConfig())
        assert svc.check_review_ready()["clean"] is False

    async def test_the_inactive_state_has_every_active_field(self, service):
        state = service.get_review_state()
        assert state["active"] is False
        assert set(state) == {
            "active",
            "branch",
            "branch_tip",
            "base_commit",
            "parent_commit",
            "original_branch",
            "commits",
            "changed_files",
            "stats",
            "permission_mode_at_entry",
        }

    async def test_the_pre_change_symbol_map_is_gone(self, service):
        """It was the largest thing in the state and nothing reads it now."""
        assert "pre_change_symbol_map" not in service.get_review_state()

    async def test_the_state_snapshot_carries_the_review(self, service):
        assert (await service.get_current_state())["review_state"]["active"] is False

    async def test_the_commit_graph_reaches_the_repo(self, service):
        graph = service.get_commit_graph(limit=10)
        assert {"commits", "branches", "has_more"} <= set(graph)

    async def test_no_repo_gives_an_empty_graph_not_an_error(self, config):
        svc = ClaudeCodeService(config, repo=None, engine_config=EngineConfig())
        assert svc.get_commit_graph() == {
            "commits": [],
            "branches": [],
            "has_more": False,
        }

    async def test_a_diff_outside_a_review_is_an_error(self, service):
        answer = service.get_review_file_diff("new.py")
        assert "not active" in answer["error"].lower()


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_entry_arranges_the_repo_and_exit_restores_it(
        self, service, repo_dir, feature_tip, events
    ):
        started = await service.start_review("feature", feature_tip)

        assert started["status"] == "review_active"
        assert started["branch"] == "feature"
        assert started["stats"]["commit_count"] >= 1
        assert service.review.active is True

        # Disk carries the branch's content while HEAD sits at the base.
        assert (repo_dir / "new.py").exists()
        assert run_git(repo_dir, "rev-parse", "HEAD") != feature_tip

        state = service.get_review_state()
        assert state["branch"] == "feature"
        assert state["changed_files"]
        assert "new.py" in [f["path"] for f in state["changed_files"]]

        # Both events reach every client, with the full state.
        assert events.payload_of("reviewStarted")["active"] is True
        assert events.payload_of("filesChanged") == []

        ended = await service.end_review()
        assert ended["status"] == "restored"
        assert service.review.active is False
        assert service.get_review_state()["branch"] is None
        assert events.payload_of("reviewEnded")["active"] is False
        # Back on the branch we came from, with a clean tree.
        assert run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert service.check_review_ready() == {"clean": True}

    async def test_entry_clears_the_selection(self, service, feature_tip):
        service._selected_files = ["seed.md"]
        await service.start_review("feature", feature_tip)
        assert service.get_selected_files() == []

    async def test_a_dirty_tree_is_refused_at_entry(self, service, repo_dir):
        (repo_dir / "new.md").write_text("content")
        run_git(repo_dir, "add", "new.md")
        answer = await service.start_review("main", "HEAD")
        assert "error" in answer
        assert service.review.active is False

    async def test_a_second_review_is_refused(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        answer = await service.start_review("feature", feature_tip)
        assert "already active" in answer["error"].lower()

    async def test_exit_without_a_review_is_an_error(self, service):
        answer = await service.end_review()
        assert "not active" in answer["error"].lower()

    async def test_both_indexes_are_rebuilt_at_entry_and_exit(
        self, service, feature_tip
    ):
        index = FakeSymbolIndex()
        service._attach_symbol_index(index)
        await service.start_review("feature", feature_tip)
        assert len(index.index_calls) == 1
        assert service.review.doc_builder.schedules == [True]
        await service.end_review()
        assert len(index.index_calls) == 2
        assert service.review.doc_builder.schedules == [True, True]

    async def test_a_broken_index_does_not_break_the_entry(
        self, service, feature_tip, caplog
    ):
        """An unindexed review is navigable; a half-entered one is not."""

        class Exploding:
            def index_repo(self, file_list):
                raise RuntimeError("tree-sitter fell over")

        service._attach_symbol_index(Exploding())
        started = await service.start_review("feature", feature_tip)
        assert started["status"] == "review_active"
        assert "tree-sitter fell over" in caplog.text

    async def test_the_diff_of_a_changed_file_reads_forward(
        self, service, feature_tip
    ):
        await service.start_review("feature", feature_tip)
        diff = service.get_review_file_diff("new.py")
        assert "error" not in diff
        assert "def hello" in str(diff)

    async def test_the_returned_state_is_a_copy(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        state = service.get_review_state()
        state["commits"].append({"sha": "forged"})
        state["stats"]["commit_count"] = 999
        assert service.get_review_state()["stats"]["commit_count"] != 999
        assert len(service.get_review_state()["commits"]) == len(
            state["commits"]
        ) - 1


# ---------------------------------------------------------------------------
# What entry and exit leave in `events.jsonl`
# ---------------------------------------------------------------------------


class TestTheHistoryRecord:
    """A review is ours, so the engine's transcript never mentions one.

    Without these records a browsed conversation shows the agent going
    read-only and back with no explanation
    (``specs5/3-engine/history.md`` § One Store, One Index, One Events Log).
    """

    @pytest.fixture
    def service(self, service):
        service.session.session_id = "22222222-2222-4222-8222-222222222222"
        return service

    async def records(self, service, event: str) -> list[dict]:
        loaded = await service.events_log.load(service.session.session_id)
        return [record for record in loaded if record["event"] == event]

    async def test_entry_records_the_base_the_head_and_the_files(
        self, service, feature_tip
    ):
        await service.start_review("feature", feature_tip)
        (record,) = await self.records(service, "review_start")
        assert record["payload"]["head"] == "feature"
        assert record["payload"]["base"] == feature_tip
        assert record["payload"]["files"] == ["new.py"]
        assert "Review started" in record["content"]

    async def test_exit_records_the_review_that_just_ended(
        self, service, feature_tip
    ):
        """Exit clears the state, so the record is assembled from what was
        read before it — otherwise it names a review with no branch."""
        await service.start_review("feature", feature_tip)
        await service.end_review()
        (record,) = await self.records(service, "review_end")
        assert record["payload"]["head"] == "feature"
        assert record["payload"]["base"] == feature_tip
        assert record["payload"]["files"] == ["new.py"]

    async def test_a_refused_entry_records_nothing(self, service, repo_dir):
        (repo_dir / "new.md").write_text("content")
        run_git(repo_dir, "add", "new.md")
        await service.start_review("main", "HEAD")
        assert await self.records(service, "review_start") == []

    async def test_a_refused_exit_records_nothing(self, service):
        await service.end_review()
        assert await self.records(service, "review_end") == []

    async def test_only_the_paths_are_archived(self, service, feature_tip):
        """Not the per-file line counts: those are recomputed from git on
        every read, and a frozen copy would drift out of agreement with the
        diff the browser renders beside it."""
        await service.start_review("feature", feature_tip)
        (record,) = await self.records(service, "review_start")
        assert all(isinstance(path, str) for path in record["payload"]["files"])


# ---------------------------------------------------------------------------
# The read-only posture
# ---------------------------------------------------------------------------


class TestPosture:
    async def test_a_live_session_is_switched_to_plan(
        self, service, feature_tip, events
    ):
        service.session.ready = True
        started = await service.start_review("feature", feature_tip)
        assert started["permission_mode"] == "plan"
        assert "warning" not in started
        assert ("set_permission_mode", ("plan",)) in service.session.control_calls
        assert events.payload_of("permissionModeChanged") == {
            "mode": "plan",
            "by": "review mode",
        }

    async def test_a_cold_engine_records_the_posture_for_its_connect(
        self, service, feature_tip
    ):
        """The ordinary case: review entered before the first turn."""
        service.session.ready = False
        started = await service.start_review("feature", feature_tip)
        assert started["permission_mode"] == "plan"
        assert "warning" not in started
        assert (
            "prefer_permission_mode",
            ("plan",),
        ) in service.session.control_calls
        assert (await service.get_current_state())["permission_mode"] == "plan"

    async def test_exit_restores_the_mode_entry_found(self, service, feature_tip):
        service.session.ready = True
        service.session.permission_mode = "acceptEdits"
        await service.start_review("feature", feature_tip)
        assert service.review.state()["permission_mode_at_entry"] == "acceptEdits"
        ended = await service.end_review()
        assert ended["permission_mode"] == "acceptEdits"
        assert service.session.permission_mode == "acceptEdits"

    async def test_a_posture_that_could_not_be_applied_is_reported(
        self, service, feature_tip
    ):
        """The user needs to know the agent can still edit the branch."""
        service.session.ready = True
        service.session.control_error = RuntimeError("control socket closed")
        started = await service.start_review("feature", feature_tip)
        assert started["status"] == "review_active"
        assert started["permission_mode"] is None
        assert "can still edit" in started["warning"]

    async def test_the_posture_is_restored_even_when_git_exit_fails(
        self, service, feature_tip, monkeypatch
    ):
        service.session.ready = True
        service.session.permission_mode = "default"
        await service.start_review("feature", feature_tip)
        assert service.session.permission_mode == "plan"

        monkeypatch.setattr(
            service._repo,
            "exit_review_mode",
            lambda *a, **k: {"error": "simulated failure"},
        )
        ended = await service.end_review()
        assert ended["status"] == "partial"
        assert "simulated" in ended["error"]
        # Both halves of "do not leave the user stuck".
        assert service.review.active is False
        assert service.session.permission_mode == "default"

    async def test_a_lost_session_at_exit_does_not_hide_the_git_result(
        self, service, feature_tip, caplog
    ):
        from ac_dc.claude_code.session import SessionLostError

        service.session.ready = True
        await service.start_review("feature", feature_tip)
        service.session.control_error = SessionLostError("session gone")
        ended = await service.end_review()
        assert ended["status"] == "restored"
        assert service.review.active is False


# ---------------------------------------------------------------------------
# What a review forbids
# ---------------------------------------------------------------------------


class TestRefusalsDuringReview:
    async def test_commit_is_refused(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        answer = await service.commit_all()
        assert "review" in answer["error"].lower()
        assert service._committing is False

    async def test_reset_is_refused(self, service, feature_tip, repo_dir):
        await service.start_review("feature", feature_tip)
        answer = await service.reset_to_head()
        assert "review" in answer["error"].lower()
        # The reviewed content is still on disk.
        assert (repo_dir / "new.py").exists()

    async def test_snippets_switch_to_the_review_set(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        assert service.get_snippets() == service._config.get_snippets("review")
        await service.end_review()
        assert service.get_snippets() == service._config.get_snippets("code")


# ---------------------------------------------------------------------------
# Collaboration
# ---------------------------------------------------------------------------


class TestParticipants:
    async def test_a_participant_cannot_enter_a_review(self, service, feature_tip):
        service._collab = FakeCollab(is_localhost=False)
        answer = await service.start_review("feature", feature_tip)
        assert answer["error"] == "restricted"
        assert service.review.active is False

    async def test_a_participant_cannot_leave_one(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        service._collab = FakeCollab(is_localhost=False)
        answer = await service.end_review()
        assert answer["error"] == "restricted"
        assert service.review.active is True

    async def test_a_participant_may_read_the_review(self, service, feature_tip):
        await service.start_review("feature", feature_tip)
        service._collab = FakeCollab(is_localhost=False)
        assert service.get_review_state()["active"] is True
        assert "error" not in service.get_review_file_diff("new.py")


# ---------------------------------------------------------------------------
# Stats, in isolation
# ---------------------------------------------------------------------------


class TestStats:
    async def test_stats_sum_the_per_file_counts(self):
        stats = compute_review_stats(
            [{"sha": "a"}, {"sha": "b"}],
            [
                {"path": "a.py", "additions": 10, "deletions": 2},
                {"path": "b.py", "additions": 1, "deletions": 0},
            ],
        )
        assert stats == {
            "commit_count": 2,
            "files_changed": 2,
            "additions": 11,
            "deletions": 2,
        }

    async def test_missing_and_null_counts_are_zero_not_a_crash(self):
        stats = compute_review_stats(
            [], [{"path": "a.py"}, {"path": "b.py", "additions": None}]
        )
        assert stats["additions"] == 0
        assert stats["files_changed"] == 2

    async def test_a_bare_review_mode_needs_no_collaborators(self):
        """Every one is optional, so a caller can wire only what it has."""
        review = ReviewMode()
        assert review.active is False
        assert review.check_ready()["clean"] is False
