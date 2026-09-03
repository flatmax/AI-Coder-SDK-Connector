"""Tests for aic_dc.antigravity.service — the second engine's adapter.

Three assertions are load-bearing.

**It mounts.** ``build_router(…, engine=ANTIGRAVITY)`` refuses an adapter
that cannot serve the core surface, and the whole point of this module is
to be the thing that satisfies it. That test is the one that fails when a
method is renamed on either side.

**It shares rather than copies.** A third of the surface is repository and
index work that is not engine-specific, and the failure to avoid is the
second engine growing its own file tree. The symbol index is the injected
one; ``commit_all`` calls the shared module; ``ReviewMode`` is the real
class with this engine's posture rather than a reimplementation.

**It declines rather than pretends.** Where something is unbuilt —
resuming a conversation, image input — the call returns an error saying
so. A turn that silently dropped a user's attached screenshot would
answer the wrong question convincingly, and a ``connect_engine(resume=…)``
that quietly started a fresh session would lose the context that was asked
for. Those are the two places a stub would have been easiest and worst.

Everything runs offline. Nothing here starts a harness.
"""

from __future__ import annotations

import asyncio
import inspect
import types

import pytest

from aic_dc import capabilities
from aic_dc.antigravity.credentials import GEMINI_API, NONE, Credentials
from aic_dc.antigravity.service import PERMISSION_MODES, AntigravityService
from aic_dc.claude_code.messages import Event
from aic_dc.capabilities import ANTIGRAVITY, CLAUDE
from aic_dc.engine_router import RPC_SURFACES, build_router


def config(tmp_path):
    return types.SimpleNamespace(repo_root=str(tmp_path), config_dir=None)


def service(tmp_path, **kw):
    kw.setdefault(
        "credentials", Credentials(mode=GEMINI_API, api_key="k", source="test")
    )
    return AntigravityService(config(tmp_path), **kw)


def exposed(obj) -> set[str]:
    cls = obj if inspect.isclass(obj) else type(obj)
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


# ----------------------------------------------------------------------
# The first one that matters: it mounts
# ----------------------------------------------------------------------


class TestItMounts:
    def test_the_router_accepts_it(self, tmp_path):
        assert build_router(service(tmp_path), engine=ANTIGRAVITY) is not None

    def test_it_implements_every_core_method(self, tmp_path):
        """The complement of the router's guard, stated from this side."""
        from aic_dc.claude_code import ClaudeCodeService

        core = {
            name
            for name in exposed(ClaudeCodeService)
            if name not in RPC_SURFACES
            or capabilities.supports(ANTIGRAVITY, RPC_SURFACES[name])
        }
        assert core <= exposed(AntigravityService), (
            f"missing {sorted(core - exposed(AntigravityService))}"
        )

    def test_it_implements_no_method_the_descriptor_hides(self, tmp_path):
        """No stubs returning an empty dict.

        Anything absent is absent by declaration in ``capabilities.py``,
        where the reason is recorded, rather than by an implementation
        here that returns nothing convincingly.
        """
        hidden = {
            name
            for name, surface in RPC_SURFACES.items()
            if not capabilities.supports(ANTIGRAVITY, surface)
        }
        assert hidden, "nothing is hidden on this engine"
        overlap = hidden & exposed(AntigravityService)
        assert not overlap, (
            f"{sorted(overlap)} are implemented here but hidden by the "
            "descriptor. Either the surface is supported — say so in "
            "capabilities.py — or the method should not exist."
        )

    def test_it_does_not_mount_as_the_other_engine(self, tmp_path):
        """It genuinely lacks Claude's surface; the guard must notice."""
        with pytest.raises(ValueError, match="cannot be mounted"):
            build_router(service(tmp_path), engine=CLAUDE)

    def test_async_ness_matches_the_claude_adapter(self, tmp_path):
        """A method that changed sync-ness would break the RPC contract."""
        from aic_dc.claude_code import ClaudeCodeService

        for name in exposed(AntigravityService) & exposed(ClaudeCodeService):
            mine = inspect.iscoroutinefunction(getattr(AntigravityService, name))
            theirs = inspect.iscoroutinefunction(getattr(ClaudeCodeService, name))
            assert mine == theirs, f"{name} disagrees about being async"


# ----------------------------------------------------------------------
# The second: shared objects, not a second copy
# ----------------------------------------------------------------------


class TestItSharesRatherThanCopies:
    def test_the_symbol_index_is_the_injected_one(self, tmp_path):
        index = types.SimpleNamespace(
            lsp_get_hover=lambda p, line, col: {"contents": "shared"}
        )
        svc = service(tmp_path, symbol_index=index)
        assert svc.lsp_get_hover("a.py", 1, 1) == {"contents": "shared"}

    def test_lsp_before_the_index_is_built_is_no_answer(self, tmp_path):
        """``None`` reads to Monaco as "no hover here", which is the truth."""
        svc = service(tmp_path)
        assert svc.lsp_get_hover("a.py", 1, 1) is None
        assert svc.lsp_get_references("a.py", 1, 1) == []

    def test_review_is_the_real_shared_class(self, tmp_path):
        from aic_dc.claude_code.review import ReviewMode

        assert isinstance(service(tmp_path).review, ReviewMode)

    def test_review_carries_this_engines_posture(self, tmp_path):
        """Shared git arrangement, own permission mode.

        ``ReviewMode``'s collaborators are injectable precisely so a
        second engine can own its posture without a second copy of the
        branch handling.
        """
        svc = service(tmp_path)
        asyncio.run(svc.set_permission_mode("plan"))
        assert svc._permission_mode == "plan"

    def test_commit_uses_the_shared_module(self, tmp_path):
        """Rather than a second two hundred lines of git handling."""
        from pathlib import Path

        from aic_dc.antigravity import service as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "from aic_dc.claude_code.commit import commit_all" in source

    def test_it_provides_the_contract_commit_reaches_for(self, tmp_path):
        """``commit.py`` takes the service and reads these off it.

        They are its interface, so a rename there should fail here rather
        than at the first commit somebody tries.
        """
        svc = service(tmp_path)
        for attribute in (
            "_check_localhost_only",
            "_repo",
            "review",
            "_committing",
            "_turn_tasks",
            "_broadcast",
        ):
            assert hasattr(svc, attribute), attribute


# ----------------------------------------------------------------------
# The third: it declines rather than pretends
# ----------------------------------------------------------------------


class TestItDeclinesRatherThanPretends:
    def test_resume_is_refused_not_silently_ignored(self, tmp_path):
        """Starting a fresh session would lose the context asked for."""
        result = asyncio.run(service(tmp_path).connect_engine(resume="abc"))
        assert result["error"] == "unsupported"
        assert "phase 5" in result["message"]

    def test_image_input_is_refused_not_dropped(self, tmp_path):
        """A turn without the screenshot answers the wrong question well."""
        result = asyncio.run(
            service(tmp_path).chat_streaming("r1", "look", images=["data:..."])
        )
        assert result["error"] == "unsupported"

    def test_a_missing_credential_is_a_named_failure(self, tmp_path):
        """AG-R-8's most likely first experience of this engine."""
        svc = service(
            tmp_path, credentials=Credentials(mode=NONE, source="nowhere")
        )
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "no_credentials"
        assert "credentials" in result

    def test_no_usd_figure_is_invented(self, tmp_path):
        """AG-6: a zero would be a measurement."""
        state = asyncio.run(service(tmp_path).get_current_state())
        assert not any("cost" in k or "usd" in k for k in state)


class FakeSession:
    """A session whose turn does not finish until the test lets it.

    The gate is the whole point. Every assertion below is about what is
    true *while a turn is still running*, and a fake that completed
    promptly would pass against the very implementation this class exists
    to keep from coming back.
    """

    def __init__(self, release, fail=None):
        self._release = release
        self._fail = fail
        self.cancelled = False

    async def cancel(self):
        self.cancelled = True

    async def stream_turn(self, prompt, *, translator=None):
        yield Event("streamChunk", {"block_id": "b1", "content": "working"})
        await self._release.wait()
        if self._fail is not None:
            raise self._fail
        for event in translator.stream_complete():
            yield event


def _running(svc, release, fail=None):
    """Attach a gated fake session and collect dispatched event names."""
    fake = FakeSession(release, fail)

    async def _ensure():
        return fake

    names: list[str] = []

    async def _callback(name, *args):
        names.append(name)

    svc._ensure_session = _ensure
    svc._event_callback = _callback
    return fake, names


async def _start(svc, *args):
    """``chat_streaming`` under a deadline, because the bug is a hang.

    Every test below gates its fake turn open and then asserts something
    about the reply. Against the implementation this class guards — one
    that awaited the turn — that reply never comes, so a bare ``await``
    would hang the suite instead of failing it. Discovered the honest way:
    checking these tests against the old code timed out a five-minute
    command with nothing to show for it.

    A regression guard that hangs is worse than one that fails, so the
    deadline is part of the assertion rather than a convenience.
    """
    return await asyncio.wait_for(svc.chat_streaming(*args), timeout=5)


class TestATurnDoesNotHoldTheRpcOpen:
    """The phase-4 live run's finding, pinned.

    Until 2026-09-03 ``chat_streaming`` awaited the turn to exhaustion, so
    the browser's 75s JRPC deadline fired mid-turn on anything slow — and a
    permission dialog, which waits on a human by design, guaranteed it. The
    browser then took the transport's contentless timeout down ``input.js``'s
    ``catch`` branch and erased the transcript it had already drawn, while
    the turn ran on and edited the file. Nothing in this suite failed,
    because nothing here had ever driven a turn.

    See ``specs5/plan-ag/delivery.md`` § *Phase 4 — the live run*.
    """

    def test_the_reply_arrives_before_the_turn_finishes(self, tmp_path):
        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()
            _fake, names = _running(svc, release)

            reply = await _start(svc, "r1", "hi")
            # The turn is provably still running: nothing has released it.
            assert reply == {"status": "started"}
            assert "r1" in svc._turns
            assert svc._turn_tasks

            task = next(iter(svc._turn_tasks))
            release.set()
            await task
            assert "streamComplete" in names
            assert "r1" not in svc._turns

        asyncio.run(body())

    def test_the_reply_carries_no_turn_result(self, tmp_path):
        """A result key here would be a second, racing source of truth.

        Everything about the turn arrives as a pushed event keyed on the
        request id — which is what lets a browser that reloaded mid-turn
        re-attach rather than lose it.
        """

        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()
            _fake, _names = _running(svc, release)
            reply = await _start(svc, "r1", "hi")
            assert set(reply) == {"status"}
            release.set()
            await next(iter(svc._turn_tasks))

        asyncio.run(body())

    def test_a_second_turn_is_refused_synchronously(self, tmp_path):
        """``stream_turn`` raises, but it is a generator.

        Its ``TurnInProgressError`` lands on first iteration, which is now
        inside the background task — where no synchronous refusal can be
        made out of it. So the adapter answers before it spawns, the way
        the Claude adapter's ``session.admit`` does.
        """

        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()
            _fake, _names = _running(svc, release)
            await _start(svc, "r1", "hi")

            second = await _start(svc, "r2", "again")
            assert second["reason"] == "turn_in_progress"
            assert "r2" not in svc._turns

            release.set()
            await next(iter(svc._turn_tasks))
            # And the refusal lifts once the first turn is done.
            assert not svc._turns

        asyncio.run(body())

    def test_a_pending_dialog_does_not_delay_the_reply(self, tmp_path):
        """The case that actually bit, stated as itself.

        ``permissions.py`` lets a request wait indefinitely on purpose, so
        a gated engine that held the RPC open for the turn would be holding
        it open for the user's attention span.
        """

        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()  # stands in for an unanswered dialog
            _fake, _names = _running(svc, release)

            reply = await _start(svc, "r1", "hi")
            assert reply == {"status": "started"}

            release.set()
            await next(iter(svc._turn_tasks))

        asyncio.run(body())

    def test_a_turn_that_raises_still_settles_the_browser(self, tmp_path):
        """With no RPC reply left, the event stream is the only channel.

        A path that emits no terminal event is now a spinner that never
        stops, so the failure branch closes the turn out of the
        translator's own state rather than leaving ``stream_turn``'s
        closing events unrun.
        """

        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()
            _fake, names = _running(svc, release, fail=RuntimeError("harness died"))

            await _start(svc, "r1", "hi")
            task = next(iter(svc._turn_tasks))
            release.set()
            await task

            assert "systemEvent" in names
            assert "streamComplete" in names, "the spinner would never stop"
            assert "r1" not in svc._turns
            assert svc._errors, "the failure is recorded, not just broadcast"

        asyncio.run(body())

    def test_cancel_reaches_the_running_turn(self, tmp_path):
        """⏹ has to work against a turn whose RPC already returned."""

        async def body():
            svc = service(tmp_path)
            release = asyncio.Event()
            fake, _names = _running(svc, release)
            svc._session = fake

            await _start(svc, "r1", "hi")
            assert (await svc.cancel_streaming("r1"))["status"] == "ok"
            assert fake.cancelled

            # A stale cancel from a reconnecting browser hits nothing.
            assert (await svc.cancel_streaming("gone"))["status"] == "not_running"

            release.set()
            await next(iter(svc._turn_tasks))

        asyncio.run(body())


class TestPermissionPostures:
    def test_it_offers_no_mode_that_skips_the_dialog(self, tmp_path):
        """AG-5: the dialog is a requirement of this engine, not a feature."""
        assert "acceptEdits" not in PERMISSION_MODES
        assert "bypassPermissions" not in PERMISSION_MODES

    @pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
    def test_a_bypass_posture_is_refused(self, tmp_path, mode):
        result = asyncio.run(service(tmp_path).set_permission_mode(mode))
        assert result["error"] == "unsupported"

    @pytest.mark.parametrize("mode", PERMISSION_MODES)
    def test_the_offered_modes_apply(self, tmp_path, mode):
        assert asyncio.run(service(tmp_path).set_permission_mode(mode)) == {
            "permission_mode": mode
        }

    def test_a_session_with_no_dialog_would_be_read_only(self, tmp_path):
        """Before connecting there is no gate, so nothing may write."""
        state = asyncio.run(service(tmp_path).get_current_state())
        assert state["read_only"] is True


# ----------------------------------------------------------------------
# Localhost, which is a property of the product rather than of an engine
# ----------------------------------------------------------------------


class TestLocalhostOnly:
    def remote(self, tmp_path):
        svc = service(tmp_path)
        svc._collab = types.SimpleNamespace(
            is_caller_localhost=lambda: False,
            has_localhost_client=lambda: True,
        )
        return svc

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.connect_engine(),
            lambda s: s.chat_streaming("r1", "hi"),
            lambda s: s.new_session(),
            lambda s: s.restart_session(),
            lambda s: s.set_model("m"),
            lambda s: s.set_permission_mode("plan"),
            lambda s: s.commit_all(),
            lambda s: s.resolve_permission("p1"),
        ],
    )
    def test_a_remote_caller_is_refused(self, tmp_path, call):
        result = asyncio.run(call(self.remote(tmp_path)))
        assert result["error"] == "restricted"

    def test_sync_writes_are_refused_too(self, tmp_path):
        svc = self.remote(tmp_path)
        assert svc.set_denied_read_files(["a"])["error"] == "restricted"
        assert svc.set_viewer_state("a.py")["error"] == "restricted"

    def test_it_fails_closed_when_the_check_raises(self, tmp_path):
        """An exception from the collab check is a denial, not an allow."""

        def boom():
            raise RuntimeError("collab is confused")

        svc = service(tmp_path)
        svc._collab = types.SimpleNamespace(is_caller_localhost=boom)
        assert svc._check_localhost_only()["error"] == "restricted"

    def test_navigate_file_is_unrestricted(self, tmp_path):
        """Showing someone a file changes nothing on disk."""
        assert self.remote(tmp_path).navigate_file("a.py")["status"] == "ok"


# ----------------------------------------------------------------------
# Events reach the browser in the shared vocabulary
# ----------------------------------------------------------------------


class TestEvents:
    def collect(self, tmp_path):
        seen = []

        async def callback(name, *args):
            seen.append((name, args))

        return service(tmp_path, event_callback=callback), seen

    def test_a_turn_scoped_event_leads_with_the_request_id(self, tmp_path):
        """Exactly as the Claude pump's do — one browser handler per name."""
        from aic_dc.claude_code.messages import Event

        svc, seen = self.collect(tmp_path)
        asyncio.run(svc._dispatch(Event("streamChunk", {"a": 1}), "r1"))
        assert seen == [("streamChunk", ("r1", {"a": 1}))]

    def test_a_session_event_carries_only_its_payload(self, tmp_path):
        from aic_dc.claude_code.messages import Event

        svc, seen = self.collect(tmp_path)
        asyncio.run(
            svc._dispatch(Event("permissionMode", {"mode": "plan"}, False), None)
        )
        assert seen == [("permissionMode", ({"mode": "plan"},))]

    def test_a_dead_client_does_not_raise(self, tmp_path):
        from aic_dc.claude_code.messages import Event

        async def callback(name, *args):
            raise RuntimeError("socket closed")

        svc = service(tmp_path, event_callback=callback)
        asyncio.run(svc._dispatch(Event("streamChunk", {}), "r1"))


class TestDiagnostics:
    def test_health_names_the_credential_and_the_workspace(self, tmp_path):
        health = service(tmp_path).get_engine_health()
        assert health["engine"] == "antigravity"
        assert health["workspace"] == str(tmp_path)
        assert "credentials" in health

    def test_health_carries_no_secret(self, tmp_path):
        assert "k" not in str(service(tmp_path).get_engine_health()["credentials"])

    def test_server_info_carries_the_descriptor(self, tmp_path):
        info = asyncio.run(service(tmp_path).get_server_info())
        assert info["capabilities"] == capabilities.descriptor(ANTIGRAVITY)

    def test_the_sdk_surface_probe_is_reachable(self, tmp_path):
        report = asyncio.run(service(tmp_path).get_sdk_surface())
        assert "sections" in report

    def test_errors_are_recorded_for_the_diagnostics_tab(self, tmp_path):
        svc = service(tmp_path)
        svc._record_error("turn", RuntimeError("boom"))
        errors = asyncio.run(svc.get_engine_errors())
        assert errors["total"] == 1
        assert errors["errors"][0]["phase"] == "turn"


class TestCancel:
    def test_cancelling_an_unknown_turn_is_not_an_error(self, tmp_path):
        """A stale cancel from a reconnecting browser must not stop the
        turn that replaced the one it was looking at."""
        result = asyncio.run(service(tmp_path).cancel_streaming("gone"))
        assert result["status"] == "not_running"

    def test_resolve_without_a_session_says_so(self, tmp_path):
        result = asyncio.run(service(tmp_path).resolve_permission("p1"))
        assert result["error"] == "unknown"
