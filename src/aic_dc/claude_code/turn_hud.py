"""The post-turn terminal summary — one block per turn, on the server's log.

``specs5/5-webapp/viewers-hud.md`` § *Terminal HUD* has specified this since
the conversion and nothing printed it until now. It was found by clearing the
last entry in ``specs5/known-issues.md``: the entry named one stale section of
one reference twin, and the sweep that fixed it found a whole surface specified
in four places — two spec files, a reference twin carrying the exact column
layout, and **an invariant in each of the two spec files** — with no producer.
It survived four phases because *an invariant is not a test*. The queue entry
is ``specs5/next.md`` § B6.

Why a terminal block at all, when the browser HUD says the same thing: the
server's terminal is the only surface that survives the browser. A turn that
ends while nothing is connected reports to nobody otherwise, and the operator
watching the process start — which is how the auth failure in § C9 was noticed
at all — has no other view of what the engine is doing.

**The costliest thing here is which fields it reads.** The specs said this line
prints "cost or billing mode", which is the *pre-phase-6* reading: it dates from
when ``total_cost_usd`` was believed to be per-turn and null under a
subscription. Both halves are wrong (``cost.py`` holds the correction), and a
terminal line that answered differently from the browser would be a second
definition of what a turn cost — the shape ``specs5/next.md`` §§ C3, C7 keep
converging away from. So this module reads ``turn_cost_usd`` /
``turn_cost_basis`` / ``turn_model_usage`` and **never** ``total_cost_usd`` or
``model_usage``, and it gives the same three-way answer the browser's
``turn-cost.js`` gives: a figure, "nothing extra", or "cost unknown" with the
reason. The three spec files were corrected before this was written.

**Path naming.** Modified files are shortened against the repo root, and a path
outside it is printed absolute — the rule ``permissions.py``'s
``build_diff_payload`` already uses for the dialog headline. That is a
*server-side* naming of a file for a human, which § C3 converged onto the
browser; the exemption is the one § C3 itself names, that some surfaces "have
to exist before there is a browser to ask". A terminal line never has a browser
to ask, so it is the same exemption rather than a fourth mechanism.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .cost import MEASURED, RESET, UNPRICED

logger = logging.getLogger(__name__)

__all__ = ["format_turn_hud", "format_cost", "log_turn_hud"]

#: Model names are long and the block is read in a terminal, so the name column
#: is capped rather than allowed to push the numbers off an 80-column screen. A
#: name over the cap keeps its tail, because the distinguishing part of
#: ``us.anthropic.claude-opus-5-v1:0`` is the end of it.
_MODEL_COLUMN_CEILING = 28

#: How many modified paths the ``Files:`` line names before summarising. A turn
#: that touched forty files says so in one clause; forty paths in a log record
#: is the turn scrolling its own summary off the screen.
_FILES_CEILING = 8


def format_cost(usd: Any) -> str | None:
    """A USD figure in the CLI's own format, or ``None`` if it is not one.

    Four decimals up to fifty cents, two above — the same rule as the
    browser's ``formatCost``, lifted from the bundled ``claude`` binary so a
    figure here reads like the one the terminal already shows. Two decimals
    throughout would print most per-turn costs as ``$0.00``, which reads as
    free rather than as small.

    A negative figure is refused rather than printed: it is a bug upstream,
    not a refund.
    """
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        return None
    value = float(usd)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return f"${value:.2f}" if value > 0.5 else f"${value:.4f}"


def _tokens(value: Any) -> int:
    """One usage counter, floored at zero.

    Missing reads as 0 and negative reads as 0, on the same grounds as a
    negative cost.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _usage_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-model counters for **this turn**, busiest first.

    Reads ``turn_model_usage`` and nothing else. Falling back to the engine's
    cumulative ``model_usage`` would put the session's totals on a line
    labelled with this turn's, which is the misreading ``cost.py`` exists to
    end.

    The label prefers ``canonicalModel`` because the map's key is the raw
    provider string, which on Bedrock or Vertex is an id like
    ``us.anthropic.claude-opus-5-v1:0`` rather than a name.
    """
    usage = result.get("turn_model_usage")
    if not isinstance(usage, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, entry in usage.items():
        if not isinstance(entry, dict):
            continue
        row = {
            "input": _tokens(entry.get("inputTokens")),
            "output": _tokens(entry.get("outputTokens")),
            "cache_read": _tokens(entry.get("cacheReadInputTokens")),
            "cache_write": _tokens(entry.get("cacheCreationInputTokens")),
        }
        row["total"] = (
            row["input"] + row["output"] + row["cache_read"] + row["cache_write"]
        )
        if row["total"] <= 0:
            continue
        canonical = entry.get("canonicalModel")
        row["model"] = canonical if isinstance(canonical, str) and canonical else str(key)
        rows.append(row)
    rows.sort(key=lambda row: row["total"], reverse=True)
    return rows


def _cache_hit(row: dict[str, Any]) -> str:
    """This model's cache hit rate, or ``—`` when there is no denominator.

    ``cacheRead / (input + cacheRead)``: what fraction of the prompt the
    engine did not have to re-send. The browser deliberately does *not*
    compute this — it reports the counters and lets the reader do it, because
    in 300px there is no room (``specs5/5-webapp/viewers-hud.md``
    § *Per-Model Rows Are Not Summed*). A terminal line has the width, which
    is the whole reason the two surfaces differ here.
    """
    denominator = row["input"] + row["cache_read"]
    if denominator <= 0:
        return "—"
    return f"{round(row['cache_read'] / denominator * 100)}%"


def _cost_line(result: dict[str, Any]) -> str | None:
    """The turn's cost, or the reason there is not one.

    The same four answers as ``turn-cost.js``, in the same order, for the same
    reason: a turn that cost nothing extra and a turn whose cost is unknown
    used to render identically, and telling them apart is what phase 6 was
    for. ``None`` means no ``Cost:`` line at all — a browsed or unattributed
    turn, where "we never recorded it" is a different fact from "we lost track
    of it" and neither is worth a line that says ``$0.00``.
    """
    basis = result.get("turn_cost_basis")
    if basis == MEASURED:
        figure = format_cost(result.get("turn_cost_usd"))
        if figure is None:
            # A `measured` basis with no usable number behind it is a
            # contradiction. Trust the number, not the label.
            basis = UNPRICED
        elif result.get("turn_cost_usd"):
            total = format_cost(result.get("total_cost_usd"))
            # The one place `total_cost_usd` is read, and it is labelled as
            # the session's running total where it is printed.
            return f"{figure} this turn" + (f" · {total} session total" if total else "")
        else:
            return "nothing extra — the session's estimate did not move"
    if basis == RESET:
        return "cost unknown — the engine's running total restarted mid-turn"
    if basis == UNPRICED:
        return "cost unknown — the turn ended without a usable figure"
    return None


def _turn_line(result: dict[str, Any]) -> str:
    """Duration, turns, how it ended, and what it had to ask.

    ``cancelled`` is reported ahead of ``terminal_reason`` because a cancelled
    turn carries one of several reasons and the useful word is the same for
    all of them.
    """
    parts: list[str] = []
    duration_ms = _tokens(result.get("duration_ms"))
    if duration_ms:
        parts.append(f"{duration_ms / 1000:.1f}s")
    num_turns = _tokens(result.get("num_turns"))
    if num_turns:
        parts.append(f"{num_turns} turn{'s' if num_turns != 1 else ''}")

    if result.get("cancelled"):
        parts.append("cancelled")
    else:
        reason = result.get("terminal_reason")
        if isinstance(reason, str) and reason:
            parts.append(reason.replace("_", " "))
        elif result.get("is_error"):
            parts.append("error")
        else:
            parts.append("completed")

    prompts = _tokens(result.get("permission_prompts"))
    if prompts:
        parts.append(f"{prompts} permission prompt{'s' if prompts != 1 else ''}")
    if result.get("mirror_gap"):
        # The turn is on screen but not in the transcript, so a reader coming
        # back to this block later will not find the turn it describes.
        parts.append("not mirrored")
    if result.get("continuation"):
        # A turn with a background subagent reaches this twice, and the second
        # block is a revision of the first rather than a second turn. A
        # terminal cannot replace a line it already printed, so it says so.
        parts.append("revised after background work")
    return " · ".join(parts)


def _context_line(usage: Any) -> str | None:
    """Where the context window stands, and where auto-compact will take it.

    The marker is the point of the figure: "59% full" does not answer "am I
    about to be compacted?", and the threshold beside it does. A disabled
    auto-compact is stated rather than omitted, because its absence changes
    what the percentage means.
    """
    if not isinstance(usage, dict):
        return None
    total = _tokens(usage.get("totalTokens"))
    maximum = _tokens(usage.get("maxTokens"))
    if maximum <= 0:
        return None
    line = f"{total:,} / {maximum:,} ({round(total / maximum * 100)}%)"
    if usage.get("isAutoCompactEnabled") is False:
        return f"{line} · no auto-compact"
    threshold = _tokens(usage.get("autoCompactThreshold"))
    if threshold:
        line = f"{line} · auto-compact at {threshold:,}"
    return line


def _files_line(result: dict[str, Any], repo_root: Path | str | None) -> str | None:
    """The files this turn changed, named the way a human reads them.

    Shortened against the repo root; a path outside it is printed absolute,
    because *that it is outside* is the most important thing about it. Both
    halves are ``build_diff_payload``'s rule — see the module docstring on why
    a server-side naming is the right answer here and not a fourth mechanism.
    """
    files = result.get("files_modified")
    if not isinstance(files, list) or not files:
        return None
    root = Path(repo_root) if repo_root else None
    names: list[str] = []
    for entry in files:
        if not isinstance(entry, str) or not entry:
            continue
        name = entry
        if root is not None:
            try:
                name = Path(entry).relative_to(root).as_posix()
            except ValueError:
                # Outside the repo, or not absolute at all. Either way the
                # string we were given is the honest thing to print.
                name = entry
        if name not in names:
            names.append(name)
    if not names:
        return None
    if len(names) > _FILES_CEILING:
        shown = ", ".join(names[:_FILES_CEILING])
        return f"{shown}, and {len(names) - _FILES_CEILING} more"
    return ", ".join(names)


def format_turn_hud(
    result: Any,
    context_usage: Any = None,
    repo_root: Path | str | None = None,
) -> str | None:
    """One turn's summary block, or ``None`` when there is nothing to say.

    ``result`` is a ``streamComplete`` payload as
    :meth:`~aic_dc.claude_code.session.EngineSession._price_turn` leaves it —
    per-turn cost fields already folded in. ``context_usage`` is the
    ``get_context_usage()`` response the same post-turn pass fetched, which may
    be ``None`` when the engine could not answer; the block drops the ``Ctx:``
    line rather than failing.

    Returns ``None`` only for a payload that is not a turn at all. An *empty*
    turn still prints: unlike the browser HUD, which pops over the transcript
    and so must earn its interruption, this is a log — a line saying a turn
    happened and cost nothing is a record, not an interruption, and its
    absence would read as the server having missed the turn.
    """
    if not isinstance(result, dict):
        return None

    rows = _usage_rows(result)
    lines: list[tuple[str, str]] = []

    if rows:
        lines.append(("Model", rows[0]["model"]))
    lines.append(("Turn", _turn_line(result)))

    if rows:
        width = min(max(len(row["model"]) for row in rows), _MODEL_COLUMN_CEILING)
        # Right-align every counter across the block so two models' figures can
        # be compared down the column, which is the only reason to print them
        # one above the other rather than in one line.
        widths = {
            key: max(len(f"{row[key]:,}") for row in rows)
            for key in ("input", "output", "cache_read", "cache_write")
        }
        for index, row in enumerate(rows):
            name = row["model"]
            if len(name) > width:
                name = "…" + name[-(width - 1):]
            lines.append((
                "Usage" if index == 0 else "",
                f"{name:<{width}}  "
                f"{row['input']:>{widths['input']},} in / "
                f"{row['output']:>{widths['output']},} out · cache "
                f"{row['cache_read']:>{widths['cache_read']},} r / "
                f"{row['cache_write']:>{widths['cache_write']},} w · "
                f"{_cache_hit(row):>4} hit",
            ))

    cost = _cost_line(result)
    if cost:
        lines.append(("Cost", cost))
    context = _context_line(context_usage)
    if context:
        lines.append(("Ctx", context))
    files = _files_line(result, repo_root)
    if files:
        lines.append(("Files", files))

    label_width = max(len(label) for label, _ in lines if label) + 1
    return "\n".join(
        f"{(label + ':' if label else ''):<{label_width}} {body}"
        for label, body in lines
    )


def log_turn_hud(
    result: Any,
    context_usage: Any = None,
    repo_root: Path | str | None = None,
) -> None:
    """Print the block, and never let doing so cost a turn.

    One ``logger.info`` record rather than one per line, so the block cannot
    be interleaved with anything the engine logs concurrently — a HUD split
    down the middle by a stderr line is worse than no HUD. Every failure is
    swallowed at ``debug``: this is a summary of work that already finished
    successfully, and there is no formatting bug worth turning a completed
    turn into an error for.
    """
    try:
        block = format_turn_hud(result, context_usage, repo_root)
    except Exception as exc:
        logger.debug("Could not format the turn summary: %s", exc)
        return
    if block:
        logger.info("Turn summary\n%s", block)
