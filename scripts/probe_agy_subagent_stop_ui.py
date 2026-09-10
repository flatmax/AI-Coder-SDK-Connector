#!/usr/bin/env python3
"""Does ⏹ on a subagent's row work in a browser, on a live `agy` turn?

`scripts/probe_agy_subagent_stop.py` measured the mechanism at the gate: an
aimed refusal denies the stopped subagent's later calls and leaves the
parent's reaching the dialog. This is the other half, and it exists because
this directory has been caught once already shipping a feature whose wire was
argued rather than watched.

The specific claim under test is **"no webapp change"**. `block-render.js`
draws Stop on `live && row.task_id && supports(SURFACE.SUBAGENT_STOP)`; the
pump has always set `task_id`, and the only thing that was false was the
capability. So flipping `subagent_stop` to SUPPORTED on this transport should
make the button appear with nothing in the browser edited — and if it does not,
the row contract is wrong rather than the descriptor.

Four checks:

  1. A **live** subagent row carries a Stop button. Before today it did not,
     on this engine, and nothing in the webapp changed to make it.
  2. Clicking it settles that row **`stopped`** — amber — rather than
     `completed`. `agy` reports a starved subagent as DONE (measured), so this
     is the override in `AgyTranslator.mark_stopped` arriving on screen, which
     is the only place it matters.
  3. The **parent turn still finishes**. This is the whole difference from
     `cancel_streaming`, and the failure it guards against is the one that kept
     the surface unbuilt: a ⏹ wired to the turn-wide refusal would stop the
     turn the user was still using.
  4. The stopped subagent's **transcript is still readable**. A stop is not a
     deletion, and the row is the evidence it ran.

The delegation is deliberately long — fourteen files, read one at a time — so
there is a live subagent on screen for long enough to press a button on. A run
where the subagent finishes first is uninterpretable rather than passing.

Usage:
    .venv/bin/python scripts/probe_agy_subagent_stop_ui.py [--headless]

Exit 0 when every check passed, 1 on a failure, 2 when the run could not be
interpreted.

What it costs: one turn on the paid subscription, in a throwaway repository
where nothing is written.

Governing spec: `specs5/plan-ag/` — AG-5, AG-R-4, and the `subagent_stop` row
of `src/aic_dc/capabilities.py`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _agy_probe_support import probe_root  # noqa: E402
from _live_app_probe import (  # noqa: E402
    PANEL_JS,
    REPO,
    Backend,
    Browser,
    Page,
    Report,
    answer_confirm,
    answer_permission,
    dump,
    note,
    permission_on_screen,
    records,
    send_turn,
    wait_for_app,
)

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy import tools as agy_tools  # noqa: E402

SHOTS = REPO / ".aic-dc" / "live-probe"

NOTE_COUNT = 14

PROMPT = (
    "Do exactly this and nothing else. Delegate to a single subagent using "
    "your subagent tool. Instruct the subagent to read every file in the "
    "notes/ directory — there are 14, named note-01.md to note-14.md — "
    "strictly one at a time, each with its own separate file-reading call, "
    "writing a one-sentence summary of each as it goes, and tell it not to "
    "stop early and not to read them in bulk. Do not read any file yourself. "
    "When the subagent has finished or stopped, reply with the single word: "
    "done."
)

ROWS_JS = """
(() => {
  const p = %s;
  if (!p || !p.shadowRoot) return null;
  return [...p.shadowRoot.querySelectorAll('.subagent-row')].map((r) => ({
    agent_id: r.getAttribute('data-agent-id'),
    live: r.classList.contains('live'),
    desc: (r.querySelector('.subagent-desc') || {}).textContent || null,
    status: (r.querySelector('.subagent-status') || {}).textContent || null,
    has_stop: !!r.querySelector('button.subagent-stop'),
  }));
})()
""" % PANEL_JS

CLICK_STOP_JS = """
(() => {
  const p = %s;
  if (!p || !p.shadowRoot) return 'no panel';
  const row = p.shadowRoot.querySelector(
    '.subagent-row[data-agent-id=' + JSON.stringify(%s) + ']');
  if (!row) return 'no row';
  const btn = row.querySelector('button.subagent-stop');
  if (!btn) return 'no stop button';
  btn.click();
  return 'clicked';
})()
"""


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def decide(request: dict) -> str:
    """Allow everything but the write seam, so the reading task can run."""
    tool = request.get("tool_name") or ""
    if tool in ("invoke_subagent", "start_subagent", "run_subagent"):
        return "allow"
    return "deny" if tool in agy_tools.MUTATING_TOOLS else "allow"


def switch_to_agy(page: Page, timeout: float = 120.0) -> None:
    page.eval(
        """
        (async () => {
          const p = %s;
          try { return await p.rpcExtract('ClaudeCodeService.switch_engine', 'agy'); }
          catch (err) { return {error: String(err)}; }
        })()
        """
        % PANEL_JS,
        await_promise=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        engines = page.eval(
            """
            (async () => {
              const p = %s;
              try { return await p.rpcExtract('ClaudeCodeService.list_engines'); }
              catch (err) { return {error: String(err)}; }
            })()
            """
            % PANEL_JS,
            await_promise=True,
        )
        if isinstance(engines, dict) and engines.get("active") == "agy":
            return
        time.sleep(1.0)
    raise SystemExit("the engine never became agy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    if not shutil.which("agy"):
        raise SystemExit("no `agy` on PATH")
    state = install.status(Path.home() / ".config" / "aic-dc")
    if state.get("state") != "current":
        raise SystemExit(f"the agy gate reads {state.get('state')!r}, not 'current'")

    SHOTS.mkdir(parents=True, exist_ok=True)
    report = Report()
    root = probe_root("agy-stop-ui-")
    notes = root / "notes"
    notes.mkdir(exist_ok=True)
    for index in range(1, NOTE_COUNT + 1):
        (notes / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n\nFact {index}: the shelf holds {index} jars.\n",
            encoding="utf-8",
        )
    log(f"scratch repo: {root}")

    backend = Backend(root)
    browser = None
    try:
        browser = Browser(backend.url, headless=args.headless)
        page = browser.page
        wait_for_app(page)
        switch_to_agy(page)
        note(page, "delegating")
        # `_stopSubagent` on the row does not confirm, but the tab strip's ⏹
        # does; set it once so either affordance goes through rather than
        # silently returning.
        answer_confirm(page, True)
        send_turn(page, PROMPT)

        stopped_id = None
        stop_shot = None
        deadline = time.time() + 600.0
        seen_complete = len(records(page, "stream-complete"))
        while time.time() < deadline:
            request = permission_on_screen(page)
            if request is not None:
                action = decide(request)
                answer_permission(page, action)
                log(f"dialog: {request.get('tool_name')} -> {action}")
                continue
            if stopped_id is None:
                rows = page.eval(ROWS_JS) or []
                live = [r for r in rows if r.get("live") and r.get("agent_id")]
                if live:
                    row = live[0]
                    report.add(
                        "a live subagent row carries Stop, with no webapp change",
                        bool(row.get("has_stop")),
                        f"row {row.get('desc')!r} has_stop={row.get('has_stop')}",
                    )
                    if not row.get("has_stop"):
                        break
                    stop_shot = page.shot(SHOTS / "agy-subagent-stop-before.png")
                    outcome = page.eval(
                        CLICK_STOP_JS % (PANEL_JS, json.dumps(row["agent_id"]))
                    )
                    log(f"clicking Stop on {row['agent_id']}: {outcome}")
                    if outcome != "clicked":
                        report.void(f"could not press Stop: {outcome}")
                        break
                    stopped_id = row["agent_id"]
                    continue
            if len(records(page, "stream-complete")) > seen_complete:
                break
            time.sleep(0.5)

        if stopped_id is None and not report.rows:
            report.void(
                "no live subagent row was ever on screen — the delegation "
                "finished before it could be pressed, or none happened"
            )
            dump(page, ("subagent-event",))
            return report.verdict()

        if stopped_id is not None:
            # The parent turn must still finish. Waited for rather than
            # assumed: this is the failure that kept the surface unbuilt.
            settled = False
            deadline = time.time() + 420.0
            while time.time() < deadline:
                request = permission_on_screen(page)
                if request is not None:
                    answer_permission(page, decide(request))
                    continue
                if len(records(page, "stream-complete")) > seen_complete:
                    settled = True
                    break
                time.sleep(0.5)
            report.add(
                "the parent turn still finished after one subagent was stopped",
                settled,
                "stream-complete arrived" if settled else "the turn never settled",
            )

            rows = page.eval(ROWS_JS) or []
            row = next((r for r in rows if r.get("agent_id") == stopped_id), None)
            events = [
                r
                for r in records(page, "subagent-event")
                if r.get("agent_id") == stopped_id
            ]
            terminal = [e for e in events if e.get("terminal")]
            status = terminal[-1].get("status") if terminal else None
            after_shot = page.shot(SHOTS / "agy-subagent-stop-after.png")
            report.add(
                "the stopped row settles `stopped`, not `completed`",
                status == "stopped",
                f"terminal status = {status!r} (agy reports the step DONE; the "
                f"word is mark_stopped's)",
                after_shot,
            )
            report.add(
                "and it is no longer live, so its Stop button is gone",
                row is not None and not row.get("live") and not row.get("has_stop"),
                f"row = {row}",
                stop_shot,
            )
            transcript = page.eval(
                """
                (async () => {
                  const p = %s;
                  try {
                    return await p.rpcExtract(
                      'ClaudeCodeService.get_subagent_transcript', %s, null);
                  } catch (err) { return {error: String(err)}; }
                })()
                """
                % (PANEL_JS, json.dumps(stopped_id)),
                await_promise=True,
            )
            report.add(
                "a stopped subagent's transcript is still readable",
                isinstance(transcript, list) and len(transcript) > 0,
                f"{type(transcript).__name__} of "
                f"{len(transcript) if isinstance(transcript, list) else 'n/a'}",
            )

        dump(page, ("subagent-event",))
        return report.verdict()
    except Exception as exc:  # noqa: BLE001 - a probe reports rather than raises
        report.void(f"{type(exc).__name__}: {exc}")
        print(backend.tail(40))
        return report.verdict()
    finally:
        if browser is not None:
            browser.stop()
        backend.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
