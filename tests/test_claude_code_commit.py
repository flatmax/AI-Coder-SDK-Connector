"""Commit and reset — the two git writes the user still performs by hand.

The git operations belong to ``Repo`` and are tested there; what is under
test here is the pipeline around them, where the conversion changed the
interesting part. The commit message used to come from a blocking call to
a separately-configured "smaller model". It now comes from a **stateless
one-shot** ``claude_agent_sdk.query`` — a second, short-lived CLI with no
tools and no settings sources.

The properties worth a test, in the order they can hurt:

- **A failure never commits anyway.** There is no fallback message: a
  generated-message request that could not be answered leaves the tree
  staged and says so, rather than writing "chore: update files" into
  permanent history.
- **The button always hears back.** Every exit path broadcasts exactly one
  ``commitResult`` and clears ``_committing``, including a wedged one-shot
  and an exception out of ``Repo.commit``.
- **The diff never reaches the chat session.** Routing it through the live
  session would put the whole staged diff in the user's conversation and
  deadlock behind a turn in flight.
- **The one-shot is defanged.** ``tools=[]``, ``setting_sources=[]``,
  ``max_turns=1``, ``permission_mode="plan"``. Each omission is a thing
  the throwaway session then cannot do.
- **An oversized diff fails before the round trip**, with a sentence
  naming the size.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ac_dc.claude_code import commit as commit_mod
from ac_dc.claude_code.engine_config import EngineConfig
from ac_dc.claude_code.service import ClaudeCodeService

from .test_claude_code_service import (
    FakeCollab,
    FakeConfig,
    FakeSession,
    Recorder,
    finish_turns,
)

DIFF = "diff --git a/x.py b/x.py\n+++ b/x.py\n+print('hi')\n"


class FakeRepo:
    """The six git calls this module makes, and nothing else."""

    def __init__(self, root: Path, diff: str = DIFF):
        self.root = root
        self.diff = diff
        self.stage_calls = 0
        self.commits: list[str] = []
        self.resets = 0
        self.commit_error: BaseException | None = None
        self.reset_error: BaseException | None = None
        # What the two history records read. Both are asked *before* the
        # write lands, so a fake that answered afterwards would hide the
        # ordering the records depend on.
        self.staged_files: list[dict] = [{"path": "x.py"}]
        self.file_tree: dict = {
            "modified": ["x.py"],
            "staged": ["y.py"],
            "deleted": ["z.py"],
            "untracked": ["scratch.py"],
        }
        self.status_error: BaseException | None = None

    def stage_all(self):
        self.stage_calls += 1

    def get_staged_diff(self):
        return self.diff

    def get_review_changed_files(self):
        if self.status_error is not None:
            raise self.status_error
        return list(self.staged_files)

    def get_file_tree(self):
        if self.status_error is not None:
            raise self.status_error
        return dict(self.file_tree)

    def commit(self, message):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits.append(message)
        return {"sha": "0123456789abcdef0123", "message": message}

    def reset_hard(self):
        if self.reset_error is not None:
            raise self.reset_error
        self.resets += 1
        self.file_tree = {"modified": [], "staged": [], "deleted": [], "untracked": []}


class FakeQuery:
    """Stands in for ``claude_agent_sdk.query``: records, then replays.

    Recording the ``options`` is half the point — it is the only place the
    one-shot's shape can be asserted.
    """

    def __init__(self, *, text: str = "feat: add x", error: BaseException | None = None,
                 delay: float = 0.0):
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    def __call__(self, *, prompt, options):
        self.calls.append({"prompt": prompt, "options": options})
        return self._stream()

    async def _stream(self):
        from claude_agent_sdk import AssistantMessage, TextBlock

        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        yield AssistantMessage(content=[TextBlock(text=self.text)], model="opus")

    @property
    def options(self):
        """The options of the single call, for the assertions."""
        assert len(self.calls) == 1, f"expected one call, got {len(self.calls)}"
        return self.calls[0]["options"]


@pytest.fixture
def repo(tmp_path: Path) -> FakeRepo:
    return FakeRepo(tmp_path)


@pytest.fixture
def events() -> Recorder:
    return Recorder()


@pytest.fixture
def service(tmp_path: Path, repo: FakeRepo, events: Recorder) -> ClaudeCodeService:
    svc = ClaudeCodeService(
        FakeConfig(tmp_path),
        repo=repo,
        event_callback=events,
        engine_config=EngineConfig(),
    )
    svc.session = FakeSession()
    return svc


@pytest.fixture
def sdk_query(monkeypatch) -> FakeQuery:
    """Replace the one-shot with a fake, and hand it to the test."""
    import claude_agent_sdk

    fake = FakeQuery()
    monkeypatch.setattr(claude_agent_sdk, "query", fake)
    return fake


def in_review(service: ClaudeCodeService) -> None:
    """Mark a review active without arranging a repository.

    The arrangement itself, and the refusals seen through it, are covered
    in ``test_claude_code_review.py``; here it is only a flag to trip.
    """
    service.review._state["active"] = True


async def commit(service: ClaudeCodeService) -> dict:
    """Launch a commit and wait for the background pipeline to finish."""
    answer = await service.commit_all()
    await finish_turns(service)
    return answer


# ---------------------------------------------------------------------------
# Who may commit, and when
# ---------------------------------------------------------------------------


class TestGates:
    async def test_a_participant_may_not_commit(self, service, sdk_query):
        service._collab = FakeCollab(is_localhost=False)
        assert (await service.commit_all())["error"] == "restricted"
        assert service._committing is False
        assert sdk_query.calls == []

    async def test_a_participant_may_not_reset(self, service, repo):
        service._collab = FakeCollab(is_localhost=False)
        assert (await service.reset_to_head())["error"] == "restricted"
        assert repo.resets == 0

    async def test_no_repo_is_an_error_not_a_crash(self, tmp_path, events):
        svc = ClaudeCodeService(
            FakeConfig(tmp_path), event_callback=events, engine_config=EngineConfig()
        )
        svc.session = FakeSession()
        assert "repository" in (await svc.commit_all())["error"].lower()
        assert "repository" in (await svc.reset_to_head())["error"].lower()

    async def test_a_second_commit_is_refused_while_one_runs(self, service):
        service._committing = True
        assert "in progress" in (await service.commit_all())["error"]

    async def test_a_review_refuses_both(self, service, repo):
        in_review(service)
        commit_answer = await service.commit_all()
        assert "review" in commit_answer["error"].lower()
        assert "merge-base" in commit_answer["error"]
        reset_answer = await service.reset_to_head()
        assert "review" in reset_answer["error"].lower()
        assert repo.commits == [] and repo.resets == 0


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class TestCommitPipeline:
    async def test_it_returns_before_the_work_and_reports_after(
        self, service, repo, events, sdk_query
    ):
        launched = await service.commit_all()
        assert launched == {"status": "started"}
        assert service._committing is True

        await finish_turns(service)

        assert repo.stage_calls == 1
        assert repo.commits == ["feat: add x"]
        payload = events.payload_of("commitResult")
        assert payload["sha"] == "0123456789abcdef0123"
        assert payload["short_sha"] == "0123456"
        assert payload["message"] == "feat: add x"
        assert "0123456" in payload["system_event_message"]
        assert "feat: add x" in payload["system_event_message"]
        # The picker's badges all change at once.
        assert "filesModified" in events.names()
        assert service._committing is False

    async def test_the_diff_is_what_the_one_shot_is_asked_about(
        self, service, sdk_query
    ):
        await commit(service)
        assert sdk_query.calls[0]["prompt"] == DIFF

    async def test_the_diff_never_reaches_the_chat_session(self, service, sdk_query):
        """Sending it there would put the whole diff in the conversation."""
        await commit(service)
        assert service.session.turns == []
        assert service.session.connect_calls == []

    async def test_nothing_staged_is_said_plainly(self, service, repo, events, sdk_query):
        repo.diff = "\n  \n"
        await commit(service)
        assert "No staged changes" in events.payload_of("commitResult")["error"]
        assert repo.commits == []
        assert sdk_query.calls == []
        assert service._committing is False

    async def test_an_oversized_diff_fails_before_the_round_trip(
        self, service, repo, events, sdk_query
    ):
        repo.diff = "x" * (commit_mod.MAX_DIFF_CHARS + 1)
        await commit(service)
        error = events.payload_of("commitResult")["error"]
        # The two numbers the user needs to act on.
        assert f"{len(repo.diff) // 1000}k" in error
        assert f"{commit_mod.MAX_DIFF_CHARS // 1000}k" in error
        assert "smaller pieces" in error
        assert "staged changes are unchanged" in error
        assert sdk_query.calls == [], "the diff was sent anyway"
        assert repo.commits == []

    async def test_a_failed_generation_does_not_commit_anything(
        self, service, repo, events, monkeypatch
    ):
        """No fallback message. A bad one would be permanent."""
        import claude_agent_sdk

        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            FakeQuery(error=RuntimeError("CLI exited 1")),
        )
        await commit(service)
        error = events.payload_of("commitResult")["error"]
        assert "Could not generate a commit message" in error
        assert "untouched" in error
        assert repo.commits == []
        assert repo.stage_calls == 1, "the staging should stand"
        assert service._committing is False

    async def test_an_empty_answer_is_a_failure_not_an_empty_message(
        self, service, repo, events, monkeypatch
    ):
        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk, "query", FakeQuery(text="   \n  "))
        await commit(service)
        assert "Could not generate" in events.payload_of("commitResult")["error"]
        assert repo.commits == []

    async def test_a_wedged_one_shot_gives_the_button_back(
        self, service, repo, events, monkeypatch
    ):
        import claude_agent_sdk

        monkeypatch.setattr(commit_mod, "GENERATE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(claude_agent_sdk, "query", FakeQuery(delay=30.0))
        await commit(service)
        assert "Could not generate" in events.payload_of("commitResult")["error"]
        assert repo.commits == []
        assert service._committing is False

    async def test_a_git_failure_surfaces_and_releases_the_flag(
        self, service, repo, events, sdk_query
    ):
        repo.commit_error = RuntimeError("pre-commit hook rejected the change")
        await commit(service)
        assert "pre-commit hook" in events.payload_of("commitResult")["error"]
        assert service._committing is False
        assert "filesModified" not in events.names()

    async def test_a_fenced_message_is_unwrapped_before_it_is_committed(
        self, service, repo, monkeypatch
    ):
        import claude_agent_sdk

        monkeypatch.setattr(
            claude_agent_sdk,
            "query",
            FakeQuery(text="```\nfix: unwrap the fence\n```"),
        )
        await commit(service)
        assert repo.commits == ["fix: unwrap the fence"]


# ---------------------------------------------------------------------------
# The throwaway session's shape
# ---------------------------------------------------------------------------


class TestOneShotOptions:
    async def test_it_can_neither_read_nor_write_nor_iterate(
        self, service, sdk_query, tmp_path
    ):
        await commit(service)
        options = sdk_query.options
        assert options.tools == [], "the one-shot was given tools"
        assert options.setting_sources == [], "CLAUDE.md and plugins leaked in"
        assert options.max_turns == 1
        assert options.permission_mode == "plan"
        assert options.cwd == str(tmp_path)
        assert options.system_prompt == service._config.get_commit_prompt()

    async def test_it_uses_the_binary_the_live_session_checked(
        self, service, sdk_query
    ):
        service.session.health.cli_path = "/opt/claude/bin/claude"
        await commit(service)
        assert sdk_query.options.cli_path == "/opt/claude/bin/claude"

    async def test_it_falls_back_to_the_configured_binary(
        self, tmp_path, repo, events, sdk_query
    ):
        svc = ClaudeCodeService(
            FakeConfig(tmp_path),
            repo=repo,
            event_callback=events,
            engine_config=EngineConfig(cli_path="/usr/local/bin/claude"),
        )
        svc.session = FakeSession()
        await commit(svc)
        assert sdk_query.options.cli_path == "/usr/local/bin/claude"

    async def test_the_configured_model_is_used(self, tmp_path, repo, events, sdk_query):
        svc = ClaudeCodeService(
            FakeConfig(tmp_path),
            repo=repo,
            event_callback=events,
            engine_config=EngineConfig(model="claude-sonnet-5"),
        )
        svc.session = FakeSession()
        await commit(svc)
        assert sdk_query.options.model == "claude-sonnet-5"

    async def test_a_moved_sdk_surface_reports_instead_of_raising(
        self, service, repo, events, monkeypatch, caplog
    ):
        """A constructor TypeError must not become a transport error."""
        import claude_agent_sdk

        class Fussy:
            def __init__(self, **kwargs):
                raise TypeError("unexpected keyword argument 'setting_sources'")

        monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", Fussy)
        await commit(service)
        assert "Could not generate" in events.payload_of("commitResult")["error"]
        assert repo.commits == []
        assert "_one_shot_options" in caplog.text, (
            "the log should name the function to re-read the SDK against"
        )


# ---------------------------------------------------------------------------
# Fence stripping, in isolation
# ---------------------------------------------------------------------------


class TestStripFence:
    async def test_a_bare_fence_is_dropped(self):
        assert commit_mod._strip_fence("```\nfeat: x\n```") == "feat: x"

    async def test_a_tagged_fence_is_dropped(self):
        assert commit_mod._strip_fence("```text\nfeat: x\n```") == "feat: x"

    async def test_a_body_survives_the_unwrap(self):
        message = "```\nfeat: x\n\nWhy: because.\n```"
        assert commit_mod._strip_fence(message) == "feat: x\n\nWhy: because."

    async def test_an_unfenced_message_is_untouched(self):
        assert commit_mod._strip_fence("feat: x\n\nWhy: because.") == (
            "feat: x\n\nWhy: because."
        )

    async def test_a_lone_fence_line_is_not_a_wrapper(self):
        assert commit_mod._strip_fence("```") == "```"

    async def test_an_inner_fence_is_left_alone(self):
        """A message that quotes code keeps its own fences."""
        message = "fix: quote it\n\n```\nsome code\n```"
        assert commit_mod._strip_fence(message) == message


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    async def test_it_discards_and_tells_everyone(self, service, repo, events):
        answer = await service.reset_to_head()
        assert answer["status"] == "ok"
        assert "Reset to HEAD" in answer["system_event_message"]
        assert repo.resets == 1
        # The broadcast is fire-and-forget, so let the loop run it.
        await finish_turns(service)
        assert "filesModified" in events.names()

    async def test_a_git_failure_is_returned(self, service, repo, events):
        repo.reset_error = RuntimeError("index.lock exists")
        answer = await service.reset_to_head()
        assert "index.lock" in answer["error"]
        await finish_turns(service)
        assert "filesModified" not in events.names()


# ---------------------------------------------------------------------------
# What each one leaves in `events.jsonl`
# ---------------------------------------------------------------------------


class TestTheHistoryRecord:
    """Neither write appears in the engine's transcript.

    The CLI never hears about a commit or a reset, so if these do not reach
    ``.ac-dc4/events.jsonl`` they are absent from browsed history entirely —
    and for the reset that record is the *only* surviving trace of the work
    it destroyed (``specs5/3-engine/history.md`` § One Store, One Index, One
    Events Log).
    """

    @pytest.fixture
    def service(self, service):
        # A record with no session is dropped by design, so these tests
        # need the engine to have connected at least once.
        service.session.session_id = "11111111-1111-4111-8111-111111111111"
        return service

    async def records(self, service, event: str) -> list[dict]:
        loaded = await service.events_log.load(service.session.session_id)
        return [record for record in loaded if record["event"] == event]

    async def test_a_commit_records_its_sha_message_and_files(
        self, service, repo, sdk_query
    ):
        await commit(service)
        (record,) = await self.records(service, "commit")
        assert record["payload"]["sha"] == "0123456789abcdef0123"
        assert record["payload"]["message"] == "feat: add x"
        assert record["payload"]["files"] == ["x.py"]

    async def test_the_toast_and_the_archive_say_the_same_sentence(
        self, service, events, sdk_query
    ):
        """A user comparing the two is entitled to find one wording."""
        await commit(service)
        (record,) = await self.records(service, "commit")
        toast = events.payload_of("commitResult")
        assert record["content"] == toast["system_event_message"]
        assert "`0123456`" in record["content"]

    async def test_a_failed_commit_records_nothing(self, service, repo, sdk_query):
        repo.commit_error = RuntimeError("hook rejected it")
        await commit(service)
        assert await self.records(service, "commit") == []

    async def test_a_reset_records_what_it_destroyed(self, service, repo):
        await service.reset_to_head()
        (record,) = await self.records(service, "reset")
        assert record["payload"]["to"] == "HEAD"
        assert record["payload"]["files"] == ["x.py", "y.py", "z.py"]

    async def test_the_files_are_read_before_they_are_gone(self, service, repo):
        """The fake clears its status on reset, as git does.

        So an empty list here would mean the record was assembled after the
        reset, when there is nothing left to ask — which is the one ordering
        bug this record cannot survive.
        """
        await service.reset_to_head()
        (record,) = await self.records(service, "reset")
        assert record["payload"]["files"]

    async def test_untracked_files_are_not_called_discarded(self, service, repo):
        """``git reset --hard`` leaves them alone, and this record is permanent."""
        await service.reset_to_head()
        (record,) = await self.records(service, "reset")
        assert "scratch.py" not in record["payload"]["files"]

    async def test_a_failed_reset_records_nothing(self, service, repo):
        repo.reset_error = RuntimeError("index.lock exists")
        await service.reset_to_head()
        assert await self.records(service, "reset") == []

    async def test_the_record_lands_before_the_caller_is_told(self, service, repo):
        """Not a fire-and-forget task: the only trace of destroyed work is
        on disk by the time ``reset_to_head`` returns."""
        answer = await service.reset_to_head()
        assert answer["status"] == "ok"
        assert await self.records(service, "reset")

    async def test_a_status_git_will_not_answer_does_not_fail_the_write(
        self, service, repo, sdk_query
    ):
        """The file list is decoration on a record of something that
        happened; losing it must not report a successful write as failed."""
        repo.status_error = RuntimeError("git is confused")
        await commit(service)
        reset = await service.reset_to_head()
        (commit_record,) = await self.records(service, "commit")
        assert commit_record["payload"]["files"] == []
        assert repo.commits == ["feat: add x"]
        assert reset["status"] == "ok"
        (reset_record,) = await self.records(service, "reset")
        assert reset_record["payload"]["files"] == []
