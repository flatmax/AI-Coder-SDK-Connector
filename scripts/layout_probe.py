#!/usr/bin/env python3
"""Measure the layouts no unit test can see, and write the pictures beside them.

specs5/next.md § D2 has three rendering behaviours with no test and no way
to get one from jsdom, and all three are the same shape: the CSS rule
arrives — which is what the unit suite asserts — and nothing measures what
it lays out. The permission dialog's Monaco style clone, the tool-card
header's two-column grid (read by hand at 520, 400 and 300px, twice), and
the ~570px of empty editor the dialog drew for a one-line diff in front of
60 passing dialog tests.

§ B1's residue joined them: whether five usage-HUD sections and their heads
fit 300px on a real screen. Its collapse behaviour *is* asserted by the unit
suite, honestly — a collapsed section's body is absent and its headline is
not, which is presence rather than layout — and the two claims underneath it
are neither. "Closing one costs no height" is an equality between two
rendered heights, and "the model name gives way before the numbers do" is
about which half of a token row the ellipsis lands on.

**This is a measurement harness that writes screenshots, not a screenshot
harness.** The distinction is the whole design. An image is only a
regression test if somebody looks at it, and nobody looks at a passing run;
so every check here asserts on numbers read out of a real layout engine and
writes its PNG as evidence for whoever has to understand a failure. That
also settles the constraint § D2 states — the harness must write files
rather than return images inline, because a run produces many and the
buffer ceiling raised in `plan/README.md` open item 1 is a ceiling, not a
budget. Nothing here ever hands an image back to a caller.

What it drives, and why not a live turn: these are layout questions. The
components are mounted directly with synthetic payloads (the shapes
`permissions.py` sends), because a live engine would add credentials,
latency and a non-deterministic payload to a question about pixels. What is
load-bearing — the real CSS, the real component, a real layout engine — is
all present. The standing recipe's argument for driving the live app
(specs5/0-overview/implementation-guide.md § Verifying UI Work Against a
Running Engine) is about claims in prose and mechanisms in our own code.

Served by Vite dev, deliberately. A harness pointed at `webapp/dist`
reports on whatever was last built, and a regression harness that can pass
against stale bytes is worse than none. The serving-mode warning in that
same section does not bite here: every style involved is either a Lit `css`
literal on the component or emitted by Monaco's JavaScript at runtime, so
there is no build-time transform for the two modes to disagree about.

Usage:
    .venv/bin/python scripts/layout_probe.py [--out DIR] [--headed] [--keep]

Exit status is 0 when every check passed, 1 when one failed or the run was
uninterpretable.
"""

from __future__ import annotations

import argparse
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
WEBAPP = REPO / "webapp"

CHROME_CANDIDATES = (
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# 1100 rather than 900 on purpose. `.diff-host`'s ceiling is
# `min(52vh, 520px)`, and below about 1000px of viewport the vh term wins —
# so a shorter window would never exercise the pixel branch, and the
# reported defect was seen on a tall one.
WINDOW = (1400, 1100)

# How much taller than its content an editor may be before the empty space
# is the finding. Monaco adds a horizontal scrollbar row and a pixel or two
# of border; 24 covers both without covering a second empty line.
CONTENT_SLACK_PX = 24

# The widths the header's grid was read at by hand. 300 is DIALOG_MIN_WIDTH
# and sits below the container query's 360px threshold, so it is also the
# one-column case.
CARD_WIDTHS = (520, 400, 300)

# Sets the rail width. `NotebookEdit` is the longest built-in tool name, and
# a name that wraps to its own line — leaving a caret and a dot alone above
# it — is the one thing in this header that must not happen.
LONGEST_TOOL_NAME = "NotebookEdit"

# The HUD's four collapsible sections, by the name each displays. The fifth
# section — "This turn" — is deliberately a plain row with no head: its
# entire content is its headline, so a caret there would hide the figure the
# HUD exists to show (usage-hud.js § _section).
#
# "7-day Sonnet limit" rather than "5-hour limit" because the scene drives
# `seven_day_sonnet`, the longest label in `rate-limit.js`'s table. The name
# a section displays is not the key its collapse is stored under, and that is
# the reason: this one changes with the window the account is billed against.
HUD_SECTIONS = ("Context", "Per-model usage", "7-day Sonnet limit", "Files modified")

# The turn `usage-hud.js`'s own max-height comment calls an ordinary
# refactor, and the reason the ceiling was written: "a turn that touches
# forty files is an ordinary refactor, and without a ceiling the overlay runs
# off the bottom of the screen".
HUD_FILES_MANY = 40

# `.hud` is `width: 300px`, fixed by the spec since phase 3.
HUD_WIDTH = 300


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


class Page:
    """Just enough CDP to drive one page over its own WebSocket.

    It keeps the console, which the first real run of this probe earned. A
    backtick inside a `css` template literal broke a module, the page never
    mounted, and all this said was "the harness page never became ready" —
    true, useless, and a sitting's worth of guessing away from the parse
    error Chrome had already printed. A harness that watches a page and
    throws away what the page said is reporting an absence it created.
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
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") in ("error", "warning"):
                where = entry.get("url", "")
                self.console.append(f"{entry['level']}: {entry.get('text')} {where}".strip())

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
        )
        if res.get("exceptionDetails"):
            details = res["exceptionDetails"]
            text = details.get("exception", {}).get("description") \
                or details.get("text", "eval failed")
            raise RuntimeError(text)
        return res.get("result", {}).get("value")

    def shot(self, path: Path, clip: dict | None = None) -> Path:
        params = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
            params["captureBeyondViewport"] = True
        res = self.send("Page.captureScreenshot", **params)
        path.write_bytes(b64decode(res["data"]))
        return path

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def start_vite(requested: int) -> tuple[subprocess.Popen, str, Path]:
    """Start Vite dev and read the URL it actually bound.

    The requested port is a request. Vite's `strictPort` is false in
    `vite.config.js`, so it walks forward from whatever it is given and a
    harness that trusted the number it asked for would navigate to
    somebody else's server.
    """
    log = Path(tempfile.mkstemp(prefix="layout-vite-", suffix=".log")[1])
    handle = log.open("wb")
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(requested)],
        cwd=WEBAPP,
        stdout=handle,
        stderr=subprocess.STDOUT,
        # Its own process group, so the whole Vite tree can be signalled
        # without the shell that launched this probe going with it.
        start_new_session=True,
    )
    deadline = time.time() + 90
    pattern = re.compile(r"(http://(?:127\.0\.0\.1|localhost):(\d+))/?")
    while time.time() < deadline:
        if proc.poll() is not None:
            handle.close()
            raise RuntimeError(f"vite exited early; log at {log}\n{log.read_text()[-2000:]}")
        text = log.read_text(errors="replace")
        found = pattern.search(text)
        if found:
            handle.close()
            return proc, found.group(1), log
        time.sleep(0.5)
    handle.close()
    raise RuntimeError(f"vite never announced a URL; log at {log}")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class Report:
    """Every check's verdict, and the file that shows it."""

    def __init__(self):
        self.rows: list[tuple[str, bool, str, Path | None]] = []

    def add(self, name: str, ok: bool, detail: str, shot: Path | None = None):
        self.rows.append((name, ok, detail, shot))
        mark = "PASS" if ok else "FAIL"
        where = f"  [{shot.name}]" if shot else ""
        print(f"  {mark}  {name} — {detail}{where}")

    @property
    def failures(self) -> int:
        return sum(1 for _, ok, _, _ in self.rows if not ok)


def px(value: str | None) -> float | None:
    """A CSS length the app declared, as a number. None when it declared none."""
    if not value:
        return None
    found = re.match(r"^([0-9.]+)px$", value.strip())
    return float(found.group(1)) if found else None


def build(page: Page, scene: str, **opts):
    payload = json.dumps(opts)
    return page.eval(
        f"window.__layout.build({json.dumps(scene)}, {payload})",
        await_promise=True,
    )


def check_dialog_sizing(page: Page, out: Path, report: Report) -> None:
    """A one-line diff must not open a screenful of empty editor.

    The failure this exists for: `.diff-host` was a flat
    `height: min(52vh, 520px)`, so a +1 −0 change filled the viewport. It
    was visible in a single frame in a browser and invisible to 60 passing
    dialog tests, because a fixed height is a rule that arrives correctly.
    """
    print("\n[1] the permission dialog on a one-line diff")
    scene = build(page, "dialog-write", lines=1)
    shot = page.shot(out / "dialog-one-line.png")

    host_h = scene["diffHost"]["h"]
    content = scene["contentHeight"]
    floor = px(scene.get("declaredMinHeight"))
    print(f"      editor box {host_h}px, content {content}px, "
          f"declared floor {scene.get('declaredMinHeight') or 'none'}, "
          f"window {scene['window']['h']}px")

    if floor is None:
        report.add(
            "editor height is content-driven", False,
            f"the editor declares no floor, so its height is not content-driven: "
            f"{host_h}px of box for {content}px of content",
            shot,
        )
    else:
        allowed = max(floor, content + CONTENT_SLACK_PX)
        report.add(
            "editor height is content-driven",
            host_h <= allowed,
            f"{host_h}px box for {content}px of content "
            f"(allowed {allowed:.0f}px = max(floor {floor:.0f}, content + {CONTENT_SLACK_PX}))",
            shot,
        )
        # A floor that is not reached is a floor nothing has demonstrated.
        report.add(
            "the floor keeps a one-line diff legible",
            host_h >= floor - 1,
            f"{host_h}px against a declared floor of {floor:.0f}px",
        )


def check_dialog_cap(page: Page, out: Path, report: Report) -> None:
    """The positive control, and the run means nothing without it.

    Check 1 asks whether the editor is as small as its content. An
    implementation that made every editor 90px tall would pass it — and
    would have replaced a screenful of empty editor with a two-line
    letterbox onto a 200-line change. So the same rule the standing recipe
    states for a probe reporting an absence applies to one reporting a
    measurement: drive the case that *must* come out the other way. A
    content-driven height whose ceiling has never been reached has not been
    shown to have one.
    """
    print("\n[2] positive control: the same dialog on a 200-line diff")
    scene = build(page, "dialog-write", lines=200)
    shot = page.shot(out / "dialog-200-lines.png")

    host_h = scene["diffHost"]["h"]
    content = scene["contentHeight"]
    ceiling = px(scene.get("declaredMaxHeight"))
    vh_ceiling = min(0.52 * scene["window"]["h"], 520)
    expected = ceiling if ceiling is not None else vh_ceiling
    print(f"      editor box {host_h}px, content {content}px, "
          f"ceiling {expected:.0f}px")

    report.add(
        "a long diff is capped, not unbounded",
        content > host_h and abs(host_h - expected) <= 4,
        f"{host_h}px box for {content}px of content, ceiling {expected:.0f}px",
        shot,
    )
    report.add(
        "and it scrolls rather than truncating",
        scene["scrolls"] is True,
        f"scrollable content overflows: {scene['scrolls']}",
    )
    return scene


def check_monaco_layout(scene: dict, out: Path, report: Report, shot: Path) -> None:
    """Did Monaco's own metrics survive the crossing into the shadow root?

    The dialog clones `document.head` into its shadow root because Monaco
    writes its theme and font-metric rules there. The existing tests assert
    the rules *arrive*. What went wrong when they did not is three
    measurable things — line numbers piled onto one row, every line
    soft-wrapped at the dialog's own font, and nothing decorated as changed
    — and none of the three is visible to an assertion about stylesheets.
    """
    print("\n[3] Monaco laid out, not merely styled")
    pitch = scene["viewLineHeight"]
    report.add(
        "the line pitch is Monaco's, not the dialog's",
        14 <= pitch <= 40,
        f"a rendered line is {pitch}px tall",
        shot,
    )
    report.add(
        "line numbers are on their own rows",
        scene["distinctMarginRows"] > 5,
        f"{scene['distinctMarginRows']} distinct margin rows for "
        f"{scene['lineNumberCount']} line numbers",
    )
    report.add(
        "the change is decorated as a change",
        scene["decorations"] >= 1,
        f"{scene['decorations']} insert/delete decorations",
    )
    report.add(
        "the editor paints in its own font",
        bool(scene["monacoFont"]) and scene["monacoFont"] != scene["dialogFont"],
        f"editor {scene['monacoFont']!r} against dialog {scene['dialogFont']!r}",
    )


def check_tool_header(page: Page, out: Path, report: Report) -> None:
    """The header grid at the three widths it was read at by hand.

    `.tool-header` is `7rem 1fr` above 360px of *card* width and one column
    below it, and the rail's width was chosen because a narrower one
    stranded a caret and a dot above a wrapped tool name. All of that was
    read in a browser twice and none of it is repeatable by the suite:
    jsdom does no layout, so `block-render.test.js` can only assert that
    the rules exist.
    """
    print("\n[4] the tool-card header grid")
    for width in CARD_WIDTHS:
        scene = build(page, "tool-card", width=width, toolName=LONGEST_TOOL_NAME)
        shot = page.shot(
            out / f"tool-card-{width}px.png",
            clip={
                "x": scene["card"]["x"],
                "y": scene["card"]["y"],
                "width": max(scene["card"]["w"], 1),
                "height": max(scene["card"]["h"], 1),
            },
        )
        columns = (scene["gridTemplateColumns"] or "").split()
        two_column = len(columns) == 2
        wants_two = width > 360
        print(f"      {width}px card: columns {scene['gridTemplateColumns']!r}, "
              f"summary {scene['summary']['w'] if scene['summary'] else '—'}px")

        report.add(
            f"{width}px: {'rail plus summary' if wants_two else 'the rail lies down'}",
            two_column == wants_two,
            f"grid-template-columns is {scene['gridTemplateColumns']!r}",
            shot,
        )
        if wants_two:
            rail = px(columns[0] + "px" if columns[0].isdigit() else columns[0])
            report.add(
                f"{width}px: the rail is 7rem",
                rail is not None and abs(rail - 112) <= 2,
                f"first column measures {rail}px against 7rem = 112px",
            )
        report.add(
            f"{width}px: the tool name shares the caret's line",
            scene["nameOnCaretsLine"] is True,
            f"caret at y={scene['caret']['y']}, "
            f"{LONGEST_TOOL_NAME} at y={scene['name']['y']}",
        )
        report.add(
            f"{width}px: nothing overflows the card",
            scene["overflows"] is False,
            f"scrollWidth against clientWidth: overflows={scene['overflows']}",
        )


def check_usage_hud(page: Page, out: Path, report: Report) -> dict:
    """Five HUD sections in 300px, and the two rules that are pure layout.

    specs5/next.md § B1 shipped the HUD's last three sections and left one
    clause behind, which § D2 then named as this harness's: the collapse
    behaviour is asserted from the DOM, and jsdom answers that honestly
    because it is *presence* — a collapsed section's body is absent and its
    headline is not — while nothing measures whether five sections and
    their heads fit 300px on a real screen.

    Two claims in `usage-hud.js` are layout and nothing else, so they have
    never been checked by anything. "The head keeps the section's headline
    figure, so closing one costs no height and hides no answer" is an
    equality between two rendered heights. And "the model name gives way
    before the numbers do" is a statement about which half of a token row
    the ellipsis lands on, which is decided by `flex: none` against
    `min-width: 0` and is invisible to a stylesheet assertion.
    """
    print("\n[5] the usage HUD, five sections in 300px")
    scene = build(page, "usage-hud", files=3)
    shot = page.shot(
        out / "usage-hud-open.png",
        clip={
            "x": scene["hud"]["x"],
            "y": scene["hud"]["y"],
            "width": max(scene["hud"]["w"], 1),
            "height": max(scene["hud"]["h"], 1),
        },
    )
    print(f"      HUD {scene['hud']['w']}×{scene['hud']['h']}px "
          f"(declared {scene['declaredWidth']}, max {scene['declaredMaxHeight']}), "
          f"content {scene['contentHeight']}px, "
          f"{scene['sectionCount']} collapsible sections")

    # The content box, not the border box. `.hud` declares `width: 300px`
    # under the default `box-sizing: content-box` and draws a 1px border, so
    # its footprint on screen is 302px — and asserting 300 against the border
    # box would be asserting a `box-sizing: border-box` the component never
    # declared and nothing asks it for. The footprint is printed anyway,
    # because "300px wide" and "occupies 300px" are two claims and only one
    # of them is true.
    report.add(
        "the HUD is 300px wide",
        abs(scene["contentWidth"] - HUD_WIDTH) <= 1,
        f"content box {scene['contentWidth']}px against a declared "
        f"{scene['declaredWidth']}; footprint {scene['borderBoxWidth']}px "
        f"with its border",
        shot,
    )
    # The one number the whole item reduces to. `.hud` clips its y-axis, so
    # its x-axis computes to `auto` — anything too wide for 300px shows up
    # here as a horizontal scrollbar in a corner overlay.
    report.add(
        "nothing overflows those 300px",
        scene["overflowsX"] is False,
        f"scrollWidth against clientWidth: overflowsX={scene['overflowsX']}",
    )

    # Four heads plus the plain "This turn" row is the five sections § B1's
    # clause counts. Asserted rather than assumed: a section that failed to
    # render would otherwise make every per-section check below vacuous.
    names = [s["name"] for s in scene["sections"]]
    report.add(
        "all four collapsible sections rendered",
        names == list(HUD_SECTIONS),
        f"heads: {names}",
    )
    report.add(
        "and the fifth section is the plain turn row",
        scene["turnRow"] is not None and scene["turnRowOverflows"] is False,
        f"{scene['turnRowText']!r}",
    )

    for section in scene["sections"]:
        name = section["name"]
        report.add(
            f"{name}: the headline shares the name's line",
            section["headlineOnNameRow"] is True,
            f"name at y={section['nameBox']['y'] if section['nameBox'] else '—'}, "
            f"headline {section['headlineText']!r} at "
            f"y={section['headline']['y'] if section['headline'] else '—'}",
        )
        report.add(
            f"{name}: the head does not overflow",
            section["overflows"] is False,
            f"head {section['head']['w']}px, overflows={section['overflows']}",
        )

    # The rule the token row's two CSS declarations exist for. It is a claim
    # about which half yields *when a row cannot fit*, so it takes two
    # assertions: no row may ever clip its count, and at least one row has to
    # be narrow enough to make the question live. Two dated model ids fit
    # 300px with room to spare — so a fixture holding only those would pass
    # the first assertion while demonstrating nothing, which is the shape
    # check 2 exists to rule out for the dialog.
    for row in scene["tokenRows"]:
        report.add(
            f"token row {row['model']}: the count is never clipped",
            row["valueClipped"] is False,
            f"value {row['valueText']!r} clipped={row['valueClipped']}, "
            f"name clipped={row['modelClipped']}",
        )
        report.add(
            f"token row {row['model']}: name and count share a line",
            row["sameRow"] is True,
            f"sameRow={row['sameRow']}",
        )
    clipped = [row for row in scene["tokenRows"] if row["modelClipped"]]
    report.add(
        "and where a row cannot fit, the name is what gives way",
        len(clipped) >= 1,
        f"{len(clipped)} of {len(scene['tokenRows'])} rows clipped their name: "
        f"{[row['model'] for row in clipped]}",
    )

    return scene


def check_hud_collapse(page: Page, out: Path, report: Report, open_scene: dict) -> None:
    """Closing a section costs no height and hides no answer.

    Both halves of one sentence in `usage-hud.js` § _section, and both are
    measurements. jsdom can see that a body element went away; it cannot
    see that the head left behind is exactly as tall as it was, which is
    the whole reason the headline was put on the head rather than in the
    body.
    """
    print("\n[6] the HUD with every section closed")
    scene = build(page, "usage-hud", files=3, collapse=list(HUD_SECTIONS))
    shot = page.shot(
        out / "usage-hud-collapsed.png",
        clip={
            "x": scene["hud"]["x"],
            "y": scene["hud"]["y"],
            "width": max(scene["hud"]["w"], 1),
            "height": max(scene["hud"]["h"], 1),
        },
    )
    print(f"      HUD {scene['hud']['w']}×{scene['hud']['h']}px, "
          f"was {open_scene['hud']['h']}px open")

    was = {s["name"]: s for s in open_scene["sections"]}
    for section in scene["sections"]:
        name = section["name"]
        before = was.get(name)
        if before is None:
            report.add(f"{name}: was open a moment ago", False,
                       "the open build had no section by this name", shot)
            continue
        report.add(
            f"{name}: closing costs no height",
            section["collapsed"] is True
            and abs(section["head"]["h"] - before["head"]["h"]) <= 1,
            f"head {section['head']['h']}px closed against "
            f"{before['head']['h']}px open, collapsed={section['collapsed']}",
            shot,
        )
        report.add(
            f"{name}: closing hides no answer",
            section["bodyHeight"] == 0 and bool(section["headlineText"]),
            f"body {section['bodyHeight']}px, headline {section['headlineText']!r}",
        )

    # The HUD as a whole has to actually get shorter, or "costs no height"
    # is being satisfied by a collapse that collapsed nothing.
    report.add(
        "the HUD is shorter with everything closed",
        scene["hud"]["h"] < open_scene["hud"]["h"],
        f"{scene['hud']['h']}px against {open_scene['hud']['h']}px open",
    )
    report.add(
        "and still fits 300px",
        scene["overflowsX"] is False,
        f"overflowsX={scene['overflowsX']}",
    )


def check_hud_ceiling(page: Page, out: Path, report: Report) -> None:
    """The positive control, and check 5 means little without it.

    Check 5 asks whether five sections fit. A HUD short enough to fit
    anything would pass it, and the `max-height` those checks never reach
    is the one thing standing between a forty-file turn and an overlay that
    runs off the bottom of the screen — taking the dismiss button, which is
    at the top, out of reach of the part that is off-screen. That is the
    reason `usage-hud.js` gives for the ceiling, and it had never been
    demonstrated: a ceiling nothing has reached has not been shown to exist.

    **What this does not demonstrate, measured rather than assumed.** With
    the ceiling deleted, forty files make the HUD 1069px in this probe's
    1100px window — unbounded and no longer scrolling, which the first two
    assertions catch, but still 15px inside the viewport. So "the overlay
    stays on screen" passes either way at this window height and is the one
    assertion here whose failure has not been witnessed. The comment's
    stated consequence is real on a shorter screen (a 1080p laptop gives a
    browser about 900px) and the fixture is deliberately not inflated past
    the forty files that comment calls an ordinary refactor to manufacture
    it. WINDOW is where that would be changed, and changing it moves every
    vh-derived number in this file.
    """
    print(f"\n[7] positive control: a {HUD_FILES_MANY}-file turn")
    scene = build(page, "usage-hud", files=HUD_FILES_MANY)
    shot = page.shot(
        out / "usage-hud-many-files.png",
        clip={
            "x": scene["hud"]["x"],
            "y": scene["hud"]["y"],
            "width": max(scene["hud"]["w"], 1),
            "height": max(scene["hud"]["h"], 1),
        },
    )
    ceiling = px(scene["declaredMaxHeight"]) or 0.8 * scene["window"]["h"]
    print(f"      HUD {scene['hud']['h']}px for {scene['contentHeight']}px of "
          f"content, ceiling {ceiling:.0f}px, bottom at {scene['bottom']}px "
          f"of a {scene['window']['h']}px window")

    report.add(
        "a long turn is capped, not unbounded",
        scene["contentHeight"] > scene["hud"]["h"]
        and abs(scene["hud"]["h"] - ceiling) <= 4,
        f"{scene['hud']['h']}px box for {scene['contentHeight']}px of content, "
        f"ceiling {ceiling:.0f}px",
        shot,
    )
    report.add(
        "and it scrolls rather than truncating",
        scene["scrollsY"] is True,
        f"scrollsY={scene['scrollsY']}",
    )
    # The reason the ceiling was written, stated as a measurement.
    report.add(
        "the overlay stays on screen",
        scene["bottom"] is not None and scene["bottom"] <= scene["window"]["h"],
        f"bottom edge at {scene['bottom']}px of {scene['window']['h']}px",
    )
    report.add(
        "the dismiss button stays inside the overlay",
        scene["dismissInside"] is True,
        f"dismiss at x={scene['dismiss']['x'] if scene['dismiss'] else '—'}, "
        f"HUD spans {scene['hud']['x']}–{round(scene['hud']['x'] + scene['hud']['w'], 1)}px",
    )
    report.add(
        "forty file chips still fit 300px",
        scene["overflowsX"] is False,
        f"overflowsX={scene['overflowsX']}",
    )


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / ".aic-dc" / "layout-probe"),
                    help="where the PNGs are written (default: .aic-dc/layout-probe)")
    ap.add_argument("--headed", action="store_true",
                    help="watch it run; headless is the default and is what CI would use")
    ap.add_argument("--keep", action="store_true",
                    help="leave Vite and Chrome running for a hand inspection")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not (WEBAPP / "node_modules").is_dir():
        sys.exit(f"{WEBAPP}/node_modules is missing — run `npm ci` in webapp/ first")

    print(f"writing screenshots to {out}")
    vite, base_url, vite_log = start_vite(free_port())
    print(f"vite serving {base_url} (log {vite_log})")

    profile = tempfile.mkdtemp(prefix="layout-chrome-")
    debug_port = free_port()
    cmd = [
        find_chrome(),
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # A device pixel ratio of 1 and a fixed window: every number this
        # probe prints is in CSS pixels and has to mean the same thing on
        # the next machine that runs it.
        "--force-device-scale-factor=1",
        f"--window-size={WINDOW[0]},{WINDOW[1]}",
    ]
    if not args.headed:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    report = Report()

    try:
        target = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = requests.get(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=2
                ).json()
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not target:
            print("UNINTERPRETABLE: chrome never exposed a page target")
            return 1

        page = Page(target)
        page.send("Page.enable")
        page.send("Runtime.enable")
        # Both, because they carry different failures: a module that fails to
        # parse arrives as a Log entry, and one that throws while evaluating
        # arrives as a Runtime exception.
        page.send("Log.enable")
        # Headless Chrome sizes its viewport from --window-size, but say it
        # again over CDP: a viewport that differs by a few pixels moves
        # every vh-derived number in this file.
        page.send(
            "Emulation.setDeviceMetricsOverride",
            width=WINDOW[0], height=WINDOW[1], deviceScaleFactor=1, mobile=False,
        )

        url = f"{base_url}/layout-harness.html"
        print(f"navigating to {url}")
        page.send("Page.navigate", url=url)

        # The module graph pulls in Monaco, so `load` is not the signal.
        ready_deadline = time.time() + 120
        while time.time() < ready_deadline:
            try:
                if page.eval("!!window.__layoutReady"):
                    break
            except RuntimeError:
                pass
            time.sleep(0.5)
        else:
            print("UNINTERPRETABLE: the harness page never became ready")
            page.report_console("while waiting for ready")
            return 1
        print(f"harness ready; scenes: {page.eval('window.__layout.scenes')}")

        check_dialog_sizing(page, out, report)
        long_scene = check_dialog_cap(page, out, report)
        # The style-clone checks ride on the long diff: they are about line
        # pitch and stacked line numbers, and one line cannot show either.
        check_monaco_layout(long_scene, out, report, out / "dialog-200-lines.png")
        check_tool_header(page, out, report)
        # The collapse check needs the open build to compare against, so it
        # takes the scene rather than building a second one: two builds of
        # "open" could differ, and then the equality would be measuring the
        # harness instead of the HUD.
        open_hud = check_usage_hud(page, out, report)
        check_hud_collapse(page, out, report, open_hud)
        check_hud_ceiling(page, out, report)

        print(f"\n{len(report.rows)} checks, {report.failures} failed")
        if report.failures:
            page.report_console("during the run")
        print(f"screenshots in {out}")
        if args.keep:
            print(f"\n--keep: vite and chrome left running. {url}")
            print("stop them yourself; the profile is at " + profile)
        return 1 if report.failures else 0
    finally:
        if page:
            page.close()
        if not args.keep:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
            stop(vite)
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
