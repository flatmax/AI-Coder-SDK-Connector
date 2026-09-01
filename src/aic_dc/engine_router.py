"""The engine router: two adapters, one RPC namespace.

``specs5/plan-ag/decisions.md`` AG-3. The second engine mounts under the
**same** RPC namespace as the first, so the browser's call sites do not
fork, and the webapp learns what the running engine can feed from a
capability descriptor rather than from the engine's name.

Why not a second namespace
==========================
``ClaudeCodeService.<method>`` appears at 43 methods across 59 webapp
files. A second namespace turns every one of those into a routing
decision, in the layer with the least test coverage and the most
incidental coupling. ``add_service`` already takes a ``name`` override, so
the alternative costs one argument.

The class name stays ``ClaudeCodeService`` even when it is fronting
Antigravity. That reads oddly and is the correct trade: it is an interface
identifier, not a description of the implementation, and renaming it is a
separate mechanical change that can happen later or never.

Why the methods are generated
=============================
jrpc-oo finds a service's methods with
``inspect.getmembers(cls, predicate=inspect.isfunction)`` on the **class**
(``jrpc_oo/ExposeClass.py:37-41``), so a ``__getattr__`` that forwarded
everything would expose nothing at all: the handshake sends a method list,
and a name that is not on the class is not in it.

So :func:`build_router` generates one real delegating function per public
method of the master adapter, at construction time. That is not cleverness
for its own sake — it is the only shape in which the router's surface
**cannot drift from the adapter's**. A hand-written router is 48 methods
that must be remembered when a 49th is added, and the failure mode is a
method that works in Python and 404s over RPC, which nothing would catch
until somebody clicked the button.

What this router does *not* do yet
==================================
**It routes to one engine.** The Antigravity adapter does not implement
the 43-method surface — it has a session, a step pump and a permission
gate, and no ``chat_streaming``, ``history_list`` or ``get_model`` — so
there is nothing to switch to. :func:`build_router` therefore takes one
master today, and the capability descriptor it publishes is that engine's.

That is deliberate and it is the whole reason this lands now rather than
with a second master. The router is **behaviour-preserving**: it exposes
exactly the method names the adapter exposes, and every call reaches the
same object it reached before. That claim is cheap to test and expensive
to give up, and it is what makes the switch, when it comes, a change to
one constructor rather than to a working system.

Governing spec: ``specs5/plan-ag/`` — AG-3, AG-9, AG-R-4.
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

from aic_dc import capabilities

logger = logging.getLogger(__name__)

#: The RPC namespace both engines mount under (AG-3).
#:
#: A constant rather than a literal at the call site, because the whole
#: point is that there is exactly one of these and the webapp's 59 files
#: assume it. ``main.py`` passes it to ``add_service``; nothing else
#: should ever name it.
RPC_NAME = "ClaudeCodeService"

#: Which RPC method belongs to which hideable surface.
#:
#: The bridge between :mod:`aic_dc.capabilities`, which is about *panels*,
#: and the RPC surface, which is about *methods*. A method listed here is
#: only meaningful on an engine whose descriptor supports its surface;
#: on any other engine the router refuses it with
#: :class:`UnsupportedOnThisEngine` rather than delegating.
#:
#: **A method absent from this table is core**, and every engine must
#: implement it. That default is the safe direction: forgetting to map a
#: new method makes it *required* — a loud failure at startup on an engine
#: that lacks it — where the opposite default would silently make it
#: optional and let an engine mount with a hole in it.
#:
#: This is derived from AG-9 rather than invented. The webapp should not
#: be calling these at all on an engine that cannot feed them, because the
#: panel is hidden; the refusal is what happens when it does anyway, and
#: it has to be a stated "this engine has no such thing" rather than an
#: ``AttributeError`` that reads as a crash.
RPC_SURFACES: dict[str, str] = {
    "get_account_usage": "account_rate_limits",
    "get_context_usage": "context_window_usage",
    "list_commands": "slash_commands",
    "get_mcp_status": "mcp_server_inventory",
    "reconnect_mcp_server": "mcp_server_inventory",
    "toggle_mcp_server": "mcp_server_inventory",
    "get_session_storage": "session_mirror",
    "resume_session": "session_mirror",
    "history_delete": "transcript_history",
    "history_image": "transcript_history",
    "history_list": "transcript_history",
    "history_load": "transcript_history",
    "history_search": "transcript_history",
    "get_subagent_transcript": "subagent_tabs",
    "list_subagent_transcripts": "subagent_tabs",
}


class UnsupportedOnThisEngine(RuntimeError):
    """An RPC method was called for a surface this engine cannot feed.

    Raised rather than returning an empty value, and that is the whole
    point of AG-9 restated at the RPC layer: an empty list does not say
    "no servers", it says "no answer". A caller that reaches here has
    ignored the capability descriptor, and the honest response is to say
    so rather than to synthesise a plausible-looking nothing.
    """


#: Methods the router serves itself rather than delegating.
#:
#: Named here so :func:`build_router` refuses to generate a delegate that
#: would shadow one. A master adapter that grew a ``get_engine_capabilities``
#: of its own would otherwise silently win, and the descriptor the webapp
#: reads would become whatever that engine happened to return.
ROUTER_OWNED = frozenset(
    {
        "get_engine_capabilities",
        "list_engines",
    }
)


class EngineRouterBase:
    """The router's own surface, before the delegates are grafted on.

    Everything here is engine-agnostic by construction: it answers from
    :mod:`aic_dc.capabilities` and from its own registry, never by asking
    an adapter. An engine cannot be the authority on what it cannot do —
    that is the question the descriptor exists to answer, and asking the
    engine would reintroduce exactly the "no answer looks like no data"
    failure AG-9 is written against.
    """

    def __init__(self, master: Any, *, engine: str) -> None:
        if engine not in capabilities.ENGINES:
            raise ValueError(
                f"{engine!r} is not a known engine. Add it to "
                f"capabilities.ENGINES with a column in the descriptor "
                f"first — an engine nothing can describe cannot be hidden "
                f"correctly."
            )
        self._master = master
        self._engine = engine

    # ------------------------------------------------------------------
    # The router's own RPC methods
    # ------------------------------------------------------------------

    def get_engine_capabilities(self) -> dict[str, Any]:
        """Which surfaces the running engine can feed (AG-3, AG-9).

        The webapp reads this to decide whether to render a panel at all.
        It carries statuses and no engine identity: AG-R-4 requires that
        no webapp branch key off an engine name string, and the surest way
        to hold that line is to give the browser nothing to branch on.
        """
        return capabilities.descriptor(self._engine)

    def list_engines(self) -> dict[str, Any]:
        """What engines exist, and which one is master.

        Deliberately *not* a list the browser picks a behaviour from. It
        is for the engine selector and for diagnostics: a human choosing
        which engine to start a session on is a different thing from a
        component deciding whether to draw a bar, and only the second is
        forbidden from knowing the name.
        """
        return {
            "active": self._engine,
            "available": list(capabilities.ENGINES),
            "mountable": [self._engine],
        }

    # ------------------------------------------------------------------
    # Not RPC — leading underscore keeps jrpc-oo out of them
    # ------------------------------------------------------------------

    @property
    def _adapter(self) -> Any:
        return self._master


def _public_methods(instance: Any) -> list[str]:
    """The adapter's RPC surface, read the way jrpc-oo reads it.

    Deliberately the same call jrpc-oo makes — ``getmembers`` over
    functions on the class, minus underscores — rather than a ``dir()``
    filter that happens to agree today. If the two ever disagree, the
    router would advertise a method the transport does not, or the
    reverse, and both are invisible until a button stops working.
    """
    return sorted(
        name
        for name, _ in inspect.getmembers(
            type(instance), predicate=inspect.isfunction
        )
        if not name.startswith("_")
    )


def _delegate(name: str) -> Any:
    """One forwarding method, preserving async-ness and signature.

    ``functools.wraps`` copies the adapter's own docstring and signature
    onto the delegate, which matters for more than tidiness: jrpc-oo
    inspects the exposed callables, and a wall of identical
    ``(*args, **kwargs)`` stubs would erase the surface's self-description
    for anything that reads it.

    Async and sync are generated separately rather than handled with one
    ``await``-if-coroutine wrapper. The adapter has both — ``shutdown`` is
    a coroutine, ``get_server_info`` is not — and a wrapper that returned
    a coroutine for a synchronous method would change its contract for
    every caller, not just the RPC one.
    """

    def _sync_delegate(self: Any, *args: Any, **kwargs: Any) -> Any:
        return getattr(self._master, name)(*args, **kwargs)

    async def _async_delegate(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await getattr(self._master, name)(*args, **kwargs)

    return _sync_delegate, _async_delegate


def _refusal(name: str, surface: str, engine: str) -> Any:
    """A method that exists on the wire and says why it has no answer.

    Generated rather than omitted, because *omitting* it would take the
    name out of the handshake's method list and the browser would get a
    transport-level "no such method" — indistinguishable from a version
    mismatch or a broken build. The method is there; it declines.
    """

    def _declines(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedOnThisEngine(
            f"{name} serves the {surface!r} surface, which the {engine} "
            f"engine cannot feed. get_engine_capabilities() reports it as "
            f"unsupported and the panel should be hidden rather than "
            f"calling this."
        )

    _declines.__name__ = name
    _declines.__doc__ = (
        f"Unsupported on this engine: {surface}. See "
        f"get_engine_capabilities()."
    )
    return _declines


def _missing_core_methods(master: Any, engine: str) -> list[str]:
    """Core methods this adapter does not implement.

    Core is *everything not in* :data:`RPC_SURFACES`, so a method nobody
    mapped counts as required. An engine missing one cannot be mounted:
    the browser would call it and get an attribute error at click time,
    which is the failure this converts into a refusal to start.
    """
    have = set(_public_methods(master))
    want = {
        name
        for name in _public_methods_of_reference()
        if name not in RPC_SURFACES
        or capabilities.supports(engine, RPC_SURFACES[name])
    }
    return sorted(want - have)


def _public_methods_of_reference() -> list[str]:
    """The RPC surface the webapp expects, read off the Claude adapter.

    The reference is the shipped engine rather than a hand-kept list,
    for the same reason the delegates are generated: a second copy of the
    method names is a second thing to forget. Imported lazily because
    ``service.py`` is heavy and nothing else here needs it.
    """
    from aic_dc.claude_code import ClaudeCodeService

    return sorted(
        name
        for name, _ in inspect.getmembers(
            ClaudeCodeService, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    )


def build_router(
    master: Any,
    *,
    engine: str = capabilities.CLAUDE,
    require_full_surface: bool = True,
) -> Any:
    """A router exposing ``master``'s whole surface, plus its own.

    Parameters
    ----------
    master:
        The engine adapter this session routes to. Its public methods
        become the router's, generated rather than listed.
    engine:
        Which engine ``master`` is, for the capability descriptor. Checked
        against :data:`capabilities.ENGINES` rather than accepted as a
        free string, because an engine nothing can describe cannot be
        hidden correctly.
    require_full_surface:
        Refuse to build if the adapter is missing a core method — one that
        no capability marks optional. On by default, and the reason is
        that a half-mounted engine fails at *click* time, one button at a
        time, with an ``AttributeError`` that reads as a crash. Turning it
        off is for tests that route to a stub.

    Returns
    -------
    An instance of a freshly-built class. Fresh per call rather than
    cached: the generated methods close over nothing but their own name,
    but the class *identity* is what jrpc-oo keys its method list off, and
    sharing one between two routers in one process — which the tests do —
    would make the second registration silently reuse the first's surface.

    Raises
    ------
    ValueError
        If the adapter shadows a router method, or — with
        ``require_full_surface`` — if it cannot serve the core surface.
        The second message *is* the to-do list for mounting that engine.
    """
    if engine not in capabilities.ENGINES:
        # Checked before anything is generated so the message is about the
        # engine rather than about a surface lookup failing downstream.
        raise ValueError(
            f"{engine!r} is not a known engine. Add it to "
            f"capabilities.ENGINES with a column in the descriptor first."
        )

    delegates: dict[str, Any] = {}
    collisions = []
    for name in _public_methods(master):
        if name in ROUTER_OWNED:
            collisions.append(name)
            continue
        surface = RPC_SURFACES.get(name)
        if surface is not None and not capabilities.supports(engine, surface):
            delegates[name] = _refusal(name, surface, engine)
            continue
        sync, asynchronous = _delegate(name)
        original = getattr(type(master), name)
        chosen = asynchronous if inspect.iscoroutinefunction(original) else sync
        delegates[name] = functools.wraps(original)(chosen)

    if collisions:
        # Loud, because the silent version is the descriptor quietly
        # becoming whatever the engine returned.
        raise ValueError(
            f"{', '.join(collisions)} is both a router method and an adapter "
            f"method. Rename one: the router must be the authority on what "
            f"the engine cannot do, and a delegate here would make the "
            f"engine the authority on itself."
        )

    if require_full_surface:
        missing = _missing_core_methods(master, engine)
        if missing:
            raise ValueError(
                f"The {engine} adapter cannot be mounted: it is missing "
                f"{len(missing)} method(s) the webapp calls — "
                f"{', '.join(missing)}. Implement them, or — where a method "
                f"belongs to a surface this engine genuinely cannot feed — "
                f"map it in RPC_SURFACES so the router refuses it instead."
            )

    # Refusals for surfaces this engine cannot feed, where the adapter did
    # not define the method at all. Without this the name would be absent
    # from the handshake and the browser would see "no such method",
    # which is indistinguishable from a broken build.
    for name, surface in RPC_SURFACES.items():
        if name not in delegates and not capabilities.supports(engine, surface):
            delegates[name] = _refusal(name, surface, engine)

    cls = type(RPC_NAME, (EngineRouterBase,), delegates)
    return cls(master, engine=engine)
