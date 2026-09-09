#!/usr/bin/env python3
"""Does the cost chip's exceptional wording ever reach a screen?

`specs5/next.md` § D4. `turn-cost.js` renders three things where the CLI shows
one, and two of them have never been seen on a turn an engine priced:

  - **"nothing extra"** — a turn the session's cost estimate did not move for.
  - **"cost unknown"** — a turn whose share of the total cannot be separated
    out, in two flavours: the engine restarted its ledger (`reset`), or there
    was no usable figure at all (`unpriced`).

Sixty unit tests pin all three against hand-built payloads. That is the whole
problem: a hand-built payload with `turn_cost_basis: 'reset'` on it proves the
renderer, and says nothing about whether any turn a person can type produces
one. Phase 6's own rule applies — **two turns minimum**, because a session's
first turn has `turn_cost_usd == total_cost_usd` and cannot tell a difference
from a running total.

The gesture that provokes "nothing extra" turns out to be ordinary: a slash
command the router forwards to the CLI (`/help` — not in `SLASH_ROUTES`, not in
`SLASH_DENIED`). The CLI answers it locally in about 6ms, makes no model call,
and returns a *successful* result carrying the session's running total
unchanged. `total == baseline` is `measured` with a difference of zero, which
is the case `cost.py` says out loud is a real answer.

The first run of this probe expected `reset` there, on the strength of a
`scripts/engine_smoke.py` run whose `/help` reported `total_cost_usd: 0`. That
reading was wrong, and the way it was wrong is worth keeping: in the smoke run
`/help` was the session's *only* turn, so the total was zero because nothing
had been spent — not because the command zeroes it. Measured in a session that
had already spent, the same command reports 0.050424 both before and after.

Six scenarios, in one session because the state is cumulative:

  A. A real prompt. The chip must show a **price**. This is the positive
     control: a run where B and C look right but A shows nothing has proved
     that the footer is missing, not that the wording is reachable.
  B. `/help`. Expect `measured` zero → **"nothing extra"**, and the session's
     own cumulative total unmoved (the Context tab reads that figure, and a
     zero-cost turn must not disturb it).
  C. `/help` again — so the wording is not a once-per-session artefact.
  D. A second real prompt: **two turns minimum**. Turn A's cost necessarily
     equalled the session total, so A alone cannot show that the footer
     reports a difference rather than a running total. D can: its chip must
     read `total_D - total_A`, not `total_D`.
  E. Kill the CLI mid-turn. `_fail_turn` writes a synthetic footer with no
     cost on it, which must read **"cost unknown"** and never "nothing extra"
     — phase 6 names this case by hand, because a turn that fails late has
     usually spent real money. With `reset` unreachable (F), this is the only
     route to "cost unknown" that a user can actually arrive at.
  F. `reset` — not a scenario, a statement. Nothing a user can type reaches
     it: the CLI restarts its ledger on `/clear`, `SLASH_ROUTES` intercepts
     `/clear` before it gets there, and a resume reconnects and calls
     `CostLedger.reset()` so the next turn is `measured` against no baseline.
     The renderer stays because the CLI's schema warns the total can restart;
     the probe says out loud that it is untested rather than leaving a silent
     gap in a green run.

Both halves of every rendering check are asserted separately: the **basis** the
engine sent (from the recorder) and the **chip** the browser drew (from the
DOM). A basis that arrives and renders nothing is a finding this probe should
be able to state, and a single combined check could not.

Usage:
    .venv/bin/python scripts/turn_cost_probe.py [--repo /tmp/aicdc-uitest]
                                                [--headless] [--only A,E]

Exit 0 when every check passed, 1 on a failure or an uninterpretable run.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _live_app_probe import (  # noqa: E402
    PANEL_JS,
    REPO,
    Backend,
    Browser,
    Page,
    Report,
    dump,
    note,
    records,
    send_turn,
    wait_for_app,
    wait_for_turn,
)

SHOTS = REPO / ".aic-dc" / "live-probe"

# Plain prose, no tools. `permissions.py` gates `write`, `exec`, `interact`,
# `plan` and `mcp` by default, and a probe that tripped a gate would sit at a
# dialog nothing here answers.
PROMPT_A = "Reply with exactly the word: alpha. Nothing else, and use no tools."
PROMPT_D = "Reply with exactly the word: delta. Nothing else, and use no tools."
# Long enough to still be streaming when the kill lands.
PROMPT_E = (
    "Count from 1 to 400, one number per line, nothing else. Use no tools."
)

# What the chip must say, per `turn-cost.js` § costLabel.
NOTHING_EXTRA = "nothing extra"
COST_UNKNOWN = "cost unknown"

# Two bases render the same two words, and their tooltips are the only thing
# that tells them apart: one blames a restarted ledger, the other a turn that
# ended without a figure. So the tooltip is asserted, not just the text — a
# check on "cost unknown" alone would pass with the wrong explanation on
# screen. `reset`'s is here unused, and F says why.
UNPRICED_TITLE = "without a usable cost figure"

CHIP_JS = """
(() => {
  const p = %s;
  if (!p) return null;
  const chips = Array.from(p.shadowRoot.querySelectorAll('.turn-cost'));
  const stats = Array.from(p.shadowRoot.querySelectorAll('.turn-footer'));
  const last = chips[chips.length - 1];
  return {
    count: chips.length,
    footers: stats.length,
    text: last ? last.textContent.trim() : null,
    title: last ? last.getAttribute('title') : null,
    unknown: last ? last.classList.contains('turn-cost-unknown') : null,
    lastFooter: stats.length
      ? Array.from(stats[stats.length - 1].querySelectorAll('.turn-stat'))
          .map((s) => s.textContent.trim().replace(/\\s+/g, ' '))
      : [],
  };
})()
""" % PANEL_JS

SESSION_USAGE_JS = """
(async () => {
  const p = %s;
  if (!p) return null;
  const state = await p.rpcExtract('ClaudeCodeService.get_current_state');
  const usage = (state && state.session_usage) || null;
  if (!usage) return null;
  return {
    total_cost_usd: usage.total_cost_usd,
    models: Object.keys(usage.model_usage || {}),
  };
})()
""" % PANEL_JS


def chip(page: Page) -> dict:
    got = page.eval(CHIP_JS)
    if got is None:
        raise RuntimeError("the chat panel went away")
    return got


def session_usage(page: Page) -> dict | None:
    return page.eval(SESSION_USAGE_JS, await_promise=True)


def basis_of(rec: dict) -> tuple[str | None, float | None, float | None, bool]:
    res = rec.get("result", {}) or {}
    return (
        res.get("turn_cost_basis"),
        res.get("turn_cost_usd"),
        res.get("total_cost_usd"),
        bool(res.get("is_error")),
    )


def scenario(page: Page, label: str, name: str) -> None:
    print(f"\n[{label}] {name}")
    note(page, f"{label}: {name}")


def check_rendering(
    report: Report,
    page: Page,
    label: str,
    *,
    want_basis: str,
    want_text: str | None,
    want_title: str | None,
    want_unknown: bool,
    rec: dict,
    before_chips: int,
) -> dict:
    """Assert the basis the engine sent and the chip the browser drew."""
    basis, usd, total, errored = basis_of(rec)
    report.add(
        f"[{label}] basis",
        basis == want_basis,
        f"turn_cost_basis={basis!r} (wanted {want_basis!r}), "
        f"turn_cost_usd={usd!r}, total_cost_usd={total!r}, is_error={errored}",
    )
    got = chip(page)
    shot = page.shot(SHOTS / f"turn-cost-{label}.png")
    if got["count"] <= before_chips:
        report.add(
            f"[{label}] chip",
            False,
            f"no new cost chip rendered ({got['count']} chips, "
            f"{got['footers']} footers) — the basis reached the browser and "
            "the footer did not draw it",
            shot,
        )
        return got
    if want_text is not None:
        report.add(
            f"[{label}] chip text",
            got["text"] == want_text,
            f"{got['text']!r} (wanted {want_text!r}); footer: {got['lastFooter']}",
            shot,
        )
    report.add(
        f"[{label}] chip class",
        got["unknown"] is want_unknown,
        f"turn-cost-unknown={got['unknown']} (wanted {want_unknown})",
    )
    if want_title is not None:
        report.add(
            f"[{label}] chip tooltip",
            want_title in (got["title"] or ""),
            f"tooltip {'names' if want_title in (got['title'] or '') else 'does not name'} "
            f"{want_title!r}: {got['title']!r}",
        )
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/tmp/aicdc-uitest",
                    help="scratch git repo for the engine to work in")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="serve the webapp through Vite instead of the bundle")
    ap.add_argument("--only", default="ABCDE",
                    help="which scenarios to run, e.g. 'A,E'. A is forced on "
                         "whenever B, C or D is asked for: those three need a "
                         "baseline, and without one B's measured zero is a "
                         "measured *total*")
    ap.add_argument("--turn-timeout", type=float, default=300.0)
    args = ap.parse_args()

    wanted = {c for c in args.only.upper() if c.isalpha()}
    if wanted & {"B", "C", "D"}:
        wanted.add("A")

    SHOTS.mkdir(parents=True, exist_ok=True)
    report = Report()
    backend = Backend(Path(args.repo), dev=args.dev)
    browser = None
    total_a = usd_a = None
    usage_a = None
    try:
        browser = Browser(backend.url, headless=args.headless)
        page = browser.page
        wait_for_app(page)
        print("app ready")

        # ---- A: the positive control. A real turn must show a price.
        if "A" in wanted:
            scenario(page, "A", "a real prompt — the chip must show a price")
            before = chip(page)["count"]
            send_turn(page, PROMPT_A)
            rec_a = wait_for_turn(page, timeout=args.turn_timeout)
            basis_a, usd_a, total_a, _ = basis_of(rec_a)
            got_a = check_rendering(
                report, page, "A",
                want_basis="measured", want_text=None,
                want_title="added to the session's cost", want_unknown=False,
                rec=rec_a, before_chips=before,
            )
            priced = (
                basis_a == "measured"
                and isinstance(usd_a, (int, float))
                and usd_a > 0
                and (got_a["text"] or "").startswith("$")
            )
            report.add(
                "[A] control priced a turn",
                priced,
                f"basis={basis_a!r} usd={usd_a!r} chip={got_a['text']!r}",
            )
            if not priced:
                report.void(
                    "the control turn did not render a price, so nothing this "
                    "run says about the exceptional wording can be read as "
                    "evidence — an absent footer looks identical to an absent "
                    "basis here"
                )
                dump(page)
                page.report_console("control failed")
                return report.verdict()

            usage_a = session_usage(page)
            print(f"    session usage after A: {usage_a}")

        # ---- B and C: a forwarded slash command, which costs nothing.
        #
        # Twice, and the second one is not padding. Once could be a first-time
        # artefact of some state that only exists between turn 1 and turn 2;
        # `measured` zero arriving again from an unchanged total is the claim
        # that the wording belongs to the *gesture*.
        for label in ("B", "C"):
            if label not in wanted:
                continue
            scenario(page, label,
                     "/help — a forwarded command that makes no model call")
            before = chip(page)["count"]
            send_turn(page, "/help")
            rec = wait_for_turn(page, timeout=args.turn_timeout)
            check_rendering(
                report, page, label,
                want_basis="measured", want_text=NOTHING_EXTRA,
                want_title="did not move for this turn", want_unknown=False,
                rec=rec, before_chips=before,
            )
            usage_now = session_usage(page)
            print(f"    session usage after {label}: {usage_now}")
            if label == "B" and isinstance(usage_a, dict):
                # The other half of "nothing extra", and the half no rendering
                # check would notice: the *session's* cumulative total is a
                # separate reading (`CostLedger.session_totals`, the Context
                # tab's Usage section), and a turn that added nothing must
                # leave it exactly where it was — neither advanced nor, if the
                # engine had reported a lower total, re-anchored to it.
                held = (
                    isinstance(usage_now, dict)
                    and usage_now.get("total_cost_usd") == usage_a.get("total_cost_usd")
                    and usage_now.get("models") == usage_a.get("models")
                )
                report.add(
                    "[B] the session's own total is untouched",
                    held,
                    f"session cost {usage_a} → {usage_now}",
                )

        # ---- D: two turns minimum. A difference, not a running total.
        if "D" in wanted:
            scenario(page, "D",
                     "a second real prompt — a difference, not a running total")
            before = chip(page)["count"]
            send_turn(page, PROMPT_D)
            rec_d = wait_for_turn(page, timeout=args.turn_timeout)
            _, usd_d, total_d, _ = basis_of(rec_d)
            got_d = chip(page)
            shot_d = page.shot(SHOTS / "turn-cost-D.png")
            print(f"    A: total={total_a!r} turn={usd_a!r}")
            print(f"    D: total={total_d!r} turn={usd_d!r} chip={got_d['text']!r}")
            print(f"    D footer: {got_d['lastFooter']}")
            if not isinstance(total_d, (int, float)) or not isinstance(usd_d, (int, float)):
                report.void(
                    f"turn D reported total={total_d!r} turn={usd_d!r}, so the "
                    "arithmetic phase 6 exists for has nothing to work on"
                )
            elif not isinstance(total_a, (int, float)) or total_d <= total_a:
                report.skip(
                    "[D] the second turn is priced as itself",
                    f"the engine's total did not advance across the run "
                    f"({total_a!r} → {total_d!r}), so a difference and a total "
                    "are the same number here and this check cannot separate "
                    "them",
                )
            else:
                true_cost = round(total_d - total_a, 10)
                # Which of the two numbers is the chip closer to? Stated as a
                # comparison rather than an equality because the engine's own
                # rounding is not ours to predict — but the two candidates are
                # far apart, so "closer to" is not a soft assertion.
                as_total = abs(usd_d - total_d) < abs(usd_d - true_cost)
                report.add(
                    "[D] the second turn is priced as itself",
                    not as_total,
                    (
                        f"turn D was priced at {usd_d!r}, which is the session's "
                        f"whole running total ({total_d!r}) rather than the "
                        f"{true_cost!r} this turn added"
                        if as_total else
                        f"turn D priced at {usd_d!r} against a true delta of "
                        f"{true_cost!r} (session total {total_a!r} → {total_d!r}) "
                        f"— and turn A, being first, could only report "
                        f"{usd_a!r} == {total_a!r}"
                    ),
                    shot_d,
                )

        # ---- E: a turn that fails late must not read as free.
        if "E" not in wanted:
            report.skip("[E] a failed turn reads 'cost unknown'",
                        f"not selected (--only {args.only})")
        else:
            scenario(page, "E", "kill the CLI mid-turn — the footer is ours, not the engine's")
            before = chip(page)["count"]
            send_turn(page, PROMPT_E)
            # The CLI is a streaming-input session, so the child has been alive
            # since the first turn — finding it is not the wait. The sleep is:
            # a kill that lands before the engine has streamed anything tests a
            # turn that failed early, and phase 6's case is the one that failed
            # *late*, having already spent.
            time.sleep(6)
            killed = []
            for pid in backend.cli_children():
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except OSError:
                    pass
            if not killed:
                report.skip(
                    "[E] a failed turn reads 'cost unknown'",
                    "no CLI child could be found to kill, so `_fail_turn` was "
                    "never provoked. What was under the backend: "
                    + ", ".join(
                        f"{pid}:{cmd[:60]}" for pid, cmd in backend.descendants()
                    ),
                )
            else:
                print(f"    killed CLI pid(s) {killed}")
                try:
                    rec_e = wait_for_turn(page, timeout=120.0)
                except RuntimeError as exc:
                    report.add(
                        "[E] a failed turn reports a footer at all",
                        False,
                        f"killing the CLI produced no streamComplete: {exc}",
                    )
                else:
                    basis_e, usd_e, _, errored_e = basis_of(rec_e)
                    got_e = chip(page)
                    shot_e = page.shot(SHOTS / "turn-cost-E.png")
                    report.add(
                        "[E] a killed turn is unpriced",
                        basis_e == "unpriced",
                        f"basis={basis_e!r} usd={usd_e!r} is_error={errored_e}",
                    )
                    # The named case, stated as its own check: the failure mode
                    # phase 6 was worried about is not "no chip", it is a chip
                    # that says the turn was free.
                    report.add(
                        "[E] a killed turn never reads 'nothing extra'",
                        got_e["text"] != NOTHING_EXTRA,
                        f"chip={got_e['text']!r} title={str(got_e['title'])[:60]!r}",
                        shot_e,
                    )
                    if got_e["count"] > before:
                        report.add(
                            "[E] chip text",
                            got_e["text"] == COST_UNKNOWN,
                            f"{got_e['text']!r} (wanted {COST_UNKNOWN!r})",
                        )
                        report.add(
                            "[E] chip tooltip",
                            UNPRICED_TITLE in (got_e["title"] or ""),
                            f"{got_e['title']!r}",
                        )
                    else:
                        report.add(
                            "[E] chip",
                            False,
                            f"no new chip after the kill ({got_e['count']} chips)",
                        )

        # ---- F: the basis nothing can reach, said out loud rather than left
        # as a gap in a green run.
        report.skip(
            "[F] the 'reset' basis",
            "unreachable from the app: the CLI restarts its ledger on /clear, "
            "`SLASH_ROUTES` routes /clear to a new session before the CLI sees "
            "it, and a resume reconnects and calls CostLedger.reset() so the "
            "next turn is measured against no baseline. The renderer stays "
            "because the CLI's schema warns the total can restart — its "
            "coverage is the unit tests, and this probe cannot add to it",
        )

        dump(page)
        page.report_console("end of run")
        return report.verdict()
    except Exception as exc:  # noqa: BLE001 — the report is the deliverable
        print(f"\nPROBE ERROR: {exc}", file=sys.stderr)
        if browser is not None:
            try:
                dump(browser.page)
                browser.page.report_console("probe error")
                browser.page.shot(SHOTS / "turn-cost-error.png")
            except Exception:
                pass
        print("\nbackend log tail:", file=sys.stderr)
        print(backend.tail(30), file=sys.stderr)
        report.void(f"the probe could not complete: {exc}")
        return report.verdict()
    finally:
        if browser is not None:
            browser.stop()
        backend.stop()
        print(f"\nscreenshots: {SHOTS}")
        print(f"backend log: {backend.log}")


if __name__ == "__main__":
    sys.exit(main())
