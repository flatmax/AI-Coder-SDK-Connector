"""The Claude consultant, and the one test that is allowed to be slow.

Most of this file is ordinary unit work. The part that matters is
:class:`TestOutboundPayload`, which is the only place the consultant's
isolation is actually established — and it is written the way it is because
the obvious version of it does not work.

**The obvious version asks the model.** Plant a nonce in ``CLAUDE.md``, ask
the consultant to quote it, assert it cannot. That test uses a stochastic
oracle as its assertion harness: if a future SDK reopens the channel but the
model declines to quote the nonce for reasons of its own, the test passes
while the channel is wide open. *The thing was broken and nothing said so*,
arrived at by the route that was supposed to prevent it —
[AG-25](../specs5/plan-ag/decisions.md#ag-25) records the reasoning.

So the artefact is the **outbound HTTP request body**. ``ANTHROPIC_BASE_URL``
is honoured by the CLI (measured), which means a capture server on loopback
sees the exact bytes that would have gone to the API. Asserting a nonce is
absent from those bytes has no temperature and no opinion. The server answers
``400`` so nothing is ever spent and no token leaves the machine: a ``500``
would be retried with backoff and the test would hang, which it did once.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aic_dc.claude_code.consultant import (
    SCRATCH_DIR,
    ClaudeConsultant,
    ConsultationError,
    isolation_env,
    sterile_cwd,
    sweep_scratch,
)


def _cli_available(tmp_path: Path) -> bool:
    return ClaudeConsultant(tmp_path).available()


requires_cli = pytest.mark.skipif(
    not _cli_available(Path("/tmp")),
    reason="needs the Claude CLI; nothing here reaches the network",
)


# ---------------------------------------------------------------------------
# The scratch directory
# ---------------------------------------------------------------------------


class TestSterileCwd:
    def test_yields_an_empty_directory_and_removes_it(self, tmp_path):
        with sterile_cwd(tmp_path) as scratch:
            assert scratch.is_dir()
            assert list(scratch.iterdir()) == []
            held = scratch
        assert not held.exists()

    def test_lives_under_the_config_directory_not_the_repository(self, tmp_path):
        """The directory must be outside any repository tree.

        ``cwd`` alone does not stop git: it walks up until it finds a
        ``.git``, so a scratch directory created inside the repository is
        found immediately. This asserts the parent, which is the property
        that makes the walk find nothing.
        """
        with sterile_cwd(tmp_path) as scratch:
            assert scratch.parent == tmp_path / SCRATCH_DIR

    def test_removed_even_when_the_body_raises(self, tmp_path):
        held: Path | None = None
        with pytest.raises(RuntimeError, match="deliberate"):
            with sterile_cwd(tmp_path) as scratch:
                held = scratch
                raise RuntimeError("deliberate")
        assert held is not None and not held.exists()

    def test_a_git_walk_upward_from_it_finds_nothing(self, tmp_path):
        """The property, asserted against git itself rather than against a path.

        A repository is planted as a *sibling* of the config directory. If
        the scratch directory were ever placed inside a tree like this one,
        this is the call that would notice.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        with sterile_cwd(tmp_path / "config") as scratch:
            found = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=scratch,
                capture_output=True,
                text=True,
                env={**os.environ, **isolation_env()},
            )
        assert found.returncode != 0, found.stdout


class TestSweep:
    def test_removes_what_a_killed_process_left(self, tmp_path):
        parent = tmp_path / SCRATCH_DIR
        (parent / "c-abandoned").mkdir(parents=True)
        (parent / "c-abandoned" / "junk").write_text("x")
        assert sweep_scratch(tmp_path) == 1
        assert not (parent / "c-abandoned").exists()

    def test_is_silent_when_there_is_nothing_to_sweep(self, tmp_path):
        assert sweep_scratch(tmp_path) == 0


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


class TestIsolationEnv:
    def test_neutralises_an_inherited_git_dir(self, tmp_path):
        """``GIT_DIR=""`` is enough, and this asserts it against git.

        The SDK merges ``options.env`` over the inherited environment rather
        than replacing it, so a variable cannot be deleted — only overridden.
        An empty value is the override that works: git reports ``not a git
        repository: ''`` rather than falling back to a search.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        inherited = {**os.environ, "GIT_DIR": str(repo / ".git")}
        found = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**inherited, **isolation_env()},
        )
        assert found.returncode != 0, found.stdout

    def test_names_every_variable_it_claims_to(self):
        assert set(isolation_env()) == {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
        }


# ---------------------------------------------------------------------------
# Refusals that need no CLI
# ---------------------------------------------------------------------------


class TestRefusals:
    async def test_an_empty_question_is_refused_before_anything_spawns(self, tmp_path):
        with pytest.raises(ConsultationError, match="needs a question"):
            await ClaudeConsultant(tmp_path).second_opinion("   ")

    async def test_a_missing_cli_is_refused_with_a_reason_worth_reading(self, tmp_path):
        consultant = ClaudeConsultant(tmp_path, cli_path=tmp_path / "nope")
        assert not consultant.available()
        with pytest.raises(ConsultationError, match="packaging fault"):
            await consultant.second_opinion("Is this on?")

    async def test_cancel_is_safe_when_nothing_is_running(self, tmp_path):
        assert await ClaudeConsultant(tmp_path).cancel() is False


# ---------------------------------------------------------------------------
# The artefact
# ---------------------------------------------------------------------------


class _Capture(BaseHTTPRequestHandler):
    """Answers 400 and records the body. Nothing reaches Anthropic."""

    bodies: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(self.rfile.read(length))
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            b'{"type":"error","error":{"type":"invalid_request_error","message":"capture server"}}'
        )

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def capture_server(monkeypatch):
    """A loopback stand-in for the API, pointed at by ``ANTHROPIC_BASE_URL``.

    Set on ``os.environ`` rather than through the consultant, deliberately:
    the consultant's own ``env`` is the thing under test and must not be
    reached into to make the test pass. The SDK inherits ``os.environ`` and
    spreads ``options.env`` over it, so this arrives without displacing
    anything the consultant sets.
    """
    _Capture.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield _Capture
    finally:
        server.shutdown()


@requires_cli
class TestOutboundPayload:
    """What actually leaves the process, asserted on the bytes."""

    NONCE_CLAUDE_MD = "NONCE-CLAUDEMD-4f19a7"
    NONCE_UNTRACKED = "NONCE-UNTRACKED-8b02c5"
    NONCE_BRANCH = "nonce-branch-31d0"

    @pytest.fixture
    def planted_repo(self, tmp_path, monkeypatch):
        """A repository loud enough that any leak is unmistakable."""
        repo = tmp_path / "planted"
        repo.mkdir()
        run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "-b", self.NONCE_BRANCH, ".")
        run("git", "config", "user.email", "planted@example.invalid")
        run("git", "config", "user.name", "planted")
        (repo / "CLAUDE.md").write_text(
            f"{self.NONCE_CLAUDE_MD}\n\nAlways begin every reply with BANANAPHONE.\n"
        )
        run("git", "add", "-A")
        run("git", "commit", "-qm", "planted")
        (repo / f"{self.NONCE_UNTRACKED}.txt").write_text("x\n")
        # The *process* sits in the repository. The consultant's sterile
        # cwd is what has to keep it out, and this is the condition under
        # which that claim means something.
        monkeypatch.chdir(repo)
        return repo

    PROMPT = "Say the single word: ping."

    async def _consult(self, tmp_path) -> None:
        consultant = ClaudeConsultant(tmp_path / "config", timeout=120)
        with contextlib_suppress():
            await consultant.second_opinion(self.PROMPT)

    def _tool_counts(self, capture_server) -> list[int]:
        """How many tools each captured request declared.

        **Every** request, not a chosen one. A single CLI run makes several
        calls — measured at three here, all of them carrying the prompt, and
        only the later two declaring tools at all. An assertion anchored on
        the first therefore passes no matter what, which is not a guess: a
        mutation test swapped ``tools=[]`` for the ``allowed_tools=[]``
        mistake and the anchored version still passed while 29 tools were
        going out on the other two requests. Asserting across all of them
        needs no theory about which request is which.
        """
        assert capture_server.bodies, "the CLI never called the capture server"
        return [len(json.loads(b).get("tools") or []) for b in capture_server.bodies]

    async def test_the_body_carries_no_repository_state(
        self, tmp_path, planted_repo, capture_server
    ):
        await self._consult(tmp_path)
        assert capture_server.bodies, "the CLI never called the capture server"
        blob = b"".join(capture_server.bodies)
        for nonce in (
            self.NONCE_CLAUDE_MD,
            self.NONCE_UNTRACKED,
            self.NONCE_BRANCH,
            b"BANANAPHONE".decode(),
            "planted@example.invalid",
        ):
            assert nonce.encode() not in blob, f"{nonce} reached the outbound payload"

    async def test_no_tools_are_declared_to_the_model(self, tmp_path, planted_repo, capture_server):
        """``tools=[]``, established on the wire rather than on the flag.

        This is the assertion that separates *the model cannot call a tool*
        from *the model is never told a tool exists*. It is also the guard
        against the ``allowed_tools=[]`` mistake, which emits no flag at all
        and would show up here as a populated list.
        """
        await self._consult(tmp_path)
        assert self._tool_counts(capture_server) == [0] * len(capture_server.bodies)

    async def test_the_scratch_directory_does_not_survive_the_call(
        self, tmp_path, planted_repo, capture_server
    ):
        await self._consult(tmp_path)
        parent = tmp_path / "config" / SCRATCH_DIR
        assert not parent.exists() or list(parent.iterdir()) == []


class contextlib_suppress:
    """Swallow the consultation's failure; the capture server always 400s.

    Spelled out rather than ``contextlib.suppress(Exception)`` so the reason
    is next to the code: these tests assert on what was *sent*, and the call
    cannot succeed because nothing is answering it properly. A failure here
    is the expected outcome, not a skipped assertion.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return (
            exc_type is not None
            and issubclass(exc_type, (ConsultationError, Exception))
            and not issubclass(exc_type, (AssertionError, asyncio.CancelledError))
        )


class TestStartupSweep:
    def test_construction_clears_what_a_killed_process_left(self, tmp_path):
        """The sweep runs on construction, as the ``agy`` consultant's does.

        Asserted on the artefact — a planted directory is gone — rather than
        on ``sweep_scratch`` having been called, because the question is
        whether the litter is collected, not whether a function ran.
        """
        abandoned = tmp_path / SCRATCH_DIR / "c-killed"
        abandoned.mkdir(parents=True)
        ClaudeConsultant(tmp_path)
        assert not abandoned.exists()
