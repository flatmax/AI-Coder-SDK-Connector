"""``AskUserQuestion`` previews and annotations — the round trip, live.

Not a test. It runs the real engine against a real ``claude`` CLI with the
user's real credentials, so it lives in ``scripts/`` rather than the suite —
it costs tokens and needs a login.

It exists because the two halves of that feature rest on a contract that is
the *CLI's*, not ours, and a version bump can move it without breaking a
single unit test:

1. **Previews arrive.** ``ClaudeAgentOptions.env`` carries
   ``CLAUDE_CODE_QUESTION_PREVIEW_FORMAT``, which decides whether the tool's
   prompt documents the per-option ``preview`` field and in which format.
   Setting it is *not* what makes previews possible — the field is in the
   schema either way, and ``--without`` demonstrates exactly that — but it
   is what makes the format ours rather than the model's guess. See
   ``options.py``, ``QUESTION_PREVIEW_FORMAT``.
2. **Answers and annotations are accepted.** The dialog's answer goes back
   as ``answers`` plus ``annotations`` merged into the tool's own input. If
   either shape were wrong the CLI would reject the ``updated_input`` or
   report that the user never answered — and the model's reply after the
   allow is where that shows up. Nothing mocked stands in for it: a
   ``FakeSession`` would accept any shape we invented.

**Most of what this script established is now checked offline.**
``aic_dc.claude_code.cli_surface`` reads the four load-bearing facts
straight out of the CLI binary — the variable name, the per-format prompt
block, the unconditional schema field and the sentence that defers the
format to the block — and ``tests/test_claude_code_cli_surface.py`` fails by
name when a release moves one. That is ``specs5/next.md`` § D3's purpose:
the static half no longer waits on somebody remembering to run this.

What still needs a real turn is everything that is *behaviour* rather than
bytes. A string in the binary does not prove the block reaches the model,
and no static read can show that the CLI accepts the answer we build. Those
are this script's two jobs, and ``--ab`` is the first of them made
repeatable.

Usage::

    python scripts/question_preview_smoke.py
    python scripts/question_preview_smoke.py --ab
    python scripts/question_preview_smoke.py --neutral --without
    python scripts/question_preview_smoke.py --deny

``--ab``
    Run both arms in one invocation and compare them, instead of two runs
    and a reader holding one in their head. Forces ``--neutral``, because
    naming the field settles in the prompt the very thing being measured.

    **The expected result is no difference**, and that is the recorded
    finding rather than a null result: the ``preview`` field is in the
    tool's input schema unconditionally, so previews arrive with the
    variable unset. A run where previews appear *only* in the "with" arm
    contradicts the record and means the CLI has begun gating the field on
    the format — exit 1, loudly. Previews in neither arm is the model
    declining an optional field, which is inconclusive and exits 2.

``--neutral``
    Ask for the previews' *purpose* — seeing the layouts — without naming the
    ``preview`` field. Whether the model was told the field exists is exactly
    what ``--without`` measures, so naming it there would settle the question
    in the prompt. The cost is that the model may then answer by writing its
    mockups into its reply instead, leaving the field empty; the run says so
    and is inconclusive rather than failed. Default names the field, which
    makes the transport check repeatable.

``--without``
    Remove the format variable from ``options.env`` *and* from the inherited
    environment, then compare. The second half matters and is easy to miss:
    the SDK spawns the CLI with ``{**os.environ, **options.env}``, so
    emptying ``options.env`` alone leaves an inherited value in place — and
    a session run from inside AIC⚡DC inherits one from the engine hosting it,
    which is how a first attempt at this A/B produced two identical runs.

``--deny``
    Stop at the permission callback without answering. Prints the tool input
    and exits — the fastest way to see what the model actually sent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Allow running straight from a checkout without installing.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aic_dc.claude_code import (  # noqa: E402
    EngineConfig,
    build_options,
    resolve_cli,
)
from aic_dc.claude_code.options import QUESTION_PREVIEW_ENV  # noqa: E402
from aic_dc.claude_code.permissions import (  # noqa: E402
    build_answer_input,
    build_question_payload,
)

PREVIEW_ENV_KEY = next(iter(QUESTION_PREVIEW_ENV))

# Two prompts, because the script has two jobs and they need opposite things
# from it.
#
# The default names the field, so the plumbing is exercised deterministically.
# Asked only to "show me the layouts", the model may satisfy that by writing
# mockups into its *reply* and leaving `preview` empty — observed on the first
# run of this script — which tests the model's taste rather than our transport.
PROMPT_EXPLICIT = (
    "Use the AskUserQuestion tool right now, once, to ask me which sidebar "
    "layout I want for this webapp. Offer exactly two options, and give each "
    "option a preview containing an ASCII mockup of that layout so I can "
    "compare them side by side. Do not do anything else first, and do not "
    "read any files."
)

# `--neutral` does not name the field, which is what the --without A/B needs:
# naming it tells the model the field exists, and whether the model is told is
# the very thing being measured. This one asks for the *need* — seeing the
# layouts — and leaves the mechanism to the tool's own description.
PROMPT_NEUTRAL = (
    "Use the AskUserQuestion tool right now, once, to ask me which sidebar "
    "layout I want for this webapp. Offer exactly two options. I want to "
    "see what each layout actually looks like before I pick one. Do not do "
    "anything else first, and do not read any files."
)

# The note the "user" attaches to their answer, and the token looked for in
# the reply. A note is read but rarely quoted — the first run came back
# paraphrased as "with a 48px icon gutter" — so the marker is a number odd
# enough that the model has no other reason to produce it. It also has to be
# something a model could plausibly *act* on, or a model that read it would
# still have nothing to say about it.
NOTE_MARK = "47px"
NOTE = f"picked for the icon rail — keep the gutter at exactly {NOTE_MARK}"


def _summarise_previews(label: str, options: list[Any]) -> int:
    carried = [
        option.get("preview")
        for option in options
        if isinstance(option, dict)
    ]
    filled = sum(1 for preview in carried if preview)
    print(f"  {label}: {filled} of {len(carried)} options carry an example")
    return filled


class Probe:
    """The permission callback, holding what it saw for the report.

    Every call, not just the last: told to ask one question the model may
    still ask three, splitting them across calls as it narrows down. A
    single slot silently discarded the earlier ones, and with them the note
    whose arrival is being looked for.
    """

    def __init__(self, *, answer: bool) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if tool_name != "AskUserQuestion":
            # Nothing else is expected; refusing keeps the run to one turn
            # rather than letting the model wander into the repo.
            return PermissionResultDeny(message="probe: only AskUserQuestion")

        self.calls.append(tool_input)
        if not self.answer:
            return PermissionResultDeny(message="probe: stopping here", interrupt=True)

        # The dialog's path exactly: normalise the payload the browser would
        # render, then answer question 0 with its first option plus a note.
        # Every later question gets its first option and no note, because one
        # unanswered question holds the whole call.
        try:
            payload = build_question_payload(tool_input)
            asked = (payload or {}).get("questions") or []
            selections = [
                {"options": [0], "text": "", "notes": NOTE if index == 0 else ""}
                for index in range(len(asked))
            ]
            updated = build_answer_input(tool_input, payload, selections)
        except Exception as err:  # noqa: BLE001
            # Loudly. An exception here otherwise reads as a plain deny, and
            # the report then blames the CLI for our own bug.
            traceback.print_exc()
            return PermissionResultDeny(message=f"probe: raised — {err}")
        if updated is None:
            return PermissionResultDeny(message="probe: could not build an answer")
        self.updates.append(updated)
        return PermissionResultAllow(updated_input=updated)


async def _one_arm(
    *,
    repo: str,
    without: bool,
    neutral: bool,
    answer: bool,
) -> tuple[Probe, list[str]]:
    """One session, start to finish, returning what it saw.

    Split out of ``main`` so ``--ab`` can run two of these in one process
    rather than asking a reader to compare two invocations by eye. The
    environment surgery has to happen per arm and in this order: the
    "without" arm removes the variable from ``options.env`` *and* from the
    inherited environment, because the SDK spawns the CLI with
    ``{**os.environ, **options.env}`` and emptying the dict alone leaves an
    inherited value in place. A session hosted by AIC⚡DC inherits one,
    which is how the first attempt at this A/B produced two identical runs.
    """
    from claude_agent_sdk import ClaudeSDKClient

    probe = Probe(answer=answer)
    options = build_options(
        repo_root=repo,
        config=EngineConfig.load(None),
        cli_path=resolve_cli(None).path,
        can_use_tool=probe,
        # Nothing here should edit anything; plan is the posture that says so.
        permission_mode="plan",
    )
    if without:
        options.env = {key: value for key, value in options.env.items()
                       if key != PREVIEW_ENV_KEY}
        inherited = os.environ.pop(PREVIEW_ENV_KEY, None)
        if inherited is not None:
            print(f"popped an inherited {PREVIEW_ENV_KEY}={inherited}")

    print(f"{PREVIEW_ENV_KEY} in options.env: "
          f"{options.env.get(PREVIEW_ENV_KEY, '(unset)')}")
    prompt = PROMPT_NEUTRAL if neutral else PROMPT_EXPLICIT
    print(f"asking: {'neutral — the field is not named' if neutral else 'the field is named'}")

    replies: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            for block in getattr(message, "content", None) or []:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text.strip():
                    replies.append(text)
    return probe, replies


def _filled_count(probe: Probe) -> int:
    """How many options across every call carried an example.

    Counted off the raw tool input rather than the normalised payload, so
    it answers "what did the model send" — which is the arm's measurement.
    ``_report`` counts the normalised side separately and compares.
    """
    return sum(
        1
        for call in probe.calls
        for question in call.get("questions") or []
        for option in question.get("options") or []
        if isinstance(option, dict) and option.get("preview")
    )


async def _ab(repo: str) -> int:
    """Both arms, compared, with the recorded expectation stated up front.

    Neutral is forced rather than offered: the whole variable under test is
    whether the model was *told* the field exists, and a prompt naming it
    answers that in the prompt.
    """
    print("=" * 66)
    print("A/B — does the format variable change whether previews arrive?")
    print("Recorded finding: NO. The `preview` field is in the tool's input")
    print("schema unconditionally; the variable buys the format, not the")
    print("field. A difference here contradicts the record.")
    print("The static half of this is now in tests/test_claude_code_cli_surface.py.")
    print("=" * 66)

    arms: dict[str, int] = {}
    for label, without in (("with", False), ("without", True)):
        print(f"\n--- arm: {label} the variable ---")
        probe, _ = await _one_arm(
            repo=repo, without=without, neutral=True, answer=True
        )
        if not probe.calls:
            print(f"\nNO AskUserQuestion CALL in the {label!r} arm — "
                  "inconclusive, rerun")
            return 2
        arms[label] = _filled_count(probe)
        print(f"  {label}: {arms[label]} option(s) carried an example")

    print("\n" + "=" * 66)
    print(f"with the variable:    {arms['with']} example(s)")
    print(f"without the variable: {arms['without']} example(s)")
    if arms["with"] and arms["without"]:
        print("\nMATCHES THE RECORD — previews arrive either way, so the "
              "field is still unconditional and the variable still buys "
              "only the format.")
        return 0
    if arms["with"] and not arms["without"]:
        print("\nCONTRADICTS THE RECORD — previews arrived only with the "
              "variable set. The CLI may have started gating the field "
              "itself, which would make this variable load-bearing for "
              "presence rather than format. Re-read the tool's input "
              "schema in the binary before believing one run: a model "
              "declining an optional field looks identical.")
        return 1
    print("\nINCONCLUSIVE — the model filled no example in the arm that "
          "should be easiest. That is a model declining an optional field, "
          "not a finding. Rerun.")
    return 2


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--without",
        action="store_true",
        help="drop the preview-format variable, from options.env and the "
        "inherited environment both",
    )
    parser.add_argument(
        "--neutral",
        action="store_true",
        help="ask without naming the preview field — what --without needs",
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        help="run both arms and compare them; forces --neutral",
    )
    parser.add_argument(
        "--deny",
        action="store_true",
        help="stop at the callback without answering",
    )
    parser.add_argument("--repo", default=str(Path.cwd()))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.ab:
        if args.deny:
            parser.error("--ab needs an answered turn; drop --deny")
        return await _ab(args.repo)

    probe, replies = await _one_arm(
        repo=args.repo,
        without=args.without,
        neutral=args.neutral,
        answer=not args.deny,
    )

    if not probe.calls:
        print("\nNO AskUserQuestion CALL — inconclusive, rerun")
        return 2

    all_questions = [
        question
        for call in probe.calls
        for question in call.get("questions") or []
    ]
    print(f"\n--- the model made {len(probe.calls)} call(s), "
          f"{len(all_questions)} question(s) ---")
    for call_index, call in enumerate(probe.calls):
        for index, question in enumerate(call.get("questions") or []):
            print(f"  [{call_index}.{index}] {question.get('question')}")
            _summarise_previews(
                f"[{call_index}.{index}]", question.get("options") or [],
            )
    # One definition of the total, shared with ``--ab``'s per-arm count.
    # Accumulating it here as well would be a second way to count the same
    # thing, and the two could then disagree about what the arm measured.
    filled = _filled_count(probe)

    # The same content again after normalisation. A preview the model sent
    # and the payload dropped is our bug, and this is where it shows: the
    # counts above and below have to match.
    print("\n--- what survives build_question_payload ---")
    survived = 0
    for call_index, call in enumerate(probe.calls):
        payload = build_question_payload(call)
        for index, question in enumerate((payload or {}).get("questions") or []):
            survived += _summarise_previews(
                f"[{call_index}.{index}]", question.get("options") or [],
            )
    if survived != filled:
        print(f"\nDROPPED {filled - survived} example(s) in normalisation — "
              "that is ours, not the CLI's")

    if filled:
        first = next(
            option["preview"]
            for question in all_questions
            for option in question.get("options") or []
            if isinstance(option, dict) and option.get("preview")
        )
        print("\n--- the first example, as the dialog receives it ---")
        print(first)
    else:
        # Not a failure of ours, and worth saying so plainly: the model can
        # answer "show me the layouts" by writing mockups into its reply and
        # leaving the field empty, which was the first run of this script.
        # Nothing about the transport is proven either way.
        print("\nNO PREVIEWS — the model filled no option's example.")
        print("  Half of this check is inconclusive; the answer round trip "
              "below still stands.")
        if args.neutral:
            print("  Expected sometimes with --neutral, which is the point of "
                  "it. Rerun, or drop --neutral to name the field.")

    if not probe.updates:
        print("\nNOTHING WAS ANSWERED — no updated_input was built")
        return 1

    print("\n--- what went back to the CLI ---")
    for index, updated in enumerate(probe.updates):
        print(f"  [{index}] answers:     "
              + json.dumps(updated.get("answers"), ensure_ascii=False)[:400])
        print(f"  [{index}] annotations: "
              + json.dumps(updated.get("annotations"), ensure_ascii=False)[:400])

    said = "\n".join(replies)
    print("\n--- the model's reply after the last allow ---")
    print("\n".join(replies[-2:]).strip()[:900] or "(no text)")

    # The reply is the only place a rejected `answers` shape shows up: the
    # CLI would report that no answer was given rather than erroring.
    answered = "did not answer" not in said.lower()
    # `annotations` is softer evidence by nature. The model reads a note and
    # acts on it; it rarely quotes it back, so the whole reply is searched for
    # the marker rather than the note's wording, and a miss is reported as
    # unproven rather than failed — the note may simply not have been worth
    # mentioning.
    noted = NOTE_MARK in said
    print(f"\nanswer was read:       {answered}")
    print(f"note reached the model: {noted}"
          + ("" if noted else f"  (unproven — no {NOTE_MARK!r} in the reply)"))
    return 0 if answered and survived == filled else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
