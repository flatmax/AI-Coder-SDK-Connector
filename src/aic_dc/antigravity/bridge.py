"""The consultant's two tools, packaged for a Claude Code turn.

AG-7's deliverable as the master engine sees it: ``second_opinion`` and
``generate_image``, reachable from a Claude Code turn as MCP tools.

Why this is a *second* MCP server
---------------------------------
``aic_dc.claude_code.mcp_server`` already exposes an in-process server,
and adding two tools to it would have been one line shorter and wrong.

That server's tools are ungated by construction:
``permissions.can_use_tool`` early-returns an allow for anything matching
``mcp__aic-dc__*``, with no dialog and no broadcast, because
``specs5/3-engine/permissions.md`` puts them in the read-only row —
"displayed, not gated". The bridge's own docstring states the invariant
that earns it: *"Read-only, all six. Nothing here mutates the repository,
the engine or the UI."*

``generate_image`` writes a file. ``second_opinion`` spends money on a
different provider's account. Neither belongs in a row whose entry
condition is "the same class of consequence as Read", and putting them
there would not merely stretch a category — it would route a file write
around the permission dialog entirely, which is the thing AG-5 calls
non-negotiable.

So they mount under their own name. Nothing special-cases it, which means
they classify as ``mcp`` and reach the dialog by the ordinary path. The
cost is one more server in the Context tab's inventory; the alternative
was an ungated write.

Why it lives in this package
----------------------------
AG-4's observation is that the ``@tool``-decorated wrappers are
Claude-specific *packaging* around functions that are not. The packaging
is here rather than in ``claude_code`` because AG-1 requires the mirror —
Claude reachable as a consultant *from* Antigravity — and both directions
of that symmetry want to sit next to each other rather than one per
engine package.

Governing spec: ``specs5/plan-ag/`` — AG-7, and AG-5 for why this is not
on the ungated server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from typing import Any

from aic_dc.antigravity.consultant import Consultant, ConsultationError
from aic_dc.antigravity.credentials import MissingCredentialsError
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)

#: The server name the CLI prefixes onto every tool:
#: ``mcp__aic-dc-antigravity__second_opinion``. Deliberately *not*
#: ``aic-dc``: that prefix is what ``permissions.AIC_DC_MCP_SERVER``
#: ungates, and these two tools must reach the dialog.
SERVER_NAME = "aic-dc-antigravity"

#: How often a running consultation says it is still running.
#:
#: Two live browser consultations on 2026-09-02 showed a spinner and
#: nothing else for the full timeout, because Google had accepted the
#: request and never answered. The tab could not distinguish that from a
#: fast model thinking hard, and neither could the person watching it.
#:
#: A heartbeat is the smallest honest fix: it claims nothing about
#: progress — there is none to report, the harness is blocked on a socket
#: — and only says how long the wait has been. That is exactly the
#: distinction the UI was missing, and it is deliberately *not* dressed up
#: as progress, because a bar that moves while nothing happens is worse
#: than a number that grows.
#:
#: **It says two different things, and the difference is diagnostic.**
#: Google confirmed (2026-09-02) that free-tier requests are queued behind
#: paid traffic rather than refused, and that *"the capacity queueing
#: occurs at the routing layer before a model is allocated … the entire
#: wait time is absorbed into your Time to First Token"*. So a
#: consultation that has produced no step yet is queued, and one that has
#: is merely thinking — two states with the same spinner and completely
#: different meanings. Before the first step the heartbeat names the
#: queue, because "your free tier is waiting behind paying customers" is
#: something a reader can act on and "still working" is not.
HEARTBEAT_SECONDS = 20.0

#: How long the terminal event waits for the text it is terminating.
#:
#: The observer schedules each push as a task so the step loop is never
#: blocked, which leaves an ordering problem at the end: ``terminal: True``
#: overtaking the last ``streamChunk`` renders a settled tab that is missing
#: its final words. Draining before announcing fixes the order.
#:
#: **Bounded, because the drain is the one place a sink can reach the
#: answer.** ``_tab`` closes before ``second_opinion`` returns, so an
#: unbounded ``gather`` here hands a slow consumer — a paused browser tab, a
#: TCP window that has stopped opening — a hold on the tool result, and the
#: calling model waits on a rendering detail. Five seconds is long enough
#: that a local websocket never reaches it and short enough that nothing
#: waits on one that has stopped reading. Expiry is logged and the tab
#: settles anyway: a stale tab is a smaller fault than a stalled turn.
DRAIN_SECONDS = 5.0

#: How many un-consumed tool ids the anchor buffer keeps. Every entry is
#: normally consumed microseconds later by the handler the hook fired for,
#: so the buffer holds one item; what this bounds is the abnormal case,
#: where a call is denied at the permission dialog or the turn is aborted
#: between the hook and the handler and the entry is never taken back.
#: Turn-scoping in :meth:`ConsultantBridge._claim` clears those on the next
#: turn, so this only has to survive one turn's worth of them.
STAGE_LIMIT = 16

#: The arguments that identify one call of each consultation tool, in the
#: order they are read. Comparing the whole ``tool_input`` would be
#: stricter and worse: the hook reads it off the CLI's wire protocol while
#: the handler reads it from the in-process MCP call, and any divergence
#: in defaulting between those two paths would silently stop every
#: consultation from anchoring. These are model-authored strings both
#: sides carry verbatim, so they cannot drift.
#:
#: Both of ``second_opinion``'s arguments, not just the question. One turn
#: can issue several consultations concurrently, and a model comparing two
#: files writes the *same* question over two different contexts —
#: "review this for bugs" twice, with a different diff in each. On the
#: question alone those two are indistinguishable and pair by arrival
#: order, which is the index-pairing this design exists to avoid: handlers
#: do not start in the order their hooks fired, so the two live streams
#: would render under each other's cards.
ANCHOR_ARGS: dict[str, tuple[str, ...]] = {
    "second_opinion": ("question", "context"),
    "generate_image": ("prompt", "output_name", "aspect_ratio"),
}


def _normalise(value: Any) -> str:
    """One anchor argument as both sides of the join must see it.

    Shared by :func:`_anchor_key` and :meth:`ConsultantBridge._claim` so
    the hook's reading of a payload and the handler's reading of its own
    arguments agree by construction, rather than by two transformations
    being written the same way twice and staying that way.

    ``strip()`` is the only normalisation applied to a string, and it is
    applied because whitespace is the one difference the two transports
    could plausibly introduce without the model having written it.

    A non-string is JSON rather than empty. The schema declares every
    anchor argument as a string, so a payload carrying a dict here is
    already off the documented path — but collapsing all of them to one
    empty value would make two such calls indistinguishable, which is the
    one thing the key exists to prevent. ``sort_keys`` because the two
    sides may have round-tripped the value through different JSON
    decoders; ``default=str`` because a key that cannot be computed is
    worse than an approximate one.
    """
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers it
        return repr(value)


def _anchor_key(tool_name: str, tool_input: Any) -> tuple[str, ...]:
    """The identifying arguments of a consultation call, from either side.

    Both the ``PreToolUse`` hook and the tool handler call this on what
    they were handed, so the two agree by construction rather than by two
    call sites staying in step.

    Keyed by tool name rather than by sweeping every known argument: a
    ``generate_image`` payload that happened to carry a ``question`` field
    would otherwise be identified by it, and match a consultation.

    ``strip()`` is the only normalisation, and it is here because
    whitespace is the one difference the two transports could plausibly
    introduce without the model having written it.
    """
    names = ANCHOR_ARGS.get(tool_name, ())
    if not names or not isinstance(tool_input, dict):
        return ()
    return tuple(_normalise(tool_input.get(name)) for name in names)


def _text(body: str, *, is_error: bool = False) -> dict[str, Any]:
    """One text block, the shape every handler returns.

    ``is_error`` is passed through to ``CallToolResult(isError=...)`` by the
    SDK's in-process server, and the browser turns it into the card's status
    (``messages.py``). It is off by default because the ordinary failures here
    return prose explaining themselves — a consultation that could not be
    obtained says so in a sentence the asking model can act on, and flagging
    that as a tool fault would add nothing the sentence does not already say.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        result["is_error"] = True
    return result


def _accounting(observer: Any) -> tuple[tuple, tuple, tuple]:
    """What the gate saw, as ``(reached, escaped, unknown)``.

    Read in one place because two callers need the same three answers and
    must not be able to disagree: :func:`_grounding` composes the paragraph
    the asking model reads, and :meth:`second_opinion` decides from the same
    facts where that paragraph goes and whether the call failed.

    ``getattr`` rather than attributes, because the SDK transport's
    translator has no such notion and the bridge is not allowed to know which
    transport it holds. Normalised to tuples so a caller can test them and
    count them without consuming a one-shot iterator.
    """
    translator = getattr(observer, "translator", None)
    return (
        tuple(getattr(translator, "ungrounded_tools", ()) or ()),
        tuple(getattr(translator, "breached_tools", ()) or ()),
        tuple(getattr(translator, "unverified_tools", ()) or ()),
    )


def _grounding(observer: Any) -> str:
    """What the consultation could not do, for the model that asked.

    **Two sentences, and the first is unconditional.** This used to be
    empty whenever nothing had been refused, on the reasoning that a
    consultation asked to reason about a diff reaches for no tools and
    needs no disclaimer. That reasoning had the case backwards. The model
    that tries to read a file and is stopped is the *safe* one — it
    learns it has no tools, usually says so, and this fires. The
    dangerous one is the model that never tries: asked what a config file
    sets a timeout to, it answers from its weights, no tool call is made,
    nothing is refused, and the old header stayed silent — leaving the
    agent that asked to suppose the answer came from the repository it
    can itself see. A posture is a static property of how the consultant
    was launched, so it is stated whether or not anything happened to
    demonstrate it.

    The second sentence is added only when the model *tried* to retrieve
    and came back with nothing — the case where the prose beneath may
    describe a page nobody fetched (AG-R-22). Note what it reports:
    retrievals that failed, not refusals this app issued. Which component
    said no is this app's business; that the answer is missing what it
    reached for is the asking model's.

    **The tab is not enough on its own.** The reader of that tab is a
    person who may not be watching; the consumer of this string is the
    agent that asked, is blocked on it, and is about to act. Told in this
    module's own voice rather than folded into the answer, for the same
    reason ``second_opinion`` returns the answer verbatim: the consultant
    said what it said, and this is the app speaking beside it.

    **Above the answer, and outside it.** This began as a tagged block
    appended after the reply, and the reply is passed through verbatim —
    so nothing stopped the consultant from writing that same tag itself,
    saying it had been refused nothing, and leaving the asking model with
    two contradictory blocks and no way to tell which was ours. Reading
    order fixed half of that: whatever appears below is subordinate to
    the framing above it. The other half is :func:`_fence`, which puts
    the answer inside markers this sentence is outside of — so "not from
    the consultant" is a structural claim rather than an assurance.

    **And the unconditional sentence is retractable, because it is a
    claim rather than a disclaimer.** "Nothing below was read" is stated
    as fact, in this app's own voice, to a model that will act on it. One
    observation can make it false: a tool the consultation was not
    permitted to use that came back with output rather than an error,
    which means it ran. That is reported in the tab in a row of its own;
    here it replaces the posture outright, because a paragraph that
    asserted the isolation held and then mentioned that it had not would
    be read by exactly the wrong half of its audience.

    **And a third thing can be true, which is that this app does not
    know.** A barred call can end carrying neither the gate's refusal nor
    any output — rejected upstream, or killed mid-flight with the answer
    already streamed. The first version of this wrote such a call into
    the same sentence as a refusal, "and got nothing back", which is the
    app manufacturing a certainty out of an absence of evidence: the one
    failure it is in this paragraph to prevent, committed by the
    paragraph. It gets its own sentence, and the sentence says
    *unverified rather than absent* — weaker than the refusal's claim,
    stronger than the breach's retraction, and the only one of the three
    that is true of a subprocess this app watched die.

    ``getattr`` rather than an attribute, because the SDK transport's
    translator has no such notion and the bridge is not allowed to know
    which transport it holds.
    """
    reached, escaped, unknown = _accounting(observer)
    if escaped:
        return (
            f"Before the answer, from AIC⚡DC and not from the consultant: "
            f"a second opinion is launched with no tools and no repository "
            f"access, and for this one that did not hold — {_named(escaped)} "
            f"ran. So the usual assurance is withdrawn: "
            f"the answer below may contain material that was actually read "
            f"or fetched, by a process that was not supposed to be able to. "
            f"Treat it as unverified either way, and tell the user.\n\n"
        )
    # Two forms of the same posture, and which one is used is the whole
    # of the fix a seventh round asked for. The first *concludes*:
    # launched without tools, therefore nothing below was retrieved. The
    # second states only the launch. A live run (P27, arm J) composed the
    # first one and then appended the unverified sentence under it, so
    # the paragraph the asking model received asserted that nothing had
    # been read and then, one sentence later, said it could not establish
    # that — a contradiction in this app's own voice, in the one
    # paragraph whose entire job is to be the part that can be trusted. A
    # reader resolves a contradiction by picking a side, and the side
    # with the flatter grammar wins.
    certain = (
        "Before the answer, from AIC⚡DC and not from the consultant: a "
        "second opinion runs with no tools and no repository access, so "
        "nothing below was read, fetched or looked up. Treat any file "
        "contents, page contents, search results or live values in it as "
        "the consultant's own rather than a source's."
    )
    qualified = (
        "Before the answer, from AIC⚡DC and not from the consultant: a "
        "second opinion is launched with no tools and no repository "
        "access — it is given no way to read a file, fetch a page or run "
        "a command, and that is how this one was started."
    )
    if not reached and not unknown:
        return f"{certain}\n\n"
    sentences = [certain if not unknown else qualified]
    if reached:
        sentences.append(
            f"This one reached for {_named(reached)}, and got nothing back."
        )
    if unknown:
        sentences.append(
            f"It {'also ' if reached else ''}reached for {_named(unknown)}, "
            f"and ended without reporting what came back from "
            f"{'those' if len(tuple(unknown)) > 1 else 'that'}, so whether "
            f"anything was retrieved is not something this app can "
            f"establish. Treat any file contents, page contents, search "
            f"results or live values in the answer as unverified rather "
            f"than as absent."
        )
    return " ".join(sentences) + "\n\n"


def _named(tools: Any) -> str:
    """A list of tool names as a sentence, not as a list.

    "a, b and c" rather than "a, b, c": this is read by a model, and the
    live run that produced two refusals read "reached for search_web,
    read_url_content and was refused", which is a list that has lost its
    last comma rather than prose.
    """
    names = tuple(tools)
    if len(names) == 1:
        return str(names[0])
    return f"{', '.join(str(n) for n in names[:-1])} and {names[-1]}"


def _fence(answer: str) -> str:
    """The consultant's answer, quoted inside markers it cannot forge.

    Everything this module says about a consultation — that a tool was
    refused, that the answer is evidence rather than a verdict — is prose
    sitting next to more prose written by a model that was asked to
    reason about text an agent supplied. Without a boundary, the answer
    can close with a line in AIC⚡DC's voice and the reading model has no
    way to tell which of the two is the app.

    A per-call nonce is what makes the boundary hold rather than merely
    exist. The consultant never sees this string — it is generated after
    its answer is already in hand — so it cannot open a second fence, and
    a marker *inside* the quote is by construction the consultant's own
    words. A fixed delimiter would be guessable from this source file.

    :func:`secrets.token_hex` rather than :mod:`random`, because the
    guarantee is "not predictable by a model that has read this code",
    which is what a cryptographic source gives and a seeded PRNG does
    not. Four bytes: enough that guessing is hopeless, short enough that
    the markers stay readable in the tool card a person is looking at.

    **One sentence of explanation, not three.** The first version spelled
    out that a marker appearing *inside* the quote was therefore the
    consultant's own — true, and an invitation to spend the answer's
    reading on forgery that has not happened. The nonce makes the
    boundary hold whether or not the reader is told how; what the reader
    needs is which side of it this module is speaking from.

    **Brackets rather than an XML tag**, which a reviewer preferred as
    the more familiar shape. A consultation is asked about code, and its
    answers routinely contain markup, JSX and tag-shaped prose; a
    delimiter drawn from the same alphabet as the payload is the one kind
    a reader has to disambiguate. ``⟦aic-dc:…⟧`` cannot occur by
    accident, and the nonce means it cannot occur on purpose either.
    """
    mark = secrets.token_hex(4)
    return (
        f"The consultant's answer is quoted below between ⟦aic-dc:{mark}⟧ "
        "markers; the suffix is unique to this call and the consultant "
        "never saw it, so anything outside them is AIC⚡DC speaking.\n\n"
        f"⟦aic-dc:{mark}⟧\n{answer}\n⟦/aic-dc:{mark}⟧"
    )


class _Observer:
    """The open tab, as a consultant sees it.

    Callable, so the call site stays ``observer(step)`` and nothing about
    the SDK path changed when the CLI transport arrived. It carries the
    translator because the two transports read their answer from
    different places — ``conversation.last_response`` for the SDK, an
    absorbed ``result`` frame for ``agy`` — and the second of those lives
    in the pump.

    ``None`` is still the no-browser case, so every consultant must work
    with no observer at all; this object never means "no tab".
    """

    def __init__(self, feed: Any, translator: Any) -> None:
        self._feed = feed
        #: The pump this tab's events came out of, for a consultant that
        #: needs to read the turn back off it.
        self.translator = translator

    def __call__(self, raw: Any) -> None:
        self._feed(raw)


class ConsultantBridge:
    """The consultant, as tools a Claude Code turn can call.

    Holds a :class:`~aic_dc.antigravity.consultant.Consultant` and turns
    its exceptions into prose. Every failure comes back as *text* rather
    than as a raised error, because the caller is a model: an exception
    ends the tool call with a stack trace it cannot act on, while a
    sentence saying what went wrong lets it choose to try something else
    or to tell the user.
    """

    def __init__(
        self,
        consultant: Consultant,
        *,
        emit: Any = None,
        request_id: Any = None,
    ) -> None:
        self._consultant = consultant
        self._emit = emit
        self._request_id = request_id
        self._counter = 0
        self._tasks: set[Any] = set()
        # (turn, tool, key) -> tool_use_id, staged by the `PreToolUse`
        # hook and consumed by the handler. See :meth:`note_tool_use`.
        self._staged: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # AG-13 — the consultation as its own agent tab
    # ------------------------------------------------------------------

    def _new_task_id(self) -> str:
        """A fresh *task* identity for one consultation.

        Minted, and it stays minted even when the row's other two ids are
        borrowed from the real tool call (see :meth:`note_tool_use`). This
        is the id the ⏹ button sends, and ``stop_task`` routes on its
        shape: anything starting ``consultation-`` is stopped by this
        bridge without ever reaching the CLI, which has never heard of it.
        A real ``toolu_`` here would be offered to the CLI first and
        refused.
        """
        self._counter += 1
        return f"consultation-{id(self):x}-{self._counter}"

    def note_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None,
    ) -> None:
        """Record the id of a consultation tool call about to run. AG-28.

        Called from the ``PreToolUse`` hook, which fires immediately
        before the handler and is the *only* place this id is visible: an
        in-process MCP handler is invoked with its ``args`` dict and no
        context object (``claude_agent_sdk.tool``, verified 2026-09-01).
        Without it the consultation has to mint an identity of its own and
        render detached from the card that spawned it — the cost AG-13
        accepted and this retires.

        Correlating instead against "the most recent
        ``mcp__aic-dc-antigravity__*`` card in the pump" was the
        alternative, and it was rejected for being a guess whose failure
        mode is attaching a consultation's output to the *wrong* card.
        This is not a guess; the hook and the handler see the same call.

        Keyed on the call's arguments rather than paired by arrival order,
        because a turn can issue several consultations concurrently and
        nothing guarantees the handlers start in the order the hooks
        fired. Two calls identical in *every* argument remain
        indistinguishable and pair FIFO — recorded as AG-R-28 — but that
        is a repeated question with a repeated context, not the ordinary
        case of one question asked over two different diffs. Even then the
        answers cannot be swapped: each returns through its own tool call.
        """
        if not tool_use_id:
            return
        tool = str(tool_name or "").rsplit("__", 1)[-1]
        self._staged.append(
            {
                "turn": self._turn(),
                "tool": tool,
                "key": _anchor_key(tool, tool_input),
                "tool_use_id": str(tool_use_id),
            }
        )
        # A staged id is consumed by the handler that follows it — unless
        # there is no handler, which is the case worth bounding: a call
        # denied at the permission dialog, or a turn aborted between the
        # hook and the tool, leaves its entry behind. Turn-scoping in
        # :meth:`_claim` clears those on the next turn; the cap is what
        # keeps a *single* long turn of denials from growing without end.
        del self._staged[:-STAGE_LIMIT]

    def _claim(self, tool: str, *args: str) -> str | None:
        """Take back the id staged for this call, if it is still ours.

        Turn-scoped and self-cleaning, which is most of what makes a
        denied call harmless: the phantom entry it left is discarded the
        first time a *later* turn looks, so it can never be handed to a
        consultation in some other turn. No turn-end hook is needed for
        that, and one that was needed would be a second thing to keep in
        agreement.

        The rest of what makes it harmless is the ambiguity rule below.
        Within a single turn the phantom outlives its denial, so a retry
        of the same call would otherwise claim the *denied* id and render
        its live card under the call the user refused. Two indistinguish-
        able entries therefore claim nothing at all.

        **Reads nothing destructively on the way out.** A first cut
        cleared the whole buffer when there was no live turn, on the
        reasoning that entries staged outside one are unusable. They are
        unusable *to this call*; they may be the next one's. Failing to
        claim is a lost nesting, while clearing is a lost nesting for
        every other consultation in flight, so the quiet path returns
        empty-handed and touches nothing.

        Returns ``None`` whenever the join is not certain — no hook ran,
        the arguments did not match, the turn moved on. The caller then
        falls back to the minted identity, which is exactly the behaviour
        that shipped before this existed.
        """
        turn = self._turn()
        if turn is None:
            return None
        key = tuple(_normalise(value) for value in args)
        self._staged = [e for e in self._staged if e["turn"] == turn]
        matches = [
            index
            for index, entry in enumerate(self._staged)
            if entry["tool"] == tool and entry["key"] == key
        ]
        if len(matches) == 1:
            return str(self._staged.pop(matches[0])["tool_use_id"])
        if matches:
            # Two staged calls this turn are indistinguishable from each
            # other, so nothing here can say which of them is us. Taking
            # the first is a coin toss, and a lost toss is worse than an
            # unanchored row: the card would render under a *different*
            # call, beside that call's own result, and the two would
            # disagree in the one place a reader is looking.
            logger.debug(
                "%d staged tool ids for %s are indistinguishable; the "
                "consultation's row will not nest under its card",
                len(matches),
                tool,
            )
            return None
        logger.debug(
            "No staged tool id for %s; the consultation's row will not nest "
            "under its card",
            tool,
        )
        return None

    def _turn(self) -> str | None:
        """The request id the tab must be attributed to.

        ``subagentEvent`` is turn-scoped, and ``onSubagentEvent`` drops any
        event whose request id does not match a *live* Main tab. That is
        satisfied by construction here — a consultation only runs inside a
        Claude turn — and it is the requirement that fails silently if the
        bridge is ever driven from outside one.
        """
        source = self._request_id
        try:
            return source() if callable(source) else source
        except Exception:  # noqa: BLE001 - no tab is better than no answer
            logger.exception("Could not read the active request id")
            return None

    async def _announce(
        self, scope: str, label: str, *, task_id: str, **fields: Any
    ) -> None:
        """One ``subagentEvent``, in the shape the tab strip already reads.

        No new event name and no webapp change: ``subagent-tabs.js`` joins
        on identifiers alone, and ``subagent_type``/``description`` are
        labels. ``terminal`` is what stops the tab streaming forever.

        Waited for, because both announcements are ordering constraints —
        the opening row has to exist before blocks claim it, and the
        terminal one has to arrive after the text it terminates — but
        waited for *briefly*. A tab strip that is not reading is not a
        reason to hold a consultation.
        """
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        payload = {
            # Minted, so ⏹ routes here rather than to the CLI, which has
            # never heard of this id.
            "task_id": task_id,
            # Minted too, and deliberately: `agent_id` is the *transcript*
            # key. It keys the tab, it is what the "read this subagent's
            # transcript" button sends, and it is what appears in a log
            # line when that read fails. A consultation has no transcript
            # on disk (its blocks live in memory only), so this id names
            # something that cannot be fetched — and an id that is
            # obviously ours is the right way to say so. A `toolu_` here
            # would make a structural limitation read as a dropped
            # session in every log that saw it.
            "agent_id": task_id,
            # Borrowed when the hook caught it, and the only field that
            # moves. `groupBlocksByScope` places a row immediately after
            # the main-transcript tool block whose `block_id` matches
            # this, and a tool block's `block_id` *is* its `tool_use_id`
            # — so this one value is the whole of what nests the
            # consultation inside the card that spawned it.
            "tool_use_id": scope,
            "description": label,
            # The comment above `agent_id`, as a field. That comment is the
            # only place the browser's most consequential fact about this row
            # was written down, and a comment is not readable from a tab
            # strip: every reader of `agent_id` is a *transcript fetch*, and
            # each one had to infer "there is nothing to fetch" from the row's
            # type. Inferring a storage property from a UI classification is
            # how a future consultant-shaped tool with no transcript ends up
            # reading disk and reporting a missing session.
            #
            # Absent means "has one". The server's persistence predicate
            # enumerates two tool names (`_SUBAGENT_TOOLS` in
            # claude_code/history.py) and every other producer of this event
            # is on that side of it, so only the one producer that knows
            # otherwise has to say anything, and nothing else changes.
            "has_transcript": False,
            "task_type": "consultation",
            "subagent_type": "Antigravity",
            "status": "running",
            "terminal": False,
            **fields,
        }
        # `_push` already absorbs a dead client; scheduling it puts the
        # timeout on the *wait* rather than on the send.
        await self._drain(
            scope,
            {self._schedule(Event("subagentEvent", payload), request_id)},
            f"the tab's {'terminal' if fields.get('terminal') else 'opening'} row",
        )

    async def _push_posture(self, agent_id: str) -> None:
        """What this consultation cannot do, said before it does anything.

        The tab's counterpart to the first sentence of :func:`_grounding`,
        and it exists for the same reason on the surface that reason was
        first applied to only the other one. A consultation that reaches
        for a page and is refused raises a notice; a consultation that
        reaches for nothing raises none — and *that* is the dangerous
        shape, because it is what a model does when it answers from its
        weights. Measured: a consultation asked to review a patch spent
        45 events and 1844 output tokens on confident prose without
        calling a tool, and the tab rendered it with nothing to say that
        the process it came from could not see the repository the reader
        is looking at.

        Raised here rather than from the pump, because the pump sees only
        frames: a consultation that dies before its first frame would
        render as a tab with an answer-shaped silence in it. This is the
        one place that runs for every consultation that has a tab at all,
        and it runs before the consultant is even started.

        A statement of posture, not of an event, so it carries no toast:
        the warning that deserves interrupting a user is the one where a
        retrieval was attempted and lost, which is
        ``consultation_ungrounded``'s.
        """
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        event = Event(
            "systemEvent",
            {
                "subtype": "consultation_posture",
                "data": {
                    "agent_id": agent_id,
                    "message": (
                        "A second opinion runs with no tools and no "
                        "repository access: it answers from the question "
                        "and the context it was given, and cannot read a "
                        "file, fetch a page or run a command. Anything "
                        "below about file contents, page contents, search "
                        "results or live values is this model's own rather "
                        "than a source's."
                    ),
                },
            },
        )
        await self._drain(
            agent_id,
            {self._schedule(event, request_id)},
            "its opening posture",
        )

    async def _flush_pending(
        self, agent_id: str, translator: Any, pending: set[Any]
    ) -> None:
        """Close any tool card the consultation left open.

        A card is drawn the moment ``agy`` names the call and settled when
        the call reports. A consultation that timed out, crashed, hit a
        token ceiling or was stopped between those two frames left a
        spinner on the tab under a finished answer, which reads as a
        retrieval still running — and a reader waiting for it to resolve
        is waiting on something already over. A review round raised it;
        the translator does the settling and this is what runs it on the
        path a consultation actually takes.

        **Here rather than only in the pump's footer**, because a
        consultation never reaches that footer:
        ``AgyConsultant._run`` iterates the frames itself and this
        manager is what ends the tab. Placed before the drain so the
        results are among the things drained, and before the terminal row
        so they are not stranded behind it.

        ``getattr``, for the reason the rest of this class uses one: the
        SDK transport's translator has no such notion and the bridge is
        not allowed to know which one it holds. Failure here is swallowed
        — a tidy-up that raised inside a ``finally`` would replace the
        consultation's own exception with its own, which is the worst
        possible trade for a spinner.
        """
        settle = getattr(translator, "settle_pending", None)
        if self._emit is None or self._turn() is None or not callable(settle):
            return
        request_id = self._turn()
        try:
            events = settle()
        except Exception:  # noqa: BLE001 - never above the real failure
            logger.exception("Could not settle the consultation's open cards")
            return
        for event in events:
            pending.add(self._schedule(event, request_id))

    def _observer(
        self,
        agent_id: str,
        translator: Any,
        progress: dict | None = None,
        pending: set | None = None,
    ) -> Any:
        """Feed each step through the shared pump, tagged with our id.

        The translator is the *consultant's own* — ``StepTranslator`` for
        the SDK, ``AgyTranslator`` for the CLI — imported by whichever
        module can read those objects rather than reimplemented here,
        which is AG-R-9's redrawn tripwire and, since AG-16, the seam
        that keeps this class from knowing which transport it holds. Its
        ``agent_id`` makes every block it produces carry that scope, and
        that is the whole of what puts the text in the tab.

        Returned as a callable *object* rather than a closure so the
        consultant can read the translator back off it. That matters for
        the ``agy`` transport, where the turn's prose arrives in a
        ``result`` frame the pump has already absorbed: without it the
        consultant would have to translate the frames a second time to
        find the answer, and two passes over one stream is two chances
        for them to disagree.

        **This object is made whether or not anything is listening**, which
        is the invariant the rest of this class is arranged around: *no sink
        is load-bearing, and the result never depends on one*. An observer
        with nowhere to push still translates, so the consultant has exactly
        one way to assemble an answer and cannot acquire a second by having
        no browser attached. What the emit gate below decides is only
        whether anyone sees it.
        """

        def observe(step: Any) -> None:
            if progress is not None:
                progress["steps"] += 1
            # **Translate first, and unconditionally.** On this transport
            # the answer exists *because* a frame was absorbed here:
            # ``AgyConsultant._run`` calls ``observer(frame)`` instead of
            # ``translator.translate(frame)`` whenever a browser is
            # attached, so this is the only pass over the stream, and
            # ``response_text()`` reads back what it accumulated. An
            # earlier version returned before this loop when there was
            # nothing to emit to, which entangled rendering with
            # answer-extraction: any window where ``_turn()`` read ``None``
            # silently shortened the reply as well as the tab.
            #
            # Outside the `try` below on purpose. This call *is* the
            # accumulator, so a translator that raises is a failed
            # consultation and must say so; everything after it is
            # rendering, and rendering is not allowed to fail a
            # consultation. The line between the two is the whole point of
            # this method.
            events = translator.translate(step)
            request_id = self._turn()
            if self._emit is None or request_id is None:
                # Once per consultation rather than once per step — a gate
                # that closes stays closed, and the whole reason this is
                # logged is that its silence made an empty tab
                # undiagnosable (`specs5/known-issues.md` § *An empty
                # consultation tab*).
                #
                # **Two absences, two levels.** No emit at all is a server
                # with no browser attached: ordinary, expected, and a
                # warning per consultation would train a reader to ignore
                # the line. An emit with no live turn is the shape that
                # shipped the defect — something to push to, nowhere to
                # push it — and that one is worth waking up for.
                if progress is not None and not progress.get("muted"):
                    progress["muted"] = True
                    if self._emit is None:
                        logger.debug(
                            "Consultation %s has no browser to stream to; "
                            "translating for the answer alone",
                            agent_id,
                        )
                    else:
                        logger.warning(
                            "Consultation %s is streaming to nothing: there is "
                            "an emit but no live turn to attribute it to. The "
                            "answer is unaffected — translation happens above "
                            "this gate — but the tab will be missing text "
                            "until the turn is readable again",
                            agent_id,
                        )
                return
            try:
                for event in events:
                    # Counted so the tab can tell "said nothing" from "said
                    # nothing *here*", which is what `_tab` seeds against.
                    if progress is not None and event.name == "streamChunk":
                        progress["text"] = progress.get("text", 0) + 1
                    # Fire-and-forget: an observer is called from inside the
                    # step loop and must not block it, and a dropped chunk
                    # costs a frame of text rather than the consultation.
                    self._schedule(event, request_id, pending)
            except Exception:  # noqa: BLE001 - a tab is worth less than an answer
                # Reached by a malformed event, or by a loop with no
                # running one. The step is already accumulated, so the
                # answer survives this and the tab loses a frame.
                logger.exception(
                    "Consultation %s could not dispatch a translated step; "
                    "the answer is unaffected",
                    agent_id,
                )

        return _Observer(observe, translator)

    async def _push(self, event: Any, request_id: str) -> None:
        try:
            await self._emit(event, request_id)
        except Exception:  # noqa: BLE001
            logger.exception("Dropping consultation event %s", event.name)

    def _schedule(
        self, event: Any, request_id: str, pending: set | None = None
    ) -> Any:
        """Hand one event to the sink as a task, and answer with the task.

        **Every emit in this class goes through here, including the ones
        the consultation then waits for.** Awaiting ``self._emit`` directly
        is what made four of them load-bearing: a consumer that never
        returns holds the coroutine, and the coroutine is on the stack of
        the tool call. As a task, the wait is separable from the send —
        :meth:`_drain` can give up on it without touching it.
        """
        task = asyncio.ensure_future(self._push(event, request_id))
        # Two references, and they are not redundant. ``_tasks`` keeps the
        # task alive for as long as it runs — a task with no strong
        # reference can be garbage collected mid-await — while ``pending``
        # is one consultation's own drain list, emptied and forgotten when
        # its tab settles. One shared set for both jobs is how a long
        # consultation ends up waiting on a sibling's unsent frames.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if pending is not None:
            pending.add(task)
        return task

    async def _drain(self, agent_id: str, pending: set, what: str) -> None:
        """Wait for scheduled frames to land, but not forever.

        Ordering is the reason to wait at all — ``terminal: True`` must not
        overtake the last chunk, and a row must exist before blocks are
        attributed to it — and the answer is the reason to stop waiting.
        Every caller runs inside the tool call the model is blocked on.

        **Nothing is cancelled on expiry, and that is the deliberate half.**
        The original reason recorded here was that a send interrupted
        mid-``await`` leaves a half-written frame on a shared socket.
        **Measured 2026-09-11: that is false, and the real reason is
        better.** In websockets 16.1.1 a ``send(str)`` runs to
        ``transport.write()`` with no suspension point; the only await,
        ``drain()``, comes after the bytes are already in the transport
        buffer. Cancelling a genuinely suspended send left 403 frames
        parsing cleanly with none malformed, and the cancelled frame's full
        60,004 bytes still reached the peer.

        So cancelling cannot corrupt the stream — but it cannot *retract*
        the frame either, which is why it stays forbidden: a caller that
        cancels and concludes "not delivered" would be wrong, and the frame
        arrives anyway, later. Cancelling would buy nothing and cost the
        illusion of having withdrawn something. The tasks stay anchored in
        ``_tasks`` and finish or fail on their own time. What expiry gives
        up is only the guarantee about *order*, and it is logged, because a
        tab that settled early is indistinguishable from a model that
        stopped talking. See ``specs5/plan-ag/risks.md`` § AG-R-19.
        """
        if not pending:
            return
        _, unfinished = await asyncio.wait(list(pending), timeout=DRAIN_SECONDS)
        pending.clear()
        if unfinished:
            logger.warning(
                "Consultation %s: %s did not reach the browser within %.0fs "
                "(%d outstanding). Carrying on — the answer does not wait on "
                "a tab, so this costs rendering order, not the reply.",
                agent_id,
                what,
                DRAIN_SECONDS,
                len(unfinished),
            )

    @contextlib.asynccontextmanager
    async def _tab(self, label: str, *, anchor: str | None = None) -> Any:
        """Open a tab for one consultation, and settle it however it ends.

        A context manager because the *settling* is the part that must not
        be forgotten: ``state.streaming = !row.terminal`` in the webapp, so
        a consultation that raised without a terminal event leaves a tab
        spinning for the rest of the session. ``finally`` is the only
        placement that survives a refusal, a timeout and a cancel alike.

        Yields the observer to hand to the consultant — **always an
        observer, never ``None``**, even with no browser in sight. It used
        to yield ``None`` there, and that one word was the load-bearing
        sink: ``AgyConsultant._run`` read it as *translate the frames
        yourself*, so whether a tab existed decided which code assembled
        the reply. Two paths to one answer is two things to keep in
        agreement, and the empty tab is what it looks like when they
        stop being. Now the sink's absence is visible in exactly one
        place, the emit gate, and nothing downstream can see it.

        What the gate still buys is the tab machinery — announce,
        heartbeat, seed and drain — none of which a browserless session
        pays for.

        **The status is earned, not assumed.** An earlier version reported
        ``completed`` unconditionally from the ``finally``, and the first
        live run through the browser showed what that costs: Google never
        answered, the consultation timed out after 180s, and the row
        settled to a **green** "completed" LED — because the webapp maps
        that status straight to green (``subagent-tabs.js`` ``_TERMINAL_LED``).
        A failure rendered as a success is the one outcome worth more than
        a spinner, and it is exactly the manufactured-consent shape AG-5
        and AG-R-3 are both written against.

        So the caller must let the exception propagate *through* this
        manager rather than catching it inside: ``else`` is what earns
        ``completed``, and a ``return`` from inside the ``with`` block
        would look like success here no matter what it returned.
        """
        task_id = self._new_task_id()
        # Two ids, and the split is AG-28. The consultation keeps one
        # coherent identity of its own — `task_id`, which is also its
        # `agent_id` — and gains a *pointer* to where it attaches: the
        # spawning tool call's id, when the `PreToolUse` hook caught it.
        #
        # `scope` is that pointer, and it is what every block, notice and
        # heartbeat below is stamped with, because the renderer fills a
        # row from the blocks whose `agent_id` matches the row's
        # `tool_use_id`. Falling back to the task id is not a degraded
        # mode so much as the previous one: it is exactly what shipped
        # before the hook existed, and it still renders — beside the card
        # instead of inside it.
        scope = anchor or task_id
        # Asked for rather than named: the consultant knows which raw
        # objects it will feed this thing (AG-16). `StepTranslator` is
        # still the answer on the SDK transport, and the fallback keeps a
        # consultant written before this seam existed working.
        make = getattr(self._consultant, "make_translator", None)
        translator = (
            make(self._turn() or "", scope)
            if callable(make)
            else StepTranslator(self._turn() or "", agent_id=scope)
        )
        # Mutable, shared with the heartbeat: it is the only way the
        # heartbeat can tell a queued consultation from a working one, and
        # that distinction is the whole of what it has to say.
        progress = {"steps": 0, "text": 0}
        pending: set[Any] = set()
        observer = self._observer(scope, translator, progress, pending)

        if self._emit is None or self._turn() is None:
            # Built above the gate, deliberately: the observer is how the
            # answer is assembled, so it is not part of what a missing
            # browser switches off. Everything below this line is.
            yield observer
            return

        await self._announce(scope, label, task_id=task_id)
        await self._push_posture(scope)
        status = "failed"
        heartbeat = asyncio.ensure_future(self._heartbeat(scope, progress))
        try:
            yield observer
        except BaseException as exc:
            # The reason, into the tab. Until now a failed consultation
            # settled red and said nothing about why; the explanation went
            # to the model as tool text, which the user does not read.
            await self._push_reason(scope, exc)
            # Including cancellation: a consultation the user stopped did
            # not complete, and saying so is the point of the ⏹ button.
            raise
        else:
            status = "completed"
            # **The tab and the answer are not fed by the same frames.**
            # Text reaches the tab as ``streamChunk`` deltas; the answer
            # comes from ``response_text()``, which prefers the prose
            # ``agy`` assembles in its ``result`` frame. So a turn that
            # sent no deltas returns a complete answer to the caller and
            # leaves the tab blank — which is how one shipped, and it read
            # as a consultation that never ran
            # (`specs5/known-issues.md` § *An empty consultation tab*).
            # Seeded from the same translator the answer came out of, so
            # the two cannot disagree about what was said.
            if not progress["text"]:
                await self._seed_tab(scope, translator)
        finally:
            heartbeat.cancel()
            # Before the drain and before the terminal row, because both
            # of those close the tab over whatever is still on it. See
            # `_flush_pending`.
            await self._flush_pending(scope, translator, pending)
            # Drain what *this* consultation scheduled before saying its
            # tab is done, or the terminal event can arrive ahead of the
            # text it is meant to be terminating. Bounded: see
            # `DRAIN_SECONDS` for why a rendering wait must not be able to
            # hold the answer.
            await self._drain(scope, pending, "its streamed text")
            await self._announce(
                scope,
                label,
                task_id=task_id,
                status=status,
                terminal=True,
                usage=translator.turn_usage() or None,
            )

    async def _heartbeat(self, agent_id: str, progress: dict | None = None) -> None:
        """Say how long the wait has been, until cancelled.

        Cancelled in the ``finally`` of :meth:`_tab`, so it cannot outlive
        the consultation it is reporting on. ``CancelledError`` is allowed
        to propagate — swallowing it is how a "harmless" background task
        becomes one that never stops.

        Its notice is *scheduled* rather than awaited, which is what makes
        that cancel safe: awaiting the send here put a half-finished frame
        inside the thing being cancelled, so a consultation that ended
        while the beat was in flight could cut a message in half on a
        socket the next one is going to use.
        """
        waited = 0.0
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            waited += HEARTBEAT_SECONDS
            request_id = self._turn()
            if self._emit is None or request_id is None:
                return
            started = bool(progress and progress.get("steps"))
            message = (
                f"Antigravity is working — {waited:.0f}s so far."
                if started
                else (
                    f"Waiting for Google to start — {waited:.0f}s so far, and "
                    "nothing has arrived yet. On a free-tier key requests are "
                    "queued behind paid traffic rather than refused, and the "
                    "whole wait lands before the first token."
                )
            )
            self._schedule(
                Event(
                    "systemEvent",
                    {
                        "subtype": "engine_notice",
                        "data": {"message": message},
                        "agent_id": agent_id,
                    },
                ),
                request_id,
            )

    async def _seed_tab(self, agent_id: str, translator: Any) -> None:
        """Put the answer in the tab when no delta ever did.

        One ``streamChunk`` in the shape ``AgyTranslator._agent_response``
        already emits, so the browser needs no change: it replaces by
        ``block_id``, and ``seq`` 1 is the first and only frame for a block
        no delta ever wrote to.

        Never raises. A tab is worth less than the answer it is describing,
        and this runs on the path that has already earned ``completed``.
        """
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        read = getattr(translator, "response_text", None)
        if not callable(read):
            return
        try:
            text = read().strip()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("Could not read the consultation's prose for its tab")
            return
        if not text:
            return
        logger.info(
            "Consultation %s streamed no text; seeding its tab from the result frame",
            agent_id,
        )
        seed = Event(
            "streamChunk",
            {
                "block_id": f"{agent_id}-answer",
                "seq": 1,
                "content": text,
                "done": True,
                "agent_id": agent_id,
            },
        )
        # Waited for so the terminal row cannot overtake it, bounded so the
        # answer this was read *out of* is never held up by showing it.
        await self._drain(
            agent_id, {self._schedule(seed, request_id)}, "the seeded answer"
        )

    async def _push_reason(self, agent_id: str, exc: BaseException) -> None:
        """The failure's own words, into the tab that is about to go red."""
        request_id = self._turn()
        if self._emit is None or request_id is None:
            return
        reason = Event(
            "systemEvent",
            {
                "subtype": "engine_error",
                "data": {"message": " ".join(str(exc).split())[:600]},
                "agent_id": agent_id,
            },
        )
        # Bounded like the rest: this runs on the way out of a *failing*
        # consultation, and a sink that stopped reading must not turn a
        # reported failure into a hung tool call.
        await self._drain(
            agent_id, {self._schedule(reason, request_id)}, "the failure's reason"
        )

    async def cancel(self) -> bool:
        """Stop a running consultation. AG-13's ⏹, and it is real.

        Reached from ``stop_task``, which is why that method belongs to
        the ``subagent_stop`` surface. A button that did nothing would
        read as a hung engine rather than as a missing feature. Note that
        the surface being ``UNBUILT`` on this transport does not make this
        method decorative: a consultation is stopped here, by the bridge,
        without ever reaching the CLI — what is unbuilt is stopping a
        subagent *`agy` itself* spawned.
        """
        return await self._consultant.cancel()

    @property
    def available(self) -> bool:
        """Whether the tools can do anything.

        Read by the caller deciding whether to register them at all —
        AG-9's "hidden rather than stubbed", applied to a tool definition:
        two tools that always answer "no credentials" cost context on
        every turn and buy nothing.
        """
        return self._consultant.available

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def second_opinion(self, question: str, context: str = "") -> dict[str, Any]:
        """Ask Antigravity, and hand the answer back verbatim.

        Verbatim on purpose. The value of a second opinion is that it was
        not produced by the agent reading it, so summarising it here — or
        framing it as "Antigravity agrees" — would put this module's
        judgement between two models that are supposed to disagree in
        front of the user.
        """
        # Claimed before the tab is opened, and only ever claimed here:
        # one call consumes one staged id, so a handler that runs without
        # a hook having fired gets `None` and the minted identity.
        anchor = self._claim("second_opinion", question, context)
        try:
            # The catch is *outside* the tab, so a failure propagates
            # through it and settles the row as failed rather than as a
            # green "completed". See `_tab`.
            async with self._tab("Second opinion", anchor=anchor) as observer:
                answer = await self._consultant.second_opinion(
                    question, context, observer=observer
                )
        except (ConsultationError, MissingCredentialsError) as exc:
            return _text(f"The second opinion could not be obtained: {exc}")
        # Ordering is priority, and priority moves when something is wrong.
        # Normally the first thing to say is what this *is* — a second model
        # reasoning independently, to be weighed rather than obeyed. When the
        # gate did not hold, or could not be shown to have held, the first
        # thing to say is that, because a surface that previews a result by
        # its opening line is otherwise shown the reassurance and never the
        # withdrawal (AG-R-29). The grounding paragraph opens "Before the
        # answer, from AIC⚡DC and not from the consultant", which names its
        # speaker and its position, so it reads as a lede without rewording.
        #
        # A breach also returns `is_error`. The consultation produced prose,
        # so this is not the tool failing to answer — it is the tool failing
        # to be what it promised, which is the thing the card beside it
        # cannot show on its own: `agy`'s own success renders truthfully and
        # a reader skimming a restored session sees nothing wrong. The cost
        # is that the asking model may retry, and the per-turn consultation
        # quota (AG-26) is what bounds that.
        # Only `escaped` and `unknown` hoist. A consultation that merely
        # reached for a tool and was refused is one where the gate *worked*,
        # so the paragraph still opens with an assurance that holds and
        # there is nothing to lead with.
        _, escaped, unknown = _accounting(observer)
        attribution = (
            "A second opinion from Google Antigravity (a different model, "
            "reasoning independently — treat it as evidence, not as a "
            "verdict)."
        )
        grounding = _grounding(observer)
        body = (
            f"{grounding}{attribution}\n\n{_fence(answer)}"
            if escaped or unknown
            else f"{attribution}\n\n{grounding}{_fence(answer)}"
        )
        return _text(body, is_error=bool(escaped))

    async def generate_image(
        self,
        prompt: str,
        output_name: str = "",
        aspect_ratio: str = "",
    ) -> dict[str, Any]:
        """Generate an image into the repository and report where it went.

        The reported path is repo-relative and verified against the
        filesystem (AG-R-3), so it is directly usable in a markdown or
        HTML reference — which is the only reason the agent asked.
        """
        anchor = self._claim("generate_image", prompt, output_name, aspect_ratio)
        try:
            # Outside the tab, for the same reason as second_opinion.
            async with self._tab("Generate image", anchor=anchor) as observer:
                result = await self._consultant.generate_image(
                    prompt,
                    output_name=output_name,
                    aspect_ratio=aspect_ratio,
                    observer=observer,
                )
        except (ConsultationError, MissingCredentialsError) as exc:
            return _text(f"The image could not be generated: {exc}")
        return _text(
            f"Image written to {result.path} ({result.bytes_written:,} bytes). "
            "The path is repo-relative and verified on disk; the file tree "
            "and the viewer can open it."
            + (f"\n\nThe model said: {result.summary}" if result.summary else "")
        )

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------

    def build_tools(self) -> list[Any]:
        """The two ``SdkMcpTool`` definitions, before they are wrapped.

        Separate from :meth:`build_server` for the reason the index
        bridge's equivalent is: ``create_sdk_mcp_server`` folds them into
        an object that does not hand them back, and the descriptions,
        schemas and annotations are exactly what wants checking.

        Neither carries ``readOnlyHint``. ``generate_image`` writes a
        file, and ``second_opinion`` reaches a third-party service and
        bills a different account — a hint that either is read-only would
        be a claim about consequence, not about the repository.
        """
        from claude_agent_sdk import tool
        from mcp.types import ToolAnnotations

        external = ToolAnnotations(openWorldHint=True)

        @tool(
            "second_opinion",
            "Ask Google's Gemini, running as an independent agent, to review a "
            "question you have already formed a view on — a diff, a design "
            "choice, a diagnosis. Two models disagreeing is information; the "
            "same model asked twice is not. One call is one round, and a "
            "first answer is rarely the answer: end each round by asking what "
            "it needs next, run the checks it asks for, and bring the results "
            "back until the positions converge or the disagreement is stated "
            "plainly. It keeps no memory between calls, so carry the exchange "
            "forward in `context`, attributing earlier answers to an earlier "
            "reviewer rather than to it — a model shown its own words defends "
            "them. It has no repository access either, so pass the code it "
            "needs. Costs a call on a separate Google account.",
            {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to put, stated so it can be "
                        "answered without the surrounding conversation.",
                    },
                    "context": {
                        "type": "string",
                        "description": "The code, diff or facts it needs, "
                        "including the earlier rounds of this consultation. It "
                        "cannot read the repository and remembers nothing, so "
                        "anything omitted here is unavailable to it.",
                    },
                },
                "required": ["question"],
            },
            external,
        )
        async def second_opinion(args: dict[str, Any]) -> dict[str, Any]:
            return await self.second_opinion(
                question=args.get("question", ""),
                context=args.get("context", ""),
            )

        @tool(
            "generate_image",
            "Generate an image with Google's image model and write it into the "
            "repository. Anthropic's models cannot generate images, so this is "
            "a capability the session otherwise does not have. Returns the "
            "repo-relative path, verified on disk, ready to reference from "
            "markdown or HTML.",
            {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the image should show.",
                    },
                    "output_name": {
                        "type": "string",
                        "description": "Preferred filename, e.g. "
                        "'docs/architecture.png'. The model chooses one if "
                        "this is omitted.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "Aspect ratio such as '16:9' or '1:1'.",
                    },
                },
                "required": ["prompt"],
            },
        )
        async def generate_image(args: dict[str, Any]) -> dict[str, Any]:
            return await self.generate_image(
                prompt=args.get("prompt", ""),
                output_name=args.get("output_name", ""),
                aspect_ratio=args.get("aspect_ratio", ""),
            )

        return [second_opinion, generate_image]

    def build_server(self) -> Any:
        """The ``McpSdkServerConfig`` for ``ClaudeAgentOptions.mcp_servers``.

        In-process, like the index bridge — but under its own name, so the
        permission layer treats these two as the third-party calls they
        effectively are.
        """
        from claude_agent_sdk import create_sdk_mcp_server

        return create_sdk_mcp_server(name=SERVER_NAME, tools=self.build_tools())
