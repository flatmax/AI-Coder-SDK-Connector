#!/usr/bin/env python3
"""Does a page load raise the bypass-permissions confirmation?

The symptom was reported from a live run: the `Bypass permissions?` confirm
appearing on page load, over a selector still reading "Ask". It did not
reproduce on the dev backend on 2026-08-25, and the culprit found there was the
*harness* — chrome-devtools-mcp re-surfacing a native `confirm` it had already
handled, once per navigation. Guards went in anyway (a gesture latch plus
`autocomplete="off"`), because a `change` here authorises a destructive
confirmation and a browser restoring form state can raise one. The fix list
that tracked this closed on 2026-08-26; the standing recipe is
`specs5/0-overview/implementation-guide.md` § Verifying UI Work Against a
Running Engine.

This probe exists because that investigation could not separate two things:

  1. Whether the *app* ever raises the confirmation on load.
  2. Whether the *harness* invents one.

It separates them by owning the browser outright. Chrome is launched on a
scratch profile with its own debugging port, and `window.confirm` is replaced
**before any app script runs** — so no native dialog is ever created, and
nothing downstream can re-surface one. Every call is recorded with the stack
that made it, which is what answers "the mechanism is something else" directly
rather than by elimination.

Three scenarios, and the third is the one that makes the other two mean
anything:

  A. **Plain reloads.** Does a confirmation appear with nobody touching the
     control? This is the reported symptom.
  B. **Restoration bait.** Park the select's DOM value on `bypassPermissions`
     with no gesture behind it, then reload. If Chrome restores form state into
     a `change`, this is the shape that would show it.
  C. **Positive control.** Drive a real gesture and a real `change`. The
     confirmation *must* appear here. A run where A and B are clean and C is
     also clean has proved nothing except that the recorder is broken.

Usage:
    .venv/bin/python scripts/permission_mode_load_probe.py \
        --url 'http://localhost:19110/?port=18190' [--reloads 4] [--headless]

Exit status is 0 when the run is interpretable (C fired), 1 when it is not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websockets.sync.client as ws_client

CHROME_CANDIDATES = (
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# Installed with `Page.addScriptToEvaluateOnNewDocument`, so it runs before any
# application script on the initial load *and* on every reload. Records into
# `sessionStorage`, which survives a reload in the same tab; that is what lets a
# page-load fault be read after the load that produced it is gone.
RECORDER = r"""
(() => {
  const KEY = '__probe6c';
  const load = () => {
    try { return JSON.parse(sessionStorage.getItem(KEY)) || []; }
    catch (_) { return []; }
  };
  const save = (r) => { try { sessionStorage.setItem(KEY, JSON.stringify(r)); } catch (_) {} };
  const rec = (kind, data) => {
    const r = load();
    r.push(Object.assign({ kind: kind, t: Date.now() }, data));
    save(r);
  };
  const stack = (n) => (new Error().stack || '').split('\n').slice(2, 2 + n)
    .map((s) => s.trim()).join(' | ');

  rec('doc-start', { nav: performance.navigation ? performance.navigation.type : null });

  // 1. No native dialog is ever created, so nothing can re-surface one later.
  //    Returning false means even a real confirmation never grants anything:
  //    this probe cannot change the engine's mode by accident.
  window.confirm = function (msg) {
    rec('confirm', { msg: String(msg).slice(0, 160), stack: stack(10) });
    return false;
  };

  // 2. `change` is composed:false, so it never leaves the shadow root and a
  //    window-level listener cannot see it. Wrapping the registration is how we
  //    observe events delivered to the app's own handler, including any that
  //    arrive before we could have queried for the element.
  //
  //    Both listener forms have to be handled. Lit's EventPart registers the
  //    part *object* rather than a closure — `addEventListener(type, this)`,
  //    dispatched through `handleEvent` — so a wrapper that only accepts
  //    functions silently declines to wrap the one listener this probe exists
  //    to watch. The first run of this probe did exactly that: the positive
  //    control's confirmation fired with no `change` recorded in front of it,
  //    and the stack (`Bu.handleEvent`) is what gave it away.
  const realAdd = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    const isFn = typeof fn === 'function';
    const isObj = fn && typeof fn === 'object' && typeof fn.handleEvent === 'function';
    if (type !== 'change' || (!isFn && !isObj)) {
      return realAdd.call(this, type, fn, opts);
    }
    const self = this;
    const tag = self && self.tagName ? self.tagName.toLowerCase() : String(self);
    const cls = self && self.className ? String(self.className).slice(0, 60) : '';
    const note = (ev) => rec('change', {
      tag: tag,
      cls: cls,
      value: ev && ev.target ? ev.target.value : null,
      isTrusted: ev ? ev.isTrusted : null,
      form: isFn ? 'function' : 'handleEvent',
      stack: stack(6),
    });
    if (isFn) {
      return realAdd.call(self, type, function (ev) {
        note(ev);
        return fn.apply(this, arguments);
      }, opts);
    }
    return realAdd.call(self, type, {
      handleEvent: (ev) => {
        note(ev);
        return fn.handleEvent(ev);
      },
    }, opts);
  };

  // 3. What the control actually reads a few seconds in, once the engine has
  //    had a chance to broadcast a mode.
  const snapshot = () => {
    const found = [];
    const walk = (root, depth) => {
      if (!root || depth > 8 || !root.querySelectorAll) return;
      root.querySelectorAll('select.permission-mode-select').forEach((s) => {
        found.push({
          value: s.value,
          selectedIndex: s.selectedIndex,
          options: s.options.length,
          disabled: s.disabled,
          autocomplete: s.getAttribute('autocomplete'),
        });
      });
      root.querySelectorAll('*').forEach((el) => {
        if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      });
    };
    walk(document, 0);
    rec('snapshot', { selects: found });
  };
  addEventListener('load', () => setTimeout(snapshot, 4500));
})();
"""

# Walks the shadow roots to reach the selector. `aic-chat-panel` sits at depth 2,
# but the walk is generic so a re-nesting does not break it.
FIND_SELECT = r"""
(() => {
  const walk = (root, depth) => {
    if (!root || depth > 8 || !root.querySelectorAll) return null;
    const hit = root.querySelector('select.permission-mode-select');
    if (hit) return hit;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const r = walk(el.shadowRoot, depth + 1);
        if (r) return r;
      }
    }
    return null;
  };
  return walk(document, 0);
})()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if not found:
        sys.exit("no Chrome binary found")
    return found


class Page:
    """Just enough CDP to drive one page over its own WebSocket."""

    def __init__(self, url: str):
        self._ws = ws_client.connect(url, max_size=32 * 1024 * 1024)
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        want = self._id
        self._ws.send(json.dumps({"id": want, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # Events are not interesting here; the recorder is the record.

    def eval(self, expr: str, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(res["exceptionDetails"].get("text", "eval failed"))
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def read_records(page: Page) -> list[dict]:
    raw = page.eval("sessionStorage.getItem('__probe6c')")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def wait_for_app(page: Page, timeout: float = 40.0) -> bool:
    """Wait until the shell has mounted and the selector exists."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.eval(f"!!({FIND_SELECT})"):
                return True
        except RuntimeError:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="webapp URL including ?port=")
    ap.add_argument("--reloads", type=int, default=4)
    ap.add_argument("--headless", action="store_true",
                    help="less faithful for a form-restoration question; headed by default")
    args = ap.parse_args()

    port = free_port()
    profile = tempfile.mkdtemp(prefix="6c-chrome-")
    cmd = [
        find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--window-size=1400,900",
    ]
    if args.headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    print(f"launching chrome on debug port {port} (profile {profile})")
    chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        target_url = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    target_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.5)
        if not target_url:
            return fail("chrome never exposed a page target", chrome)

        page = Page(target_url)
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Page.addScriptToEvaluateOnNewDocument", source=RECORDER)

        print(f"navigating to {args.url}")
        page.send("Page.navigate", url=args.url)
        if not wait_for_app(page):
            return fail("the permission-mode selector never appeared", chrome)
        print("app mounted; letting the engine settle")
        time.sleep(6)

        # ---- Scenario A: plain reloads, nobody touching anything.
        print(f"\n[A] {args.reloads} plain reloads")
        for i in range(args.reloads):
            page.send("Page.reload")
            wait_for_app(page)
            time.sleep(5)
            print(f"    reload {i + 1}/{args.reloads} done")
        after_a = read_records(page)

        # ---- Scenario B: park the value on bypass with no gesture, then reload.
        print("\n[B] restoration bait: park the DOM value on bypassPermissions, reload")
        parked = page.eval(f"""
          (() => {{
            const s = {FIND_SELECT};
            if (!s) return 'no select';
            s.value = 'bypassPermissions';
            return s.value;
          }})()
        """)
        print(f"    parked value: {parked!r}")
        for i in range(2):
            page.send("Page.reload")
            wait_for_app(page)
            time.sleep(5)
            print(f"    reload {i + 1}/2 done")
        after_b = read_records(page)

        # ---- Scenario C: positive control. This MUST produce a confirmation.
        print("\n[C] positive control: real gesture + real change")
        control = page.eval(f"""
          (() => {{
            const s = {FIND_SELECT};
            if (!s) return 'no select';
            if (s.disabled) return 'select disabled';
            s.dispatchEvent(new PointerEvent('pointerdown', {{bubbles: true}}));
            s.value = 'bypassPermissions';
            s.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'dispatched';
          }})()
        """)
        print(f"    {control}")
        time.sleep(2)
        after_c = read_records(page)

        # ---- Scenario D: is `autocomplete="off"` load-bearing, or is Chrome
        # simply declining to restore a dynamically-created shadow-DOM select?
        # B cannot tell those apart, and the difference decides whether the
        # attribute is a guard or a decoration.
        print("\n[D] strip autocomplete=off, park on bypass again, reload")
        stripped = page.eval(f"""
          (() => {{
            const s = {FIND_SELECT};
            if (!s) return 'no select';
            s.removeAttribute('autocomplete');
            s.value = 'bypassPermissions';
            return s.getAttribute('autocomplete') + '/' + s.value;
          }})()
        """)
        print(f"    autocomplete/value now: {stripped!r}")
        for i in range(2):
            page.send("Page.reload")
            wait_for_app(page)
            time.sleep(5)
            print(f"    reload {i + 1}/2 done")
        records = read_records(page)

        report(records, len(after_a), len(after_b), len(after_c))

        confirms = [r for r in records if r["kind"] == "confirm"]
        changes = [r for r in records if r["kind"] == "change"]
        # Both arms must fire in C, not just the one the verdict reads. A dead
        # change-recorder still lets the confirm arm answer the headline
        # question, but it does so while quietly reporting "no phantom change
        # events" for a mechanism it was never watching — which reads as
        # evidence and is not.
        if not confirms:
            print("\nVERDICT: UNINTERPRETABLE — the positive control raised no "
                  "confirmation, so a clean A and B prove nothing.")
            return 1
        if not changes:
            print("\nVERDICT: UNINTERPRETABLE — the confirm arm works but the "
                  "change arm recorded nothing even under the positive control, "
                  "so 'no phantom change' is an untested claim rather than a "
                  "finding. Fix the listener wrapper before reading this run.")
            return 1

        # Only A and B are "on load with the shipped guards in place". C is
        # driven on purpose, and D deliberately removes a guard.
        load_confirms = [r for r in confirms if records.index(r) < len(after_b)]
        d_confirms = [r for r in confirms if records.index(r) >= len(after_c)]
        d_changes = [r for r in changes if records.index(r) >= len(after_c)]
        print()
        if load_confirms:
            print(f"VERDICT: REPRODUCED — {len(load_confirms)} confirmation(s) on "
                  "load. 6c is real and the stacks above name the caller.")
        else:
            print("VERDICT: NOT REPRODUCED on load, and the recorder is proven "
                  "live by the positive control.")
        if d_confirms or d_changes:
            print("  autocomplete=off is LOAD-BEARING: stripping it let the "
                  f"reload raise {len(d_changes)} change(s) and "
                  f"{len(d_confirms)} confirmation(s).")
        else:
            print("  autocomplete=off is NOT what suppresses restoration here: "
                  "with it stripped, the reload still raised nothing. Chrome is "
                  "declining to restore this control for another reason, so the "
                  "gesture latch is the guard actually carrying 6c.")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def fail(msg: str, chrome) -> int:
    print(f"FAILED: {msg}", file=sys.stderr)
    return 1


def report(records: list[dict], a_end: int, b_end: int, c_end: int) -> None:
    print("\n" + "=" * 68)
    print(f"{len(records)} records")
    print("=" * 68)
    for i, r in enumerate(records):
        if i < a_end:
            phase = "A"
        elif i < b_end:
            phase = "B"
        elif i < c_end:
            phase = "C"
        else:
            phase = "D"
        kind = r["kind"]
        if kind == "doc-start":
            print(f"[{phase}] document start")
        elif kind == "confirm":
            print(f"[{phase}] *** CONFIRM: {r['msg']!r}")
            print(f"          stack: {r['stack']}")
        elif kind == "change":
            print(f"[{phase}] change on {r['tag']}.{r['cls']} "
                  f"value={r['value']!r} isTrusted={r['isTrusted']}")
            print(f"          stack: {r['stack']}")
        elif kind == "snapshot":
            for s in r["selects"]:
                print(f"[{phase}] selector reads {s['value']!r} "
                      f"(idx={s['selectedIndex']}/{s['options']}, "
                      f"disabled={s['disabled']}, autocomplete={s['autocomplete']!r})")
            if not r["selects"]:
                print(f"[{phase}] snapshot found no selector")


if __name__ == "__main__":
    sys.exit(main())
