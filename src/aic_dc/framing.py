"""Turn framing — the one description of what the user is looking at.

Answers exactly one question, *what is the user looking at?*, which no tool
can answer because the answer is in a browser. Everything the agent might
want to **read** it reads with its own tools, so this carries paths, ranges
and mode facts only, and never file content
(``specs5/3-engine/session.md`` § Turn framing, ``specs5/plan/decisions.md``
CC-14).

Why this is a module of its own
===============================
The text used to live in :func:`aic_dc.claude_code.session.build_framing`,
where it was reachable by exactly one engine — so on both Antigravity
transports the browser's report of the open file was **stored and never
read**. ``AgyService._viewer`` and ``AntigravityService._viewer`` were
written by two paths each and read by nobody
([AG-33](../../specs5/plan-ag/decisions.md#ag-33)).

The fix is not three builders. A model asked *"why is this failing?"* about
the file on the user's screen has to be told the same fact in the same
words whichever engine is master, or the answer depends on a choice the
user made in Settings for unrelated reasons — and
[AG-9](../../specs5/plan-ag/decisions.md#ag-9) is the standing rule against
paying for one feature twice. So the wrapper, the validator, the sentences
and the composition all live here, and every engine adapter calls them.

Only facts the user could not reasonably have typed
===================================================
A file the user *wants* named is named in the prompt — the picker inserts
the path there rather than into a block out here
(``specs5/plan/decisions.md`` CC-21). So this carries the viewer's live
cursor and the review's shape, and nothing else. :func:`build` returns the
empty string when there is nothing to say, so an ordinary turn is sent
exactly as the user typed it.

The block is the user's prompt, and it is not the user's words
=============================================================
Framing rides **on the prompt** rather than on a side channel, and that is
a decision rather than an inheritance. [AG-32](../../specs5/plan-ag/decisions.md#ag-32)
had just moved the ``agy`` write guidance *off* the prompt onto
``PreInvocation`` for three faults, and only one of the three applies here:

- *Once per turn* was a fault for standing guidance, because the write it
  guards against can happen at any invocation. It is **correct** here.
  "What the user was looking at when they pressed send" is a fact about the
  turn, and re-asserting it every invocation would either repeat a stale
  claim as though it were live, or contradict the turn's own opening
  framing when the user scrolled mid-turn.

  Said plainly, because it is the one thing this arrangement gives up: on
  these transports the framing is the **only** report, and there is no way
  to ask for a fresh one. Claude's model can call ``ui_state``
  (``claude_code/mcp_server.py``) whenever it wants the live answer; the
  only MCP server ``agy`` is given is the consultation listener, so its
  model cannot. The framing is still a true statement about the turn, and
  the missing tool is a separate gap recorded as
  [AG-R-33](../../specs5/plan-ag/risks.md#ag-r-33) rather than a reason to
  re-assert a fact every invocation and hope it is still current.
- *Master only* was a fault for guidance a subagent needs. A subagent is
  given its own task rather than the user's screen, and Claude frames the
  master's turn only, so matching that is parity rather than a gap.
- *Stored as the user's words* is a real cost and it is paid the same way
  Claude pays it: a named wrapper the model can tell from the user's own
  text, and :func:`aic_dc.claude_code.history.strip_framing` removing the
  block at read time. The mirror stores it **verbatim** on purpose — see
  :meth:`aic_dc.antigravity.mirror.AntigravityMirror.note_prompt`, whose
  reason is that a transcript which quietly disagreed with what the model
  was sent would be worse than one carrying a block the reader strips.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The wrapper around our framing, so the model can tell it from the user's
#: own words and so a user who pastes similar text is not confused with the
#: real thing.
#:
#: Defined here rather than in the three places that used to spell it:
#: :func:`aic_dc.claude_code.history.strip_framing` reads what :func:`build`
#: writes, and a wrapper edited on one side only would leave the block in
#: every rendered prompt with nothing reporting it.
FRAMING_OPEN = "<aic-dc-ui-context>"
FRAMING_CLOSE = "</aic-dc-ui-context>"

#: The review keys reported, in the order they are rendered.
_REVIEW_KEYS = ("branch", "base_branch", "merge_base")


def _as_int(value: Any) -> int | None:
    """An ``int`` or ``None``, never a raise.

    Moved from :mod:`aic_dc.claude_code.session` unchanged, guard and all.
    The browser is the source and a line number arriving as a string is a
    serialisation detail rather than a fault, so ``str`` is converted; but
    the accepted types are named rather than inferred from whether
    ``int()`` happens to work, because an object with an ``__int__`` did not
    come from JSON and a line number is not what it is.

    ``bool`` is excluded ahead of the whitelist it would otherwise pass: it
    is an ``int`` in Python, and "cursor on line True" is not a sentence
    anyone meant to write.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Viewer:
    """What the user has open, for turn framing. Never file content.

    Aliased in :mod:`aic_dc.claude_code.session` as ``ViewerFraming``, which
    is the name the package's ``__all__`` publishes and the specs and tests
    refer to. One class rather than two, because :meth:`from_dict` is the
    **only** validator: an engine that normalised the browser's dict itself
    would accept a shape the others reject, and the symptom is a framing
    block that reads correctly on one engine and says ``None`` on another.
    """

    path: str
    start_line: int | None = None
    end_line: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Viewer | None:
        """Build from the RPC payload, tolerating a missing or bad shape.

        ``None`` for anything without a usable ``path``, which covers the
        ordinary cases — no viewer open, a cleared pane, a payload that
        never carried one — as well as the malformed ones. There is nothing
        to say about a file nobody is looking at, and *saying* so would put
        a sentence in front of the model on every turn that has none.
        """
        if not isinstance(data, Mapping):
            return None
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return None
        return cls(
            path=path,
            start_line=_as_int(data.get("start_line")),
            end_line=_as_int(data.get("end_line")),
        )

    def as_payload(self) -> dict[str, Any]:
        """The stored shape, for an adapter keeping this as its last known.

        The inverse of :meth:`from_dict`, and the reason it exists is that a
        viewer stated on the turn has to be *storable*: an adapter that kept
        the raw argument instead would hold a shape nothing had validated,
        one push away from the ``(cursor on line None)`` this module exists
        to prevent. Absent lines are omitted rather than sent as ``None``,
        which is what :func:`viewer_payload` guarantees on the other path.
        """
        return viewer_payload(self.path, self.start_line, self.end_line) or {}


def viewer_payload(
    path: Any = None,
    start_line: Any = None,
    end_line: Any = None,
) -> dict[str, Any] | None:
    """The stored shape for a ``set_viewer_state`` push, or ``None`` to clear.

    The *other* end of the same rule. :meth:`Viewer.from_dict` validates
    what a turn is framed from; this validates what the browser pushes
    between turns, and every adapter's ``set_viewer_state`` goes through it
    so all three store the same thing.

    ``None`` for a falsy or non-string path, because closing the pane has to
    be sayable: the alternative leaves the agent pointed at a file nobody is
    looking at for the rest of the session. A line that is not an ``int`` is
    **omitted rather than stored as itself** — a ``None`` in the slot would
    render as ``(cursor on line None)`` in front of the model, which is the
    kind of sentence that gets believed.

    ``bool`` is excluded where the Claude adapter's original accepted it.
    That was never a live defect — :func:`_as_int` dropped it one layer
    later — but storing ``start_line: True`` and rendering nothing is a
    disagreement between two records of the same fact, and there is no
    reason to keep it.
    """
    if not path or not isinstance(path, str):
        return None
    state: dict[str, Any] = {"path": path}
    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if isinstance(value, int) and not isinstance(value, bool):
            state[name] = value
    return state


def viewer_lines(viewer: Viewer | None) -> list[str]:
    """The viewer's own two lines, or none at all."""
    if viewer is None:
        return []
    where = f"- {viewer.path}"
    if viewer.start_line is not None:
        if viewer.end_line is not None and viewer.end_line != viewer.start_line:
            where += f" (lines {viewer.start_line}-{viewer.end_line} selected)"
        else:
            where += f" (cursor on line {viewer.start_line})"
    return ["Open in the user's editor pane:", where]


def review_lines(review: Mapping[str, Any] | None) -> list[str]:
    """The review's shape, when review is on.

    Silent on an inactive review rather than reporting it as off: a turn
    that is not in review mode has nothing to say about review, and a line
    saying so would be one more sentence between the user's words and the
    model on every ordinary turn.
    """
    facts = review or {}
    if not facts.get("active"):
        return []
    lines = ["Code review is active:"]
    for key in _REVIEW_KEYS:
        value = facts.get(key)
        if value:
            lines.append(f"- {key.replace('_', ' ')}: {value}")
    return lines


def build(
    viewer: Viewer | None = None,
    review: Mapping[str, Any] | None = None,
) -> str:
    """Describe UI state the agent cannot otherwise see.

    The empty string when there is nothing to say, which is what makes an
    ordinary turn arrive exactly as the user typed it — the caller composes
    unconditionally and gets the user's own text back unchanged.
    """
    lines = [*viewer_lines(viewer), *review_lines(review)]
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{FRAMING_OPEN}\n{body}\n{FRAMING_CLOSE}"


def compose(framing: str, message: str) -> str:
    """Framing plus the user's text, in that order.

    Framing first because it is context for what follows, and because
    :func:`aic_dc.claude_code.history.strip_framing` recovers the user's
    words by matching the block at the **start** of the prompt — a block
    appended instead would render as part of what the user typed.
    """
    if not framing:
        return message
    return f"{framing}\n\n{message}"


def resolve(
    turn_viewer: Any,
    pushed: Any,
) -> Viewer | None:
    """Which of the two arrival paths answers for this turn.

    The browser can report the open file on the turn itself or by pushing
    it between turns through ``set_viewer_state``, and in this app it is
    always the push: ``webapp/src/app-shell/viewer-framing.js`` is the only
    writer and it deliberately leaves the turn argument null, because two
    sources for one field disagree and the resulting bug is unattributable.

    Both are honoured anyway, and the precedence is what the distinction
    costs. ``None`` on the turn means *not stated*, so the push stands in —
    which is the path that still works when the turn did not come from this
    browser at all. Anything else on the turn is an **answer**, including an
    empty mapping, so a caller that means "nothing is open" can say it and
    is not overridden by a stale push.
    """
    if turn_viewer is None:
        return Viewer.from_dict(pushed)
    return Viewer.from_dict(turn_viewer)
