#!/usr/bin/env python3
"""Phase 3's live verification. A spike, not a test.

The third of these, after ``probe_edit_args.py`` (phase 2) and
``probe_consultant.py`` (phase 1), and for the same reason: it needs a
real Gemini API key, it hits the network and it costs money, so it does
not live in ``tests/``. Phase 3's offline half is
``tests/test_antigravity_{options,steps,session}.py``, which run with no
credentials and no harness process — and which carry the assertions. This
prints.

What it settles
---------------
``README.md`` § *Phases*, phase 3: *"A CLI-side smoke test sends a prompt
and prints the streamed step taxonomy, including a tool call and its
result."*

So it does three things and asserts on the third:

1. **Starts a real session.** ``AntigravitySession.start()`` spawns
   ``localharness`` and opens a conversation, which is the lifecycle the
   phase built. A credential failure surfaces here rather than mid-turn.

2. **Prints every step as it arrives**, in the taxonomy the pump
   dispatches on — type, source, target, status, trajectory depth — beside
   the AIC⚡DC events it produced. Printing both is the point: the exit
   criterion is about the *translation* being legible, and a list of
   events with no steps beside them cannot show that.

3. **Requires a tool call and its result.** The prompt asks for a
   directory listing, which is a read-only builtin, so the turn exercises
   the ``ACTIVE`` → ``DONE`` transition that carries a result back in the
   *same* sub-message as the arguments — the finding phase 3's
   ``TOOL_RESULT_FIELDS`` exists for. The probe exits non-zero if no
   ``toolUse``/``toolResult`` pair arrived, because a turn that answered
   from memory would print a clean transcript and prove nothing.

Read-only on purpose
--------------------
The session is created with **no decide hook**, so
``options.build_config_kwargs`` enables no mutating tool at all — not
``run_command``, not the file tools. That is AG-5's posture for a phase
with no permission dialog yet, and it is also what makes this probe safe
to point at a real repository: it cannot write to the tree it is reading.
The scratch directory below is for a predictable listing, not for
containment.

Usage
-----
::

    export GEMINI_API_KEY=...          # or the AG-11 key file
    .venv/bin/python scripts/probe_session.py

``--repo`` points it at a real repository instead of the scratch one.
``--prompt`` overrides the question, in which case criterion 3 may not
hold and ``--allow-no-tool`` is how to say that is expected.

On hitting the free tier's limit
--------------------------------
The free tier throttles at 5 RPM and an agent turn is many model calls,
so a 429 mid-turn is expected rather than exceptional — ``delivery.md``
phases 1 and 2 both record it. Two things follow, and both are
deliberate:

- **This probe does not retry.** The SDK's own ``retry_config`` already
  retries a 429 or a 503 without the caller seeing it (measured in phase
  1), so a failure that reaches here has already been retried through. A
  retry loop on top would burn the same quota to no effect.
- **A quota refusal is reported as a distinct outcome, not as a broken
  build.** ``limit: 0`` means the plan's allowance for that model is zero
  and no wait changes it; anything else means wait a minute and rerun.
  Google's own message says *"retry in 57s"* in both cases, which is why
  the distinction is drawn here rather than trusted from the text.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aic_dc.antigravity import (  # noqa: E402
    AntigravitySession,
    resolve_credentials,
)

#: The default prompt. Names a read-only builtin explicitly, because the
#: exit criterion needs a tool call and a model asked an open question may
#: reasonably answer without one.
DEFAULT_PROMPT = (
    "List the files in the current directory using your list_directory "
    "tool, then tell me in one sentence what you found. Do not modify "
    "anything."
)

#: Files seeded into the scratch repository so the listing has content.
SCRATCH_FILES = {
    "README.md": "# scratch\n\nA throwaway repository for the phase-3 probe.\n",
    "hello.py": 'def hello():\n    return "hello"\n',
}


def seed(root: Path) -> None:
    for name, body in SCRATCH_FILES.items():
        (root / name).write_text(body, encoding="utf-8")


def describe(step: object) -> str:
    """One step as the taxonomy line the exit criterion asks to see."""
    parts = []
    for attr in ("type", "source", "target", "status"):
        value = getattr(step, attr, None)
        parts.append(f"{attr}={getattr(value, 'name', value)}")
    depth = getattr(step, "depth", 0)
    if depth:
        parts.append(f"depth={depth}")
    calls = getattr(step, "tool_calls", None) or []
    if calls:
        parts.append("tools=" + ",".join(getattr(c, "name", "?") for c in calls))
    return " ".join(parts)


def summarise(event) -> str:
    """One event as a line, with the field that makes it worth reading."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.name in ("streamChunk", "thinkingChunk"):
        content = str(payload.get("content", ""))
        return f"{event.name} seq={payload.get('seq')} {content[:60]!r}"
    if event.name == "toolUse":
        return f"toolUse {payload.get('name')} input={payload.get('input')}"
    if event.name == "toolResult":
        preview = str(payload.get("preview", "")).replace("\n", "⏎")
        return (
            f"toolResult {payload.get('status')} "
            f"{payload.get('duration_ms')}ms {preview[:80]!r}"
        )
    if event.name == "systemEvent":
        return f"systemEvent {payload.get('subtype')} {payload.get('data')}"
    return f"{event.name} {payload}"


async def run(repo: Path, prompt: str, model: str | None) -> tuple[list, object]:
    kwargs = {"model": model} if model else {}
    session = AntigravitySession(repo, **kwargs)
    print(f"credentials: {session.credentials.report()}")
    for warning in session.credentials.warnings:
        print(f"  warning: {warning}")
    print(f"read_only: {session.read_only}")
    print(
        "enabled tools: "
        + ", ".join(session.config_kwargs()["capabilities"]["enabled_tools"])
    )
    print()

    events = []
    async with session:
        print(f"--- prompt ---\n{prompt}\n\n--- stream ---")
        async for event in session.stream_turn(prompt):
            events.append(event)
            print(f"  event  {summarise(event)}")
        # The steps themselves are printed from the conversation's history
        # rather than tapped out of the pump, because the pump is what is
        # under test: reading its input from the SDK's own record is the
        # version that cannot be flattered by a bug in the translation.
        history = getattr(session._conversation, "history", [])
        print(f"\n--- step taxonomy ({len(history)} steps) ---")
        for step in history:
            print(f"  step   {describe(step)}")
    return events, history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="", help="repository root (default: scratch)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--model", default="", help="override the pinned model")
    parser.add_argument(
        "--allow-no-tool",
        action="store_true",
        help="do not fail when the turn made no tool call",
    )
    args = parser.parse_args()

    credentials = resolve_credentials()
    if not credentials.available:
        print("No usable credential. AG-R-8: a Gemini API key or a Vertex")
        print("project is mandatory — the agy OAuth login cannot supply one.")
        print(f"Looked in: {credentials.source or 'the environment and key file'}")
        return 2

    scratch = None
    if args.repo:
        repo = Path(args.repo).resolve()
    else:
        scratch = Path(tempfile.mkdtemp(prefix="ag-session-probe-"))
        seed(scratch)
        repo = scratch
    print(f"repo: {repo}\n")

    try:
        events, history = asyncio.run(run(repo, args.prompt, args.model or None))
    except Exception as exc:  # noqa: BLE001 - a spike reports, it does not raise
        text = " ".join(str(exc).split())
        print(f"\nFAILED: {text[:600]}")
        if "limit: 0" in text:
            print(
                "\nThis key's plan has no quota for that model at all "
                "(limit: 0). Retrying will not help — it needs a billing "
                "account on the project. See AG-12."
            )
        elif "429" in text or "RESOURCE_EXHAUSTED" in text:
            print(
                "\nRate-limited. The free tier allows 5 requests per minute "
                "and an agent turn is many model calls, so this is the "
                "expected free-tier failure rather than a broken build. "
                "Wait a minute and rerun; do not add a retry loop, the SDK "
                "already retried."
            )
        return 1
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    names = [e.name for e in events]
    print("\n--- verdict ---")
    print(f"events: {len(events)} ({', '.join(sorted(set(names)))})")

    ok = True
    if "streamComplete" not in names:
        print("FAIL: the turn never completed.")
        ok = False
    if "toolUse" in names and "toolResult" in names:
        print("PASS: a tool call and its result both reached the pump.")
    elif args.allow_no_tool:
        print("no tool call, and --allow-no-tool was given.")
    else:
        print(
            "FAIL: no toolUse/toolResult pair. The exit criterion needs one, "
            "because the result arrives in the *same* sub-message as the "
            "arguments and that split is what phase 3 built. Rerun, or pass "
            "--allow-no-tool if the prompt was changed deliberately."
        )
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
