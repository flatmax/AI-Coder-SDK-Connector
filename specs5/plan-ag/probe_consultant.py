#!/usr/bin/env python3
"""Phase 1's live verification. A spike, not a test.

``probe_edit_args.py`` is the phase-2 equivalent and this is its
counterpart for phase 1: it needs a real Gemini API key, it hits the
network, and it costs money, so it does not live in ``tests/``. The
offline half of phase 1 is covered by ``tests/test_antigravity_*.py``,
which run with no credentials and no harness process.

What it settles
---------------
The three exit criteria in ``README.md`` § *Phases*, in order:

1. **The consultant answers.** A ``second_opinion`` round trip through
   ``google.antigravity.Agent``, on a real key, with the credential's
   source reported — which is the whole of AG-R-8 turning from a wall
   into a bill.

2. **An image lands in the repo where the file tree finds it.** The
   owner's own worked example (AG-1): a capability Anthropic's models do
   not have. The check is not that the tool said it succeeded.

3. **The sentinel write lands at its expected absolute path.** AG-R-3,
   which is the reason criterion 2 is not just "no exception". ``agy``
   was measured writing a file into a scratch directory under
   ``~/.gemini/`` and reporting success with a ``file://`` link, because
   the workspace was untrusted. Whether the SDK's ``workspaces`` is
   subject to the CLI's ``trustedWorkspaces`` list is a phase-0 unknown,
   and this is what closes it: the sentinel is written into a scratch
   repository and then **stat-ed at the absolute path**, so a diversion
   fails loudly instead of passing as an empty file tree.

It also prints the probe's ``unclassified`` buckets, which criterion 1
requires to be empty by declaration.

Usage
-----
::

    export GEMINI_API_KEY=...          # or the AG-11 key file
    .venv/bin/python specs5/plan-ag/probe_consultant.py --repo .

The free tier throttles at 5 RPM and an agent turn is many model calls,
so a 429 mid-run is a rate limit rather than a failure of the code —
``delivery.md`` phase 2 records the same. Rerun after a minute.

``--skip-image`` runs only the text half, which is one model call and the
cheapest way to confirm a key works at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aic_dc.antigravity import (  # noqa: E402
    Consultant,
    ConsultationError,
    resolve_credentials,
    surface_report,
)

#: Written into the scratch repository before the image call, and looked
#: for afterwards. Its job is to make a diverted write *visible*: if the
#: workspace is not honoured, this file's directory stays empty and the
#: image turns up somewhere under ``~/.gemini/``.
SENTINEL_NAME = "aic-dc-sentinel.txt"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def report_credentials() -> bool:
    """Print what will authenticate, and where it came from."""
    creds = resolve_credentials()
    print("\n== Credentials ==")
    print(f"  mode:   {creds.mode}")
    print(f"  source: {creds.source}")
    for warning in creds.warnings:
        print(f"  warn:   {warning}")
    if not creds.available:
        print(f"\n{FAIL} No credential. The SDK has no OAuth path at 0.1.15 and")
        print("       cannot borrow the agy login — that is AG-R-8, not a bug.")
        print("       export GEMINI_API_KEY=... and rerun.")
        return False
    return True


def report_surface() -> bool:
    """The probe's gate, printed rather than asserted.

    Phase 1's exit criterion is that ``unclassified`` is empty *by
    declaration*. ``tests/test_antigravity_surface.py`` is what enforces
    it; this prints it so a live run is self-contained.
    """
    report = surface_report()
    print("\n== Surface probe ==")
    print(f"  google-antigravity {report['versions']['sdk_version']}")
    print(f"  harness: {report['versions']['harness_binary'] or '<not found>'}")
    for section, counts in report["counts"].items():
        print(
            f"  {section:13} handled={counts['handled']:2}  "
            f"declined={counts['declined']:2}  pending={counts['pending']:2}"
        )
    unclassified = report["unclassified"]
    if unclassified:
        print(f"\n{FAIL} Untriaged surface: {unclassified}")
        return False
    print(f"  {PASS} nothing untriaged")
    return True


async def check_second_opinion(consultant: Consultant) -> bool:
    """One model call. The cheapest proof that the credential works."""
    print("\n== 1. second_opinion ==")
    try:
        answer = await consultant.second_opinion(
            "Reply with exactly one short sentence: what is a race condition?"
        )
    except ConsultationError as exc:
        print(f"  {FAIL} {exc}")
        return False
    print(f"  answer: {answer[:200]}")
    print(f"  {PASS} the consultant answered on a live key")
    return True


async def check_image_and_sentinel(consultant: Consultant, repo: Path) -> bool:
    """Criteria 2 and 3 together, because they are one measurement.

    The image is the deliverable; the containment check is what makes the
    deliverable a fact rather than a report.
    """
    print("\n== 2/3. generate_image and the AG-R-3 sentinel ==")
    sentinel = repo / SENTINEL_NAME
    sentinel.write_text("written by probe_consultant.py\n", encoding="utf-8")
    print(f"  workspace: {repo}")
    print(f"  sentinel:  {sentinel}")
    try:
        return await _image_check(consultant, repo, sentinel)
    finally:
        # `--repo .` points this at a real checkout, and a probe that
        # leaves a stray file behind on the failure path shows up as a
        # dirty tree in somebody else's commit.
        sentinel.unlink(missing_ok=True)


async def _image_check(consultant: Consultant, repo: Path, sentinel: Path) -> bool:
    try:
        result = await consultant.generate_image(
            "A simple flat-colour icon of a lightning bolt on a dark background.",
            output_name="probe-bolt.png",
            aspect_ratio="1:1",
        )
    except ConsultationError as exc:
        # The containment failure prints itself here, with the real path
        # in the message. That is the AG-R-3 tripwire firing, and it is a
        # *result* rather than a crash — record it either way.
        print(f"  {FAIL} {exc}")
        return False

    print(f"  path:      {result.path}")
    print(f"  absolute:  {result.absolute_path}")
    print(f"  bytes:     {result.bytes_written:,}")

    # Re-derived here rather than trusted from ImageResult: this file
    # exists to distrust success reports, and that includes our own.
    absolute = Path(result.absolute_path)
    if not absolute.is_file() or not absolute.is_relative_to(repo.resolve()):
        print(f"  {FAIL} the image is not inside {repo}")
        return False
    if not sentinel.is_file():
        print(f"  {FAIL} the sentinel vanished — the workspace was not honoured")
        return False

    print(f"  {PASS} the write was contained; workspaces is honoured (AG-R-3 closed)")
    return True


async def check_workspace_containment(repo: Path) -> bool:
    """AG-R-3 on its own, without the image model.

    Phase 1's exit criterion asks for a sentinel write landing at its
    expected absolute path. That was originally folded into the image
    check, and the image models are all ``limit: 0`` on a free-tier key —
    so the criterion would have been blocked on *billing* rather than on
    the question it is actually asking, which is whether the SDK's
    ``workspaces`` is honoured or is subject to the CLI's
    ``trustedWorkspaces`` list the way ``agy`` was.

    ``create_file`` on the free text model asks exactly that question and
    costs one turn. The config is built here rather than through
    :class:`Consultant` on purpose: writing a file is not a consultant
    capability, and adding a method for a probe is how AG-R-9's boundary
    erodes.
    """
    from google.antigravity import Agent, LocalAgentConfig, types
    from google.antigravity.hooks import policy

    from aic_dc.antigravity.consultant import DEFAULT_TEXT_MODEL

    print("\n== 3. AG-R-3: workspace containment ==")
    target = repo / SENTINEL_NAME
    target.unlink(missing_ok=True)
    print(f"  workspace: {repo}")
    print(f"  expecting: {target}")

    enabled = [types.BuiltinTools.FINISH, types.BuiltinTools.CREATE_FILE]
    config = LocalAgentConfig(
        model=DEFAULT_TEXT_MODEL,
        workspaces=[str(repo)],
        capabilities=types.CapabilitiesConfig(enabled_tools=enabled),
        policies=[policy.deny_all(), *(policy.allow(t.value) for t in enabled)],
        **resolve_credentials().config_kwargs(),
    )
    try:
        async with asyncio.timeout(180):
            async with Agent(config) as agent:
                response = await agent.chat(
                    f"Create a file named {SENTINEL_NAME} containing exactly the "
                    "line: sentinel. Then finish."
                )
                said = (await response.text()).strip()
    except Exception as exc:  # noqa: BLE001 - a spike; the error is the result
        # Type included: the SDK raises several errors with empty messages,
        # and "FAIL" with a blank line says nothing at all.
        detail = " ".join(str(exc).split()) or "<no message>"
        print(f"  {FAIL} {type(exc).__name__}: {detail[:300]}")
        return False

    print(f"  model said: {said[:120]}")
    # The whole point: stat the absolute path rather than believe the
    # tool. `agy` reported success for a file it had put under ~/.gemini/.
    if target.is_file():
        print(f"  {PASS} the write landed at the expected absolute path")
        print("        workspaces is honoured; trustedWorkspaces does not divert it")
        target.unlink(missing_ok=True)
        return True

    print(f"  {FAIL} nothing at {target} — the write was diverted (AG-R-3 lives)")
    strays = sorted(Path.home().glob(f".gemini/**/{SENTINEL_NAME}"))
    for stray in strays[:5]:
        print(f"        found instead at: {stray}")
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-image",
        action="store_true",
        help="run only the text half — one model call, cheapest key check",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="workspace to write into (default: a fresh temp directory). "
        "Point it at this repo to see the write land where the file tree is.",
    )
    args = parser.parse_args()

    print("Phase 1 verification — specs5/plan-ag/README.md § Phases")

    ok = report_surface()
    if not report_credentials():
        return 1

    with tempfile.TemporaryDirectory(prefix="aic-dc-ag-probe-") as scratch:
        repo = Path(args.repo).resolve() if args.repo else Path(scratch)
        consultant = Consultant(repo)

        ok = await check_second_opinion(consultant) and ok
        if args.skip_image:
            print(f"\n== 2. generate_image ==\n  {SKIP} --skip-image")
        else:
            ok = await check_image_and_sentinel(consultant, repo) and ok
        # Run last and always: it is the one criterion that does not need
        # the image model, and it is the phase-0 unknown worth closing
        # even when billing blocks everything above it.
        ok = await check_workspace_containment(repo) and ok

    print(f"\n== Verdict ==\n  {PASS if ok else FAIL}")
    if ok:
        print("  Phase 1's exit criteria are met. Record it in delivery.md.")
    return 0 if ok else 1


if __name__ == "__main__":
    # Deliberately not an ``$GEMINI_API_KEY`` check. AG-11 added key files
    # the SDK itself never reads, so the environment variable is one
    # source of several and testing it directly would report a working
    # setup as broken. `report_credentials` asks the resolver.
    raise SystemExit(asyncio.run(main()))
