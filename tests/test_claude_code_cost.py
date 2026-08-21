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
        ledger.price(result(0.21))
        ledger.reset()
        assert ledger.price(result(0.05))["turn_cost_usd"] == 0.05


class TestTheDifference:
    def test_each_turn_is_priced_against_the_previous_total(self):
        ledger = CostLedger()
        assert ledger.price(result(0.10))["turn_cost_usd"] == 0.10
        assert ledger.price(result(0.30))["turn_cost_usd"] == 0.20
        assert ledger.price(result(0.35))["turn_cost_usd"] == 0.05

    def test_a_total_that_did_not_move_is_a_turn_that_cost_nothing_extra(self):
        """The distinction phase 6 exists to draw: zero here is an answer,
        not a missing one, so it must not arrive as ``None``."""
        ledger = CostLedger()
        ledger.price(result(0.10))
        priced = ledger.price(result(0.10))
        assert priced["turn_cost_usd"] == 0.0
        assert priced["turn_cost_basis"] == MEASURED

    def test_float_noise_does_not_leak_into_the_figure(self):
        ledger = CostLedger()
        ledger.price(result(0.1))
        # 0.3 - 0.1 is 0.19999999999999998 in binary floating point, and a
        # HUD rendering four decimals would print $0.2000 anyway — but the
        # payload should not carry the noise to every other reader.
        assert ledger.price(result(0.3))["turn_cost_usd"] == 0.2


class TestWhenTheAnswerIsUnavailable:
    def test_a_total_below_the_baseline_is_a_reset(self):
        """`/clear` restarts the CLI's ledger mid-session. The turn's share
        of a total that just went backwards is not recoverable."""
        ledger = CostLedger()
        ledger.price(result(0.40))
        priced = ledger.price(result(0.05))
        assert priced["turn_cost_usd"] is None
        assert priced["turn_cost_basis"] == RESET

    def test_the_turn_after_a_reset_is_priced_from_the_new_total(self):
        ledger = CostLedger()
        ledger.price(result(0.40))
        ledger.price(result(0.05))
        assert ledger.price(result(0.09))["turn_cost_usd"] == 0.04

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
        ledger.price(result(0.40))
        priced = ledger.price(result(0.0, is_error=True))
        assert priced["turn_cost_basis"] == UNPRICED

    def test_a_real_late_failure_still_gets_priced(self):
        """`error_max_turns` and the budget-exhausted footers carry the true
        running total, so a late failure is measurable and only the
        fabricated ones are not."""
        ledger = CostLedger()
        ledger.price(result(0.40))
        priced = ledger.price(result(0.55, is_error=True))
        assert priced["turn_cost_usd"] == cents(0.15)
        assert priced["turn_cost_basis"] == MEASURED

    def test_unattributable_spend_lands_on_the_next_priced_turn(self):
        """An unpriced footer must not move the baseline: adopting its zero
        would make the next turn's difference negative and lose the money
        out of both turns."""
        ledger = CostLedger()
        ledger.price(result(0.40))
        ledger.price(result(0.0, is_error=True))
        assert ledger.price(result(0.60))["turn_cost_usd"] == cents(0.20)

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
        ledger.price(result(0.10, {"m": entry(0.10, input=100, output=20)}))
        priced = ledger.price(result(0.30, {"m": entry(0.30, input=180, output=45)}))
        assert priced["turn_model_usage"]["m"]["inputTokens"] == 80
        assert priced["turn_model_usage"]["m"]["outputTokens"] == 25
        assert priced["turn_model_usage"]["m"]["costUSD"] == cents(0.20)

    def test_a_model_that_did_not_answer_gets_no_row(self):
        """A row of zeroes reads as "this model answered and cost nothing",
        which is a different and false claim from "this model was not used"."""
        ledger = CostLedger()
        ledger.price(
            result(0.10, {"opus": entry(0.10, input=100), "haiku": entry(0.01, input=9)})
        )
        priced = ledger.price(
            result(0.20, {"opus": entry(0.20, input=200), "haiku": entry(0.01, input=9)})
        )
        assert set(priced["turn_model_usage"]) == {"opus"}

    def test_a_subagent_appearing_mid_session_is_a_full_row(self):
        ledger = CostLedger()
        ledger.price(result(0.10, {"opus": entry(0.10, input=100)}))
        priced = ledger.price(
            result(0.12, {"opus": entry(0.10, input=100), "haiku": entry(0.02, input=40)})
        )
        assert set(priced["turn_model_usage"]) == {"haiku"}
        assert priced["turn_model_usage"]["haiku"]["inputTokens"] == 40

    def test_the_model_properties_are_passed_through_not_differenced(self):
        """`contextWindow` and `maxOutputTokens` describe the model, not the
        spend. Differencing them would report a 0-token context window for
        every turn after the first."""
        ledger = CostLedger()
        ledger.price(result(0.10, {"m": entry(0.10, input=100, window=200_000)}))
        priced = ledger.price(result(0.20, {"m": entry(0.20, input=200, window=200_000)}))
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
        ledger.price(result(0.40, {"m": entry(0.40, input=400)}))
        priced = ledger.price(result(0.05, {"m": entry(0.05, input=50)}))
        assert priced["turn_model_usage"] is None

    def test_the_turn_after_a_reset_differences_the_new_baseline(self):
        ledger = CostLedger()
        ledger.price(result(0.40, {"m": entry(0.40, input=400)}))
        ledger.price(result(0.05, {"m": entry(0.05, input=50)}))
        priced = ledger.price(result(0.09, {"m": entry(0.09, input=90)}))
        assert priced["turn_model_usage"]["m"]["inputTokens"] == 40

    def test_a_counter_that_went_backwards_is_clamped(self):
        """Per-model noise on an otherwise-forward total is not a session
        reset, and a negative token count is not a thing to render."""
        ledger = CostLedger()
        ledger.price(result(0.10, {"m": entry(0.10, input=100, cache_read=50)}))
        priced = ledger.price(result(0.20, {"m": entry(0.20, input=200, cache_read=10)}))
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


class TestTheIdentityBetweenTheTwoFigures:
    def test_the_per_model_costs_sum_to_the_turn_cost(self):
        """The schema says `total_cost_usd` covers "the same query-pipeline
        calls as modelUsage", so the two paths to this turn's cost should
        agree. A drift here means one of them is being read wrong."""
        ledger = CostLedger()
        ledger.price(result(0.10, {"opus": entry(0.10, input=100)}))
        priced = ledger.price(
            result(
                0.34,
                {"opus": entry(0.30, input=300), "haiku": entry(0.04, input=90)},
            )
        )
        per_model = sum(row["costUSD"] for row in priced["turn_model_usage"].values())
        assert per_model == cents(priced["turn_cost_usd"])

