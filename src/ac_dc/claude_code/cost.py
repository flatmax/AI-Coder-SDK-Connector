"""What one turn cost, from an engine that reports the session's total.

``ResultMessage.total_cost_usd`` and ``ResultMessage.model_usage`` do not
describe the turn that just finished. The CLI's own wire schema says so, in
the ``describe()`` text it validates the result against::

    total_cost_usd: "Cumulative estimated cost in USD for this query() call,
    covering the same query-pipeline calls as modelUsage and sharing its
    lifecycle: cumulative across turns in streaming-input sessions — each
    result carries the running total so far, so read the latest result rather
    than summing across results. Crash/startup-error results may carry zeroed
    values, resumed sessions start fresh, and a mid-session /clear resets the
    running total. An estimate, not a billing statement."

    modelUsage: "Per-model totals for every model call made through the query
    pipeline during this query() call — main loop, Task subagents, sidechains,
    and internal calls such as compaction and Workflow agents. Cumulative
    across turns in streaming-input sessions […]"

AC⚡DC runs one ``ClaudeSDKClient`` in streaming-input mode, so both fields are
session running totals. Rendering either as "this turn" is wrong and gets more
wrong the longer the session runs — the tenth turn of a session reports what
the first nine also cost. Only ``usage`` is per-turn, and it is *main loop
only*: it excludes the subagents that are usually the expensive part.

So the turn's cost is a **difference**, and a difference needs a baseline.
This module holds it. The baseline is session state — the translator sees one
turn and is deliberately pure — which is why this is a small object owned by
:class:`~ac_dc.claude_code.session.EngineSession` rather than a function.

Three answers, and the difference between the last two is the point:

``measured``
    A running total was in hand and the reported one is at or above it. The
    difference is the turn's cost, and **zero is a real answer**: a turn that
    made no model call added nothing to the session's bill.
``reset``
    The reported total is *below* the running one. The CLI restarts its ledger
    on ``/clear``, and a fabricated crash footer carries a hard zero, so the
    turn's share cannot be recovered — the total is not a total any more.
``unpriced``
    No usable number: a synthetic result AC⚡DC wrote itself for a failure it
    saw from outside the engine, or an error footer the CLI zeroed. The
    schema's own warning — "crash/startup-error results may carry zeroed
    values" — is why a zero on an errored result is treated as no evidence
    rather than as free. This is the case
    ``specs5/plan/README.md`` names for phase 6: a turn that fails late
    carrying real usage must read as *cost unknown*, never as *nothing extra*.

Spend that cannot be attributed is not thrown away: the baseline stays where it
was, so the next turn we *can* price carries it. The alternative — adopting an
unusable total — would make the following turn's difference negative and lose
the money out of both turns.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CostLedger", "MEASURED", "RESET", "UNPRICED", "COUNTER_FIELDS"]

MEASURED = "measured"
RESET = "reset"
UNPRICED = "unpriced"

#: The ``ModelUsage`` fields that accumulate, and so must be differenced. The
#: rest of the entry — ``contextWindow``, ``maxOutputTokens``,
#: ``canonicalModel``, ``provider`` — describes the *model*, not the spend, and
#: is passed through as reported. Differencing ``contextWindow`` would report a
#: 0-token window for every turn after the first.
COUNTER_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "webSearchRequests",
    "costUSD",
)


class CostLedger:
    """Per-session baseline for the engine's cumulative usage figures.

    One instance per :class:`~ac_dc.claude_code.session.EngineSession`, reset
    on every connect: the CLI's ledger is per-process and "resumed sessions
    start fresh", so a reconnect starts both sides from nothing.
    """

    def __init__(self) -> None:
        self._total: float | None = None
        self._models: dict[str, dict[str, float]] = {}

    def reset(self) -> None:
        """Forget the baseline, because the engine just forgot its own."""
        self._total = None
        self._models = {}

    def price(self, result: dict[str, Any]) -> dict[str, Any]:
        """The three turn-scoped fields to fold into a ``streamComplete``.

        Returns ``turn_cost_usd`` (the difference, or ``None`` when there
        isn't one), ``turn_cost_basis`` (which of the three answers above),
        and ``turn_model_usage`` (the same difference per model, in the
        engine's own camelCase field names so the two are comparable).

        Called once per result, in arrival order. Calling it twice for one
        turn would price the second call at zero.
        """
        total = _as_cost(result.get("total_cost_usd"))
        errored = bool(result.get("is_error"))

        if total is None or (errored and total == 0.0):
            return _answer(None, UNPRICED, None)

        baseline = self._total
        self._total = total

        if baseline is None:
            # First priced result of the session. The engine's ledger starts
            # at zero on connect and on resume, so the whole running total is
            # this turn's — no earlier turn has spent against it.
            baseline = 0.0
        elif total < baseline:
            self._models = _snapshot(result.get("model_usage"))
            return _answer(None, RESET, None)

        models = _snapshot(result.get("model_usage"))
        turn_models = _model_deltas(models, self._models, result.get("model_usage"))
        self._models = models
        return _answer(round(total - baseline, 10), MEASURED, turn_models)


def _answer(
    cost: float | None, basis: str, models: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    return {
        "turn_cost_usd": cost,
        "turn_cost_basis": basis,
        "turn_model_usage": models or None,
    }


def _as_cost(value: Any) -> float | None:
    """A usable cost, or ``None``.

    ``bool`` is rejected explicitly: it is an ``int`` in Python, and a payload
    carrying ``total_cost_usd: True`` would otherwise price a turn at one
    dollar.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        return None
    return float(value)


def _snapshot(model_usage: Any) -> dict[str, dict[str, float]]:
    """The counter fields of each model entry, as plain floats."""
    if not isinstance(model_usage, dict):
        return {}
    snapshot: dict[str, dict[str, float]] = {}
    for name, entry in model_usage.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        counters: dict[str, float] = {}
        for field in COUNTER_FIELDS:
            value = _as_cost(entry.get(field))
            if value is not None:
                counters[field] = value
        snapshot[name] = counters
    return snapshot


def _model_deltas(
    now: dict[str, dict[str, float]],
    before: dict[str, dict[str, float]],
    raw: Any,
) -> dict[str, dict[str, Any]]:
    """Per-model differences, dropping the models that did not move.

    A model whose counters are unchanged did not answer this turn, and a row
    of zeroes for it would read as "answered, cost nothing". A counter that
    went *down* on an otherwise-forward total is per-model ledger noise rather
    than a session reset, so it is clamped to zero rather than reported
    negative.
    """
    entries = raw if isinstance(raw, dict) else {}
    deltas: dict[str, dict[str, Any]] = {}
    for name, counters in now.items():
        previous = before.get(name, {})
        moved = False
        delta: dict[str, Any] = {}
        for field, value in counters.items():
            change = value - previous.get(field, 0.0)
            if change < 0:
                change = 0.0
            if change > 0:
                moved = True
            delta[field] = round(change, 10) if field == "costUSD" else change
        entry = entries.get(name)
        if isinstance(entry, dict):
            for field, value in entry.items():
                if field not in COUNTER_FIELDS:
                    delta[field] = value
        if moved:
            deltas[name] = delta
    return deltas
