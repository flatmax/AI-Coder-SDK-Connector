#!/usr/bin/env python3
"""Support for probes that drive the real app against a real engine.

Two of `specs5/next.md` § D's items cannot be verified any other way. Both
name a mechanism that only exists once a browser, our server, and the Claude
Code CLI are all running at once:

  - **D1** — ⏹ Stop on a live subagent tab, and the amber LED that must follow
    it. The webapp's own button is the only caller of `stop_task` that goes
    through the confirmation, the RPC, and the LED derivation; an agent calling
    `TaskStop` on a background task proved that in the worst way, killing a
    live subagent while the CLI emitted no terminal task message at all.
  - **D4** — the cost chip's two exceptional renderings. Both hold in 60 unit
    tests against synthetic payloads. Neither has been seen on a turn the
    engine priced.

jsdom cannot answer either: it does no layout, it has no engine, and a
hand-built payload is a restatement of the assumption under test. So this
module owns a whole live rig — backend, Chrome, recorder — and every probe
built on it asserts on values read back out of it.

Three things it inherits from the probes already in this directory, and the
reasons are theirs:

  - **Plain CDP over one WebSocket** (`permission_mode_load_probe.py`), not
    `chrome-devtools-mcp`: that server holds one Chrome profile at a time, and
    a probe that fights the session the operator is using is a probe that
    reports the fight.
  - **Measure, do not eyeball** (`layout_probe.py`): every check asserts on a
    number or a string read out of a real browser. Screenshots go to files as
    evidence for a human reading a failure; they are never the assertion.
  - **Raise rather than warn** (`_agy_probe_support.py`): when a precondition
    would void every assertion downstream, this module raises. A probe that
    carries on past a dead engine reports absences it created.

The recorder is the one piece that is new, and it is cheap because of a shape
the app already has: `app-shell/index.js` re-dispatches every server push as a
window `CustomEvent` before any component sees it. So a document-start script
with `window.addEventListener` sees the engine's whole event stream without
patching a single instance — and because it is installed with
`Page.addScriptToEvaluateOnNewDocument`, it is listening before the first
module evaluates.

Not a probe. Run `turn_cost_probe.py` (D4) or `subagent_stop_probe.py` (D1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from base64 import b64decode
from pathlib import Path

import requests
import websockets.sync.client as ws_client

REPO = Path(__file__).resolve().parent.parent
VENV_AIC_DC = REPO / ".venv" / "bin" / "aic-dc"

CHROME_CANDIDATES = (
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# `main.py` logs this under `--no-browser`, and it is the only place both real
# ports appear together. Read rather than assumed: `--server-port` and
# `--webapp-port` are requests, and a probe that navigated to the number it
# asked for would eventually drive somebody else's server.
URL_LINE = re.compile(r"Webapp URL: (http://localhost:(\d+)/\?port=(\d+))")

# Where the recorder keeps what it saw. `sessionStorage`, not a JS variable, so
# a reload does not erase the record of the load that produced it.
RECORD_KEY = "__aicdc_live_probe"

# Installed with `Page.addScriptToEvaluateOnNewDocument`: it runs ahead of every
# application module, on the first load and on every reload.
#
# `window.confirm` is replaced rather than handled over CDP for the reason
# `permission_mode_load_probe.py` found: a native dialog that the harness
# answers can be re-surfaced by the harness, and then the harness is in the
# finding. Replacing it means no native dialog is ever created. The default
# answer is **false**, so a probe that forgets to opt in cannot stop, grant, or
# destroy anything by accident — see `answer_confirm`.
RECORDER = r"""
(() => {
  const KEY = '__aicdc_live_probe';
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
  window.__probeRec = rec;

  rec('doc-start', { url: String(location.href).slice(0, 200) });

  // Answer for every confirmation, read at call time so a probe can flip it
  // between scenarios. Default false: refusing is the only answer that cannot
  // spend money or end a turn.
  window.__probeConfirm = false;
  window.confirm = function (msg) {
    const answer = !!window.__probeConfirm;
    rec('confirm', { msg: String(msg).slice(0, 200), answer: answer });
    return answer;
  };

  // Every server push, as the shell re-dispatches it. Payloads are trimmed to
  // the fields these probes assert on: a whole `streamComplete` result carries
  // the turn's transcript, and 30 of those would blow `sessionStorage`'s quota
  // and take the record with it.
  const keep = (o, names) => {
    const out = {};
    if (!o || typeof o !== 'object') return out;
    for (const n of names) if (o[n] !== undefined) out[n] = o[n];
    return out;
  };

  addEventListener('stream-complete', (ev) => {
    const r = (ev.detail && ev.detail.result) || {};
    rec('stream-complete', {
      requestId: ev.detail && ev.detail.requestId,
      result: keep(r, [
        'turn_cost_basis', 'turn_cost_usd', 'total_cost_usd', 'is_error',
        'num_turns', 'duration_ms', 'subtype', 'session_id', 'tool_calls',
        'permission_prompts', 'mirror_gap',
      ]),
      // Named separately: whether the turn reported per-model usage at all is
      // the question, not what was in it. `cost.py` wipes the snapshot on a
      // reset, and an empty map is the observable.
      turn_models: Object.keys(r.turn_model_usage || {}),
      session_models: Object.keys(r.model_usage || {}),
    });
  });

  addEventListener('subagent-event', (ev) => {
    const d = (ev.detail && ev.detail.data) || {};
    rec('subagent-event', keep(d, [
      'type', 'task_id', 'agent_id', 'tool_use_id', 'description',
      'task_type', 'subagent_type', 'status', 'terminal', 'last_tool_name',
    ]));
  });

  addEventListener('session-started', (ev) => {
    const d = (ev.detail && ev.detail.data) || {};
    rec('session-started', keep(d, ['session_id', 'model', 'permission_mode']));
  });

  // A permission dialog on a probe run is a stall, not a finding: nothing here
  // answers one, so the turn would sit at the gate until the timeout. Recorded
  // so the timeout can say which it was.
  addEventListener('permission-request', (ev) => {
    const d = (ev.detail && ev.detail.data) || ev.detail || {};
    rec('permission-request', keep(d, ['tool_name', 'request_id', 'category']));
  });

  addEventListener('engine-health', (ev) => {
    const d = (ev.detail && ev.detail.data) || ev.detail || {};
    rec('engine-health', keep(d, ['connected', 'last_error', 'mirror_gap']));
  });

  addEventListener('system-event', (ev) => {
    const d = (ev.detail && ev.detail.data) || {};
    rec('system-event', keep(d, ['kind', 'level', 'message']));
  });
})();
"""

# The chat panel, by walk rather than by path. It sits at shadow-DOM depth 2
# today; the walk survives a re-nesting, which a hard-coded chain would not.
PANEL_JS = r"""
(() => {
  const walk = (root, depth) => {
    if (!root || depth > 8 || !root.querySelectorAll) return null;
    const hit = root.querySelector('aic-chat-panel');
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


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

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


class Backend:
    """One `aic-dc` server, on ports nobody else is using.

    Launched into its own process group. That is not tidiness: the server
    spawns the CLI as a child and `main.py` exits through `os._exit`, so the
    tree has to be signalled as a tree or the `claude` process outlives the
    probe holding the scratch repo as its cwd (measured at ~38s in
    `_kill_cli_children`'s own notes).

    Serving mode defaults to the bundled webapp — the mode that ships. `dev=True`
    runs Vite instead, which is the right choice while editing the webapp and
    the wrong one during a subagent run: a save triggers a reload, and a reload
    drops every live subagent tab.
    """

    def __init__(self, repo_path: Path, *, dev: bool = False, extra: tuple[str, ...] = ()):
        if not VENV_AIC_DC.is_file():
            raise RuntimeError(f"no aic-dc at {VENV_AIC_DC} — build the venv first")
        if not (repo_path / ".git").is_dir():
            raise RuntimeError(f"{repo_path} is not a git repository")
        self.repo_path = repo_path
        self.log = Path(tempfile.mkstemp(prefix="live-probe-backend-", suffix=".log")[1])
        self._handle = self.log.open("wb")
        cmd = [
            str(VENV_AIC_DC),
            "--repo-path", str(repo_path),
            "--server-port", str(free_port()),
            "--webapp-port", str(free_port()),
            "--no-browser",
            "--verbose",
        ]
        if dev:
            cmd.append("--dev")
        cmd.extend(extra)
        print(f"starting backend: {' '.join(cmd)}")
        print(f"  log: {self.log}")
        self.proc = subprocess.Popen(
            cmd, cwd=REPO, stdout=self._handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.url, self.webapp_port, self.server_port = self._await_url()
        print(f"  webapp: {self.url}")

    def _await_url(self, timeout: float = 180.0) -> tuple[str, int, int]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = self.log.read_text(errors="replace")[-3000:]
                raise RuntimeError(f"backend exited early ({self.proc.returncode}):\n{tail}")
            found = URL_LINE.search(self.log.read_text(errors="replace"))
            if found:
                return found.group(1), int(found.group(2)), int(found.group(3))
            time.sleep(0.5)
        tail = self.log.read_text(errors="replace")[-3000:]
        raise RuntimeError(f"backend never logged a webapp URL:\n{tail}")

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])

    def descendants(self) -> list[tuple[int, str]]:
        """Every process under this backend, `(pid, cmdline)`, by /proc walk.

        By parent pid rather than by process group. The first version of this
        used `pgrep -g` and found nothing at all while a turn was streaming —
        the CLI child is not in the server's process group, so a group query
        answers "no engine here" about an engine that is right there. A ppid
        walk does not care how the child was spawned.
        """
        parents: dict[int, int] = {}
        cmdlines: dict[int, str] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat = (entry / "stat").read_text()
                cmdlines[pid] = (entry / "cmdline").read_bytes().decode(
                    errors="replace"
                ).replace("\0", " ").strip()
            except OSError:
                continue
            # The comm field is parenthesised and may contain spaces, so the
            # ppid is read from after the closing paren rather than by index.
            try:
                parents[pid] = int(stat[stat.rindex(")") + 1:].split()[1])
            except (ValueError, IndexError):
                continue
        out: list[tuple[int, str]] = []
        frontier = [self.proc.pid]
        seen = {self.proc.pid}
        while frontier:
            parent = frontier.pop()
            for pid, ppid in parents.items():
                if ppid == parent and pid not in seen:
                    seen.add(pid)
                    frontier.append(pid)
                    out.append((pid, cmdlines.get(pid, "")))
        return out

    def cli_children(self) -> list[int]:
        """The live Claude Code CLI process(es) under this backend.

        Matched on the executable, not on the command line. Matching the whole
        line found nothing at all: the SDK launches the CLI with an inline
        `--mcp-config` naming our own MCP server, so every real CLI process has
        the string `aic-dc` in its arguments and an "and not aic-dc" clause
        meant to skip the server skipped the engine instead.

        Deliberately not the SDK's private `_ACTIVE_CHILDREN` registry: that
        lives in the backend's address space, and this is a different process.
        """
        found = []
        for pid, cmd in self.descendants():
            # argv[0] is the binary; argv[1] covers a `node …/cli.js` form.
            names = [Path(token).name for token in cmd.split()[:2]]
            if any(n == "claude" or n == "cli.js" for n in names):
                found.append(pid)
        return found

    def stop(self) -> None:
        self._handle.close()
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()


class Page:
    """Just enough CDP to drive one page, and it keeps the console.

    Lifted from `layout_probe.py`, which earned the console the hard way: a
    module that failed to parse showed up as "the page never became ready",
    which is true, useless, and a sitting's worth of guessing away from the
    error Chrome had already printed.
    """

    def __init__(self, url: str):
        self._ws = ws_client.connect(url, max_size=64 * 1024 * 1024)
        self._id = 0
        self.console: list[str] = []

    def _note(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text")
            self.console.append(f"exception: {text}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning"):
            args = " ".join(
                str(a.get("value", a.get("description", "")))
                for a in params.get("args", [])
            )
            self.console.append(f"console.{params['type']}: {args}")

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
            self._note(msg)

    def report_console(self, label: str) -> None:
        if not self.console:
            print(f"  ({label}: the page logged nothing)")
            return
        print(f"  what the page said ({label}):")
        for line in self.console[:20]:
            print(f"    {line}")
        if len(self.console) > 20:
            print(f"    … and {len(self.console) - 20} more")

    def eval(self, expr: str, await_promise: bool = False):
        res = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if res.get("exceptionDetails"):
            details = res["exceptionDetails"]
            text = details.get("exception", {}).get("description") \
                or details.get("text", "eval failed")
            raise RuntimeError(text)
        return res.get("result", {}).get("value")

    def shot(self, path: Path) -> Path:
        res = self.send("Page.captureScreenshot", format="png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b64decode(res["data"]))
        return path

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


class Browser:
    """Our own Chrome, on a scratch profile, with the recorder installed."""

    def __init__(self, url: str, *, headless: bool = False, window: str = "1500,1000"):
        port = free_port()
        self.profile = tempfile.mkdtemp(prefix="live-probe-chrome-")
        cmd = [
            find_chrome(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            f"--window-size={window}",
        ]
        if headless:
            cmd.append("--headless=new")
        cmd.append("about:blank")
        print(f"launching chrome on debug port {port}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        target = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.5)
        if not target:
            self.stop()
            raise RuntimeError("chrome never exposed a page target")

        self.page = Page(target)
        self.page.send("Page.enable")
        self.page.send("Runtime.enable")
        self.page.send("Page.addScriptToEvaluateOnNewDocument", source=RECORDER)
        print(f"navigating to {url}")
        self.page.send("Page.navigate", url=url)

    def stop(self) -> None:
        page = getattr(self, "page", None)
        if page:
            page.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------

def records(page: Page, kind: str | None = None) -> list[dict]:
    raw = page.eval(f"sessionStorage.getItem({RECORD_KEY!r})")
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [r for r in rows if kind is None or r.get("kind") == kind]


def answer_confirm(page: Page, answer: bool) -> None:
    """Set what the next `window.confirm` returns.

    Explicit at every call site on purpose. The recorder's default is false,
    and a probe that wants a destructive gesture to go through has to say so
    on the line before it makes the gesture.
    """
    page.eval(f"window.__probeConfirm = {'true' if answer else 'false'}")


def note(page: Page, text: str) -> None:
    """Write a marker into the record, so a reader can see the phases."""
    page.eval(f"window.__probeRec && window.__probeRec('note', {{text: {text!r}}})")


# ---------------------------------------------------------------------------
# Driving the app
# ---------------------------------------------------------------------------

def wait_for_app(page: Page, timeout: float = 90.0) -> None:
    """Wait until the panel is mounted and the RPC is up.

    Both, not just the mount. `input.js`'s `send` returns silently when
    `panel.rpcConnected` is false, so typing into a mounted-but-unconnected
    panel is a keystroke that goes nowhere and a probe that waits out its whole
    turn timeout for no stated reason.

    Raises rather than returning a flag: nothing downstream of this can mean
    anything without it.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = page.eval(f"""
              (() => {{
                const p = {PANEL_JS};
                if (!p) return 'no panel';
                if (!p.shadowRoot) return 'no shadow root';
                if (!p.rpcConnected) return 'rpc not connected';
                if (!p.shadowRoot.querySelector('.input-textarea')) return 'no composer';
                return 'ready';
              }})()
            """)
            if last == "ready":
                return
        except RuntimeError as exc:
            last = f"eval failed: {exc}"
        time.sleep(1.0)
    page.report_console("waiting for the app")
    raise RuntimeError(f"the app never became ready (last: {last})")


def send_turn(page: Page, text: str) -> None:
    """Type a prompt and press Enter, the way a user does.

    Through the composer rather than through `chat_streaming` directly, and
    that is the whole point of these probes: the RPC path is already covered
    by pytest. What is not covered is the composer's own gate, the slash
    router in front of it, and the state the panel puts itself into — which is
    what every assertion downstream reads.

    `send()` reads `panel._input`, not the textarea's value, so the `input`
    event is load-bearing: it is what writes the property.
    """
    outcome = page.eval("""
      (() => {
        const p = %s;
        if (!p) return 'no panel';
        if (p._streaming) return 'already streaming';
        const ta = p.shadowRoot.querySelector('.input-textarea');
        if (!ta) return 'no composer';
        ta.focus();
        ta.value = %s;
        ta.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
        ta.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', bubbles: true, composed: true, cancelable: true,
        }));
        return 'sent';
      })()
    """ % (PANEL_JS, json.dumps(text)))
    if outcome != "sent":
        raise RuntimeError(f"could not send {text!r}: {outcome}")


def wait_for_turn(page: Page, *, timeout: float = 300.0, poll: float = 1.0) -> dict:
    """Wait for the turn in flight to finish, and return its recorded result.

    Waits on the *recorded* `stream-complete` rather than on `panel._streaming`
    going false: the record is what the assertions read, and a turn that ends
    without one is a different failure from a turn that never ends.

    A permission request during the wait is a stall, not a finding — nothing
    here answers one — so it aborts immediately with the tool that asked,
    instead of burning the timeout.
    """
    before = len(records(page, "stream-complete"))
    gated = len(records(page, "permission-request"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = records(page, "stream-complete")
        if len(done) > before:
            return done[-1]
        asked = records(page, "permission-request")
        if len(asked) > gated:
            tools = [r.get("tool_name") for r in asked[gated:]]
            raise RuntimeError(
                f"the turn stopped at a permission gate ({tools}) — this probe "
                "answers none, so nothing past here would run. Use tools that "
                "`permissions.py` leaves ungated, or set the mode explicitly."
            )
        time.sleep(poll)
    raise RuntimeError(f"no stream-complete within {timeout:.0f}s")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    """Every check's verdict, and the file that shows it.

    `uninterpretable` is separate from `failures` on purpose. A probe whose
    positive control did not fire has not found that the thing under test is
    broken — it has found that it cannot tell, and reporting that as a pass or
    a failure would both be lies.
    """

    def __init__(self):
        self.rows: list[tuple[str, bool, str, Path | None]] = []
        self.uninterpretable: list[str] = []

    def add(self, name: str, ok: bool, detail: str, shot: Path | None = None) -> bool:
        self.rows.append((name, ok, detail, shot))
        mark = "PASS" if ok else "FAIL"
        where = f"  [{shot.name}]" if shot else ""
        print(f"  {mark}  {name} — {detail}{where}")
        return ok

    def skip(self, name: str, why: str) -> None:
        """A check that did not run, said out loud.

        Never silent: a skipped scenario that prints nothing reads as coverage
        in a run summary, which is how a green suite comes to mean less than it
        looks like it means.
        """
        print(f"  SKIP  {name} — {why}")
        self.rows.append((name, True, f"skipped: {why}", None))

    def void(self, why: str) -> None:
        self.uninterpretable.append(why)
        print(f"  VOID  {why}")

    @property
    def failures(self) -> int:
        return sum(1 for _, ok, _, _ in self.rows if not ok)

    def verdict(self) -> int:
        print("\n" + "=" * 70)
        if self.uninterpretable:
            print("VERDICT: UNINTERPRETABLE")
            for why in self.uninterpretable:
                print(f"  - {why}")
            print("  Nothing above should be read as evidence.")
            return 1
        if self.failures:
            print(f"VERDICT: {self.failures} of {len(self.rows)} checks FAILED")
            for name, ok, detail, _ in self.rows:
                if not ok:
                    print(f"  - {name}: {detail}")
            return 1
        print(f"VERDICT: all {len(self.rows)} checks passed")
        return 0


def dump(page: Page, kinds: tuple[str, ...] = ()) -> None:
    """Print the record, for a human reading a failure."""
    rows = records(page)
    print("\n" + "-" * 70)
    print(f"recorder: {len(rows)} entries")
    print("-" * 70)
    for r in rows:
        kind = r.get("kind")
        if kinds and kind not in kinds:
            continue
        if kind == "note":
            print(f"  --- {r.get('text')}")
        elif kind == "stream-complete":
            res = r.get("result", {})
            print(f"  stream-complete basis={res.get('turn_cost_basis')!r} "
                  f"turn={res.get('turn_cost_usd')!r} total={res.get('total_cost_usd')!r} "
                  f"is_error={res.get('is_error')!r} num_turns={res.get('num_turns')!r} "
                  f"turn_models={r.get('turn_models')} session_models={r.get('session_models')}")
        elif kind == "subagent-event":
            print(f"  subagent-event {r.get('type')} task={r.get('task_id')} "
                  f"status={r.get('status')!r} terminal={r.get('terminal')!r} "
                  f"type={r.get('subagent_type')!r} desc={str(r.get('description'))[:40]!r}")
        elif kind == "confirm":
            print(f"  confirm {r.get('msg')!r} → {r.get('answer')}")
        else:
            body = {k: v for k, v in r.items() if k not in ("kind", "t")}
            print(f"  {kind} {body}")
