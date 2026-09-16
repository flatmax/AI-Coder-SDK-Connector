#!/usr/bin/env python3
"""Does the "your stop did not land" card render, and does its button work?

The escalation shipped for [AG-R-16](../specs5/plan-ag/risks.md#ag-r-16) on
2026-09-12. Twelve unit tests cover it — `systemNotice` returning the action,
`onSystemEvent` putting it on the row, `renderSystemAction` drawing it,
`runSystemAction` calling the RPC and toasting either way, and a click-through
in jsdom. **None of them has seen it in a browser.**

That gap is the one this repo keeps being bitten by. jsdom does no layout and
mounts no app: a card can pass every assertion about its text while rendering
behind something, and a button can pass a synthetic `.click()` while being
`pointer-events: none` under a real cursor. The `aic-dc` history of this is
specific — `specs5/next.md` records that every commit of one week came from
driving the app rather than from reading it, and § C2's fix found that the
toast channel it depended on *had never worked*.

Four things, in a real Chrome against a real server:

  1. A `stop_ignored` report with `revived: true` renders a durable system card
     saying the stop did not land and that something is calling the model.
  2. It carries the **Force restart the engine** button, and the button is
     really clickable — hit-tested at its own centre point, not just present in
     the DOM.
  3. A report with `revived: false` renders the card and **no button**. That is
     the deliberate half: a prose turn finishes on its own, and restarting the
     engine to save a few seconds of text would end a session the user is
     holding (AG-19). A probe that only checked the positive case would pass on
     a build that offered the button always.
  4. Clicking it restarts the engine for real — the button disables itself
     while `restart_session` rebuilds the harness, and a toast says what
     happened.

**What this does not cover, stated so the pass is not read as more than it
is:** the server end. Nothing here makes a turn overrun its stop — that needs a
hostile `Stop` hook in the operator's own `~/.gemini/config/hooks.json`, which
is not a thing a probe should install. The event is dispatched onto the window
channel `app-shell/index.js` re-dispatches every server push onto, so
everything downstream of the server is real and the emit itself is covered by
`tests/test_agy_session.py`.

    .venv/bin/python scripts/stop_ignored_card_probe.py [--repo /tmp/aicdc-uitest]
                                                        [--headless]

Exit 0 when every check passed, 1 on a failure or an uninterpretable run.
"""

from __future__ import annotations

import argparse
import subprocess
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
    wait_for_app,
)

SHOTS = REPO / ".aic-dc" / "live-probe"

#: Read the last system card out of the panel's shadow DOM, and hit-test its
#: button. `elementFromPoint` at the button's own centre is the check that
#: separates "in the DOM" from "clickable": a control covered by an overlay,
#: or with `pointer-events: none`, is present and unusable, and only one of
#: those two facts is visible to jsdom.
CARD_JS = r"""
(() => {
  const p = %s;
  if (!p || !p.shadowRoot) return null;
  const cards = p.shadowRoot.querySelectorAll('.message-card.role-system');
  const card = cards[cards.length - 1];
  if (!card) return {cards: cards.length, card: null};
  const button = card.querySelector('.system-action-button');
  let hit = null;
  if (button) {
    const r = button.getBoundingClientRect();
    const at = p.shadowRoot.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    hit = {
      width: Math.round(r.width),
      height: Math.round(r.height),
      // The element a real click at that point would land on.
      lands_on_button: at === button || (at && button.contains(at)),
      disabled: !!button.disabled,
      label: button.textContent.trim(),
    };
  }
  return {
    cards: cards.length,
    text: card.textContent.replace(/\s+/g, ' ').trim(),
    has_button: !!button,
    button: hit,
  };
})()
"""

TOAST_JS = r"""
(() => {
  const shell = document.querySelector('aic-app-shell');
  const root = shell && shell.shadowRoot;
  if (!root) return [];
  return [...root.querySelectorAll('.toast')].map((t) => t.textContent.trim());
})()
"""


def fire(page: Page, request_id: str, *, seconds: float, revived: bool) -> None:
    """Push one `stop_ignored` onto the channel the server pushes onto.

    `app-shell/index.js` re-dispatches every server push as a window
    `CustomEvent` before any component sees it, so this is the same door the
    real event comes through — the components below it cannot tell the
    difference, which is the point.
    """
    page.eval(
        f"""
        window.dispatchEvent(new CustomEvent('system-event', {{detail: {{
          requestId: {request_id!r},
          data: {{subtype: 'stop_ignored',
                  data: {{seconds: {seconds}, revived: {str(revived).lower()}}}}},
        }}}})) || true
        """
    )
    time.sleep(1.0)


def scratch_repo(path: Path) -> Path:
    """A git repository for the server to open. Contents do not matter here."""
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30)
        (path / "README.md").write_text("probe scratch\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, timeout=30)
        subprocess.run(
            ["git", "-C", str(path), "-c", "user.email=p@p", "-c", "user.name=probe",
             "commit", "-qm", "scratch"],
            check=True, timeout=30,
        )
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/tmp/aicdc-uitest")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument(
        "--bundle",
        action="store_true",
        help="serve the built webapp instead of Vite. Only meaningful once "
        "this change has been built into the bundle; against source it "
        "reports the card missing.",
    )
    args = ap.parse_args()

    repo = scratch_repo(Path(args.repo))
    report = Report()
    # **Vite, not the bundle.** `Backend` defaults to serving the built
    # webapp — the mode that ships — and the first run of this probe reported
    # "no system card rendered at all" against a bundle built before any of
    # this existed. The card was fine; the server was serving last week's
    # JavaScript. Anything asserting on webapp source has to say `dev=True`,
    # and the failure it prevents looks exactly like the feature being broken.
    backend = Backend(repo, dev=not args.bundle)
    browser = None
    try:
        browser = Browser(backend.url, headless=args.headless)
        page = browser.page
        wait_for_app(page)
        print("  app is up")

        # --- 1 & 2: the overridden stop, with its escalation ------------
        fire(page, "probe-revived", seconds=31.4, revived=True)
        card = page.eval(CARD_JS % PANEL_JS)
        shot = page.shot(SHOTS / "stop-ignored-revived.png")
        if not card or not card.get("card", True) or not card.get("text"):
            report.void("no system card rendered at all; nothing below can be read")
            return report.verdict()

        text = card["text"]
        report.add(
            "the card says the stop did not land",
            "stopped this turn 31s ago" in text,
            text[:110],
            shot,
        )
        report.add(
            "and says money is being spent now",
            "calling the model repeatedly" in text,
            "names the override rather than only the delay",
            shot,
        )
        button = card.get("button") or {}
        report.add(
            "the force-restart button is there",
            bool(card.get("has_button")) and button.get("label") == "Force restart the engine",
            f"label={button.get('label')!r}",
            shot,
        )
        report.add(
            "and a real click would land on it",
            bool(button.get("lands_on_button")) and (button.get("width") or 0) > 0,
            f"{button.get('width')}x{button.get('height')}px, "
            f"hit={button.get('lands_on_button')}",
            shot,
        )

        # --- 3: the case that must NOT offer it -------------------------
        fire(page, "probe-prose", seconds=12.0, revived=False)
        quiet = page.eval(CARD_JS % PANEL_JS)
        quiet_shot = page.shot(SHOTS / "stop-ignored-prose.png")
        report.add(
            "a prose turn gets the card",
            "finish on its own" in (quiet.get("text") or ""),
            (quiet.get("text") or "")[:110],
            quiet_shot,
        )
        report.add(
            "and is offered no restart",
            not quiet.get("has_button"),
            "restarting to save a few seconds of text is not a trade to push",
            quiet_shot,
        )

        # --- 4: the button actually does something ----------------------
        # Re-fire the revived case so the button is the last card again.
        fire(page, "probe-revived-2", seconds=44.0, revived=True)
        # Disabled *while* it runs is the property that stops a second press
        # tearing down the session the first one is rebuilding — so it has to
        # be observed *during* the call, not sampled near it. The first cut
        # slept 200ms and read the button already back: the restart had
        # finished, the state had existed, and the probe reported a defect
        # that was its own clock. `updateComplete` makes it deterministic —
        # it resolves on the render that follows the click, which is after
        # `runSystemAction` has set the flag and long before a network round
        # trip can clear it.
        mid = page.eval(
            f"""
            (async () => {{
              const p = {PANEL_JS};
              const cards = p.shadowRoot.querySelectorAll('.message-card.role-system');
              const b = cards[cards.length - 1].querySelector('.system-action-button');
              b.click();
              await p.updateComplete;
              const after = p.shadowRoot.querySelectorAll('.system-action-button');
              const live = after[after.length - 1];
              return {{
                disabled: !!(live && live.disabled),
                label: live ? live.textContent.trim() : null,
                pending: p._systemActionPending || null,
              }};
            }})()
            """,
            await_promise=True,
        )
        report.add(
            "it disables itself while the restart runs",
            bool(mid.get("disabled")),
            f"label={mid.get('label')!r} pending={mid.get('pending')!r}",
        )

        toasts: list[str] = []
        deadline = time.time() + 60
        while time.time() < deadline:
            toasts = page.eval(TOAST_JS) or []
            if any("restart" in t.lower() for t in toasts):
                break
            time.sleep(0.5)
        click_shot = page.shot(SHOTS / "stop-ignored-clicked.png")
        report.add(
            "and says out loud what happened",
            any("restart" in t.lower() for t in toasts),
            f"toasts={toasts}",
            click_shot,
        )
        page.report_console("after the run")
        return report.verdict()
    finally:
        if browser is not None:
            browser.stop()
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
