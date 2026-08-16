"""History read-path smoke test — run phase 5's fold over a real transcript.

Phase 5 shipped its read path proved against fixtures we authored. This
runs the same path over a ``SessionStoreEntry`` blob the CLI actually
wrote: the store, the SDK's parser, and ``history.py``'s fold, in the
order the browser calls them.

It found the bug that two thousand nine hundred green tests agreed with —
every session row previewing the same 100 characters of
``<ac-dc-ui-context>`` boilerplate, because the CLI truncates the
sidecar's ``first_prompt`` to 200 characters and the truncation lands
inside the framing block. Then, on the run after that fix shipped, it
found the same bug from a second source: a *compacted* session previewed
the CLI's compaction summary, which opens with the same sentence every
time. Check 4 is both bugs, kept.

Unlike ``engine_smoke.py`` and ``bridge_smoke.py`` this needs **no
credentials and spends no tokens** — it reads the mirror off disk. It
still lives in ``scripts/`` rather than the suite because it needs a real
conversation to have happened in the repo, which no fixture can supply
and no CI job will have.

Usage::

    python scripts/history_smoke.py
    python scripts/history_smoke.py --repo /path/to/repo
    python scripts/history_smoke.py --session 1d53df67-...
    python scripts/history_smoke.py --verbose

Exits non-zero when a check fails, so it can gate a phase's sign-off
rather than being read by eye.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

# Allow running straight from a checkout without installing.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ac_dc.claude_code.history import (  # noqa: E402
    list_sessions,
    load_session,
    strip_framing,
)
from ac_dc.claude_code.session_store import RepoSessionStore  # noqa: E402

FRAMING_OPEN = "<ac-dc-ui-context>"

# The other prose every session shares. Check 4 printed this as a preview
# and passed, because it only knew to look for the framing tag — a human
# reading the output caught it. Asserted now so the next occurrence fails.
COMPACT_PREAMBLE = "This session is being continued from a previous conversation"

# Entry types the SDK's parser drops. All metadata: the ``attachment``
# seen live was a ``deferred_tools_delta`` and ``mode`` is
# ``{"type": "mode", "mode": "normal", "sessionId": ...}``, neither of
# them anything the human wrote or saw. Listed so an *unexpected* dropped
# type stands out — a new one showing up here is the signal that a CLI
# upgrade started writing something the fold has never seen, and the job
# is to classify it before adding it.
KNOWN_DROPPED = {
    "ai-title",
    "attachment",
    "queue-operation",
    "last-prompt",
    "mode",
}


class Report:
    """Accumulates pass/fail so every check runs before anything exits."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, passed: str, failed: str) -> bool:
        print(f"  {'ok  ' if ok else 'FAIL'}  {passed if ok else failed}")
        if not ok:
            self.failures.append(failed)
        return ok

    def note(self, message: str) -> None:
        print(f"        {message}")


def _heading(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _all_strings(value: object) -> list[str]:
    """Every string anywhere in a rendered structure.

    Deliberately not ``json.dumps``. A dump escapes newlines, so a probe
    spanning one can never match it and every multi-line prompt reads as
    dropped — a false alarm this script raised on its first run, and the
    same shape of mistake as probing with a prefix the fold legitimately
    strips.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _all_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _all_strings(item)]
    return []


def _compact_boundary(raw: list[dict]) -> int | None:
    """Index of the last ``compact_boundary`` entry, if the session has one.

    The CLI writes it as ``{"type": "system", "subtype":
    "compact_boundary"}``, immediately followed by the summary as a
    ``user`` entry carrying ``isCompactSummary``.
    """
    found = None
    for index, entry in enumerate(raw):
        if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
            found = index
    return found


def _human_texts(entry: dict) -> list[str]:
    """The human-authored text in a raw ``user`` entry, if any.

    A tool result arrives as a ``user`` entry too, so this deliberately
    reads only ``text`` blocks and plain-string content.
    """
    if entry.get("type") != "user" or "toolUseResult" in entry:
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


async def run(repo: Path, session: str | None, verbose: bool) -> int:
    report = Report()
    store = RepoSessionStore(repo / ".ac-dc4" / "sessions")
    directory = str(repo)

    from claude_agent_sdk import (  # noqa: PLC0415
        get_session_messages_from_store,
        list_sessions_from_store,
        project_key_for_directory,
    )

    _heading("1. What the store lists, and what a restart would resume")
    infos = await list_sessions_from_store(store, directory)
    if not report.check(
        bool(infos),
        f"{len(infos)} session(s) in the mirror",
        "nothing listed — the browser would show an empty history, and "
        "auto-resume would have nothing to reattach to",
    ):
        return 1
    for info in infos:
        report.note(
            f"{info.session_id}  created_at={info.created_at}  "
            f"summary={getattr(info, 'summary', None)!r}"
        )
    # The store sorts by last_modified, so the newest is the session we
    # were last in — which is exactly what auto-resume picks, with no
    # pointer file anywhere.
    report.note(f"auto-resume would pick: {infos[0].session_id}")

    sid = session or infos[0].session_id
    report.check(
        any(i.session_id == sid for i in infos),
        f"examining {sid}",
        f"{sid} is not in the mirror",
    )

    _heading("2. Raw entries -> parser messages -> rendered turns")
    key = {
        "project_key": project_key_for_directory(directory),
        "session_id": sid,
    }
    raw = await store.load(key) or []
    parsed = await get_session_messages_from_store(store, sid, directory)
    rendered = await load_session(store, sid, directory)
    print(f"        raw entries     {len(raw)}")
    print(f"        parser messages {len(parsed)}")
    print(f"        rendered turns  {len(rendered)}")
    report.check(
        len(raw) >= len(parsed) >= len(rendered) and bool(rendered),
        "the fold narrows monotonically and produced turns",
        f"unexpected shape: {len(raw)} -> {len(parsed)} -> {len(rendered)}",
    )

    raw_types = Counter(entry.get("type") for entry in raw)
    parsed_types = Counter(
        getattr(message, "type", None) or type(message).__name__
        for message in parsed
    )
    if verbose:
        report.note(f"raw types:    {dict(raw_types)}")
        report.note(f"parsed types: {dict(parsed_types)}")
    dropped = {t for t in raw_types if t not in parsed_types and t is not None}
    unexpected = dropped - KNOWN_DROPPED - {"user", "assistant", "system"}
    report.check(
        not unexpected,
        f"dropped entry types are all known metadata: {sorted(dropped)}",
        f"the parser is dropping something new: {sorted(unexpected)} — check "
        "whether it carries anything the human wrote or saw",
    )

    _heading("3. Nothing the human wrote was dropped on the floor")
    # Only what the render is actually responsible for. A compacted
    # session's transcript keeps its pre-compaction entries, and the fold
    # correctly renders from the boundary onward — the compact summary
    # stands in for everything earlier. Probing the whole file reports
    # every pre-compaction prompt as dropped.
    boundary = _compact_boundary(raw)
    if boundary is not None:
        report.note(
            f"compact_boundary at raw entry {boundary}; checking the "
            f"{len(raw) - boundary - 1} entries after it"
        )
    considered = raw[boundary + 1 :] if boundary is not None else raw

    # Two more ways this check lies if written carelessly, both hit for
    # real: probing with a prefix of the *raw* text matches the framing
    # opener that strip_framing legitimately removes, and probing against
    # a JSON dump never matches a prompt containing a newline.
    rendered_blob = "\n".join(_all_strings(rendered))
    missing = []
    for entry in considered:
        for text in _human_texts(entry):
            probe = strip_framing(text).strip()[:60]
            if probe and probe not in rendered_blob:
                missing.append(probe)
    report.check(
        not missing,
        "every human-authored text the render is responsible for reaches it",
        f"{len(missing)} prompt(s) absent from the render: {missing[:3]}",
    )

    _heading("4. The session-list preview is what the user typed")
    rows = await list_sessions(store, directory)
    previews = []
    for row in rows:
        preview = row.get("preview") or ""
        previews.append(preview)
        report.note(f"{row['session_id'][:8]}  {preview[:70]!r}")
        report.check(
            FRAMING_OPEN not in preview,
            "no framing boilerplate in the preview",
            f"framing not stripped: {preview[:60]!r}",
        )
        report.check(
            not preview.startswith(COMPACT_PREAMBLE),
            "no compaction preamble in the preview",
            "the preview is the CLI's compaction summary, which opens the "
            f"same way in every compacted session: {preview[:60]!r}",
        )
    if len(previews) > 1:
        report.check(
            len(set(previews)) > 1,
            "previews distinguish one session from another",
            "every session previews identically — session-preview is the "
            "only field a row is told apart by, so the browser is unusable",
        )

    _heading("5. Turn footers, rebuilt from a transcript with no result entry")
    # The CLI writes one entry per content block and never a result, so
    # every footer fact here was reconstructed by the fold.
    turns = [m for m in rendered if m.get("role") == "assistant" and m.get("turn")]
    report.check(
        bool(turns),
        f"{len(turns)} assistant turn(s) carry a rebuilt footer",
        "no assistant turn carries a footer — the fold rebuilt nothing",
    )
    for message in turns:
        turn = message.get("turn") or {}
        if verbose:
            report.note(json.dumps(turn, default=str)[:200])
        report.check(
            turn.get("num_turns") is not None or turn.get("tool_calls") is not None,
            "footer carries reconstructed counts",
            f"footer has neither num_turns nor tool_calls: {sorted(turn)}",
        )
        # A missing cost must be absent, never 0.0 — the whole reason
        # formatCost exists on the frontend.
        report.check(
            turn.get("total_cost_usd") is None or turn.get("total_cost_usd") > 0,
            "cost is absent rather than a misleading zero",
            f"cost rendered as {turn.get('total_cost_usd')!r}",
        )

    _heading("Result")
    if report.failures:
        print(f"  {len(report.failures)} check(s) failed:")
        for failure in report.failures:
            print(f"    - {failure}")
        return 1
    print("  every check passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repo whose .ac-dc4/sessions mirror to read (default: this one)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="session id to examine (default: the one auto-resume would pick)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print the type histograms and each rebuilt footer in full",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo / ".ac-dc4" / "sessions").is_dir():
        print(f"No session mirror at {repo / '.ac-dc4' / 'sessions'}.")
        print("Hold a conversation in this repo first — the mirror is written")
        print("by the engine, not by this script.")
        return 2
    return asyncio.run(run(repo, args.session, args.verbose))


if __name__ == "__main__":
    sys.exit(main())
