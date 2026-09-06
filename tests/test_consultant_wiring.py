"""Tests for the Antigravity consultant's mount point in the Claude engine.

AG-7's user story, and the first one the second engine delivers: Claude
stays master and can ask Google's model for a second opinion, or for an
image — a capability Anthropic does not offer — from inside an ordinary
turn.

Two assertions are load-bearing.

**It does not go on the ungated server.** ``permissions.can_use_tool``
early-returns an allow, with no dialog and no broadcast, for anything
matching ``mcp__aic-dc__*``, because the index tools are read-only.
``generate_image`` writes a file. Mounting it there would route a file
write around the permission dialog AG-5 calls non-negotiable, and it would
do so *silently* — the tool would work, the file would appear, and nothing
would look wrong. This is the same assertion ``test_antigravity_bridge.py``
makes about the bridge; it is repeated here because the mount point is a
second place to get it wrong.

**Its absence is not a failure.** A missing Gemini key is AG-R-8's most
likely first experience of this engine. The session must start, the repo
tools must survive, and the consultant must be *absent* rather than
present and answering "no credentials" on every call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aic_dc.antigravity.bridge import SERVER_NAME as AG_SERVER_NAME
from aic_dc.claude_code.mcp_server import SERVER_NAME as INDEX_SERVER_NAME
from aic_dc.claude_code.permissions import AIC_DC_MCP_SERVER
from aic_dc.claude_code.service import ClaudeCodeService


class Service(ClaudeCodeService):
    """Just enough of the service to exercise the mount point.

    The real constructor builds a session store, an events log and a
    symbol-index handle, none of which this decision touches. ``_config``
    is here because the mount point reads it since AG-16: which transport
    answers is an ``app.json`` preference, so a fake without one is
    under-specified rather than minimal.
    """

    def __init__(self, repo_root="/tmp"):
        self._repo_root = Path(repo_root)
        self._config = type(
            "Config", (), {"config_dir": None, "consultant_transport": "auto"}
        )()


def mount(monkeypatch, *, available: bool, existing=None, explode=False,
          want_bridge=False):
    """Run the mount with the credential state a test asks for.

    The choice of transport is faked at
    :func:`~aic_dc.agy.consultant.choose_consultant` rather than at the
    two consultant classes, because since AG-16 that function *is* the
    decision — it answers with a consultant or with the sentence saying
    why there is none, and the mount point does no credential reasoning
    of its own any more.
    """

    class FakeConsultant:
        def __init__(self):
            self.credentials = type(
                "C", (), {"source": "test", "available": available}
            )()

    class FakeBridge:
        def __init__(self, consultant, *, emit=None, request_id=None):
            self._c = consultant
            # Recorded rather than ignored: AG-13's tab needs both, and a
            # double that accepted them without noticing would let the
            # wiring rot silently.
            self.emit = emit
            self.request_id = request_id

        @property
        def available(self):
            return self._c.credentials.available

        def build_server(self):
            return {"kind": "sdk-mcp-server"}

    def choose(repo_root, **kw):
        if explode:
            raise RuntimeError("the SDK is not installed")
        if not available:
            return None, "no credential on this machine (test)"
        return FakeConsultant(), "a fake transport (test)"

    import aic_dc.agy.consultant as chooser
    import aic_dc.antigravity as ag

    monkeypatch.setattr(chooser, "choose_consultant", choose)
    monkeypatch.setattr(ag, "ConsultantBridge", FakeBridge)
    service = Service()
    servers = service._add_consultant(existing)
    if want_bridge:
        return servers, service.consultant_bridge
    return servers


# ----------------------------------------------------------------------
# The first one that matters: not on the ungated server
# ----------------------------------------------------------------------


class TestItIsNotOnTheUngatedServer:
    def test_the_consultant_has_its_own_server_name(self, monkeypatch):
        servers = mount(monkeypatch, available=True, existing={INDEX_SERVER_NAME: 1})
        assert AG_SERVER_NAME in servers
        assert AG_SERVER_NAME != INDEX_SERVER_NAME

    def test_that_name_is_not_the_one_permissions_ungates(self):
        """Checked against the constant, so a rename on either side fails
        here rather than quietly re-opening the hole."""
        assert AG_SERVER_NAME != AIC_DC_MCP_SERVER
        assert not AG_SERVER_NAME.startswith(f"{AIC_DC_MCP_SERVER}_")

    def test_the_index_server_is_untouched(self, monkeypatch):
        """Mounting the consultant must not disturb the repo tools."""
        servers = mount(
            monkeypatch, available=True, existing={INDEX_SERVER_NAME: "original"}
        )
        assert servers[INDEX_SERVER_NAME] == "original"

    def test_the_caller_s_dict_is_not_mutated(self, monkeypatch):
        """A copy, so a failure later cannot leave a half-built mapping."""
        existing = {INDEX_SERVER_NAME: 1}
        mount(monkeypatch, available=True, existing=existing)
        assert existing == {INDEX_SERVER_NAME: 1}


# ----------------------------------------------------------------------
# The second: absence is not a failure
# ----------------------------------------------------------------------


class TestAbsenceIsNotAFailure:
    def test_no_credential_means_no_tools(self, monkeypatch):
        """AG-9 applied to a tool definition.

        Two tools that always answer "no credentials" cost context on
        every turn and buy nothing.
        """
        servers = mount(monkeypatch, available=False, existing={INDEX_SERVER_NAME: 1})
        assert AG_SERVER_NAME not in servers

    def test_no_credential_leaves_the_repo_tools_alone(self, monkeypatch):
        servers = mount(monkeypatch, available=False, existing={INDEX_SERVER_NAME: 1})
        assert servers == {INDEX_SERVER_NAME: 1}

    def test_a_broken_consultant_does_not_break_the_session(self, monkeypatch):
        """Strictly better to start without it than to refuse to start."""
        servers = mount(
            monkeypatch, available=True, existing={INDEX_SERVER_NAME: 1}, explode=True
        )
        assert servers == {INDEX_SERVER_NAME: 1}

    def test_it_survives_the_index_bridge_having_failed(self, monkeypatch):
        """``mcp_servers`` is ``None`` when the repo tools did not build.

        The consultant is independent of them, so it should still mount —
        and the ``None`` must not become a crash.
        """
        servers = mount(monkeypatch, available=True, existing=None)
        assert list(servers) == [AG_SERVER_NAME]

    def test_absence_is_not_recorded_as_a_degradation(self, monkeypatch):
        """An unconfigured optional engine is not a fault in this one.

        The degradation banner is for things this engine lost. Listing a
        second engine nobody configured would make every default install
        look broken.
        """
        service = Service()
        service._degradations = []
        import aic_dc.agy.consultant as chooser

        monkeypatch.setattr(
            chooser,
            "choose_consultant",
            lambda *a, **k: (None, "no credential on this machine (test)"),
        )
        service._add_consultant(None)
        assert service._degradations == []


# ----------------------------------------------------------------------
# The wiring itself
# ----------------------------------------------------------------------


class TestItIsActuallyWired:
    def test_the_session_builder_calls_it(self):
        """Without this line the bridge is code nobody constructs — which
        is exactly what phase 1 left behind and this closes."""
        source = Path(
            __import__("aic_dc.claude_code.service", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        assert "mcp_servers = self._add_consultant(mcp_servers)" in source

    def test_it_runs_against_the_real_bridge(self, tmp_path):
        """No fakes: the real ``Consultant`` and ``ConsultantBridge``.

        Skipped only if the machine has no credential, because that is
        the branch the fakes already cover. What this adds is that the
        real objects construct and produce a server — the failure the
        mocked tests structurally cannot catch.
        """
        from aic_dc.agy.consultant import AgyConsultant
        from aic_dc.antigravity import resolve_credentials

        # Either transport will do, which is AG-16's point: the SDK needs
        # a Gemini credential and a wheel, `agy` needs a binary and an
        # installed gate, and a machine with either can mount a
        # consultant. Skipped only when a machine has neither, because
        # that branch is what the fakes above cover.
        if not (
            resolve_credentials().available
            or AgyConsultant(tmp_path).available
        ):
            pytest.skip("neither consultant transport is usable on this machine")
        servers = Service(tmp_path)._add_consultant(None)
        assert AG_SERVER_NAME in servers


class TestTheTabWiring:
    """AG-13: the bridge is given what it needs to open an agent tab.

    Both halves matter and both fail silently if absent. Without ``emit``
    the consultation streams nowhere; without a *live* ``request_id`` the
    browser drops every ``subagentEvent``, because ``onSubagentEvent``
    resolves the owner tab by request id and returns early when it cannot.
    """

    def test_the_bridge_is_handed_an_emit(self, monkeypatch):
        servers, bridge = mount(monkeypatch, available=True, want_bridge=True)
        assert bridge.emit is not None

    def test_the_request_id_is_read_late_not_captured_early(self, monkeypatch):
        """A callable, not a value.

        The bridge is built once when the session is constructed and the
        turn it must attribute to changes on every prompt. Capturing an id
        at construction would attach every consultation for the life of
        the session to the first turn — or, before any turn, to ``None``.
        """
        servers, bridge = mount(monkeypatch, available=True, want_bridge=True)
        assert callable(bridge.request_id)


class TestStopReachesTheConsultation:
    """AG-13's ⏹, wired end to end.

    The tab offers Stop on every subagent row, and a consultation is a
    subagent row like any other — so the click arrives at ``stop_task``.
    But a consultation is not a CLI task and the CLI has never heard of
    its id, so without this routing the button was decorative: it would
    have asked the CLI to stop something it does not know about.
    """

    class Bridge:
        def __init__(self, stopped=True):
            self._stopped = stopped
            self.cancels = 0

        async def cancel(self):
            self.cancels += 1
            return self._stopped

    def service_with_bridge(self, stopped=True):
        service = Service()
        service._collab = None
        service.consultant_bridge = self.Bridge(stopped)
        return service

    def test_a_consultation_id_reaches_the_bridge(self):
        import asyncio

        service = self.service_with_bridge()
        result = asyncio.run(service.stop_task("consultation-abc-1"))
        assert service.consultant_bridge.cancels == 1
        assert result["status"] == "stopping"

    def test_stopping_one_that_is_not_running_says_so(self):
        """Rather than claiming to have stopped something."""
        import asyncio

        service = self.service_with_bridge(stopped=False)
        assert asyncio.run(service.stop_task("consultation-abc-1"))["status"] == (
            "not_running"
        )

    def test_a_cli_task_id_does_not_reach_the_bridge(self):
        """The routing is by id shape; a real subagent must still work."""
        import asyncio

        service = self.service_with_bridge()

        class Session:
            def __init__(self):
                self.stopped = []

            async def stop_task(self, task_id):
                self.stopped.append(task_id)

        service.session = Session()
        asyncio.run(service.stop_task("toolu_01ABC"))
        assert service.consultant_bridge.cancels == 0
        assert service.session.stopped == ["toolu_01ABC"]

    def test_the_prefix_matches_what_the_bridge_mints(self):
        """Two constants that must agree, checked rather than assumed."""
        from pathlib import Path

        from aic_dc.antigravity import bridge as bridge_module
        from aic_dc.claude_code import service as service_module

        minted = Path(bridge_module.__file__).read_text(encoding="utf-8")
        routed = Path(service_module.__file__).read_text(encoding="utf-8")
        assert 'f"consultation-{id(self):x}-{self._counter}"' in minted
        assert 'startswith("consultation-")' in routed
