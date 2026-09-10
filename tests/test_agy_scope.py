"""Cgroup identity for the `agy` gate — AG-R-14.

The residues this closes are both properties of one premise: that a call is
ours only if somebody wrote its conversation id down *before* it arrived. A
grandchild is never announced, so nobody writes it down; a child starts before
its announcement, so the write can lose the race.

These tests are about the seam, not the kernel. That cgroup v2 contains
`env -i` and a `setsid` double-fork is measured in
`scripts/probe_agy_cgroup_identity.py` and recorded in risks.md; asserting it
here would be asserting Linux. What is asserted here is that we ask the right
question, of the right thing, at the right time — and, most of all, that the
answer is still *no* for a session that is not ours.

Offline. No systemd, no agy, no network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aic_dc.agy import registry, scope
from aic_dc.agy.gate_server import AgyGateServer
from aic_dc.agy.hook import decide
from aic_dc.agy.session import AgySession
from aic_dc.antigravity.permissions import AntigravityPermissionGate

UNIT = "aic-dc-testunit"
IN_SCOPE = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}.scope"
A_TERMINAL = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "ptyxis-spawn-78bfabad-9eb9-42f4-879b-5f6865548ceb.scope"
)


class TestScopeOwner:
    """Which cgroups the registry claims, and — harder — which it does not."""

    def test_a_scope_we_registered_is_ours(self, tmp_path):
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        entry = registry.scope_owner(IN_SCOPE, config_dir=tmp_path)
        assert entry is not None
        assert entry["socket"] == "/tmp/g.sock"

    def test_a_terminals_own_scope_is_not(self, tmp_path):
        """AG-R-12, which is non-negotiable: the user's own session."""
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        assert registry.scope_owner(A_TERMINAL, config_dir=tmp_path) is None

    def test_a_unit_named_like_ours_but_never_registered_is_not(self, tmp_path):
        """Matched against what this host wrote, never against the name's shape.

        A prefix test would let anybody who named a scope `aic-dc-…` route
        their calls into our permission dialog.
        """
        other = IN_SCOPE.replace(UNIT, "aic-dc-somebody-elses")
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        assert registry.scope_owner(other, config_dir=tmp_path) is None

    def test_a_longer_unit_sharing_our_prefix_is_not(self, tmp_path):
        """`aic-dc-ab` must not match `aic-dc-abcdef`, which is why the
        comparison includes the `.scope` suffix systemd renders."""
        registry.claim_scope("aic-dc-ab", "/tmp/g.sock", config_dir=tmp_path)
        longer = IN_SCOPE.replace(UNIT, "aic-dc-abcdef")
        assert registry.scope_owner(longer, config_dir=tmp_path) is None

    def test_released_scopes_stop_being_ours(self, tmp_path):
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        registry.release_scope(UNIT, config_dir=tmp_path)
        assert registry.scope_owner(IN_SCOPE, config_dir=tmp_path) is None

    def test_an_empty_cgroup_owns_nothing(self, tmp_path):
        """What `current_cgroup()` returns off Linux."""
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        assert registry.scope_owner("", config_dir=tmp_path) is None

    def test_a_scope_entry_is_not_a_conversation(self, tmp_path):
        """The two files live in one directory and must not be confused:
        `lookup` takes an id `agy` generated, `scope_owner` a unit we chose."""
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        assert registry.lookup(UNIT, config_dir=tmp_path) is None


class TestTheHookAdoptsOnFirstContact:
    """The residues, at the seam that closes them."""

    def test_an_unclaimed_conversation_in_our_scope_is_gated(
        self, tmp_path, monkeypatch
    ):
        """This is the grandchild. Nobody announced it, nobody claimed it,
        and before this change it was passed through ungated."""
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        monkeypatch.setattr(scope, "current_cgroup", lambda: IN_SCOPE)
        asked = []

        def ask(socket_path, payload):
            asked.append((socket_path, payload.get("conversationId")))
            return {"decision": "deny", "reason": "no"}

        answer = decide(
            {"conversationId": "never-registered", "toolCall": {"name": "run_command"}},
            config_dir=tmp_path,
            ask=ask,
        )
        assert answer["decision"] == "deny"
        assert asked == [("/tmp/g.sock", "never-registered")]

    def test_a_stranger_in_their_own_scope_is_still_passed_through(
        self, tmp_path, monkeypatch
    ):
        """The property that must survive every change to this hook."""
        registry.claim_scope(UNIT, "/tmp/g.sock", config_dir=tmp_path)
        monkeypatch.setattr(scope, "current_cgroup", lambda: A_TERMINAL)

        def ask(*_a, **_k):
            raise AssertionError("a stranger's call reached the host")

        answer = decide(
            {"conversationId": "theirs", "toolCall": {"name": "run_command"}},
            config_dir=tmp_path,
            ask=ask,
        )
        assert answer == {"decision": "allow"}

    def test_a_claimed_conversation_still_wins(self, tmp_path, monkeypatch):
        """The ordinary path is unchanged, and is consulted first."""
        registry.claim("mine", "/tmp/claimed.sock", config_dir=tmp_path)
        registry.claim_scope(UNIT, "/tmp/scope.sock", config_dir=tmp_path)
        monkeypatch.setattr(scope, "current_cgroup", lambda: IN_SCOPE)
        seen = []
        decide(
            {"conversationId": "mine", "toolCall": {"name": "view_file"}},
            config_dir=tmp_path,
            ask=lambda s, p: seen.append(s) or {"decision": "allow"},
        )
        assert seen == ["/tmp/claimed.sock"]

    def test_no_scope_no_change(self, tmp_path, monkeypatch):
        """Every platform without systemd takes exactly the path that
        shipped before this existed."""
        monkeypatch.setattr(scope, "current_cgroup", lambda: "")
        answer = decide(
            {"conversationId": "whoever", "toolCall": {"name": "run_command"}},
            config_dir=tmp_path,
            ask=lambda *_a, **_k: pytest.fail("should not have been asked"),
        )
        assert answer == {"decision": "allow"}


class TestTheScopeIsPublishedBeforeAgyRuns:
    """The ordering that makes this a fix rather than a narrower race."""

    def _server(self, tmp_path, unit=UNIT):
        server = AgyGateServer(
            tmp_path / "g.sock",
            gate=AntigravityPermissionGate(
                tmp_path,
                broadcast=lambda _e: None,
                localhost_available=lambda: True,
                config_dir=tmp_path / "cfg",
            ),
            config_dir=tmp_path / "cfg",
        )
        server._scope_unit = unit
        return server

    def test_start_publishes_the_scope(self, tmp_path):
        """A conversation claim can be late because the conversation exists
        before we are told its id. A scope claim cannot, because the scope
        does not exist until we make it."""
        server = self._server(tmp_path)

        async def go():
            await server.start()
            assert registry.scope_owner(IN_SCOPE, config_dir=tmp_path / "cfg")
            await server.stop()

        asyncio.run(go())

    def test_stop_releases_it(self, tmp_path):
        """A unit left behind would adopt any later process systemd placed
        in a scope of that name."""
        server = self._server(tmp_path)

        async def go():
            await server.start()
            await server.stop()
            assert registry.scope_owner(IN_SCOPE, config_dir=tmp_path / "cfg") is None

        asyncio.run(go())

    def test_no_unit_publishes_nothing(self, tmp_path):
        server = self._server(tmp_path, unit=None)

        async def go():
            await server.start()
            entries = list((tmp_path / "cfg" / "agy-sessions").glob("scope-*.json"))
            await server.stop()
            assert entries == []

        asyncio.run(go())

    def test_the_first_claim_adds_the_agent_pid_the_scope_could_not_know(
        self, tmp_path
    ):
        """The one thing publishing early costs, paid back by the first claim.

        An entry written before the child exists cannot name it, so it can
        never be read as a corpse — which would leave a killed host's scope
        on disk forever, keeping ``owns_anything`` true and denying the
        *user's* own unparseable payloads (AG-R-14 residue 3).
        """
        server = self._server(tmp_path)
        path = tmp_path / "cfg" / "agy-sessions" / f"scope-{UNIT}.json"

        async def go():
            await server.start()
            assert "agy_pid" not in json.loads(path.read_text(encoding="utf-8"))
            server.claim("a-conversation", agy_pid=4242)
            server.claim("its-subagent", agy_pid=4242)
            entry = json.loads(path.read_text(encoding="utf-8"))
            await server.stop()
            return entry

        entry = asyncio.run(go())
        assert entry["agy_pid"] == 4242
        assert entry["unit"] == UNIT


class TestArgv:
    def test_agy_is_launched_into_the_scope(self, tmp_path):
        server = AgyGateServer(
            tmp_path / "g.sock",
            policy=None,
            gate=AntigravityPermissionGate(
                tmp_path,
                broadcast=lambda _e: None,
                localhost_available=lambda: True,
                config_dir=tmp_path / "cfg",
            ),
            config_dir=tmp_path / "cfg",
        )
        server._scope_unit = UNIT
        argv = AgySession(tmp_path, gate=server)._argv()
        assert argv[:5] == [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit={UNIT}",
        ]
        assert "agy" in argv[5]

    def test_without_a_scope_argv_is_untouched(self, tmp_path):
        server = AgyGateServer(
            tmp_path / "g.sock",
            gate=AntigravityPermissionGate(
                tmp_path,
                broadcast=lambda _e: None,
                localhost_available=lambda: True,
                config_dir=tmp_path / "cfg",
            ),
            config_dir=tmp_path / "cfg",
        )
        server._scope_unit = None
        argv = AgySession(tmp_path, gate=server)._argv()
        assert argv[0] == "agy"

    def test_wrap_is_a_no_op_without_a_unit(self):
        assert scope.wrap(["agy", "--print="], None) == ["agy", "--print="]
