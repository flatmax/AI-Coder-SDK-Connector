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

Switching master, and why the wire does not move
================================================
AG-1 chooses one master **per session**, so the router mounts every
adapter it was given and swaps which one it forwards to. The thing that
makes this cheap is a measured coincidence rather than a design goal:

* Claude exposes 48 public methods, Antigravity 31, and Antigravity
  exposes **nothing Claude does not**.
* The 17-method difference is *exactly* :data:`RPC_SURFACES`. Not
  approximately — every method one engine has and the other lacks is
  already mapped to a hideable surface.

So the set of names on the wire is the same whichever engine is master:
48 delegates plus this class's own. That matters more than it looks,
because jrpc-oo sends its method list **once**, at the handshake
(``jrpc_oo/ExposeClass.py:37-41``), and cannot renegotiate it afterwards.
A router whose surface changed with the master would have to re-register
the service and reconnect every browser to switch engines. This one does
not have to, so a switch is a field assignment.

What *does* move is which names refuse. The check therefore happens **per
call**, against ``self._engine`` as it is at that moment, rather than
being baked into the generated method at build time. That is the one
structural difference from the first cut, and it is what a mutable master
requires: a delegate generated for the engine that happened to be mounted
at startup would keep answering for it after the swap.

What switching is *not*
=======================
It is not a way to move a conversation between engines. The two
transcript formats do not translate — ``sdk-surface.md`` § *What does not
translate*: Antigravity owns an opaque ``save_dir`` with no
``SessionStore`` counterpart, and its ``Step`` is flat with
``trajectory_id``/``depth`` rather than nested content blocks — so a
switch **ends the outgoing session and starts a new one**. Nothing is
deleted: each engine keeps its own mirror, and an old conversation stays
listed and loadable. Switching back is a new session too, for the same
reason.

Governing spec: ``specs5/plan-ag/`` — AG-1, AG-3, AG-9, AG-R-4.
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
    "stop_task": "subagent_tabs",
    "rewind_files": "file_checkpointing",
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
        "switch_engine",
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

    def __init__(
        self,
        master: Any,
        *,
        engine: str,
        alternates: dict[str, Any] | None = None,
        event_callback: Any = None,
    ) -> None:
        for name in (engine, *(alternates or {})):
            if name not in capabilities.ENGINES:
                raise ValueError(
                    f"{name!r} is not a known engine. Add it to "
                    f"capabilities.ENGINES with a column in the descriptor "
                    f"first — an engine nothing can describe cannot be hidden "
                    f"correctly."
                )
        # Every mountable adapter, master included. Which one is master is
        # `_engine` and nothing else: an adapter held here is *mounted*,
        # which is a statement about it being constructed and serviceable,
        # not about it answering calls right now.
        self._adapters: dict[str, Any] = dict(alternates or {})
        self._adapters[engine] = master
        self._engine = engine
        self._event_callback = event_callback

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
            "mountable": sorted(self._adapters),
            # Supplied by the server rather than mapped in the browser. A
            # label table in the webapp would be a branch on an engine
            # name, which AG-R-4 forbids — and the thing a user is choosing
            # between here is *which account pays*, which is not something
            # the browser can know. A name with no label falls back to
            # itself, so an engine added without one is legible rather
            # than blank.
            "labels": {
                name: capabilities.ENGINE_LABELS.get(name, name)
                for name in capabilities.ENGINES
            },
        }

    async def switch_engine(self, engine: str) -> dict[str, Any]:
        """Make another mounted engine the master (AG-1).
        **Localhost only.**

        Gated for the reason ``new_session`` and ``resume_session`` are:
        this ends the conversation every client is looking at, and a
        participant choosing which engine answers would be deciding for
        everyone.

        **This is a session boundary, and it cannot be anything else.**
        The two engines' transcripts do not translate (see the module
        docstring), so there is no version of this that carries the
        current conversation across. The outgoing engine is stopped, and
        the incoming one connects lazily on the next turn — with no
        resume, which is what makes it a new session. Nothing on disk is
        touched: the outgoing conversation stays in its own mirror and
        stays loadable from the history browser.

        Refused mid-turn rather than interrupting one, matching
        ``new_session``: the user can cancel first, and pulling the engine
        out from under a live turn loses its tail.
        """
        restricted = self._check_localhost_only()
        if restricted is not None:
            return restricted
        if engine not in capabilities.ENGINES:
            return {
                "error": f"{engine!r} is not a known engine",
                "reason": "unknown_engine",
            }
        if engine == self._engine:
            # Not an error. A second window may have switched already, and
            # the honest answer to "make X master" when X is master is yes.
            return {"engine": self._engine, "changed": False}
        adapter = self._adapters.get(engine)
        if adapter is None:
            return {
                "error": (
                    f"The {engine} engine is not mounted in this session. It "
                    f"is a known engine, so this is a missing credential or a "
                    f"missing optional dependency rather than a typo — "
                    f"list_engines() reports what is mountable."
                ),
                "reason": "not_mountable",
            }
        if _engine_is_busy(self._master):
            return {"error": "A turn is still running", "reason": "turn_active"}

        outgoing = self._engine
        await _stop_engine(self._master)
        self._engine = engine
        logger.info("Engine switched: %s -> %s", outgoing, engine)
        await self._announce_engine(outgoing)
        return {"engine": engine, "previous": outgoing, "changed": True}

    # ------------------------------------------------------------------
    # Not RPC — leading underscore keeps jrpc-oo out of them
    # ------------------------------------------------------------------

    @property
    def _master(self) -> Any:
        """The adapter calls forward to, resolved on every access.

        A property rather than a field so that a swap is one assignment to
        ``_engine`` and cannot leave the two disagreeing.
        """
        return self._adapters[self._engine]

    @property
    def _adapter(self) -> Any:
        return self._master

    def _check_localhost_only(self) -> dict[str, Any] | None:
        """The master's localhost gate, borrowed rather than reimplemented.

        ``permissions.md``'s localhost-only rule is a property of the
        product rather than of an engine, and both adapters implement the
        same check; a third copy here would be a third thing to keep in
        agreement. An adapter without one fails closed.
        """
        check = getattr(self._master, "_check_localhost_only", None)
        if check is None:
            return {"error": "Restricted to the host", "restricted": True}
        return check()

    async def _announce_engine(self, previous: str) -> None:
        """Tell every window the master changed.

        Carries the descriptor rather than only the name, because what the
        browser has to do on this event is re-decide which panels render,
        and making it fetch that separately opens a window in which it has
        the new engine and the old capabilities. The name is present for
        the engine selector and for diagnostics — the same carve-out
        :meth:`list_engines` documents, and it is not a licence for a
        render path to branch on it (AG-R-4).
        """
        if self._event_callback is None:
            return
        try:
            await self._event_callback(
                "engineChanged",
                {
                    "engine": self._engine,
                    "previous": previous,
                    "capabilities": capabilities.descriptor(self._engine),
                },
            )
            # The transcript on screen belongs to the engine that just went
            # away, and this is the event every client already agrees to
            # clear on — the same one `new_session` sends, with the same
            # empty message list. Reusing it rather than teaching the chat
            # panel a second way to be reset is the difference between one
            # clearing path and two that can disagree.
            #
            # Sent *after* `engineChanged` so the descriptor is already in
            # place: a panel that re-rendered on the clear while the store
            # still held the outgoing engine's capabilities would draw
            # exactly the surface the switch was meant to hide.
            await self._event_callback(
                "sessionChanged",
                {"session_id": None, "messages": [], "action": "engine"},
            )
        except Exception:
            # A failed announcement must not undo a completed switch. The
            # engine *has* changed; a window that missed the event is
            # stale, which is recoverable, where raising here would leave
            # the router switched and the caller told it failed.
            logger.exception("engineChanged announcement failed")


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


def _engine_is_busy(adapter: Any) -> bool:
    """Whether a turn is in flight on this adapter.

    Two questions rather than one, because the adapters answer in
    different vocabularies and both answers matter. ``streaming_active``
    is the Claude session's own view of a turn and is authoritative where
    it exists; ``_turn_tasks`` is the set both adapters keep — it is part
    of the contract ``claude_code.commit`` reads off a service — and it
    catches a background job like a commit that is not a chat turn but
    would still lose its tail if the engine went away underneath it.

    Fails **busy** on an adapter that answers neither, which is the safe
    direction: refusing a switch that could have been allowed costs the
    user a second attempt, where allowing one that should have been
    refused truncates a running turn.
    """
    session = getattr(adapter, "session", None)
    streaming = getattr(session, "streaming_active", None)
    if streaming:
        return True
    tasks = getattr(adapter, "_turn_tasks", None)
    if tasks is None:
        return streaming is None
    return any(not task.done() for task in tasks)


async def _stop_engine(adapter: Any) -> None:
    """Stop an adapter's engine, without letting teardown raise.

    The switch has already been decided by the time this runs, so a
    failure here must not abort it: the alternative is a router that
    refused to switch because the engine it was leaving would not go
    quietly, which strands the user on the engine they asked to leave.
    Both adapters document ``shutdown`` as never raising; this is the belt
    for the case where one grows a way to.
    """
    shutdown = getattr(adapter, "shutdown", None)
    if shutdown is None:
        return
    try:
        await shutdown()
    except Exception:
        logger.exception("Engine shutdown failed during a switch")


def _refusal_message(name: str, surface: str, engine: str) -> str:
    return (
        f"{name} serves the {surface!r} surface, which the {engine} "
        f"engine cannot feed. get_engine_capabilities() reports it as "
        f"unsupported and the panel should be hidden rather than "
        f"calling this."
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

    **The capability check is here, at call time, rather than at build
    time.** The master can change under a mounted router, so a delegate
    that decided once — when it was generated — would answer for the
    engine that happened to be mounted at startup. Reading
    ``self._engine`` on every call is what makes the two states
    impossible to disagree.
    """
    surface = RPC_SURFACES.get(name)

    def _resolve(self: Any) -> Any:
        if surface is not None and not capabilities.supports(
            self._engine, surface
        ):
            raise UnsupportedOnThisEngine(
                _refusal_message(name, surface, self._engine)
            )
        target = getattr(self._master, name, None)
        if target is None:
            # Unreachable through the mount check, which refuses an
            # adapter missing a core method. Stated rather than left to
            # AttributeError so that if it ever does happen it reads as
            # what it is instead of as a transport fault.
            raise UnsupportedOnThisEngine(
                f"{name} is not implemented by the {self._engine} engine, "
                f"and is not mapped to a surface that would excuse it."
            )
        return target

    def _sync_delegate(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _resolve(self)(*args, **kwargs)

    async def _async_delegate(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await _resolve(self)(*args, **kwargs)

    return _sync_delegate, _async_delegate


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
    alternates: dict[str, Any] | None = None,
    event_callback: Any = None,
    require_full_surface: bool = True,
) -> Any:
    """A router exposing every mounted adapter's surface, plus its own.

    Parameters
    ----------
    master:
        The engine adapter this session starts on. Which adapter is master
        can change afterwards (:meth:`EngineRouterBase.switch_engine`); the
        surface generated here cannot, and does not need to.
    engine:
        Which engine ``master`` is, for the capability descriptor. Checked
        against :data:`capabilities.ENGINES` rather than accepted as a
        free string, because an engine nothing can describe cannot be
        hidden correctly.
    alternates:
        Engine name → adapter, for every *other* engine this session may
        switch to. Each is validated exactly as the master is, at build
        time rather than at switch time, because a switch that discovered
        a half-implemented adapter would already have torn down the
        working one.
    event_callback:
        ``async (event_name, payload) -> None``. Used for one event,
        ``engineChanged``. Optional: a router with no callback switches
        silently, which is right for a test and wrong for a server.
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
    mounted: dict[str, Any] = dict(alternates or {})
    mounted[engine] = master
    for name in mounted:
        if name not in capabilities.ENGINES:
            # Checked before anything is generated so the message is about
            # the engine rather than about a surface lookup failing
            # downstream.
            raise ValueError(
                f"{name!r} is not a known engine. Add it to "
                f"capabilities.ENGINES with a column in the descriptor first."
            )

    # The surface is the union of every mounted adapter's methods and the
    # reference engine's, so the names on the wire do not depend on which
    # adapter is master. That is what lets a switch be a field assignment
    # rather than a re-registration: jrpc-oo sends this list once, at the
    # handshake, and cannot revise it afterwards.
    #
    # Including the reference even when it is not mounted is deliberate.
    # It is the surface the webapp was written against, and a name it
    # calls must exist and *decline* rather than be absent — an absent
    # method is a transport-level "no such method", indistinguishable from
    # a version mismatch or a broken build.
    names = set(_public_methods_of_reference())
    for adapter in mounted.values():
        names.update(_public_methods(adapter))

    collisions = sorted(names & ROUTER_OWNED)
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
        for name, adapter in sorted(mounted.items()):
            missing = _missing_core_methods(adapter, name)
            if missing:
                raise ValueError(
                    f"The {name} adapter cannot be mounted: it is missing "
                    f"{len(missing)} method(s) the webapp calls — "
                    f"{', '.join(missing)}. Implement them, or — where a "
                    f"method belongs to a surface this engine genuinely "
                    f"cannot feed — map it in RPC_SURFACES so the router "
                    f"refuses it instead."
                )

    delegates: dict[str, Any] = {}
    for name in sorted(names):
        sync, asynchronous = _delegate(name)
        original = _reference_function(name, mounted)
        chosen = asynchronous if inspect.iscoroutinefunction(original) else sync
        delegates[name] = (
            functools.wraps(original)(chosen) if original is not None else chosen
        )
        if original is None:
            delegates[name].__name__ = name

    cls = type(RPC_NAME, (EngineRouterBase,), delegates)
    return cls(
        master,
        engine=engine,
        alternates={k: v for k, v in mounted.items() if k != engine},
        event_callback=event_callback,
    )


def _reference_function(name: str, mounted: dict[str, Any]) -> Any:
    """The function a delegate copies its signature and docstring from.

    A mounted adapter is preferred over the reference class, because a
    session running one engine should describe itself with that engine's
    own words. Which mounted adapter is arbitrary only in appearance:
    ``test_async_ness_matches_the_claude_adapter`` already asserts the two
    agree about being coroutines, which is the one property the choice
    here can change.
    """
    for adapter in mounted.values():
        original = getattr(type(adapter), name, None)
        if original is not None:
            return original
    from aic_dc.claude_code import ClaudeCodeService

    return getattr(ClaudeCodeService, name, None)
