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

Everything runs offline. No ``agy``, no network, no subprocess except the
one entry-point test, which runs this interpreter.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aic_dc.antigravity.agy import hook, registry

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
            [sys.executable, "-m", "aic_dc.antigravity.agy.hook", str(config_dir)],
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
