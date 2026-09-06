#!/usr/bin/env python3
"""AG-16: the consultant, over the transport that reaches the paid account.

Two questions, and neither can be answered offline.

**Can it generate an image at all?** ``generate_image`` has existed since
phase 1 and has never once returned a picture, because every Gemini image
model reports ``limit: 0`` on a free-tier key — an allowance of zero
rather than a throttle (AG-12). ``agy`` authenticates against the account
holder's own subscription, and its ``init`` frame advertises
``generate_image`` among its 57 tools. This is where that stops being an
inference.

**Does the static allowlist actually contain it?** A consultation runs
with ``--dangerously-skip-permissions``, exactly like a session, and its
only restraint is
:class:`~aic_dc.agy.gate_server.StaticPolicy` answering the hook. If the
claim or the policy is wrong, the failure is not a broken consultation —
it is an unreviewed agent with 57 tools and the repository as its working
directory. So this asserts on what the gate *decided*, not only on what
came back.

What it settles that nothing else can
-------------------------------------
The image path is read through
:func:`~aic_dc.claude_code.messages.files_written_by`, which knows
``generate_image`` under ``output_path`` and ``OutputPath`` — both taken
from the *SDK's* vocabulary. Whether ``agy`` spells it either way is
**unmeasured**, and the offline tests cannot settle it because they feed
the shape they assume. So a failure here prints the tool frame verbatim:
one run tells you the field name, which is the same trap the read tools'
``ARG_ALIASES`` fell into on the SDK transport in phase 4 (``PATH (none
named)`` above an input block containing the path).

What it costs: two turns on the paid subscription, and it **writes an
image file** — inside a temporary directory that is removed afterwards.
It must be a *trusted* directory or the write is diverted into
``~/.gemini/antigravity-cli/scratch/`` and reported as a success
(AG-R-3), which is what ``_agy_probe_support.probe_root`` is for.

    uv run python scripts/probe_agy_consultant.py

Exit 0 on PASS, 2 when there is nothing to probe, 1 on a criterion not
met.

Governing spec: ``specs5/plan-ag/`` — AG-16, AG-12, AG-5, AG-R-3.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_probe_support import probe_root  # noqa: E402

from aic_dc.agy import consultant as consultant_module  # noqa: E402
from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.consultant import AgyConsultant  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.antigravity.consultant import ConsultationError  # noqa: E402

QUESTION = (
    "In one short paragraph: is a permission dialog that only gates file "
    "tools sufficient, when the agent can also run shell commands?"
)

IMAGE_PROMPT = "A simple flat-colour icon of a padlock on a plain background"


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The real gate, with every decision written down.

    Subclassed rather than replaced, so what runs is the shipping
    decision path and what is recorded is its input and its answer. The
    consultant builds its own gate, so this is substituted into the
    module it builds from — the one place a probe can see inside a
    consultation without changing how one behaves.
    """

    #: Every ``(tool, decision)`` any consultation in this process made.
    seen: list[tuple[str, str]] = []

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = await super().decide(payload)
        tool = str((payload.get("toolCall") or {}).get("name", "?"))
        RecordingGateServer.seen.append((tool, str(answer.get("decision"))))
        log(f"gate: {tool} → {answer.get('decision')}")
        return answer


def image_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``generate_image`` tool step in a consultation's frames."""
    found = []
    for frame in frames:
        if frame.get("event") != "step_update":
            continue
        step = frame.get("step_update") or {}
        info = step.get("tool_info") or {}
        name = step.get("tool_name") or info.get("name")
        if name == "generate_image":
            found.append(step)
    return found


class Recorder:
    """Stands in for the bridge's observer, keeping the frames it sees."""

    def __init__(self, translator: Any) -> None:
        self.translator = translator
        self.frames: list[dict[str, Any]] = []
        self.events: list[Any] = []

    def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        self.events.extend(self.translator.translate(frame))


async def second_opinion(consultant: AgyConsultant) -> int:
    """One real consultation, with no tools allowed at all."""
    log("asking for a second opinion (no tools allowed)")
    recorder = Recorder(consultant.make_translator("probe-consult", "consultation-1"))
    try:
        answer = await consultant.second_opinion(QUESTION, observer=recorder)
    except ConsultationError as exc:
        log(f"FAIL: the consultation did not complete: {exc}")
        return 1

    log(f"answer ({len(answer)} chars): {answer[:160]!r}")
    if len(answer.split()) < 15:
        log("FAIL: that is too short to be an opinion; treat it as no answer")
        return 1

    names = [e.name for e in recorder.events]
    if "streamComplete" in names:
        log(
            "FAIL: the consultation emitted streamComplete, which ends the "
            "Claude turn holding this tool call in the browser"
        )
        return 1
    scoped = [e for e in recorder.events if "agent_id" in e.payload]
    if not scoped or any(e.payload["agent_id"] != "consultation-1" for e in scoped):
        log("FAIL: blocks were not attributed to the consultation's own tab")
        return 1

    allowed = [t for t, d in RecordingGateServer.seen if d == "allow"]
    unexpected = [t for t in allowed if t not in consultant_module.CONTROL_TOOLS]
    if unexpected:
        log(f"FAIL: a second opinion was allowed {unexpected} — the policy leaks")
        return 1
    log(f"PASS: a second opinion, {len(scoped)} scoped blocks, allowed {allowed}")
    return 0


async def generate_image(consultant: AgyConsultant, work: Path) -> int:
    """One real image, verified on disk rather than believed."""
    log("generating an image (generate_image is the only tool allowed)")
    recorder = Recorder(consultant.make_translator("probe-image", "consultation-2"))
    before = set(RecordingGateServer.seen)
    try:
        result = await consultant.generate_image(
            IMAGE_PROMPT, output_name="probe-icon.png", observer=recorder
        )
    except ConsultationError as exc:
        log(f"FAIL: {exc}")
        steps = image_frames(recorder.frames)
        if steps:
            # The field-name trap. One run should be enough to fix it.
            log("a generate_image call *was* made; its frame was:")
            log(json.dumps(steps[-1], indent=2)[:2000])
            log(
                "if the path is in there under a key files_written_by does "
                "not know, add it to _FILE_WRITING_TOOLS rather than reading "
                "it here"
            )
        else:
            log("no generate_image call appears in the stream at all")
        return 1

    absolute = Path(result.absolute_path)
    log(f"image: {result.path} ({result.bytes_written:,} bytes)")
    if not absolute.is_file() or absolute.stat().st_size == 0:
        log("FAIL: the verified path holds no file, which should be impossible")
        return 1
    if work not in absolute.parents:
        log(f"FAIL: {absolute} is outside the probe's own directory")
        return 1

    decided = [pair for pair in RecordingGateServer.seen if pair not in before]
    allowed = [t for t, d in decided if d == "allow"]
    if "generate_image" not in allowed:
        log(f"FAIL: generate_image was never allowed; decisions were {decided}")
        return 1
    denied = [t for t, d in decided if d == "deny"]
    log(f"PASS: an image landed in the repository; denied along the way: {denied}")
    return 0


async def run() -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    # Trusted, or the image is diverted into agy's scratch directory and
    # reported as a success. See `_agy_probe_support`.
    work = probe_root("agy-consult-")
    config_dir = Path.home() / ".config" / "aic-dc"

    state = install.status(config_dir)
    log(f"installed gate: {state['state']} ({state.get('path')})")
    # **Snapshotted and put back, rather than uninstalled.** The gate this
    # machine has installed is usually somebody's *other* build — a
    # released binary, another checkout — and `install.uninstall()` would
    # remove it rather than restore it, so a probe run would silently
    # leave the user ungated for their own next `agy` session. What is
    # borrowed here is the file, and what is owed is the file back.
    hooks_path = Path(state.get("path") or install.GLOBAL_HOOKS)
    previous = hooks_path.read_bytes() if hooks_path.is_file() else None
    if state["state"] != "current":
        log("installing this checkout's gate for the duration of the probe")
        install.install(config_dir)

    # The real class, recording. Substituted in the module the consultant
    # builds its gate from, because a consultation owns its own gate by
    # design — that is what stops a dialog reaching a blocked Claude turn.
    consultant_module.AgyGateServer = RecordingGateServer
    consultant = AgyConsultant(work, config_dir=config_dir)
    log(f"available: {consultant.available} (credential: {consultant.credentials.source})")
    if not consultant.available:
        log(f"FAIL: {consultant._unavailable_reason()}")
        return 1

    try:
        code = await second_opinion(consultant)
        if code == 0:
            code = await generate_image(consultant, work)
    finally:
        consultant_module.AgyGateServer = AgyGateServer
        if previous is None:
            hooks_path.unlink(missing_ok=True)
        else:
            hooks_path.write_bytes(previous)
        log(f"gate restored: {install.status(config_dir)['state']}")
        shutil.rmtree(work, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
