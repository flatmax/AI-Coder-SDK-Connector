"""Tests for aic_dc.engine_router — one RPC namespace, two engines.

The load-bearing assertion is that the router is **behaviour-preserving**.
It sits between 59 webapp files and the engine adapter they have always
talked to, so the claim that earns it its place is that nothing changed:
the same method names, reaching the same object, with the same
async-ness. That claim is cheap to check and expensive to get wrong —
a missing name works in Python and 404s over RPC, which nothing notices
until somebody clicks the button.

``TestItExposesTheWholeSurface`` checks it against the real
``ClaudeCodeService``, not a fake, because the failure this guards against
is a drift between two real things.

The second theme is that the router — not the engine — is the authority on
what the engine cannot do. An adapter that answered
``get_engine_capabilities`` for itself would make the descriptor whatever
that engine returned, which is the "no answer looks like no data" failure
AG-9 exists to prevent.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from aic_dc import capabilities
from aic_dc.capabilities import ANTIGRAVITY, CLAUDE
from aic_dc.engine_router import (
    ROUTER_OWNED,
    RPC_NAME,
    EngineRouterBase,
    build_router,
)


class FakeAdapter:
    """Enough of an engine adapter to route to."""

    def __init__(self):
        self.calls = []

    def get_model(self):
        self.calls.append("get_model")
        return "a-model"

    def set_model(self, name, extra=None):
        self.calls.append(("set_model", name, extra))
        return True

    async def shutdown(self):
        self.calls.append("shutdown")
        return "stopped"

    async def chat_streaming(self, prompt, request_id):
        self.calls.append(("chat_streaming", prompt, request_id))
        return {"ok": True}

    def _private(self):  # never exposed
        return "no"


def stub_router(adapter, **kw):
    """A router over a stub, with the full-surface requirement waived.

    The requirement is on by default and should be: a half-mounted engine
    fails at click time, one button at a time. These fakes are four
    methods, so every test that is *about* delegation rather than about
    mounting opts out explicitly — and ``TestItRefusesAPartialEngine``
    is what covers the default.
    """
    return build_router(adapter, require_full_surface=False, **kw)


def exposed(obj) -> set[str]:
    """The method names jrpc-oo would advertise, read the way it reads them."""
    cls = obj if inspect.isclass(obj) else type(obj)
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


# ----------------------------------------------------------------------
# The one that matters: nothing changed for the browser
# ----------------------------------------------------------------------


class TestItExposesTheWholeSurface:
    def test_every_adapter_method_is_on_the_router(self):
        """Checked against the real service, because drift is the risk."""
        from aic_dc.claude_code import ClaudeCodeService

        # A stand-in carrying exactly the real method names, rather than
        # the real service: constructing that needs a config, a repo and
        # a git tree, and the question here is about the *name list*.
        stand_in = type(
            "StandIn",
            (),
            {name: (lambda self, *a, **k: None) for name in exposed(ClaudeCodeService)},
        )
        router = build_router(stand_in(), engine=CLAUDE)
        missing = exposed(ClaudeCodeService) - exposed(router)
        assert not missing, (
            f"The router does not expose {sorted(missing)}. Over RPC that is "
            "a method the browser can no longer call — it works in Python "
            "and 404s on the wire."
        )

    def test_it_adds_only_what_it_owns(self):
        from aic_dc.claude_code import ClaudeCodeService

        stand_in = type(
            "StandIn",
            (),
            {name: (lambda self, *a, **k: None) for name in exposed(ClaudeCodeService)},
        )
        router = build_router(stand_in(), engine=CLAUDE)
        assert exposed(router) - exposed(ClaudeCodeService) == set(ROUTER_OWNED)

    def test_private_methods_are_not_exposed(self):
        assert "_private" not in exposed(stub_router(FakeAdapter()))

    def test_the_namespace_is_the_one_the_webapp_assumes(self):
        """AG-3: there is no second namespace and no AntigravityService.*"""
        assert RPC_NAME == "ClaudeCodeService"
        assert type(stub_router(FakeAdapter())).__name__ == RPC_NAME


class TestCallsReachTheAdapter:
    def test_a_sync_call_is_forwarded(self):
        adapter = FakeAdapter()
        assert stub_router(adapter).get_model() == "a-model"
        assert adapter.calls == ["get_model"]

    def test_arguments_are_forwarded_positionally_and_by_keyword(self):
        adapter = FakeAdapter()
        stub_router(adapter).set_model("m", extra=7)
        assert adapter.calls == [("set_model", "m", 7)]

    def test_an_async_call_is_awaited(self):
        adapter = FakeAdapter()
        assert asyncio.run(stub_router(adapter).shutdown()) == "stopped"
        assert adapter.calls == ["shutdown"]

    def test_async_methods_stay_async_and_sync_stay_sync(self):
        """A wrapper that made everything a coroutine would change the
        contract for every in-process caller, not just the RPC one."""
        router = type(stub_router(FakeAdapter()))
        assert inspect.iscoroutinefunction(router.chat_streaming)
        assert inspect.iscoroutinefunction(router.shutdown)
        assert not inspect.iscoroutinefunction(router.get_model)

    def test_the_delegate_keeps_the_adapters_signature(self):
        """jrpc-oo inspects the exposed callables; identical
        ``(*args, **kwargs)`` stubs would erase the surface."""
        router = type(stub_router(FakeAdapter()))
        assert list(inspect.signature(router.set_model).parameters) == [
            "self",
            "name",
            "extra",
        ]

    def test_the_delegate_keeps_the_adapters_docstring(self):
        class Documented:
            def thing(self):
                """The adapter's own words."""

        router = type(stub_router(Documented()))
        assert router.thing.__doc__ == "The adapter's own words."


# ----------------------------------------------------------------------
# The router is the authority, not the engine
# ----------------------------------------------------------------------


class TestTheRouterOwnsTheDescriptor:
    def test_it_publishes_the_capability_descriptor(self):
        router = stub_router(FakeAdapter(), engine=CLAUDE)
        assert router.get_engine_capabilities() == capabilities.descriptor(CLAUDE)

    def test_the_descriptor_follows_the_engine(self):
        claude = stub_router(FakeAdapter(), engine=CLAUDE)
        antigravity = stub_router(FakeAdapter(), engine=ANTIGRAVITY)
        assert claude.get_engine_capabilities()["usd_cost"]["supported"]
        assert not antigravity.get_engine_capabilities()["usd_cost"]["supported"]

    def test_an_adapter_cannot_shadow_the_descriptor(self):
        """An engine answering for itself is the failure AG-9 is about.

        Silently letting the delegate win would make the descriptor
        whatever that engine returned, and the whole point is that the
        descriptor is the authority on what the engine *cannot* do.
        """

        class Sneaky:
            def get_engine_capabilities(self):
                return {"usd_cost": {"supported": True}}

        with pytest.raises(ValueError, match="router method"):
            build_router(Sneaky())

    def test_an_unknown_engine_is_refused(self):
        with pytest.raises(ValueError, match="not a known engine"):
            build_router(FakeAdapter(), engine="gpt")

    def test_list_engines_reports_what_is_mountable(self):
        """Honest about the second engine not being routable yet."""
        router = stub_router(FakeAdapter(), engine=CLAUDE)
        listing = router.list_engines()
        assert listing["active"] == CLAUDE
        assert set(listing["available"]) == set(capabilities.ENGINES)
        assert listing["mountable"] == [CLAUDE]


class TestClassIdentity:
    def test_each_router_gets_its_own_class(self):
        """jrpc-oo keys its method list off the class.

        Sharing one between two routers in a process — which these tests
        do — would make the second registration silently reuse the
        first's surface.
        """
        assert type(stub_router(FakeAdapter())) is not type(
            stub_router(FakeAdapter())
        )

    def test_routers_are_the_base_class(self):
        assert isinstance(stub_router(FakeAdapter()), EngineRouterBase)

    def test_the_base_exposes_only_what_it_owns(self):
        """A stray public method on the base becomes an RPC method."""
        assert exposed(EngineRouterBase) == set(ROUTER_OWNED)


class TestUnsupportedSurfacesAreRefusedNotMissing:
    """AG-9 at the RPC layer.

    The webapp should not be calling these at all on an engine that
    cannot feed them — the panel is hidden. This is what happens when it
    does anyway, and the shape of the answer matters: a *stated* refusal,
    not an empty list (which reads as "no servers" rather than "no
    answer") and not a missing method (which reads as a version mismatch
    or a broken build).
    """

    def antigravity_router(self):
        return build_router(
            FakeAdapter(), engine=ANTIGRAVITY, require_full_surface=False
        )

    def test_the_method_still_exists_on_the_wire(self):
        """Omitting it would give the browser a transport-level 404."""
        assert "get_context_usage" in exposed(self.antigravity_router())

    async def test_calling_it_says_which_surface_and_why(self):
        """Awaited, because a refused method keeps its async-ness.

        It did not always. While the master was fixed at build time a
        refusal could be a plain function whatever it replaced, and this
        called it synchronously. With a switchable master the check moved
        inside the delegate, so an async method refuses on the await —
        which is what the wire does anyway (``ExposeClass`` inspects the
        *result* and awaits a coroutine), and which keeps the surface's
        shape from changing under a switch.
        """
        from aic_dc.engine_router import UnsupportedOnThisEngine

        with pytest.raises(UnsupportedOnThisEngine) as exc:
            await self.antigravity_router().get_context_usage()
        assert "context_window_usage" in str(exc.value)
        assert "get_engine_capabilities" in str(exc.value)

    def test_a_supported_surface_still_delegates(self):
        adapter = FakeAdapter()
        build_router(
            adapter, engine=ANTIGRAVITY, require_full_surface=False
        ).get_model()
        assert adapter.calls == ["get_model"]

    def test_claude_refuses_nothing(self):
        """The behaviour-preserving claim, stated as a property.

        Every surface in the table is supported on Claude today, so the
        router must generate no refusals at all — otherwise this change
        would have broken a working engine to accommodate an unbuilt one.
        """
        from aic_dc.engine_router import RPC_SURFACES

        for name, surface in RPC_SURFACES.items():
            assert capabilities.supports(CLAUDE, surface), (
                f"{name} would be refused on Claude. The router must not "
                "take a working surface away from the shipped engine."
            )

    async def test_the_refusal_set_matches_the_descriptor(self):
        """One source of truth, not two.

        A second hand-kept list of unsupported methods is the thing that
        disagrees with the descriptor, and then the panel is hidden while
        the method works or the reverse.
        """
        from aic_dc.engine_router import RPC_SURFACES, UnsupportedOnThisEngine

        router_obj = self.antigravity_router()
        for name, surface in RPC_SURFACES.items():
            method = getattr(router_obj, name)
            if capabilities.supports(ANTIGRAVITY, surface):
                continue
            with pytest.raises(UnsupportedOnThisEngine):
                result = method()
                if inspect.isawaitable(result):
                    await result

    def test_every_mapped_method_is_a_real_rpc_method(self):
        """A typo here would refuse nothing and hide nothing."""
        from aic_dc.claude_code import ClaudeCodeService
        from aic_dc.engine_router import RPC_SURFACES

        unknown = set(RPC_SURFACES) - exposed(ClaudeCodeService)
        assert not unknown, f"{sorted(unknown)} are not RPC methods"

    def test_every_mapped_surface_is_a_real_surface(self):
        from aic_dc.engine_router import RPC_SURFACES

        for name, surface in RPC_SURFACES.items():
            assert capabilities.supports(CLAUDE, surface) in (True, False), (
                f"{name} maps to {surface!r}, which is not a declared surface"
            )


class TestItRefusesAPartialEngine:
    """A half-mounted engine fails one button at a time; this fails once.

    Without the guard the browser calls a method the adapter does not
    have and gets an ``AttributeError`` at click time, which reads as a
    crash rather than as an engine that was never ready.
    """

    def test_a_stub_adapter_cannot_be_mounted(self):
        with pytest.raises(ValueError, match="cannot be mounted"):
            build_router(FakeAdapter(), engine=CLAUDE)

    def test_the_error_is_the_to_do_list(self):
        with pytest.raises(ValueError) as exc:
            build_router(FakeAdapter(), engine=CLAUDE)
        # Names the caller has to implement, not just a count.
        assert "history_list" in str(exc.value)
        assert "get_current_state" in str(exc.value)

    def test_the_full_surface_mounts(self):
        from aic_dc.claude_code import ClaudeCodeService

        stand_in = type(
            "StandIn",
            (),
            {name: (lambda self, *a, **k: None) for name in exposed(ClaudeCodeService)},
        )
        assert build_router(stand_in(), engine=CLAUDE) is not None

    def test_an_engine_only_needs_the_surfaces_it_supports(self):
        """The point of the mapping: unsupported methods are not required.

        An adapter that omits ``history_list`` mounts on Antigravity —
        where transcript history is unbuilt and the panel is hidden — and
        does not mount on Claude, where the panel is real.
        """
        from aic_dc.claude_code import ClaudeCodeService
        from aic_dc.engine_router import RPC_SURFACES

        optional = {
            name
            for name, surface in RPC_SURFACES.items()
            if not capabilities.supports(ANTIGRAVITY, surface)
        }
        assert optional, "no surface is unsupported on Antigravity"
        names = exposed(ClaudeCodeService) - optional
        stand_in = type(
            "StandIn", (), {name: (lambda self, *a, **k: None) for name in names}
        )
        build_router(stand_in(), engine=ANTIGRAVITY)  # mounts
        with pytest.raises(ValueError, match="cannot be mounted"):
            build_router(stand_in(), engine=CLAUDE)

    def test_startup_requires_the_full_surface(self):
        """main.py must not quietly waive it."""
        from pathlib import Path

        from aic_dc import main

        source = Path(main.__file__).read_text(encoding="utf-8")
        assert "require_full_surface=False" not in source


class TestItRegistersWithTheRealTransport:
    """Against ``RpcServer``, not a mock.

    The generated-class trick is exactly the kind of thing that works
    under ``inspect`` and then does not survive contact with jrpc-oo's
    own registration, so this asserts on what actually lands in the
    server's method table.
    """

    def registered(self, port: int) -> list[str]:
        from aic_dc.claude_code import ClaudeCodeService
        from aic_dc.rpc import RpcServer

        stand_in = type(
            "StandIn",
            (),
            {name: (lambda self, *a, **k: None) for name in exposed(ClaudeCodeService)},
        )
        server = RpcServer(port=port, host="127.0.0.1")
        server.add_service(build_router(stand_in()), name=RPC_NAME)
        return sorted(server._inner.classes[-1])

    def test_every_method_lands_under_the_one_namespace(self):
        keys = self.registered(19981)
        assert keys, "nothing registered"
        assert all(key.startswith(f"{RPC_NAME}.") for key in keys), (
            "a method registered outside the shared namespace; AG-3's whole "
            "point is that there is no second one"
        )

    def test_the_generated_methods_survive_registration(self):
        from aic_dc.claude_code import ClaudeCodeService

        keys = set(self.registered(19982))
        for name in exposed(ClaudeCodeService) | set(ROUTER_OWNED):
            assert f"{RPC_NAME}.{name}" in keys, f"{name} did not register"


# ----------------------------------------------------------------------
# Startup wiring
# ----------------------------------------------------------------------


class TestStartupUsesTheRouter:
    """The two things the wiring has to get right, read from main.py.

    Checked as source rather than by starting a server, because both are
    one-line mistakes with silent failure modes and a startup test would
    need a git repo, ports and a browser.
    """

    def source(self) -> str:
        from pathlib import Path

        from aic_dc import main

        return Path(main.__file__).read_text(encoding="utf-8")

    def test_the_router_is_what_is_registered(self):
        source = self.source()
        assert "server.add_service(engine_router, name=RPC_NAME)" in source
        assert "server.add_service(claude_code_service)" not in source

    def test_the_call_proxy_is_read_off_the_router(self):
        """jrpc-oo injects ``get_call`` onto the *registered* instance.

        Reading it off the service behind the router would find nothing,
        and every server-push event — every streamed chunk, every
        permission dialog — would be dropped with a warning.
        """
        source = self.source()
        assert "engine_router.get_call()" in source
        assert "claude_code_service.get_call()" not in source


# ----------------------------------------------------------------------
# AG-1: one master per session, chosen per session
# ----------------------------------------------------------------------


class SwitchableAdapter(FakeAdapter):
    """A fake that can be asked whether it is busy, and can be stopped."""

    def __init__(self, busy=False):
        super().__init__()
        self.session = type("S", (), {"streaming_active": busy})()
        self._turn_tasks = set()
        self._collab = None

    async def get_context_usage(self):
        # A surface method, so it delegates on Claude and refuses on
        # Antigravity — which is the pair this fake exists to show.
        self.calls.append("get_context_usage")
        return {"total_tokens": 1}

    def _check_localhost_only(self):
        return None


class Recorder:
    """The event callback, remembering what the router announced."""

    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    async def __call__(self, name, *args):
        self.events.append((name, args))
        if self.fail:
            raise RuntimeError("no browser")

    def names(self):
        return [name for name, _ in self.events]

    def payload(self, name):
        for got, args in self.events:
            if got == name:
                return args[0]
        return None


def switchable(**kw):
    """A two-engine router over stubs, master Claude."""
    claude = SwitchableAdapter(**kw.pop("claude_kwargs", {}))
    antigravity = SwitchableAdapter(**kw.pop("antigravity_kwargs", {}))
    router = build_router(
        claude,
        engine=CLAUDE,
        alternates={ANTIGRAVITY: antigravity},
        require_full_surface=False,
        **kw,
    )
    return router, claude, antigravity


class TestTheMasterCanChange:
    async def test_it_forwards_to_the_new_master(self):
        router, claude, antigravity = switchable()
        router.get_model()
        await router.switch_engine(ANTIGRAVITY)
        router.get_model()
        assert claude.calls == ["get_model", "shutdown"]
        assert antigravity.calls == ["get_model"]

    async def test_the_descriptor_follows_the_switch(self):
        """The whole point of the event, and the thing a stale one breaks."""
        router, _, _ = switchable()
        assert router.get_engine_capabilities()["context_window_usage"][
            "supported"
        ] is True
        await router.switch_engine(ANTIGRAVITY)
        assert router.get_engine_capabilities()["context_window_usage"][
            "supported"
        ] is False

    async def test_a_surface_refuses_after_the_switch_and_not_before(self):
        """The reason the capability check moved to call time.

        A delegate that decided when it was generated would keep answering
        for the engine mounted at startup — the browser would go on
        calling a method the descriptor now says is hidden, and get an
        answer.
        """
        from aic_dc.engine_router import UnsupportedOnThisEngine

        router, claude, _ = switchable()
        await router.get_context_usage()
        assert "get_context_usage" in claude.calls
        await router.switch_engine(ANTIGRAVITY)
        with pytest.raises(UnsupportedOnThisEngine):
            await router.get_context_usage()

    async def test_the_wire_surface_does_not_move(self):
        """jrpc-oo sends the method list once. It cannot be renegotiated.

        If this ever fails, a switch stops being a field assignment and
        becomes a re-registration plus a browser reconnect.
        """
        router, _, _ = switchable()
        before = exposed(router)
        await router.switch_engine(ANTIGRAVITY)
        assert exposed(router) == before

    async def test_it_stops_the_outgoing_engine(self):
        router, claude, _ = switchable()
        await router.switch_engine(ANTIGRAVITY)
        assert "shutdown" in claude.calls

    async def test_it_refuses_mid_turn(self):
        """The same rule new_session has: cancel first, or lose the tail."""
        router, claude, antigravity = switchable(
            claude_kwargs={"busy": True},
        )
        answer = await router.switch_engine(ANTIGRAVITY)
        assert answer["reason"] == "turn_active"
        assert "shutdown" not in claude.calls
        assert router.list_engines()["active"] == CLAUDE

    async def test_it_refuses_an_engine_that_is_not_mounted(self):
        """A missing credential, said as a missing credential.

        Not a typo and not a crash: the engine is real, this install just
        cannot run it, and the message has to distinguish the two because
        only one of them is the user's to fix.
        """
        router = build_router(
            SwitchableAdapter(), engine=CLAUDE, require_full_surface=False
        )
        answer = await router.switch_engine(ANTIGRAVITY)
        assert answer["reason"] == "not_mountable"
        assert router.list_engines()["active"] == CLAUDE

    async def test_switching_to_the_master_is_not_an_error(self):
        """A second window may have got there first."""
        router, claude, _ = switchable()
        answer = await router.switch_engine(CLAUDE)
        assert answer["changed"] is False
        assert "shutdown" not in claude.calls

    async def test_an_unknown_engine_is_refused_by_name(self):
        router, _, _ = switchable()
        answer = await router.switch_engine("gpt")
        assert answer["reason"] == "unknown_engine"

    async def test_mountable_reports_what_is_actually_mounted(self):
        """`available` and `mountable` are different questions.

        The selector needs both: an engine this build knows about but
        cannot run here is offered disabled with the reason, and dropping
        it would make a missing key look like a build without the engine.
        """
        router, _, _ = switchable()
        assert router.list_engines()["mountable"] == sorted([CLAUDE, ANTIGRAVITY])
        solo = build_router(
            SwitchableAdapter(), engine=CLAUDE, require_full_surface=False
        )
        assert solo.list_engines()["mountable"] == [CLAUDE]


class TestTheSwitchIsAnnounced:
    async def test_it_carries_the_descriptor_not_only_the_name(self):
        """Fetching it separately opens a window of wrong panels."""
        recorder = Recorder()
        router, _, _ = switchable(event_callback=recorder)
        await router.switch_engine(ANTIGRAVITY)
        payload = recorder.payload("engineChanged")
        assert payload["engine"] == ANTIGRAVITY
        assert payload["previous"] == CLAUDE
        assert payload["capabilities"]["context_window_usage"]["supported"] is False

    async def test_it_clears_the_transcript_through_the_existing_event(self):
        """One clearing path, not two that can disagree.

        `sessionChanged` with an empty message list is what every client
        already resets on; a switch is a session boundary because the two
        transcript formats do not translate.
        """
        recorder = Recorder()
        router, _, _ = switchable(event_callback=recorder)
        await router.switch_engine(ANTIGRAVITY)
        assert recorder.names() == ["engineChanged", "sessionChanged"], (
            "The descriptor must be in place before anything re-renders on "
            "the clear, or the panel draws the surface the switch hid."
        )
        assert recorder.payload("sessionChanged")["messages"] == []

    async def test_a_failed_announcement_does_not_undo_the_switch(self):
        """The engine *has* changed. Saying otherwise is the worse lie."""
        recorder = Recorder(fail=True)
        router, _, _ = switchable(event_callback=recorder)
        answer = await router.switch_engine(ANTIGRAVITY)
        assert answer["changed"] is True
        assert router.list_engines()["active"] == ANTIGRAVITY

    async def test_a_router_with_no_callback_switches_silently(self):
        router, _, _ = switchable()
        answer = await router.switch_engine(ANTIGRAVITY)
        assert answer["changed"] is True


class TestEveryMountedEngineIsValidatedUpFront:
    def test_a_broken_alternate_refuses_at_build_time(self):
        """Not at switch time, when the working engine is already down.

        A switch that discovered a half-implemented adapter would have
        torn down the engine that worked before finding out, leaving the
        user on nothing.
        """
        from aic_dc.claude_code import ClaudeCodeService

        whole = type(
            "Whole",
            (),
            {name: (lambda self, *a, **k: None) for name in exposed(ClaudeCodeService)},
        )
        with pytest.raises(ValueError) as exc:
            build_router(
                whole(),
                engine=CLAUDE,
                alternates={ANTIGRAVITY: FakeAdapter()},
            )
        assert "antigravity adapter cannot be mounted" in str(exc.value)

    def test_the_real_adapters_both_mount(self):
        """The measurement the whole design rests on.

        Claude 48 public methods, Antigravity 31, and the 17-method
        difference is exactly RPC_SURFACES — so the wire surface is the
        same whichever is master, and a switch never renegotiates it.
        """
        from aic_dc.antigravity.service import AntigravityService
        from aic_dc.claude_code import ClaudeCodeService

        claude_names = exposed(ClaudeCodeService)
        ag_names = exposed(AntigravityService)
        assert not (ag_names - claude_names), (
            "Antigravity exposes a method Claude does not. The union is "
            "still generated, but the neat 48 = 31 + 17 identity below no "
            "longer holds and the surface table needs revisiting."
        )
        from aic_dc.engine_router import RPC_SURFACES

        assert claude_names - ag_names == set(RPC_SURFACES)
