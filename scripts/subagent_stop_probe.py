#!/usr/bin/env python3
"""Does ⏹ Stop end a live subagent, and does its LED go amber?

`specs5/next.md` § D1. The webapp's own ⏹ is the only caller that goes through
the whole chain — the confirmation, `panel._stopSubagent`, the `stop_task` RPC,
the engine's answer arriving in the *message stream* rather than as a return
value, and the LED state derived from it. Every part of that has unit coverage
against synthetic rows. The chain end to end has none.

The cheap substitute was tried and it failed in an instructive way: an agent
calling its own `TaskStop` on a live background subagent killed the subagent
while the CLI emitted no terminal task message at all, leaving both engine and
browser reading `status: null, terminal: false` and the LED cyan over something
already dead. That is a different code path with a different outcome, so it
could neither confirm nor refute the one shipped. Hence this probe.

Six things it establishes, in one turn against a live engine:

  1. A live subagent tab carries ⏹ at all — it is rendered only when the tab is
     streaming *and* the row has a `task_id`, and a row that never gets one has
     no Stop button no matter how long it runs.
  2. **The confirmation is load-bearing.** Clicked with `confirm` answering
     *no*, nothing happens: the subagent stays live and no terminal status
     arrives. This is the negative control, and without it a passing run cannot
     tell "Stop works" from "the subagent finished on its own".
  3. Its message names what the user asked for, exactly: `Stop <desc>? It
     cannot be resumed.`
  4. Answered *yes*, a terminal `subagentEvent` comes back. Which shape it
     takes is recorded rather than asserted — `stop_task` is answered either as
     a `notification` of `stopped` or as an `updated` patch of `killed`, and
     the LED table accepts both.
  5. The LED reads **amber**, with the tooltip form § Status LEDs specifies
     (`<desc>: stopped` / `<desc>: killed`) — not "status unknown at turn end",
     which is a different state that has to be earned.
  6. The stopped tab keeps its feed and loses its ⏹, and the parent turn still
     finishes. A stopped subagent must not wedge the turn that spawned it.

The positive control is the second subagent: a trivial one that runs to
completion in the same turn and must settle **green**. Amber everywhere would
satisfy every check above while meaning the LED had stopped distinguishing
anything.

The subagents are asked to read files and nothing else, because
`permissions.py` leaves `read` and `delegate` ungated — a probe that tripped a
permission dialog would sit at a gate nothing here answers.

Usage:
    .venv/bin/python scripts/subagent_stop_probe.py [--repo /tmp/aicdc-uitest]
                                                    [--headless]

Exit 0 when every check passed, 1 on a failure or an uninterpretable run.
"""

from __future__ import annotations

import argparse
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
    answer_confirm,
    dump,
    note,
    records,
    send_turn,
    wait_for_app,
    wait_for_turn,
)

SHOTS = REPO / ".aic-dc" / "live-probe"

# Two subagents in one message so they run concurrently: one long enough to be
# caught alive, one short enough to settle green while the other is still going.
# The corpus is `notes/note-01.md` … `note-30.md`, one Read each.
#
# Phrased as an instruction to delegate rather than to work, because the thing
# under test is a `Task` call. An agent that decided to read the notes itself
# would produce a turn with no subagent tab in it and nothing to stop.
PROMPT = (
    "Do exactly this and nothing else. In a single message, launch two "
    "subagents with the Task tool so they run concurrently.\n"
    "  1. description: 'read every note'. Its prompt: 'Read every file in the "
    "notes/ directory of this repository, one at a time with the Read tool, "
    "and write a one-sentence summary of each. Read them all — do not stop "
    "early and do not use Glob or Grep to shortcut it.'\n"
    "  2. description: 'count the notes'. Its prompt: 'Reply with only the "
    "number of files in the notes/ directory. Use a single Glob call and "
    "nothing else.'\n"
    "Do not read any file yourself. When both subagents have returned, reply "
    "with the single word: done."
)

# Which of the two to stop. Matched against the description the tab carries,
# which is the description the parent agent passed to `Task`.
STOP_MATCH = "read"

# `subagent-tabs.js` § _TERMINAL_LED: the two terminal statuses that mean
# "ended, but neither finished nor faulted".
STOPPED_STATUSES = ("stopped", "killed")

TABS_JS = """
(() => {
  const p = %s;
  if (!p) return null;
  const root = p.shadowRoot;
  const out = [];
  for (const [tabId, tab] of p._tabs.entries()) {
    if (!tab || !tab.subagent) continue;
    const s = tab.subagent;
    const led = root.querySelector('.led-dot[data-led-tab-id="' + tabId + '"]');
    const strip = root.querySelector('.tab-strip-tab[data-tab-id="' + tabId + '"]');
    out.push({
      tabId: tabId,
      taskId: typeof s.task_id === 'string' ? s.task_id : null,
      // The same precedence `renderSubagentTabStop` uses to build the
      // confirmation, so the expected message can be computed from here.
      desc: s.labelDescription || s.description || s.subagent_type || 'this subagent',
      subagentType: s.subagent_type || null,
      status: s.status === undefined ? null : s.status,
      terminal: !!s.terminal,
      settled: !!s.settled,
      unknown: !!s.unknown,
      errored: !!s.errored,
      streaming: !!tab.streaming,
      lastTool: s.last_tool_name || null,
      ledState: led ? led.getAttribute('data-led-state') : null,
      ledTitle: led ? led.getAttribute('title') : null,
      hasStop: !!(strip && strip.querySelector('.tab-stop')),
      hasContext: !!(strip && strip.querySelector('.tab-context')),
      label: p._tabLabels.get(tabId) || null,
    });
  }
  return {
    tabs: out,
    streaming: !!p._streaming,
    activeTab: p._activeTabId,
    tabCount: p._tabs.size,
  };
})()
""" % PANEL_JS

CLICK_STOP_JS = """
(() => {
  const p = %s;
  if (!p) return 'no panel';
  const strip = p.shadowRoot.querySelector(
    '.tab-strip-tab[data-tab-id="' + %s + '"]');
  if (!strip) return 'no tab in the strip';
  const stop = strip.querySelector('.tab-stop');
  if (!stop) return 'no stop button';
  stop.click();
  return 'clicked';
})()
"""

# What the stopped tab shows after the fact. `.messages` is the panel's log
# region; a subagent tab renders its feed there and the read-only note in place
# of the composer.
FEED_JS = """
(async () => {
  const p = %s;
  if (!p) return null;
  const led = p.shadowRoot.querySelector('.led-dot[data-led-tab-id="' + %s + '"]');
  if (!led) return {activated: false};
  led.click();
  await p.updateComplete;
  const messages = p.shadowRoot.querySelector('.messages');
  return {
    activated: p._activeTabId === %s,
    empty: !!p.shadowRoot.querySelector('.empty-state'),
    chars: messages ? messages.textContent.trim().length : 0,
    readOnlyNote: !!p.shadowRoot.querySelector('.read-only-note'),
    composer: !!p.shadowRoot.querySelector('.input-textarea'),
  };
})()
"""


def tabs(page: Page) -> dict:
    got = page.eval(TABS_JS)
    if got is None:
        raise RuntimeError("the chat panel went away")
    return got


def terminal_events(page: Page, task_id: str) -> list[dict]:
    return [
        r for r in records(page, "subagent-event")
        if r.get("task_id") == task_id and r.get("terminal")
    ]


def wait_for_live_subagent(page: Page, *, timeout: float, want: int) -> dict:
    """Wait for a stoppable subagent tab, and for its sibling if it comes.

    Returns the last snapshot. Waits past the first tab for `want` of them
    while any are still live: the control subagent is the whole reason for the
    second one, and a probe that grabbed the first tab it saw would race the
    parent's second `Task` call.
    """
    deadline = time.time() + timeout
    settled_wait = None
    last = {}
    while time.time() < deadline:
        last = tabs(page)
        live = [t for t in last["tabs"] if t["streaming"] and t["taskId"] and t["hasStop"]]
        if len(last["tabs"]) >= want and live:
            return last
        if live and settled_wait is None:
            # One is stoppable but the other has not appeared. Give it a
            # bounded grace period rather than the whole timeout — the live one
            # is spending tokens while we wait.
            settled_wait = time.time() + 25
        if settled_wait is not None and time.time() > settled_wait and live:
            print(f"    only {len(last['tabs'])} subagent tab(s) after the grace "
                  "period; going with what is here")
            return last
        if records(page, "stream-complete"):
            raise RuntimeError(
                "the turn finished before a stoppable subagent tab appeared — "
                f"tabs seen: {[t['label'] for t in last['tabs']]}"
            )
        time.sleep(1.0)
    raise RuntimeError(f"no live subagent tab with a task id within {timeout:.0f}s")


def pick(snapshot: dict) -> tuple[dict, list[dict]]:
    """The tab to stop, and the others."""
    live = [t for t in snapshot["tabs"] if t["streaming"] and t["taskId"] and t["hasStop"]]
    preferred = [t for t in live if STOP_MATCH in (t["desc"] or "").lower()]
    target = (preferred or live)[0]
    return target, [t for t in snapshot["tabs"] if t["tabId"] != target["tabId"]]


def find(snapshot: dict, tab_id: str) -> dict | None:
    return next((t for t in snapshot["tabs"] if t["tabId"] == tab_id), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/tmp/aicdc-uitest")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="serve through Vite. Note that a webapp edit during a "
                         "run triggers a reload, and a reload drops every live "
                         "subagent tab")
    ap.add_argument("--spawn-timeout", type=float, default=180.0)
    ap.add_argument("--stop-timeout", type=float, default=90.0)
    ap.add_argument("--turn-timeout", type=float, default=600.0)
    args = ap.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    report = Report()
    backend = Backend(Path(args.repo), dev=args.dev)
    browser = None
    try:
        browser = Browser(backend.url, headless=args.headless)
        page = browser.page
        wait_for_app(page)
        print("app ready")

        note(page, "spawning two subagents")
        print("\n[1] a turn that delegates twice")
        send_turn(page, PROMPT)
        snapshot = wait_for_live_subagent(
            page, timeout=args.spawn_timeout, want=2,
        )
        target, others = pick(snapshot)
        print(f"    {len(snapshot['tabs'])} subagent tab(s): "
              + ", ".join(f"{t['label']!r} live={t['streaming']} led={t['ledState']}"
                          for t in snapshot["tabs"]))
        print(f"    stopping {target['label']!r} (desc {target['desc']!r}, "
              f"task {target['taskId']})")
        page.shot(SHOTS / "subagent-stop-1-live.png")

        report.add(
            "[1] a live subagent tab offers ⏹",
            target["hasStop"] and not target["hasContext"],
            f"⏹={target['hasStop']} 📊={target['hasContext']} — Stop takes the "
            "Context icon's place on a subagent tab, it does not sit beside it",
        )
        report.add(
            "[1] a live subagent's LED flashes cyan",
            target["ledState"] == "cyan",
            f"data-led-state={target['ledState']!r} title={target['ledTitle']!r}",
        )

        # ---- 2: the negative control. Refuse the confirmation.
        note(page, "clicking stop and refusing the confirmation")
        print("\n[2] ⏹ with the confirmation refused — nothing may happen")
        confirms_before = len(records(page, "confirm"))
        answer_confirm(page, False)
        clicked = page.eval(CLICK_STOP_JS % (PANEL_JS, repr(target["tabId"])))
        report.add(
            "[2] the ⏹ click landed",
            clicked == "clicked",
            f"{clicked}",
        )
        time.sleep(6)
        refused = records(page, "confirm")[confirms_before:]
        want_msg = f"Stop {target['desc']}? It cannot be resumed."
        report.add(
            "[2] ⏹ asks before it acts",
            len(refused) == 1,
            f"{len(refused)} confirmation(s) recorded for one click",
        )
        if refused:
            report.add(
                "[2] the confirmation names what is being ended",
                refused[-1].get("msg") == want_msg,
                f"{refused[-1].get('msg')!r} (wanted {want_msg!r})",
            )
        after_refusal = find(tabs(page), target["tabId"])
        still_live = bool(after_refusal and after_refusal["streaming"])
        terminal_after_refusal = terminal_events(page, target["taskId"])
        report.add(
            "[2] a refused confirmation stops nothing",
            still_live and not terminal_after_refusal,
            (
                f"still streaming={still_live}, terminal events="
                f"{[e.get('status') for e in terminal_after_refusal]}"
                if still_live or terminal_after_refusal else
                "the subagent is no longer live after a refused confirmation"
            ),
        )
        if not still_live:
            report.void(
                "the subagent was already gone before the real Stop, so an "
                "amber LED afterwards would say nothing about ⏹ — it would be "
                "the LED of a subagent that ended on its own"
            )
            dump(page)
            return report.verdict()

        # ---- 3: the gesture itself.
        note(page, "clicking stop and confirming")
        print("\n[3] ⏹ confirmed — the engine must report a terminal status")
        answer_confirm(page, True)
        clicked = page.eval(CLICK_STOP_JS % (PANEL_JS, repr(target["tabId"])))
        report.add("[3] the ⏹ click landed", clicked == "clicked", f"{clicked}")

        deadline = time.time() + args.stop_timeout
        arrived: list[dict] = []
        while time.time() < deadline:
            arrived = terminal_events(page, target["taskId"])
            if arrived:
                break
            time.sleep(1.0)
        report.add(
            "[3] a terminal status comes back for the stopped task",
            bool(arrived),
            (
                f"{arrived[-1].get('type')} → status={arrived[-1].get('status')!r}"
                if arrived else
                f"nothing terminal for task {target['taskId']} within "
                f"{args.stop_timeout:.0f}s — the engine accepted the RPC and "
                "the browser was never told"
            ),
        )
        if arrived:
            status = arrived[-1].get("status")
            # Recorded, not asserted: `stop_task` is answered either as a
            # notification of `stopped` or as an `updated` patch of `killed`,
            # and the LED table takes both. Which one the CLI chose is the fact
            # worth writing down.
            print(f"    the engine answered as a {arrived[-1].get('type')!r} "
                  f"carrying status={status!r}")
            report.add(
                "[3] the terminal status is one the LED table names",
                status in STOPPED_STATUSES,
                f"status={status!r}; `_TERMINAL_LED` maps "
                f"{list(STOPPED_STATUSES)} to amber and anything else it does "
                "not recognise to amber as well",
            )

        # ---- 4: what the strip shows now.
        time.sleep(2)
        after = tabs(page)
        stopped = find(after, target["tabId"])
        shot = page.shot(SHOTS / "subagent-stop-2-stopped.png")
        if stopped is None:
            report.add(
                "[4] the stopped tab is still there",
                False,
                "the tab left the strip when the subagent was stopped — a "
                "stopped subagent's feed is meant to outlive it, for the rest "
                "of the turn",
                shot,
            )
        else:
            print(f"    stopped tab: status={stopped['status']!r} "
                  f"terminal={stopped['terminal']} led={stopped['ledState']!r} "
                  f"title={stopped['ledTitle']!r}")
            report.add(
                "[4] the stopped subagent's LED is amber",
                stopped["ledState"] == "amber",
                f"data-led-state={stopped['ledState']!r} "
                f"(status={stopped['status']!r} terminal={stopped['terminal']} "
                f"unknown={stopped['unknown']} errored={stopped['errored']})",
                shot,
            )
            expected_title = f"{stopped['desc']}: {stopped['status'] or 'stopped'}"
            report.add(
                "[4] the LED tooltip names the outcome, not a guess",
                (stopped["ledTitle"] or "").endswith(
                    f": {stopped['status']}"
                ) and "status unknown" not in (stopped["ledTitle"] or ""),
                f"{stopped['ledTitle']!r} (the § Status LEDs form is "
                f"{expected_title!r}; 'status unknown at turn end' is a "
                "different state and has to be earned)",
            )
            report.add(
                "[4] ⏹ is gone from a settled tab",
                not stopped["hasStop"],
                f"⏹={stopped['hasStop']} — a Stop button on something already "
                "over offers to end it again",
            )

            feed = page.eval(
                FEED_JS % (PANEL_JS, repr(target["tabId"]), repr(target["tabId"])),
                await_promise=True,
            )
            print(f"    feed after stopping: {feed}")
            report.add(
                "[4] the stopped subagent keeps its feed",
                bool(feed and feed.get("activated") and not feed.get("empty")
                     and (feed.get("chars") or 0) > 0),
                f"{feed}",
                page.shot(SHOTS / "subagent-stop-3-feed.png"),
            )
            report.add(
                "[4] the stopped tab stays read-only",
                bool(feed) and not feed.get("composer")
                and bool(feed.get("readOnlyNote")),
                f"composer present={feed.get('composer') if feed else None}, "
                f"read-only note={feed.get('readOnlyNote') if feed else None} — a "
                "subagent tab has no channel to reply down, and says so rather "
                "than greying a textarea",
            )

        # ---- 5: the turn, and the control subagent.
        print("\n[5] the turn must still finish, and the control must go green")
        try:
            result = wait_for_turn(page, timeout=args.turn_timeout)
        except RuntimeError as exc:
            report.add(
                "[5] the parent turn survives a stopped subagent",
                False,
                f"{exc}",
                page.shot(SHOTS / "subagent-stop-4-wedged.png"),
            )
        else:
            res = result.get("result", {})
            report.add(
                "[5] the parent turn survives a stopped subagent",
                True,
                f"turn ended is_error={res.get('is_error')!r} "
                f"num_turns={res.get('num_turns')!r} "
                f"basis={res.get('turn_cost_basis')!r}",
            )

        final = tabs(page)
        shot = page.shot(SHOTS / "subagent-stop-5-settled.png")
        for t in final["tabs"]:
            print(f"    {t['label']!r}: status={t['status']!r} "
                  f"terminal={t['terminal']} settled={t['settled']} "
                  f"led={t['ledState']!r} title={t['ledTitle']!r}")
        controls = [t for t in final["tabs"] if t["tabId"] != target["tabId"]]
        green = [t for t in controls if t["ledState"] == "green"]
        if not controls:
            # A loud skip rather than a void, and the difference is which
            # assertion the missing control undermines. The primary claim here
            # is a *presence* — ⏹ produced a terminal status and an amber LED —
            # and step 2 is its control: the same click with the confirmation
            # refused changed nothing, so the subagent was demonstrably still
            # live and only the confirmed click ended it. What is left untested
            # without a sibling is the weaker "the LED still distinguishes
            # states", which the unit suite does cover.
            report.skip(
                "[5] the control subagent settles green",
                "only one subagent ever got a tab, so nothing in this run "
                "shows a *green* LED beside the amber one. The stop finding "
                "stands on step 2's refused-confirmation control; 'the LED "
                "distinguishes outcomes' does not, and is untested here",
            )
        else:
            report.add(
                "[5] the control subagent settles green",
                bool(green),
                ", ".join(
                    f"{t['label']!r} led={t['ledState']!r} status={t['status']!r} "
                    f"title={t['ledTitle']!r}" for t in controls
                ),
                shot,
            )
        stopped_final = find(final, target["tabId"])
        if stopped_final:
            report.add(
                "[5] the stopped subagent stays amber at turn end",
                stopped_final["ledState"] == "amber",
                f"led={stopped_final['ledState']!r} "
                f"title={stopped_final['ledTitle']!r} — settling the turn must "
                "not re-read a stopped subagent as unknown or completed",
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
                browser.page.shot(SHOTS / "subagent-stop-error.png")
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
