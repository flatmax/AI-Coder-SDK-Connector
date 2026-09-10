"""Tests for the ``agy`` transport's permission gate.

**On this transport the hook is the only gate.** ``agy``'s own headless
permission layer cannot prompt, so the adapter runs with
``--dangerously-skip-permissions`` and everything rests here. That makes
the failure paths the subject of this file rather than an appendix to it:
the happy path is two assertions, and the rest is what happens when the
host is dead, the payload is junk, or the answer is nonsense.

Two properties are load-bearing and neither is obvious from the code:

**A call that is not ours passes through untouched.** The hook is
installed in the user's global ``~/.gemini/config/hooks.json``, because
workspace-local hooks are not loaded headlessly on 1.1.25 — so it is
handed every tool call from every ``agy`` session on the machine,
including the interactive one the user is running themselves. Intercepting
those, or merely stalling them, is a worse outcome than not shipping.

**A call that *is* ours is refused when it cannot be reviewed.** The two
directions are not symmetric and the registry exists to tell them apart:
absent ownership means somebody else's session and allows; present
ownership with an unreachable host means ours and denies. A dead host
makes our sessions un-runnable rather than un-gated.

Everything runs offline. No ``agy``, no network, and the only subprocesses
are this interpreter: the entry-point test, and the ``dead_pid`` fixture,
which runs one and reaps it so the liveness probe is exercised rather than
monkeypatched.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aic_dc.agy import hook, registry

OURS = "cd4edb7f-6de3-468f-9815-e76b310a920a"
THEIRS = "11111111-2222-3333-4444-555555555555"


def payload(conversation_id: str = OURS, tool: str = "replace_file_content"):
    """A `PreToolUse` payload in the shape `agy` 1.1.25 actually sends.

    Field names transcribed from a live capture — see ``sdk-surface.md``
    § *The `agy` hook surface*. ``workspacePaths`` is empty because it
    genuinely is, in every payload measured, which is why ownership is
    keyed on the conversation id instead.
    """
    return {
        "conversationId": conversation_id,
        "modelName": "gemini-3.8-flash-low",
        "stepIdx": 4,
        "workspacePaths": [],
        "toolCall": {
            "name": tool,
            "args": {
                "TargetFile": "/tmp/x/target.txt",
                "TargetContent": "ORIGINAL_TEXT",
                "ReplacementContent": "MODIFIED_TEXT",
            },
        },
    }


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path / "cfg"


class TestSomebodyElsesSession:
    """The common case, because the hook is global."""

    def test_an_unclaimed_conversation_is_allowed(self, config_dir):
        assert hook.decide(payload(THEIRS), config_dir=config_dir) == hook.ALLOW

    def test_it_does_not_even_ask(self, config_dir):
        """Asking would stall a stranger's turn on our socket timeout."""

        def explode(*_args):
            raise AssertionError("the host must not be consulted")

        assert hook.decide(
            payload(THEIRS), config_dir=config_dir, ask=explode
        ) == hook.ALLOW

    def test_a_released_conversation_stops_being_ours(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        registry.release(OURS, config_dir=config_dir)
        assert hook.decide(payload(), config_dir=config_dir) == hook.ALLOW

    def test_releasing_twice_is_not_an_error(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        registry.release(OURS, config_dir=config_dir)
        registry.release(OURS, config_dir=config_dir)

    def test_an_id_cannot_escape_the_registry_directory(self, config_dir):
        """The id is untrusted input arriving on a filesystem path."""
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        assert hook.decide(
            payload("../../../../etc/passwd"), config_dir=config_dir
        ) == hook.ALLOW


class TestOurSession:
    def test_the_hosts_answer_is_returned(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        seen = {}

        def ask(sock_path, sent):
            seen["sock"] = sock_path
            seen["tool"] = sent["toolCall"]["name"]
            return {"decision": "deny", "reason": "the user declined"}

        result = hook.decide(payload(), config_dir=config_dir, ask=ask)
        assert result == {"decision": "deny", "reason": "the user declined"}
        assert seen["sock"] == "/tmp/s.sock"
        # The whole payload goes to the host, so the dialog can render the
        # diff from `TargetContent` / `ReplacementContent`.
        assert seen["tool"] == "replace_file_content"

    def test_an_allow_is_returned_verbatim(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide(
            payload(), config_dir=config_dir, ask=lambda *_: {"decision": "allow"}
        )
        assert result["decision"] == "allow"


class TestOursAndUnreviewable:
    """The direction the registry split exists to make available."""

    @pytest.mark.parametrize(
        "boom",
        [
            ConnectionRefusedError("no listener"),
            FileNotFoundError("socket is gone"),
            TimeoutError("host never answered"),
            ValueError("host sent junk"),
        ],
    )
    def test_an_unreachable_host_denies(self, config_dir, boom):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)

        def ask(*_args):
            raise boom

        result = hook.decide(payload(), config_dir=config_dir, ask=ask)
        assert result["decision"] == "deny"
        # The model is told this was a fault rather than the user's choice,
        # so it does not read a refusal into it and try another route.
        assert "not a refusal by the user" in result["reason"]

    @pytest.mark.parametrize(
        "answer", [None, {}, {"decision": "ask"}, {"decision": ""}, "allow", 7]
    )
    def test_an_unusable_answer_denies(self, config_dir, answer):
        """`ask` is among these on purpose: it auto-denies headlessly.

        If it ever leaked out of this app it would be a refusal the user
        never made and never saw, which is worse than an explicit denial.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide(payload(), config_dir=config_dir, ask=lambda *_: answer)
        assert result["decision"] == "deny"


class TestAPayloadItCannotRead:
    """No id, so no way to tell whose call it is.

    The tie is broken on whether this host is running anything at all,
    because the two errors are not equally bad: refusing breaks a
    stranger's session on a bug of ours, allowing ungates one of ours.
    """

    @pytest.mark.parametrize("junk", [None, "", [], 7, "not json"])
    def test_junk_is_allowed_when_this_host_owns_nothing(self, config_dir, junk):
        assert hook.decide(junk, config_dir=config_dir) == hook.ALLOW

    @pytest.mark.parametrize("junk", [None, [], 7])
    def test_junk_is_denied_while_a_session_is_being_gated(self, config_dir, junk):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide(junk, config_dir=config_dir)
        assert result["decision"] == "deny"

    def test_a_missing_conversation_id_is_not_ours(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        assert hook.decide({"toolCall": {}}, config_dir=config_dir) == hook.ALLOW


class TestTheRegistry:
    def test_a_half_written_entry_reads_as_not_ours(self, config_dir):
        """The hook may read at any instant, so entries are moved into place."""
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        path = registry.registry_dir(config_dir) / f"{OURS}.json"
        path.write_text('{"conversation_id": "cd4', encoding="utf-8")
        assert registry.lookup(OURS, config_dir=config_dir) is None

    def test_an_entry_without_a_socket_is_not_ours(self, config_dir):
        path = registry.registry_dir(config_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{OURS}.json").write_text('{"pid": 1}', encoding="utf-8")
        assert registry.lookup(OURS, config_dir=config_dir) is None

    def test_owns_anything_is_false_before_the_directory_exists(self, config_dir):
        assert registry.owns_anything(config_dir) is False

    def test_the_registry_is_not_written_under_the_google_tree(self, config_dir):
        """That tree belongs to Google's products; our state is ours."""
        assert ".gemini" not in str(registry.registry_dir(config_dir))


@pytest.fixture
def dead_pid():
    """A pid that named a process and no longer does.

    Run and reaped, so the kernel has genuinely let it go — the point is to
    exercise the real probe rather than a monkeypatched one. Pid reuse
    inside the remaining milliseconds of a test is the only way this lies,
    and it would lie in the direction of "alive", which fails the test
    rather than passing it wrongly.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class TestAnEntryLeftByAHostThatIsGone:
    """AG-R-14 residue 3: ``claim`` wrote a ``pid`` and nothing read it.

    A killed host leaves a file behind, and until that pid was read the file
    was indistinguishable from a live claim — so the hook went on denying
    tool calls against a socket nobody was listening on, and
    ``owns_anything`` counted the corpse forever.

    The distinction these tests hold is the one that makes reading the pid
    safe: **a dead host is not the same as a dead session.** Our ``agy`` is
    a child, and a child outlives a killed parent, so an entry is only a
    corpse when both are gone.
    """

    def test_a_dead_hosts_entry_is_not_ours(self, config_dir, dead_pid):
        registry.claim(
            OURS, "/tmp/s.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=dead_pid,
        )
        assert registry.lookup(OURS, config_dir=config_dir) is None
        # Which is what the user meets: their own resumed conversation is
        # waved through instead of denied against a socket that is gone.
        assert hook.decide(payload(), config_dir=config_dir) == hook.ALLOW

    def test_an_orphaned_agent_is_still_denied(self, config_dir, dead_pid):
        """The case that makes "stale means allow" the wrong rule.

        Host killed, its ``agy`` still running: allowing here would put an
        unreviewed write into the repository, which is the one thing this
        transport has no second check for.
        """
        registry.claim(
            OURS, "/tmp/s.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=os.getpid(),
        )
        assert registry.lookup(OURS, config_dir=config_dir) is not None

        def ask(*_args):
            raise ConnectionRefusedError("the host that spawned it is gone")

        result = hook.decide(payload(), config_dir=config_dir, ask=ask)
        assert result["decision"] == "deny"

    def test_a_live_host_whose_agy_died_still_owns_the_conversation(
        self, config_dir, dead_pid
    ):
        """It is the host's to release; the gate is up and can be asked."""
        registry.claim(
            OURS, "/tmp/s.sock", config_dir=config_dir,
            pid=os.getpid(), agy_pid=dead_pid,
        )
        assert registry.lookup(OURS, config_dir=config_dir) is not None

    def test_a_legacy_conversation_entry_with_a_dead_host_is_a_corpse(
        self, config_dir, dead_pid
    ):
        """The grandfather clause, closed 2026-09-10 by measuring the orphan.

        A conversation entry is written when ``init`` names the conversation,
        so the host already has a child to name and every version that
        records an ``agy_pid`` records it here. A missing one therefore dates
        the file rather than describing it — and the agent it would have
        named outlives its host by under a second
        (``scripts/probe_agy_orphan.py``). Left immortal, these were what
        kept ``owns_anything`` permanently true on any machine that had ever
        lost a host uncleanly: eight of them on the development machine, one
        per unclean exit, each denying the user's own unparseable payloads
        from then on.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir, pid=dead_pid)
        assert registry.lookup(OURS, config_dir=config_dir) is None
        assert registry.owns_anything(config_dir) is False
        assert registry.reap_stale(config_dir) == [OURS]

    def test_a_legacy_entry_whose_host_is_alive_is_untouched(self, config_dir):
        """The host is the first question and it still answers.

        Age is only ever the *second* argument: an old entry belonging to a
        process that is still running is a live claim, and reaping it would
        un-gate a session that is happening right now.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir, pid=os.getpid())
        assert registry.lookup(OURS, config_dir=config_dir) is not None
        assert registry.reap_stale(config_dir) == []

    def test_a_scope_entry_with_no_agy_pid_is_never_a_corpse(
        self, config_dir, dead_pid
    ):
        """The asymmetry, and the reason the age argument must not reach here.

        A scope claim is published *before* ``agy`` exists — that is the whole
        of AG-18 — so an absent ``agy_pid`` is the normal state of a session
        that is starting rather than a mark of an old file. Reaping one on the
        host pid alone would release the unit an orphaned agent is still
        sitting in, under ``--dangerously-skip-permissions``.
        """
        unit = "aic-dc-starting"
        registry.claim_scope(unit, "/tmp/s.sock", config_dir=config_dir, pid=dead_pid)
        cgroup = f"0::/user.slice/user-1000.slice/{unit}.scope"
        assert registry.scope_owner(cgroup, config_dir=config_dir) is not None
        assert registry.reap_stale(config_dir) == []

    def test_owns_anything_stops_counting_a_corpse(self, config_dir, dead_pid):
        """The tie-breaker's own version of this defect, and the worse half.

        ``owns_anything`` decides an unparseable payload, and a payload with
        no id may belong to anyone — so one unclean exit of ours used to
        make every junk payload from *the user's* own ``agy`` deny, forever.
        """
        registry.claim(
            OURS, "/tmp/s.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=dead_pid,
        )
        assert registry.owns_anything(config_dir) is False
        assert hook.decide(None, config_dir=config_dir) == hook.ALLOW

    def test_owns_anything_counts_a_claim_still_being_written(self, config_dir):
        """A half-written entry is a session of ours starting, not a corpse."""
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        path = registry.registry_dir(config_dir) / f"{OURS}.json"
        path.write_text('{"conversation_id": "cd4', encoding="utf-8")
        assert registry.owns_anything(config_dir) is True


class TestReapingCorpses:
    def test_it_removes_a_dead_hosts_entry_and_names_it(
        self, config_dir, dead_pid
    ):
        registry.claim(
            OURS, "/tmp/s.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=dead_pid,
        )
        assert registry.reap_stale(config_dir) == [OURS]
        assert not (registry.registry_dir(config_dir) / f"{OURS}.json").exists()

    def test_it_leaves_another_live_hosts_entry_alone(self, config_dir, dead_pid):
        """Two hosts share one registry directory.

        The sweep is keyed on liveness rather than on ownership for exactly
        this reason: "not mine" would delete the entry of a second host
        running beside us and un-gate its session.
        """
        registry.claim(
            OURS, "/tmp/a.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=dead_pid,
        )
        registry.claim("other-host", "/tmp/b.sock", config_dir=config_dir)
        assert registry.reap_stale(config_dir) == [OURS]
        assert registry.lookup("other-host", config_dir=config_dir) is not None

    def test_it_leaves_an_unreadable_entry_alone(self, config_dir):
        """There is no liveness question to ask of it, and it denies nothing."""
        directory = registry.registry_dir(config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{OURS}.json").write_text("{half", encoding="utf-8")
        assert registry.reap_stale(config_dir) == []
        assert (directory / f"{OURS}.json").exists()

    def test_it_is_quiet_before_the_directory_exists(self, config_dir):
        assert registry.reap_stale(config_dir) == []


class TestAskingWhetherAProcessExists:
    """Every unanswerable case answers "alive", and that is deliberate.

    A live pid is a reason to leave an entry standing, and a standing entry
    makes the hook deny. So the direction of every doubt below costs a
    refusal rather than an unreviewed tool call.
    """

    @pytest.mark.parametrize("pid", [None, 0, -1, "1", True, 1.0, {}])
    def test_a_pid_that_is_not_a_pid_reads_as_alive(self, pid):
        assert registry.process_alive(pid) is True

    def test_this_process_is_alive(self):
        assert registry.process_alive(os.getpid()) is True

    def test_a_reaped_process_is_not(self, dead_pid):
        assert registry.process_alive(dead_pid) is False

    def test_os_kill_is_never_called_off_posix(self, monkeypatch, dead_pid):
        """On Windows ``os.kill`` is ``TerminateProcess``.

        A liveness probe that killed the process it asked about would be a
        worse bug than the one this function fixes, so there is no probe
        there — the answer is "alive" and the entry stands.
        """
        def explode(*_args):
            raise AssertionError("os.kill must not be reached off POSIX")

        monkeypatch.setattr(registry.os, "name", "nt")
        monkeypatch.setattr(registry.os, "kill", explode)
        assert registry.process_alive(dead_pid) is True

    def test_a_pid_owned_by_another_user_reads_as_alive(self, monkeypatch):
        """It exists; it is simply not ours to signal."""
        def refuse(*_args):
            raise PermissionError("not yours")

        monkeypatch.setattr(registry.os, "kill", refuse)
        assert registry.process_alive(4242) is True


class TestAScopeEntryIsSweptByTheSameRule:
    """AG-18 doubled the kinds of entry a killed host can leave behind.

    A scope entry is the one where getting this wrong costs more, and in the
    opposite direction to the intuition: the unit an abandoned entry names is
    exactly where an orphaned ``agy`` still sits — same unit, so
    ``scope_owner`` still matches it — and it is under
    ``--dangerously-skip-permissions``. So reaping on the host pid alone
    would open the tree rather than tidy a file.
    """

    UNIT = "aic-dc-old"
    CGROUP = "0::/user.slice/user-1000.slice/aic-dc-old.scope"

    def test_a_scope_whose_host_and_agent_are_both_gone_is_reaped(
        self, config_dir, dead_pid
    ):
        registry.claim_scope(
            self.UNIT, "/tmp/dead.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=dead_pid,
        )
        assert registry.reap_stale(config_dir) == [f"scope-{self.UNIT}"]
        assert registry.scope_owner(self.CGROUP, config_dir=config_dir) is None

    def test_a_scope_holding_an_orphaned_agent_is_kept(self, config_dir, dead_pid):
        """The case that makes the second pid worth writing down.

        Host killed, its ``agy`` still running inside the unit. The entry
        stands, ``scope_owner`` goes on matching, and the calls go on being
        denied — which is the AG-5 trade rather than a leak.
        """
        registry.claim_scope(
            self.UNIT, "/tmp/dead.sock", config_dir=config_dir,
            pid=dead_pid, agy_pid=os.getpid(),
        )
        assert registry.reap_stale(config_dir) == []
        assert registry.scope_owner(self.CGROUP, config_dir=config_dir) is not None

    def test_a_scope_claimed_before_its_agent_existed_is_kept(
        self, config_dir, dead_pid
    ):
        """The first write of every scope entry has no ``agy_pid`` to give.

        ``start`` publishes it before the child exists, on purpose, so
        nothing can beat it onto disk. Until ``claim`` rewrites it the entry
        cannot be read as a corpse — the same "cannot tell, so leave it
        standing" the conversation entries use, arrived at by design rather
        than by age.
        """
        registry.claim_scope(
            self.UNIT, "/tmp/dead.sock", config_dir=config_dir, pid=dead_pid
        )
        assert registry.reap_stale(config_dir) == []
        assert registry.scope_owner(self.CGROUP, config_dir=config_dir) is not None


class TestTheProcessAlwaysPrints:
    """The one fail-open path on this transport, closed by construction.

    ``agy`` parses exit-0-with-empty-stdout as ``{}``, whose empty decision
    defaults to **allow**. Every other failure — non-zero exit, malformed
    JSON, missing command, exceeding the timeout — blocks. So the only way
    this gate lets something through by accident is by printing nothing,
    and that is what these tests are about.
    """

    def run(self, stdin: str, config_dir: Path):
        return subprocess.run(
            [sys.executable, "-m", "aic_dc.agy.hook", str(config_dir)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_decision_is_printed_for_junk_stdin(self, config_dir):
        done = self.run("this is not json", config_dir)
        assert done.returncode == 0
        assert json.loads(done.stdout)["decision"] in ("allow", "deny")

    def test_a_decision_is_printed_for_empty_stdin(self, config_dir):
        done = self.run("", config_dir)
        assert json.loads(done.stdout)["decision"] in ("allow", "deny")

    def test_stdout_is_never_empty(self, config_dir):
        """Stated as its own assertion because it is the whole risk."""
        for stdin in ("", "null", "[]", json.dumps(payload(THEIRS))):
            assert self.run(stdin, config_dir).stdout.strip()

    def test_a_strangers_call_is_allowed_end_to_end(self, config_dir):
        done = self.run(json.dumps(payload(THEIRS)), config_dir)
        assert json.loads(done.stdout) == {"decision": "allow"}


class TestOverTheRealSocket:
    """One end-to-end pass over an actual unix socket.

    Everything above injects `ask`, which would keep passing if the wire
    format were wrong in both directions at once.
    """

    def test_a_decision_travels_both_ways(self, config_dir, tmp_path):
        sock_path = str(tmp_path / "gate.sock")
        received: list[dict] = []

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                received.append(json.loads(data.decode("utf-8")))
                conn.sendall(
                    json.dumps({"decision": "deny", "reason": "declined"}).encode()
                    + b"\n"
                )

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            registry.claim(OURS, sock_path, config_dir=config_dir)
            result = hook.decide(payload(), config_dir=config_dir)
        finally:
            thread.join(timeout=10)
            server.close()

        assert result == {"decision": "deny", "reason": "declined"}
        assert received[0]["toolCall"]["args"]["ReplacementContent"] == "MODIFIED_TEXT"


def invocation(conversation_id: str = OURS, num: int = 2):
    """A ``PostInvocation`` payload, in the shape ``hooks.md`` documents.

    Same common fields as a tool call and no ``toolCall`` at all, which is
    the point: nothing about this payload could be mistaken for something
    to put in a dialog.
    """
    return {
        "conversationId": conversation_id,
        "modelName": "gemini-3.8-flash-low",
        "workspacePaths": [],
        "invocationNum": num,
        "initialNumSteps": 4,
    }


def stopped(conversation_id: str = OURS, reason: str = "NO_TOOL_CALL"):
    """A ``Stop`` payload. ``terminationReason`` is the field that matters."""
    return {
        "conversationId": conversation_id,
        "modelName": "gemini-3.8-flash-low",
        "executionNum": 1,
        "terminationReason": reason,
        "error": "",
        "fullyIdle": True,
    }


class TestTheStopIsAMechanism:
    """``PostInvocation`` — AG-19.

    The gate refuses tool calls and hopes the agent takes the hint. This
    ends the loop whether it takes it or not, and it reads the *same*
    latch, so there is no second stop state to get out of step.
    """

    def test_a_stopped_conversation_is_terminated(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide_invocation(
            invocation(),
            config_dir=config_dir,
            ask=lambda *_: {"terminationBehavior": "terminate"},
        )
        assert result == {"terminationBehavior": "terminate"}

    def test_a_running_conversation_proceeds(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide_invocation(
            invocation(), config_dir=config_dir, ask=lambda *_: {}
        )
        assert result == {}

    def test_a_strangers_loop_is_never_ended(self, config_dir):
        """The worst thing this hook could do, and it is global."""

        def explode(*_args):
            raise AssertionError("the host must not be consulted")

        assert (
            hook.decide_invocation(
                invocation(THEIRS), config_dir=config_dir, ask=explode
            )
            == {}
        )

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            {},
            7,
            "terminate",
            {"terminationBehavior": "force_continue"},
            {"terminationBehavior": ""},
            {"decision": "deny", "reason": "the user declined"},
        ],
    )
    def test_anything_but_terminate_proceeds(self, config_dir, answer):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide_invocation(
            invocation(), config_dir=config_dir, ask=lambda *_: answer
        )
        assert result == {}

    def test_the_answer_is_built_here_not_forwarded(self, config_dir):
        """A host bug must not reach ``agy`` through this path.

        ``injectSteps`` would put words in the model's mouth and a
        ``decision`` is not this event's vocabulary at all. One value is
        read off the socket and the rest is dropped.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        result = hook.decide_invocation(
            invocation(),
            config_dir=config_dir,
            ask=lambda *_: {
                "terminationBehavior": "terminate",
                "injectSteps": [{"userMessage": "keep going"}],
                "decision": "allow",
            },
        )
        assert result == {"terminationBehavior": "terminate"}

    def test_an_unreachable_host_lets_the_loop_run(self, config_dir):
        """The opposite direction from the gate, and AG-19 says so.

        A failure here costs a loop that keeps running, on a tree the
        ``PreToolUse`` gate is still refusing every call against. A failure
        that *ended* loops would end a stranger's.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)

        def boom(*_args):
            raise ConnectionRefusedError("no listener")

        assert (
            hook.decide_invocation(invocation(), config_dir=config_dir, ask=boom) == {}
        )

    @pytest.mark.parametrize("junk", [None, "", [], 7])
    def test_junk_proceeds_even_while_a_session_is_gated(self, config_dir, junk):
        """Where :func:`decide` denies. The asymmetry is the design."""
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        assert hook.decide_invocation(junk, config_dir=config_dir) == {}
        assert hook.decide(junk, config_dir=config_dir)["decision"] == "deny"

    def test_the_event_reaches_the_host_stamped(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        seen = {}

        def ask(_sock, sent):
            seen.update(sent)
            return {}

        hook.decide_invocation(invocation(), config_dir=config_dir, ask=ask)
        assert seen[hook.EVENT_KEY] == "PostInvocation"

    def test_a_payload_cannot_name_its_own_event(self, config_dir):
        """The stamp is overwritten, and that is a safety property.

        The host answers a different shape per event, and ``{}`` — right
        for an invocation hook — is what ``agy`` reads as *allow* on a tool
        call. A payload able to relabel itself could ask for a tool call to
        be answered in the shape that waves it through.
        """
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        seen = {}

        def ask(_sock, sent):
            seen.update(sent)
            return {}

        liar = {**invocation(), hook.EVENT_KEY: "PreToolUse"}
        hook.decide_invocation(liar, config_dir=config_dir, ask=ask)
        assert seen[hook.EVENT_KEY] == "PostInvocation"

        seen.clear()
        liar = {**payload(), hook.EVENT_KEY: "Stop"}
        hook.decide(liar, config_dir=config_dir, ask=lambda _s, sent: seen.update(sent)
                    or {"decision": "allow"})
        assert seen[hook.EVENT_KEY] == "PreToolUse"


class TestTheStopEventAlwaysPermitsTheStop:
    """``Stop`` — AG-R-16.

    ``{"decision": "continue"}`` blocks a stop and re-enters the loop, and
    ``~/.gemini/config/hooks.json`` is a file this app writes one entry
    into and does not own. So this app is always a voice for stopping, and
    the string it would take to be a voice against one never appears on
    this path.
    """

    @pytest.mark.parametrize(
        "answer",
        [
            {"decision": "continue", "reason": "the tests are still running"},
            {"decision": "continue"},
            {"terminationBehavior": "force_continue"},
            None,
            7,
        ],
    )
    def test_the_host_cannot_revive_the_loop(self, config_dir, answer):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        assert (
            hook.report_stop(stopped(), config_dir=config_dir, ask=lambda *_: answer)
            == {}
        )

    def test_an_unreachable_host_still_permits_the_stop(self, config_dir):
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)

        def boom(*_args):
            raise ConnectionRefusedError("no listener")

        assert hook.report_stop(stopped(), config_dir=config_dir, ask=boom) == {}

    def test_the_host_is_told_which_reason_ended_the_loop(self, config_dir):
        """The round trip's whole purpose: ``terminationReason`` is here
        and nowhere on the stream."""
        registry.claim(OURS, "/tmp/s.sock", config_dir=config_dir)
        seen = {}

        def ask(_sock, sent):
            seen.update(sent)
            return {}

        hook.report_stop(
            stopped(reason="TERMINAL_CUSTOM_HOOK"), config_dir=config_dir, ask=ask
        )
        assert seen["terminationReason"] == "TERMINAL_CUSTOM_HOOK"
        assert seen[hook.EVENT_KEY] == "Stop"

    def test_a_strangers_stop_is_not_reported(self, config_dir):
        def explode(*_args):
            raise AssertionError("the host must not be consulted")

        assert (
            hook.report_stop(stopped(THEIRS), config_dir=config_dir, ask=explode) == {}
        )


class TestTheEventComesFromArgv:
    def test_the_gates_command_has_no_event(self):
        assert hook.parse_argv(["/cfg"]) == ("/cfg", "PreToolUse")

    @pytest.mark.parametrize("event", ["PostInvocation", "Stop"])
    def test_an_invocation_event_is_read(self, event):
        assert hook.parse_argv(["/cfg", "--event", event]) == ("/cfg", event)

    @pytest.mark.parametrize(
        "argv",
        [
            ["/cfg", "--event", "PostToolUse"],
            ["/cfg", "--event", "preToolUse"],
            ["/cfg", "--event"],
            ["/cfg", "--event", ""],
        ],
    )
    def test_anything_unrecognised_is_the_gate(self, argv):
        """The fail-closed direction, and it is not a tidy default.

        An event this parser could not name, answered as an invocation
        hook, would print ``{}`` — and ``{}`` on a tool call is *allow*.
        """
        assert hook.parse_argv(argv)[1] == "PreToolUse"

    def test_no_arguments_at_all_is_still_the_gate(self):
        assert hook.parse_argv([]) == (None, "PreToolUse")
        assert hook.parse_argv(None) == (None, "PreToolUse")


class TestTheProcessPrintsTheRightShape:
    """End to end, one subprocess per event.

    The gate must never print ``{}``; the other two must never print
    anything else when they have no opinion. Both are properties of
    :func:`hook.main`'s dispatch rather than of any decision it makes.
    """

    def run(self, stdin: str, config_dir: Path, event: str | None = None):
        argv = [sys.executable, "-m", "aic_dc.agy.hook", str(config_dir)]
        if event:
            argv += ["--event", event]
        return subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=60
        )

    @pytest.mark.parametrize("event", ["PostInvocation", "Stop"])
    @pytest.mark.parametrize("stdin", ["", "this is not json", "null", "[]"])
    def test_an_invocation_hook_says_nothing_on_junk(
        self, config_dir, event, stdin
    ):
        done = self.run(stdin, config_dir, event)
        assert done.returncode == 0
        assert json.loads(done.stdout) == {}

    @pytest.mark.parametrize("event", ["PostInvocation", "Stop"])
    def test_a_strangers_invocation_is_untouched(self, config_dir, event):
        done = self.run(json.dumps(invocation(THEIRS)), config_dir, event)
        assert json.loads(done.stdout) == {}

    def test_the_gate_never_prints_the_empty_object(self, config_dir):
        """Restated as its own assertion because it is the whole risk."""
        for stdin in ("", "this is not json", "null", "[]", json.dumps(payload())):
            printed = json.loads(self.run(stdin, config_dir).stdout)
            assert printed != {}
            assert printed["decision"] in ("allow", "deny")

    def test_a_stop_event_never_prints_continue(self, config_dir):
        for stdin in ("", json.dumps(stopped()), json.dumps(stopped(THEIRS))):
            assert "continue" not in self.run(stdin, config_dir, "Stop").stdout
