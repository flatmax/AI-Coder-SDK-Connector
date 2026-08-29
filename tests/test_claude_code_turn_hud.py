"""Tests for aic_dc.claude_code.turn_hud — the post-turn terminal summary.

The surface was specified in four places and printed by nothing for four
phases (``specs5/next.md`` § B6), which happened because **two spec files
assert it as an invariant and no test did**. These are that test.

The load-bearing group is *Cost*: the specs described this line as printing
"cost or billing mode", which is the pre-phase-6 reading of ``total_cost_usd``,
and a terminal line answering differently from the browser would be a second
definition of what a turn cost. So the assertions below are mostly about which
field is *not* read.

Nothing here touches the SDK or the service — the formatter takes a
``streamComplete`` payload, so a dict is all a test needs.
"""

from __future__ import annotations

from pathlib import Path

from aic_dc.claude_code.cost import MEASURED, RESET, UNPRICED
from aic_dc.claude_code.turn_hud import format_cost, format_turn_hud


def footer(**overrides):
    """A turn footer carrying only the fields the formatter reads."""
    payload = {
        "duration_ms": 8400,
        "num_turns": 3,
        "terminal_reason": None,
        "is_error": False,
        "cancelled": False,
        "permission_prompts": 0,
        "files_modified": [],
        "turn_cost_usd": 0.1842,
        "turn_cost_basis": MEASURED,
        "turn_model_usage": {
            "claude-opus-4-6": {
                "inputTokens": 12481,
                "outputTokens": 1208,
                "cacheReadInputTokens": 41502,
                "cacheCreationInputTokens": 0,
            }
        },
    }
    payload.update(overrides)
    return payload


def line(block, label):
    """The body of the ``label:`` line, or None."""
    for row in block.splitlines():
        if row.startswith(f"{label}:"):
            return row.split(":", 1)[1].strip()
    return None


class TestCost:
    """Which field the cost comes from, and the three ways it can be absent."""

    def test_a_measured_cost_prints_the_turns_figure_not_the_sessions(self):
        # The whole reason this module reads `turn_cost_usd`: a session that
        # has spent $9.99 and a turn that cost 18 cents must print 18 cents.
        block = format_turn_hud(footer(total_cost_usd=9.99))
        assert "$0.1842" in line(block, "Cost")
        assert "$9.99" in line(block, "Cost")
        # ...and the session total is *labelled* where it appears, so the two
        # figures on the line cannot be read as the same kind of number.
        assert "session total" in line(block, "Cost")

    def test_the_session_total_is_never_the_headline_figure(self):
        # No per-turn figure at all: the line must not fall back to the
        # cumulative one, which is what every pre-phase-6 surface did.
        block = format_turn_hud(
            footer(turn_cost_usd=None, turn_cost_basis=UNPRICED, total_cost_usd=9.99)
        )
        assert "$9.99" not in line(block, "Cost")
        assert "cost unknown" in line(block, "Cost")

    def test_a_measured_zero_says_nothing_extra_rather_than_printing_zero(self):
        block = format_turn_hud(footer(turn_cost_usd=0.0))
        assert "nothing extra" in line(block, "Cost")
        assert "$0.00" not in block

    def test_a_reset_and_an_unpriced_turn_both_say_unknown_with_a_reason(self):
        reset = format_turn_hud(footer(turn_cost_usd=None, turn_cost_basis=RESET))
        unpriced = format_turn_hud(footer(turn_cost_usd=None, turn_cost_basis=UNPRICED))
        assert "cost unknown" in line(reset, "Cost")
        assert "cost unknown" in line(unpriced, "Cost")
        # Same verdict, different reason — the distinction phase 6 exists for.
        assert line(reset, "Cost") != line(unpriced, "Cost")

    def test_a_turn_with_no_basis_gets_no_cost_line_at_all(self):
        # A browsed turn. "Never recorded" is not "we lost track of it", and
        # only the second is worth a line.
        block = format_turn_hud(footer(turn_cost_basis=None))
        assert line(block, "Cost") is None

    def test_a_measured_basis_with_no_number_is_downgraded_not_trusted(self):
        block = format_turn_hud(footer(turn_cost_usd=None, turn_cost_basis=MEASURED))
        assert "cost unknown" in line(block, "Cost")

    def test_no_surface_ever_says_subscription_or_included(self):
        # The pre-phase-6 wording. It claimed a billing mode the payload says
        # nothing about, and `credential_source` is the only real signal.
        for payload in (footer(), footer(turn_cost_usd=0.0), footer(turn_cost_basis=RESET)):
            block = format_turn_hud(payload).lower()
            assert "subscription" not in block
            assert "included" not in block

    def test_the_format_is_the_clis_own(self):
        # Four decimals up to fifty cents, two above. Two throughout would
        # print most per-turn costs as $0.00.
        assert format_cost(0.1842) == "$0.1842"
        assert format_cost(0.0001) == "$0.0001"
        assert format_cost(1.5) == "$1.50"
        assert format_cost(0.5) == "$0.5000"

    def test_a_negative_or_missing_cost_is_refused(self):
        assert format_cost(-1) is None
        assert format_cost(None) is None
        assert format_cost("0.5") is None
        assert format_cost(True) is None


class TestUsageRows:
    """Per-model rows, and the field they must not come from."""

    def test_rows_come_from_the_turns_usage_not_the_sessions(self):
        block = format_turn_hud(
            footer(
                model_usage={"claude-haiku-4-5": {"inputTokens": 999999}},
            )
        )
        assert "haiku" not in block
        assert "claude-opus-4-6" in block

    def test_rows_are_sorted_busiest_first_and_the_model_line_names_the_top(self):
        block = format_turn_hud(
            footer(
                turn_model_usage={
                    "small": {"inputTokens": 10, "outputTokens": 1},
                    "big": {"inputTokens": 5000, "outputTokens": 900},
                }
            )
        )
        assert line(block, "Model") == "big"
        rows = [row for row in block.splitlines() if "in /" in row]
        assert "big" in rows[0] and "small" in rows[1]

    def test_rows_are_never_summed_into_a_total(self):
        block = format_turn_hud(
            footer(
                turn_model_usage={
                    "a": {"inputTokens": 100, "outputTokens": 10},
                    "b": {"inputTokens": 200, "outputTokens": 20},
                }
            )
        )
        rows = [row for row in block.splitlines() if "in /" in row]
        assert len(rows) == 2
        # 300 would be the summed input. "The expensive model did a little and
        # the cheap model did a lot" is the shape a total erases.
        assert "300" not in block

    def test_a_row_prefers_the_canonical_name_over_the_provider_key(self):
        block = format_turn_hud(
            footer(
                turn_model_usage={
                    "us.anthropic.claude-opus-5-v1:0": {
                        "inputTokens": 10,
                        "canonicalModel": "claude-opus-5",
                    }
                }
            )
        )
        assert line(block, "Model") == "claude-opus-5"

    def test_an_empty_entry_is_dropped_rather_than_printed_as_zeroes(self):
        block = format_turn_hud(
            footer(turn_model_usage={"idle": {}, "real": {"inputTokens": 7}})
        )
        assert "idle" not in block
        assert "real" in block

    def test_the_cache_hit_rate_has_no_denominator_case(self):
        block = format_turn_hud(
            footer(turn_model_usage={"m": {"outputTokens": 5}})
        )
        # Nothing went up, so there is no rate. A 0% would be a claim.
        assert "—" in block

    def test_a_turn_with_no_usage_still_reports_that_it_happened(self):
        # Unlike the browser HUD, which must earn its interruption. This is a
        # log: a missing block reads as the server having missed the turn.
        block = format_turn_hud(footer(turn_model_usage=None))
        assert line(block, "Turn") is not None
        assert line(block, "Model") is None


class TestTurnLine:
    def test_a_cancelled_turn_says_so_ahead_of_its_terminal_reason(self):
        block = format_turn_hud(footer(cancelled=True, terminal_reason="abort_signal"))
        assert "cancelled" in line(block, "Turn")

    def test_permission_prompts_appear_only_when_there_were_some(self):
        assert "permission" not in line(format_turn_hud(footer()), "Turn")
        assert "2 permission prompts" in line(
            format_turn_hud(footer(permission_prompts=2)), "Turn"
        )

    def test_a_revised_block_says_it_is_a_revision(self):
        # A turn with a background subagent reaches the terminal twice, and a
        # terminal cannot replace what it already printed.
        block = format_turn_hud(footer(continuation=True))
        assert "revised after background work" in line(block, "Turn")

    def test_a_turn_missing_from_the_transcript_says_so(self):
        block = format_turn_hud(footer(mirror_gap=True))
        assert "not mirrored" in line(block, "Turn")


class TestContextLine:
    def test_the_auto_compact_threshold_is_marked_beside_the_figure(self):
        block = format_turn_hud(
            footer(),
            {"totalTokens": 118204, "maxTokens": 200000, "autoCompactThreshold": 160000},
        )
        assert line(block, "Ctx") == "118,204 / 200,000 (59%) · auto-compact at 160,000"

    def test_a_disabled_auto_compact_is_stated_rather_than_omitted(self):
        block = format_turn_hud(
            footer(),
            {
                "totalTokens": 100,
                "maxTokens": 200000,
                "autoCompactThreshold": 160000,
                "isAutoCompactEnabled": False,
            },
        )
        assert "no auto-compact" in line(block, "Ctx")

    def test_an_unanswered_context_request_drops_the_line_not_the_block(self):
        block = format_turn_hud(footer(), None)
        assert line(block, "Ctx") is None
        assert line(block, "Turn") is not None


class TestFilesLine:
    def test_paths_are_named_against_the_repo_root(self):
        block = format_turn_hud(
            footer(files_modified=["/home/u/repo/src/auth/session.py"]),
            None,
            Path("/home/u/repo"),
        )
        assert line(block, "Files") == "src/auth/session.py"

    def test_a_path_outside_the_repo_stays_absolute(self):
        # That it is outside is the most important thing about it — the rule
        # `build_diff_payload` already uses for the dialog headline.
        block = format_turn_hud(
            footer(files_modified=["/etc/hosts"]), None, Path("/home/u/repo")
        )
        assert line(block, "Files") == "/etc/hosts"

    def test_a_long_list_is_summarised_rather_than_scrolling_the_block_away(self):
        files = [f"/home/u/repo/f{n}.py" for n in range(20)]
        block = format_turn_hud(footer(files_modified=files), None, Path("/home/u/repo"))
        assert "and 12 more" in line(block, "Files")

    def test_no_files_means_no_line(self):
        assert line(format_turn_hud(footer()), "Files") is None


class TestBlockShape:
    def test_a_non_dict_payload_is_not_a_turn(self):
        assert format_turn_hud(None) is None
        assert format_turn_hud("streamComplete") is None

    def test_labels_are_aligned_so_the_bodies_form_one_column(self):
        # The block is read down its left edge in a terminal. Every body must
        # start at the same column, including the unlabelled continuation rows
        # of a multi-model Usage section.
        block = format_turn_hud(
            footer(
                files_modified=["/r/a.py"],
                turn_model_usage={
                    "a": {"inputTokens": 10},
                    "b": {"inputTokens": 20},
                },
            ),
            {"totalTokens": 10, "maxTokens": 100},
            Path("/r"),
        )
        columns = {len(row) - len(row.lstrip()) if not row.strip().startswith(
            tuple("MTUCF")
        ) else row.index(row.split(":", 1)[1].lstrip()[0]) for row in block.splitlines()}
        assert len(columns) == 1
