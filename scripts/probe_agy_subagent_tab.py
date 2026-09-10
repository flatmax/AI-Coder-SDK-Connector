#!/usr/bin/env python3
"""Does a subagent's transcript reach the tab, in a browser, on a live turn?

`specs5/plan-ag/delivery.md` § *What a subagent did* closes with the one thing
that day's work could not assert: **"nobody has opened the tab in a browser on
a live `agy` turn."** The reader was measured against all 121 transcripts on
the machine, the containment gate was shown refusing a real foreign
conversation, and the id chain was followed link by link off disk — but the
round trip was argued rather than watched. Everything between
`AgyService.get_subagent_transcript` and a rendered card is covered by tests
that build both ends themselves.

That gap is not cosmetic. The whole feature rests on one string identity — the
session id the browser holds *being* the conversation id `agy` names its own
store by — and if it were wrong, **every test would still pass** while the tab
stayed empty in the app for good. A fixture builds its store around whichever
id it is handed. Only a live turn can put the browser's id and `agy`'s store on
opposite ends of the same call.

What it establishes, in one turn on the paid subscription:

  1. The descriptor the *browser* reads says `subagent_transcripts` is
     supported here and `subagent_stop` is not — read over the same RPC the
     webapp uses, so a server that answered differently would be caught.
  2. `agy` announces a delegation, and the live strip gains a tab for it.
  3. The row in Main carries **no Stop button**. That is the second of the two
     guards `bb9846a1` shipped, and the one with a live rendering: `agy` has no
     halt frame, so a button here would send a call the router refuses.
  4. **The live tab fills from the transcript on disk.** This is the path a
     user actually meets on this transport, and it exists because a subagent's
     own frames never reach the host: the tab is a label over an empty feed
     until `loadSubagentFeedIfEmpty` reads the store. The assertion is that its
     messages are the subagent's work and *not* the ⚠️ one-liner every failure
     in `_loadSubagentTranscript` degrades to.
  5. At least one **tool card** among them — the subagent's own calls,
     rendered through the same normalizer as a resumed session.
  6. **Containment holds over the wire.** A real conversation on this machine
     that this session never announced is refused by `get_subagent_transcript`
     when the *browser* asks for it. In-process this was shown on 2026-09-10;
     what it had not been shown through is the RPC, which is the only route an
     attacker-shaped request would take.

Check 6 is also the negative control for 4 and 5. Without it a green run cannot
distinguish "the gate lets this session's subagent through" from "the gate
lets anything through".

The subagent is asked to **read** and nothing else, and the dialogs that raises
are answered rather than avoided: `invoke_subagent` is in AG-5's *still asks*
column and there is no posture that ungates it without also ungating the writes
the dialog exists for. What the first run found is that the delegation is not
the only one — `agy` opens with a `schedule` call, which `agy/tools.py` does not
classify and which is therefore gated by default — so the policy here is the
*write seam minus the delegation* rather than "everything but the delegation".
The log of what was answered is printed with the result: a run asserting a
subagent did something has to be able to show it was not denied into doing
nothing.

Usage:
    .venv/bin/python scripts/probe_agy_subagent_tab.py [--headless] [--keep]

Exit 0 when every check passed, 1 on a failure, 2 when the run could not be
interpreted — no `agy`, no delegation, or a turn that never settled.

What it costs: one turn on the paid subscription, in a throwaway git repository
under a trusted workspace, in which nothing is written.

Governing spec: `specs5/plan-ag/` — AG-13, AG-R-4, AG-R-12, and the
`subagent_transcripts` / `subagent_stop` rows of `src/aic_dc/capabilities.py`.
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
    dump,
    note,
    records,
    send_turn,
    wait_for_app,
    wait_for_turn_answering,
)

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy import tools as agy_tools  # noqa: E402

SHOTS = REPO / ".aic-dc" / "live-probe"

BRAIN = Path.home() / ".gemini" / "antigravity-cli" / "brain"

#: Allowed, because without it there is no subagent and nothing to read.
DELEGATION_TOOLS = frozenset({"invoke_subagent", "start_subagent", "run_subagent"})

#: What this probe refuses: the write seam, minus the delegation it needs.
#:
#: **Not "everything but the delegation"**, which is `probe_agy_subagent_gate`'s
#: instrument and would be the wrong one here. That probe denies broadly
#: because a file which changed anyway is its proof; this one needs the
#: subagent to actually work, and the first run showed what a blanket deny
#: costs — `agy` opened with a `schedule` call, which is not in
#: `agy/tools.py::TOOL_CLASSES` and is therefore gated by default, so the turn
#: stopped at a modal for a planning step that touches nothing. Denying the
#: write seam and nothing else keeps the containment claim honest while
#: leaving the read-only work alone.
REFUSED = agy_tools.MUTATING_TOOLS - DELEGATION_TOOLS

NOTES = {
    "note-1.md": "# Note one\n\nThe kettle is descaled every March.\n",
    "note-2.md": "# Note two\n\nThe spare key lives under the third flowerpot.\n",
}

PROMPT = (
    "Do exactly this and nothing else. Delegate the following to a single "
    "subagent using your subagent tool, rather than doing it yourself: read "
    "notes/note-1.md and then notes/note-2.md, one at a time with your "
    "file-reading tool, and reply with a one-sentence summary of each. Do not "
    "read either file yourself. When the subagent has answered, reply with "
    "the single word: done."
)

# `panel._tabs` is a Map of tab state; a subagent tab carries `subagent`. Read
# whole rather than asserted on inside the page, so a failure prints what was
# there instead of `false`.
TABS_JS = """
(() => {
  const p = %s;
  if (!p) return null;
  const out = [];
  for (const [tabId, tab] of p._tabs.entries()) {
    if (!tab || !tab.subagent) continue;
    const s = tab.subagent;
    out.push({
      tabId: tabId,
      agent_id: s.agent_id || null,
      task_id: s.task_id || null,
      description: s.description || null,
      settled: !!s.settled,
      transcriptRead: !!s.transcriptRead,
      blocks: (tab.turnBlocks && tab.turnBlocks.blocks || []).length,
      messages: (tab.messages || []).map((m) => ({
        role: m.role || null,
        system_event: !!m.system_event,
        content: typeof m.content === 'string' ? m.content.slice(0, 160) : null,
        blocks: (m.blocks || []).map((b) => ({
          kind: b.kind || null,
          tool: (b.tool && b.tool.name) || null,
        })),
      })),
    });
  }
  return out;
})()
""" % PANEL_JS

ROWS_JS = """
(() => {
  const p = %s;
  if (!p || !p.shadowRoot) return null;
  const rows = [...p.shadowRoot.querySelectorAll('.subagent-row')];
  return rows.map((r) => ({
    agent_id: r.getAttribute('data-agent-id'),
    live: r.classList.contains('live'),
    desc: (r.querySelector('.subagent-desc') || {}).textContent || null,
    has_desc_button: !!r.querySelector('button.subagent-desc-button'),
    has_stop: !!r.querySelector('button.subagent-stop'),
  }));
})()
""" % PANEL_JS


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


def preconditions() -> None:
    """Raise on anything that would make every assertion below meaningless.

    `_agy_probe_support`'s rule: a probe that carries on past a missing
    precondition reports absences it created.
    """
    if not shutil.which("agy"):
        raise SystemExit("no `agy` on PATH — this probe drives that transport")
    state = install.status(Path.home() / ".config" / "aic-dc")
    if state.get("state") != "current":
        raise SystemExit(
            f"the agy permission gate reads {state.get('state')!r}, not "
            f"'current'. AgySession refuses to start on a stale gate, so the "
            f"turn would never happen. Install it from Settings first."
        )


def foreign_conversation(exclude: set[str]) -> str | None:
    """A real `agy` conversation on this machine that is not this session's.

    The containment check is worth nothing against an id that does not exist:
    a service that refused *everything* would pass it. So this looks for a
    directory with a transcript actually in it — a conversation the reader
    could have rendered had the gate let it.
    """
    if not BRAIN.is_dir():
        return None
    for entry in sorted(BRAIN.iterdir()):
        if not entry.is_dir() or entry.name in exclude:
            continue
        logs = entry / ".system_generated" / "logs" / "transcript_full.jsonl"
        if logs.is_file() and logs.stat().st_size > 0:
            return entry.name
    return None


def switch_to_agy(page: Page, timeout: float = 120.0) -> dict:
    """Make `agy` the master engine, the way the engine notice does.

    Through `switch_engine` rather than through `app.json`, and that is not
    only tidiness: the gate installed in `~/.gemini/config/hooks.json` names
    the *real* config directory, so a probe that pointed the server at a
    scratch one would have the server writing claims where the hook does not
    read them.
    """
    result = page.eval(
        """
        (async () => {
          const p = %s;
          if (!p) return {error: 'no panel'};
          try {
            return await p.rpcExtract('ClaudeCodeService.switch_engine', 'agy');
          } catch (err) {
            return {error: String(err)};
          }
        })()
        """
        % PANEL_JS,
        await_promise=True,
    )
    if not isinstance(result, dict) or result.get("error"):
        raise SystemExit(f"could not switch to agy: {result}")
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
        # `active`, which is what `list_engines` answers — `current` reads
        # naturally and is not a key this RPC has.
        if isinstance(engines, dict) and engines.get("active") == "agy":
            return engines
        time.sleep(1.0)
    raise SystemExit(f"engine never became agy (last: {engines})")


def capabilities(page: Page) -> dict:
    """The descriptor as the browser reads it, over the browser's own RPC."""
    return page.eval(
        """
        (async () => {
          const p = %s;
          try {
            return await p.rpcExtract('ClaudeCodeService.get_engine_capabilities');
          } catch (err) { return {error: String(err)}; }
        })()
        """
        % PANEL_JS,
        await_promise=True,
    )


def surface_status(descriptor: dict, key: str) -> str | None:
    """One surface's status, as `capabilities.descriptor` publishes it.

    A flat map of surface key to `{title, supported, status, note}` — read
    rather than assumed, because this probe's whole point is that the two ends
    are not the same process. An entry that is not that shape reads as "could
    not tell" rather than as a pass.
    """
    entry = descriptor.get(key) if isinstance(descriptor, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("status"), str):
        return entry["status"]
    return None


def activate_tab(page: Page, tab_id: str) -> None:
    """Click the tab in the strip, the way a user reaches a subagent's feed.

    The setter is what starts the transcript read (`index.js` §
    `_activeTabId`), so this is the gesture under test rather than a way of
    getting somewhere else.
    """
    outcome = page.eval(
        """
        (() => {
          const p = %s;
          if (!p || !p.shadowRoot) return 'no panel';
          const el = p.shadowRoot.querySelector(
            '.tab-strip-tab[data-tab-id=' + JSON.stringify(%s) + ']');
          if (el) { el.click(); return 'clicked'; }
          p._activeTabId = %s;
          return 'set';
        })()
        """
        % (PANEL_JS, json.dumps(tab_id), json.dumps(tab_id))
    )
    log(f"activate {tab_id}: {outcome}")


def ask_transcript(page: Page, agent_id: str) -> object:
    """`get_subagent_transcript` straight over the browser's RPC."""
    return page.eval(
        """
        (async () => {
          const p = %s;
          try {
            return await p.rpcExtract(
              'ClaudeCodeService.get_subagent_transcript', %s, null);
          } catch (err) { return {error: String(err)}; }
        })()
        """
        % (PANEL_JS, json.dumps(agent_id)),
        await_promise=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the scratch repository behind for inspection",
    )
    args = parser.parse_args()

    preconditions()
    SHOTS.mkdir(parents=True, exist_ok=True)
    report = Report()

    root = probe_root("agy-subagent-tab-")
    notes = root / "notes"
    notes.mkdir(exist_ok=True)
    for name, body in NOTES.items():
        (notes / name).write_text(body, encoding="utf-8")
    log(f"scratch repo: {root}")

    backend = Backend(root)
    browser = None
    try:
        browser = Browser(backend.url, headless=args.headless)
        page = browser.page
        wait_for_app(page)
        note(page, "switching to agy")

        engines = switch_to_agy(page)
        log(f"engines: {engines.get('active')} of {engines.get('available')}")

        descriptor = capabilities(page)
        transcripts = surface_status(descriptor, "subagent_transcripts")
        stop = surface_status(descriptor, "subagent_stop")
        report.add(
            "the browser reads subagent_transcripts as supported on agy",
            transcripts == "supported",
            f"subagent_transcripts = {transcripts!r}",
        )
        report.add(
            "the browser reads subagent_stop as not supported on agy",
            stop is not None and stop != "supported",
            f"subagent_stop = {stop!r}",
        )

        note(page, "delegating")
        log("sending the delegation prompt")
        send_turn(page, PROMPT)
        result, answered = wait_for_turn_answering(
            page,
            lambda req: ("deny" if (req.get("tool_name") or "") in REFUSED else "allow"),
            timeout=600.0,
        )
        log(f"turn settled: {result.get('subtype')} answered={answered}")
        session_id = result.get("session_id") or page.eval(
            "(() => { const p = %s; return p._sessionInfo && p._sessionInfo.session_id; })()"
            % PANEL_JS
        )
        log(f"session: {session_id}")

        events = [
            r for r in records(page, "subagent-event") if r.get("agent_id")
        ]
        agent_ids = sorted({r["agent_id"] for r in events})
        if not agent_ids:
            report.void(
                "no subagent was announced — the model answered without "
                "delegating, so there is no tab to open. Nothing about the "
                "transcript path is settled either way."
            )
            dump(page, ("subagent-event", "system-event", "stream-complete"))
            page.shot(SHOTS / "agy-subagent-tab-no-delegation.png")
            return report.verdict()
        report.add(
            "agy announced a delegation with a conversation id of its own",
            True,
            f"{len(agent_ids)} subagent(s): {agent_ids}",
        )

        rows = page.eval(ROWS_JS) or []
        row = next((r for r in rows if r.get("agent_id") in agent_ids), None)
        report.add(
            "the delegation rendered as a row in Main",
            row is not None,
            f"rows: {rows}",
        )
        if row is not None:
            report.add(
                "the row carries no Stop button, because agy has no halt frame",
                not row.get("has_stop"),
                f"has_stop = {row.get('has_stop')}, task_id absent by design",
            )

        tabs = page.eval(TABS_JS) or []
        tab = next((t for t in tabs if t.get("agent_id") in agent_ids), None)
        report.add(
            "the live strip gained a tab for the subagent",
            tab is not None,
            f"tabs: {[t.get('tabId') for t in tabs]}",
        )

        shot = None
        if tab is not None:
            # What the service would hand this tab, asked before the gesture
            # rather than after it. The tab is compared against this rather
            # than against zero: a subagent tab is seeded with a line
            # describing what it was asked to do, so "the tab has messages" is
            # true before any transcript is read and a check that accepts it
            # passes on the seed alone.
            served = ask_transcript(page, tab["agent_id"])
            served_n = len(served) if isinstance(served, list) else 0
            seeded_n = len(tab.get("messages") or [])
            log(f"the service answers {served_n} message(s); the tab holds "
                f"{seeded_n} before the click")

            activate_tab(page, tab["tabId"])
            # Waiting on `transcriptRead` is waiting on the wrong thing:
            # `loadSubagentFeedIfEmpty` latches it *before* the await, so it
            # flips the instant the read starts. The first run of this probe
            # did exactly that and reported an empty tab that was merely one
            # RPC round trip from being full. Wait for the messages.
            deadline = time.time() + 90.0
            filled = None
            while time.time() < deadline:
                tabs = page.eval(TABS_JS) or []
                filled = next(
                    (t for t in tabs if t.get("agent_id") == tab["agent_id"]), None
                )
                if filled and len(filled.get("messages") or []) > seeded_n:
                    break
                time.sleep(0.5)
            messages = (filled or {}).get("messages") or []
            unreadable = [
                m
                for m in messages
                if m.get("system_event") and str(m.get("content") or "").startswith("⚠️")
            ]
            for m in messages:
                log(
                    f"  tab message: {m.get('role')} "
                    f"{str(m.get('content'))[:70]!r} "
                    f"blocks={[b.get('tool') for b in (m.get('blocks') or [])]}"
                )
            shot = page.shot(SHOTS / "agy-subagent-tab.png")
            report.add(
                "the tab filled from agy's own transcript store",
                served_n > 0 and len(messages) >= seeded_n + served_n and not unreadable,
                f"{len(messages)} message(s) in the tab, from {seeded_n} seeded "
                f"plus {served_n} the service answered"
                + (f", unreadable: {unreadable[0]['content']!r}" if unreadable else ""),
                shot,
            )
            cards = [
                b
                for m in messages
                for b in (m.get("blocks") or [])
                if b.get("kind") == "tool"
            ]
            report.add(
                "the subagent's own tool calls rendered as cards",
                bool(cards),
                f"{len(cards)} card(s): {[c.get('tool') for c in cards][:6]}",
                shot,
            )
            if not cards:
                page.report_console("the tab that did not fill")

        # The negative control, over the wire this time.
        exclude = set(agent_ids) | {str(session_id)}
        stranger = foreign_conversation(exclude)
        if stranger is None:
            report.skip(
                "containment over the RPC",
                "no other agy conversation with a transcript on this machine",
            )
        else:
            refused = ask_transcript(page, stranger)
            report.add(
                "a conversation this session never announced is refused",
                isinstance(refused, dict) and bool(refused.get("error")),
                f"{stranger} -> {json.dumps(refused)[:160]}",
            )
            mine = ask_transcript(page, agent_ids[0])
            report.add(
                "and the session's own subagent is not, so the gate discriminates",
                isinstance(mine, list) and len(mine) > 0,
                f"{agent_ids[0]} -> {type(mine).__name__} of "
                f"{len(mine) if isinstance(mine, list) else 'n/a'}",
            )

        dump(page)
        return report.verdict()
    except Exception as exc:  # noqa: BLE001 - a probe reports rather than raises
        report.void(f"{type(exc).__name__}: {exc}")
        print(backend.tail(60))
        return report.verdict()
    finally:
        if browser is not None:
            browser.stop()
        backend.stop()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            log(f"kept {root}")


if __name__ == "__main__":
    sys.exit(main())
