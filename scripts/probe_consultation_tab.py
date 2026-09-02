#!/usr/bin/env python3
"""Phase 6b's live verification. A spike, not a test.

The fourth of these, after ``probe_edit_args.py`` (phase 2),
``probe_consultant.py`` (phase 1) and ``probe_session.py`` (phase 3), and
for the same reason: it needs a real Gemini API key, it hits the network,
and it costs money, so it does not live in ``tests/``.

What it settles
---------------
[AG-13](decisions.md#ag-13) says a consultation streams into its own agent
tab, and the exit criterion is that **no webapp change is needed** — which
is only true if the server emits exactly what ``subagent-tabs.js`` joins
on. Every offline test asserts that against fakes. This asserts it against
a real turn, which is the half that has been wrong before: phase 3's live
run found three bugs that were invisible offline because the doubles
described a friendlier SDK than the real one.

So it drives a real ``second_opinion`` through the real
:class:`ConsultantBridge` with a recording emit, and then checks the
contract read off the webapp:

1. A ``subagentEvent`` arrives, turn-scoped to the live request id.
2. Its ``task_id`` / ``agent_id`` / ``tool_use_id`` are the same string —
   that identity is what picks the blocks to mirror into the tab.
3. Content blocks arrive carrying that id as their ``agent_id``. Without
   this the text renders in Main and the tab stays empty.
4. The **last** event is terminal, or the tab streams forever
   (``state.streaming = !row.terminal``).
5. Text actually streamed — more than one chunk — because a tab that
   fills in one go is tier 1, and tier 1 is not what was built.

What it cannot settle
---------------------
That the browser draws the tab. That needs a real session with a browser
attached and a Claude turn calling the tool, and it is the one part of
6b a script cannot stand in for. This checks the contract; a human checks
the rendering.

Usage
-----
::

    export GEMINI_API_KEY=...          # or the AG-11 key file
    .venv/bin/python scripts/probe_consultation_tab.py

On hitting the free tier's limit
--------------------------------
Same posture as ``probe_session.py`` and for the same reason: **no
retry**. The SDK's own ``retry_config`` already retries a 429 invisibly,
so a failure that reaches here has been retried through, and a loop on top
would burn the same quota to no effect. ``limit: 0`` means the plan's
allowance is zero and waiting will not help; a plain 429 means wait a
minute and rerun.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aic_dc.antigravity import (  # noqa: E402
    Consultant,
    ConsultantBridge,
    resolve_credentials,
)

REQUEST_ID = "probe-turn-1"

DEFAULT_QUESTION = (
    "In two sentences: is a linked list or an array better for a queue "
    "with frequent appends and pops from the front? Answer briefly."
)


async def run(question: str) -> tuple[list, str]:
    """One real consultation, with every emitted event recorded."""
    seen: list = []

    async def emit(event, request_id):
        seen.append((event, request_id))
        # Printed as it arrives, because the point of tier 2 is that
        # something is on screen *before* the answer is finished.
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.name == "streamChunk":
            text = str(payload.get("content", ""))
            print(f"  chunk seq={payload.get('seq')} {text[-60:]!r}")
        elif event.name == "subagentEvent":
            print(
                f"  row    status={payload.get('status')} "
                f"terminal={payload.get('terminal')} "
                f"id={payload.get('agent_id')}"
            )
        else:
            print(f"  {event.name} {str(payload)[:80]}")

    bridge = ConsultantBridge(
        Consultant(Path.cwd()),
        emit=emit,
        request_id=lambda: REQUEST_ID,
    )
    print("--- streaming ---")
    result = await bridge.second_opinion(question)
    answer = result["content"][0]["text"]
    return seen, answer


def check(seen: list) -> bool:
    """The contract, read off ``subagent-tabs.js``."""
    ok = True
    rows = [e.payload for e, _ in seen if e.name == "subagentEvent"]
    # Both channels: thinking renders in the tab exactly as prose does,
    # and a check that counted only `streamChunk` called a turn "not
    # streaming" on the first run here — when it had streamed two
    # thinking chunks before the provider 503'd.
    chunks = [
        e.payload
        for e, _ in seen
        if e.name in ("streamChunk", "thinkingChunk")
    ]

    def report(passed: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  {'PASS' if passed else 'FAIL'}  {label}{detail}")

    print("\n--- contract ---")
    report(bool(rows), "a subagentEvent was emitted")
    if not rows:
        return False

    report(
        all(rid == REQUEST_ID for _, rid in seen),
        "every event is turn-scoped to the live request",
    )

    row = rows[0]
    ident = {row.get("task_id"), row.get("agent_id"), row.get("tool_use_id")}
    report(
        len(ident) == 1 and None not in ident,
        "one identity joins the row to its blocks",
        f" ({row.get('agent_id')})",
    )

    agent_id = row.get("agent_id")
    tagged = [c for c in chunks if c.get("agent_id") == agent_id]
    report(
        bool(tagged),
        "content blocks carry that id",
        f" ({len(tagged)}/{len(chunks)} chunks)",
    )

    report(
        rows[-1].get("terminal") is True,
        "the last event is terminal, so the tab stops streaming",
    )

    report(
        len(chunks) > 1,
        "the answer streamed rather than arriving whole",
        f" ({len(chunks)} chunks)",
    )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args()

    credentials = resolve_credentials()
    if not credentials.available:
        print("No usable credential (AG-R-8). Set GEMINI_API_KEY or the key file.")
        return 2
    print(f"credential: {credentials.source}\n")

    try:
        seen, answer = asyncio.run(run(args.question))
    except Exception as exc:  # noqa: BLE001 - a spike reports, it does not raise
        text = " ".join(str(exc).split())
        print(f"\nFAILED: {text[:600]}")
        if "limit: 0" in text:
            print("\nThis key's plan has no quota for that model (AG-12).")
        elif "429" in text or "RESOURCE_EXHAUSTED" in text:
            print("\nRate-limited — the expected free-tier failure. Rerun in a minute.")
        return 1

    print(f"\n--- answer ---\n{answer[:400]}")
    print(f"\nevents: {len(seen)}")
    return 0 if check(seen) else 1


if __name__ == "__main__":
    raise SystemExit(main())
