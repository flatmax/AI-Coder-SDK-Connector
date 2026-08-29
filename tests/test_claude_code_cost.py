"""Tests for aic_dc.claude_code.cost — conversion phase 6.

The module exists because ``total_cost_usd`` and ``model_usage`` are session
running totals in a streaming-input session, which is the only kind AIC⚡DC
runs. Every test here is about the difference between "what the session has
spent" and "what this turn cost", and about the three cases where the second
one cannot be recovered from the first.

Nothing here touches the SDK: the ledger takes the result *payload* the
translator built, so a payload shape is all a test needs.
"""

from __future__ import annotations

import pytest

from aic_dc.claude_code.cost import MEASURED, RESET, UNPRICED, CostLedger


def cents(value):
    """Cost comparisons, tolerant of the last bit of a float subtraction."""
    return pytest.approx(value, abs=1e-9)


def result(total=None, models=None, *, is_error=False):
    """A result payload with only the fields the ledger reads."""
    return {
        "total_cost_usd": total,
        "model_usage": models,
        "is_error": is_error,
    }


def turn(ledger, *args, **kwargs):
    """Price one whole turn: anchor the baseline, then its single result.

    The baseline moves once per *turn*, not once per result, so a test that
    means "and then the next turn" has to say which it means. Most turns end
    exactly once and this helper is the whole story;
    :class:`TestATurnThatEndsMoreThanOnce` covers the case the distinction
    exists for.
    """
    ledger.start_turn()
    return ledger.price(result(*args, **kwargs))


def entry(cost=0.0, **counters):
    """One ``ModelUsage`` entry, in the engine's camelCase."""
    return {
        "inputTokens": counters.get("input", 0),
        "outputTokens": counters.get("output", 0),
        "cacheReadInputTokens": counters.get("cache_read", 0),
        "cacheCreationInputTokens": counters.get("cache_write", 0),
        "webSearchRequests": counters.get("searches", 0),
        "costUSD": cost,
        "contextWindow": counters.get("window", 200_000),
        "maxOutputTokens": counters.get("max_output", 32_000),
    }


class TestTheFirstTurn:
    def test_the_whole_running_total_is_the_first_turn(self):
        """The CLI's ledger starts at zero on connect and on resume, so
        nothing has spent against the total before the first result."""
        priced = CostLedger().price(result(0.21))
        assert priced["turn_cost_usd"] == 0.21
        assert priced["turn_cost_basis"] == MEASURED

    def test_a_reset_ledger_starts_over(self):
        ledger = CostLedger()
        turn(ledger, 0.21)
        ledger.reset()
        assert turn(ledger, 0.05)["turn_cost_usd"] == 0.05


class TestTheDifference:
    def test_each_turn_is_priced_against_the_previous_total(self):
        ledger = CostLedger()
        assert turn(ledger, 0.10)["turn_cost_usd"] == 0.10
        assert turn(ledger, 0.30)["turn_cost_usd"] == 0.20
        assert turn(ledger, 0.35)["turn_cost_usd"] == 0.05

    def test_a_total_that_did_not_move_is_a_turn_that_cost_nothing_extra(self):
        """The distinction phase 6 exists to draw: zero here is an answer,
        not a missing one, so it must not arrive as ``None``."""
        ledger = CostLedger()
        turn(ledger, 0.10)
        priced = turn(ledger, 0.10)
        assert priced["turn_cost_usd"] == 0.0
        assert priced["turn_cost_basis"] == MEASURED

    def test_float_noise_does_not_leak_into_the_figure(self):
        ledger = CostLedger()
        turn(ledger, 0.1)
        # 0.3 - 0.1 is 0.19999999999999998 in binary floating point, and a
        # HUD rendering four decimals would print $0.2000 anyway — but the
        # payload should not carry the noise to every other reader.
        assert turn(ledger, 0.3)["turn_cost_usd"] == 0.2


class TestWhenTheAnswerIsUnavailable:
    def test_a_total_below_the_baseline_is_a_reset(self):
        """`/clear` restarts the CLI's ledger mid-session. The turn's share
        of a total that just went backwards is not recoverable."""
        ledger = CostLedger()
        turn(ledger, 0.40)
        priced = turn(ledger, 0.05)
        assert priced["turn_cost_usd"] is None
        assert priced["turn_cost_basis"] == RESET

    def test_the_turn_after_a_reset_is_priced_from_the_new_total(self):
        ledger = CostLedger()
        turn(ledger, 0.40)
        turn(ledger, 0.05)
        assert turn(ledger, 0.09)["turn_cost_usd"] == 0.04

    def test_no_cost_at_all_is_unpriced(self):
        """AIC⚡DC's own synthetic failure footers, and a replayed turn: the
        engine never sent a number, so there is nothing to difference."""
        priced = CostLedger().price(result(None))
        assert priced["turn_cost_usd"] is None
        assert priced["turn_cost_basis"] == UNPRICED

    def test_a_zeroed_error_footer_is_unpriced_rather_than_free(self):
        """The CLI's schema warns that crash and startup-error results may
        carry zeroed values, and the CLI fabricates exactly that footer for
        an `error_during_execution`. Reading its zero as "nothing extra"
        would report a turn that failed late carrying real usage as free —
        the specific misreading phase 6 is here to prevent."""
        ledger = CostLedger()
        turn(ledger, 0.40)
        priced = turn(ledger, 0.0, is_error=True)
        assert priced["turn_cost_basis"] == UNPRICED

    def test_a_real_late_failure_still_gets_priced(self):
        """`error_max_turns` and the budget-exhausted footers carry the true
        running total, so a late failure is measurable and only the
        fabricated ones are not."""
        ledger = CostLedger()
        turn(ledger, 0.40)
        priced = turn(ledger, 0.55, is_error=True)
        assert priced["turn_cost_usd"] == cents(0.15)
        assert priced["turn_cost_basis"] == MEASURED

    def test_unattributable_spend_lands_on_the_next_priced_turn(self):
        """An unpriced footer must not move the baseline: adopting its zero
        would make the next turn's difference negative and lose the money
        out of both turns."""
        ledger = CostLedger()
        turn(ledger, 0.40)
        turn(ledger, 0.0, is_error=True)
        assert turn(ledger, 0.60)["turn_cost_usd"] == cents(0.20)

    def test_a_non_numeric_cost_is_unpriced(self):
        for junk in ("0.40", {}, [], object()):
            assert CostLedger().price(result(junk))["turn_cost_basis"] == UNPRICED

    def test_a_boolean_cost_is_not_a_dollar(self):
        """`True` is an `int` in Python, so a payload carrying it would be
        priced at one dollar by a naive numeric check."""
        assert CostLedger().price(result(True))["turn_cost_basis"] == UNPRICED

    def test_nan_and_infinity_are_unpriced(self):
        for junk in (float("nan"), float("inf"), float("-inf")):
            assert CostLedger().price(result(junk))["turn_cost_basis"] == UNPRICED


class TestPerModelDifferences:
    def test_the_first_turn_reports_its_models_whole(self):
        priced = CostLedger().price(
            result(0.21, {"claude-opus-5": entry(0.21, input=100, output=20)})
        )
        assert priced["turn_model_usage"]["claude-opus-5"]["inputTokens"] == 100
        assert priced["turn_model_usage"]["claude-opus-5"]["costUSD"] == 0.21

    def test_later_turns_report_only_what_this_turn_added(self):
        ledger = CostLedger()
        turn(ledger, 0.10, {"m": entry(0.10, input=100, output=20)})
        priced = turn(ledger, 0.30, {"m": entry(0.30, input=180, output=45)})
        assert priced["turn_model_usage"]["m"]["inputTokens"] == 80
        assert priced["turn_model_usage"]["m"]["outputTokens"] == 25
        assert priced["turn_model_usage"]["m"]["costUSD"] == cents(0.20)

    def test_a_model_that_did_not_answer_gets_no_row(self):
        """A row of zeroes reads as "this model answered and cost nothing",
        which is a different and false claim from "this model was not used"."""
        ledger = CostLedger()
        turn(
            ledger,
            0.10,
            {"opus": entry(0.10, input=100), "haiku": entry(0.01, input=9)},
        )
        priced = turn(
            ledger,
            0.20,
            {"opus": entry(0.20, input=200), "haiku": entry(0.01, input=9)},
        )
        assert set(priced["turn_model_usage"]) == {"opus"}

    def test_a_subagent_appearing_mid_session_is_a_full_row(self):
        ledger = CostLedger()
        turn(ledger, 0.10, {"opus": entry(0.10, input=100)})
        priced = turn(
            ledger,
            0.12,
            {"opus": entry(0.10, input=100), "haiku": entry(0.02, input=40)},
        )
        assert set(priced["turn_model_usage"]) == {"haiku"}
        assert priced["turn_model_usage"]["haiku"]["inputTokens"] == 40

    def test_the_model_properties_are_passed_through_not_differenced(self):
        """`contextWindow` and `maxOutputTokens` describe the model, not the
        spend. Differencing them would report a 0-token context window for
        every turn after the first."""
        ledger = CostLedger()
        turn(ledger, 0.10, {"m": entry(0.10, input=100, window=200_000)})
        priced = turn(ledger, 0.20, {"m": entry(0.20, input=200, window=200_000)})
        assert priced["turn_model_usage"]["m"]["contextWindow"] == 200_000
        assert priced["turn_model_usage"]["m"]["maxOutputTokens"] == 32_000

    def test_the_provider_fields_survive(self):
        priced = CostLedger().price(
            result(
                0.10,
                {
                    "m": {
                        **entry(0.10, input=100),
                        "canonicalModel": "claude-opus-5",
                        "provider": "firstParty",
                    }
                },
            )
        )
        row = priced["turn_model_usage"]["m"]
        assert row["canonicalModel"] == "claude-opus-5"
        assert row["provider"] == "firstParty"

    def test_a_reset_reports_no_per_model_rows(self):
        ledger = CostLedger()
        turn(ledger, 0.40, {"m": entry(0.40, input=400)})
        priced = turn(ledger, 0.05, {"m": entry(0.05, input=50)})
        assert priced["turn_model_usage"] is None

    def test_the_turn_after_a_reset_differences_the_new_baseline(self):
        ledger = CostLedger()
        turn(ledger, 0.40, {"m": entry(0.40, input=400)})
        turn(ledger, 0.05, {"m": entry(0.05, input=50)})
        priced = turn(ledger, 0.09, {"m": entry(0.09, input=90)})
        assert priced["turn_model_usage"]["m"]["inputTokens"] == 40

    def test_a_counter_that_went_backwards_is_clamped(self):
        """Per-model noise on an otherwise-forward total is not a session
        reset, and a negative token count is not a thing to render."""
        ledger = CostLedger()
        turn(ledger, 0.10, {"m": entry(0.10, input=100, cache_read=50)})
        priced = turn(ledger, 0.20, {"m": entry(0.20, input=200, cache_read=10)})
        assert priced["turn_model_usage"]["m"]["cacheReadInputTokens"] == 0
        assert priced["turn_model_usage"]["m"]["inputTokens"] == 100

    def test_no_model_usage_at_all_leaves_the_field_null(self):
        priced = CostLedger().price(result(0.10, None))
        assert priced["turn_model_usage"] is None

    def test_junk_entries_are_ignored_rather_than_raising(self):
        priced = CostLedger().price(
            result(0.10, {"m": "nonsense", 7: entry(0.10), "ok": entry(0.10, input=5)})
        )
        assert set(priced["turn_model_usage"]) == {"ok"}


class TestATurnThatEndsMoreThanOnce:
    """A result message ends a turn, not the run.

    With a background task in flight the engine goes on sending results for the
    same request, and each one has to report the whole turn: the footer the
    browser ends up rendering is built from the last result it receives, and a
    background subagent spends most of its tokens *after* the first one. Pricing
    each result against the previous would put that spend in a footer nobody
    renders.
    """

    def test_a_second_result_reports_the_whole_turn_not_the_step(self):
        ledger = CostLedger()
        turn(ledger, 0.10)  # an earlier, ordinary turn
        ledger.start_turn()
        assert ledger.price(result(0.30))["turn_cost_usd"] == cents(0.20)
        assert ledger.price(result(0.34))["turn_cost_usd"] == cents(0.24)

    def test_the_next_turn_starts_from_the_last_result_seen(self):
        """Not from this turn's anchor: the background work is already in the
        figure above, and charging it again would double it."""
        ledger = CostLedger()
        ledger.start_turn()
        ledger.price(result(0.30))
        ledger.price(result(0.34))
        assert turn(ledger, 0.40)["turn_cost_usd"] == cents(0.06)

    def test_per_model_rows_also_cover_the_whole_turn(self):
        """The subagent's model appears only in the continuation, and its row
        is what makes a delegated turn's cost legible."""
        ledger = CostLedger()
        turn(ledger, 0.10, {"opus": entry(0.10, input=100)})
        ledger.start_turn()
        ledger.price(result(0.20, {"opus": entry(0.20, input=200)}))
        priced = ledger.price(
            result(
                0.30,
                {"opus": entry(0.20, input=200), "haiku": entry(0.10, input=500)},
            )
        )
        assert priced["turn_model_usage"]["opus"]["inputTokens"] == 100
        assert priced["turn_model_usage"]["haiku"]["inputTokens"] == 500

    def test_an_unpriced_continuation_leaves_the_anchor_alone(self):
        ledger = CostLedger()
        turn(ledger, 0.10)
        ledger.start_turn()
        ledger.price(result(0.30))
        ledger.price(result(0.0, is_error=True))
        assert ledger.price(result(0.36))["turn_cost_usd"] == cents(0.26)

    def test_a_reset_mid_turn_re_anchors_rather_than_reporting_reset_twice(self):
        """`/clear` between two of one turn's results costs us that result's
        share and nothing more — the rest of the turn is measurable again."""
        ledger = CostLedger()
        turn(ledger, 0.40)
        ledger.start_turn()
        assert ledger.price(result(0.05))["turn_cost_basis"] == RESET
        assert ledger.price(result(0.09))["turn_cost_usd"] == cents(0.04)

    def test_start_turn_between_results_is_what_separates_two_turns(self):
        """The guard on the whole design: without the anchor moving, a second
        turn would report the first one's spend all over again."""
        ledger = CostLedger()
        ledger.start_turn()
        ledger.price(result(0.10))
        assert ledger.price(result(0.30))["turn_cost_usd"] == cents(0.30)
        assert turn(ledger, 0.30)["turn_cost_usd"] == 0.0


class TestTheIdentityBetweenTheTwoFigures:
    def test_the_per_model_costs_sum_to_the_turn_cost(self):
        """The schema says `total_cost_usd` covers "the same query-pipeline
        calls as modelUsage", so the two paths to this turn's cost should
        agree. A drift here means one of them is being read wrong."""
        ledger = CostLedger()
        turn(ledger, 0.10, {"opus": entry(0.10, input=100)})
        priced = turn(
            ledger,
            0.34,
            {"opus": entry(0.30, input=300), "haiku": entry(0.04, input=90)},
        )
        per_model = sum(row["costUSD"] for row in priced["turn_model_usage"].values())
        assert per_model == cents(priced["turn_cost_usd"])



class TestSessionTotals:
    """The one reading where the cumulative figures are the answer.

    Everywhere else in this codebase a cumulative figure rendered as a turn's
    is the bug this module exists to prevent. The Context tab's Usage section
    asks "what has this session cost", which the engine's running totals
    answer directly — so these are served under the engine's own field names
    rather than renamed, because those names mean "cumulative" everywhere and
    the one honest use of them should not be the one place they are disguised.
    """

    def test_a_session_with_no_priced_result_has_no_cost_rather_than_zero(self):
        ledger = CostLedger()
        totals = ledger.session_totals()
        # `None`, not 0.0: a session that has run nothing has no estimate, and
        # printing $0.0000 for it makes the same claim `turn_cost_basis`
        # refuses to make.
        assert totals["total_cost_usd"] is None
        assert totals["model_usage"] is None

    def test_the_total_is_the_running_one_not_the_last_turns(self):
        ledger = CostLedger()
        turn(ledger, 0.10)
        turn(ledger, 0.35)
        assert ledger.session_totals()["total_cost_usd"] == cents(0.35)

    def test_the_rows_keep_the_fields_the_deltas_drop(self):
        """`_snapshot` keeps only the counters, which is right for maths and
        wrong for display: it loses `canonicalModel`, and a row labelled from
        the map key reads `us.anthropic.claude-opus-5-v1:0`."""
        ledger = CostLedger()
        models = {"us.anthropic.claude-opus-5-v1:0": entry(0.10, input=100)}
        models["us.anthropic.claude-opus-5-v1:0"]["canonicalModel"] = "claude-opus-5"
        turn(ledger, 0.10, models)
        rows = ledger.session_totals()["model_usage"]
        assert rows["us.anthropic.claude-opus-5-v1:0"]["canonicalModel"] == "claude-opus-5"

    def test_the_rows_are_cumulative_where_the_turn_rows_are_a_difference(self):
        ledger = CostLedger()
        turn(ledger, 0.10, {"opus": entry(0.10, input=100)})
        priced = turn(ledger, 0.30, {"opus": entry(0.30, input=400)})
        # The turn moved 300 input tokens; the session has seen 400.
        assert priced["turn_model_usage"]["opus"]["inputTokens"] == 300
        assert ledger.session_totals()["model_usage"]["opus"]["inputTokens"] == 400

    def test_a_reset_moves_the_totals_down_with_the_engines_own_ledger(self):
        """A `/clear` restarts the engine's ledger and this figure follows it.

        The alternative — holding the pre-`/clear` figure — would report a
        number nothing tracks any more, and would disagree with the CLI's own
        `/usage` panel, which restarts too. What it costs is stated rather
        than hidden: after a `/clear` this understates what the account was
        actually billed, and the turn that straddled the reset is the one
        whose cost is lost (`turn_cost_basis: reset`).

        The rows move with it. Reporting the new total beside the old
        per-model rows would be one figure from each side of the reset.
        """
        ledger = CostLedger()
        turn(ledger, 0.50, {"opus": entry(0.50, input=100)})
        assert turn(ledger, 0.01, {"opus": entry(0.01, input=3)})["turn_cost_basis"] == RESET
        totals = ledger.session_totals()
        assert totals["total_cost_usd"] == cents(0.01)
        assert totals["model_usage"]["opus"]["inputTokens"] == 3

    def test_a_reconnect_forgets_them_because_the_engine_did(self):
        ledger = CostLedger()
        turn(ledger, 0.50, {"opus": entry(0.50, input=100)})
        ledger.reset()
        assert ledger.session_totals() == {"total_cost_usd": None, "model_usage": None}

    def test_an_unpriced_result_does_not_move_the_totals(self):
        ledger = CostLedger()
        turn(ledger, 0.20, {"opus": entry(0.20, input=100)})
        turn(ledger, None)
        assert ledger.session_totals()["total_cost_usd"] == cents(0.20)
