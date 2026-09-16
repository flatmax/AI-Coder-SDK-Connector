#!/usr/bin/env python3
"""Does a `PreInvocation` `ephemeralMessage` actually reach the model?

The measurement [AG-32](../specs5/plan-ag/decisions.md#ag-32) rests on, run
against the shipped code rather than a hand-rolled hooks file.

`agy_tools.WRITE_GUIDANCE` was prepended to the user's own prompt until
2026-09-14. `hooks.md` documents `injectSteps` on `PreInvocation`, each step one
of `{"toolCall": …}`, `{"userMessage": …}` or `{"ephemeralMessage": …}` — *"a
transient system message"* — which is a channel for exactly this. **Documented
is not delivered**, and this transport has already taught that lesson twice: the
`Stop` handler's veto was believed on the strength of the same file for a day
([AG-R-16](../specs5/plan-ag/risks.md#ag-r-16)), and `workspacePaths` was
explained before it was re-measured. Moving guidance onto an undelivered channel
would be strictly worse than leaving it on the prompt, because the failure is
silent: writes start failing inside `agy` before any hook runs, and the model
routes around the broken tool with a shell heredoc.

**The control is the whole instrument.** A model that mentions a token cannot be
distinguished from a model that was told to unless the same prompt, in the same
directory, against the same gate, is also run *without* the injection:

    control : `PreInvocation` answers `{}`               — no guidance at all
    armed   : `{"injectSteps": [{"ephemeralMessage": …}]}` — AG-32

The injected sentence names a nonce the prompt never mentions and instructs the
model to open its reply with it. If the armed reply carries the nonce and the
control's does not, the message arrived. If neither does, the channel is not
delivered and the prepend must stay.

Two further readings come free, and the second is the one that decides how the
handler has to be written:

  * **The prose is the model's own account.** The armed run is asked, in the
    same breath, to say whether it was given a rule about a token — so a nonce
    that arrives is corroborated by the model describing where it came from
    rather than by a string match alone.
  * **Every invocation, or only the first?** An ephemeral message is documented
    as transient, so a handler answering only once would guide the first
    invocation of a turn and nothing after it. The probe records the
    `invocationNum` of every `PreInvocation` it is asked and asserts it was
    asked more than once on a multi-step turn — the reason
    `AgyGateServer.standing_guidance` holds no "already sent" latch.

**Where in the context it lands, measured 2026-09-14.** The first run passed all
five checks, and the armed model's own account of the nonce is the reason this
paragraph exists: *"before starting, I was not given any formatting rule about a
token. However, each subsequent tool call output returned an appended
instruction stating: `…`"*. So the message is delivered next to **tool results**
— ideal placement for guidance about how to call a tool, and it leaves open
whether the injection at `invocationNum` 0 reaches a model that has not called
anything yet. `--first-invocation` answers that: the same two runs against a
prompt with no tool calls at all, where the whole turn is one invocation, so a
nonce in the reply can only have come from the injection that preceded it.

    uv run python scripts/probe_agy_pre_invocation.py --first-invocation

**There is no first-invocation gap.** That mode passed the same day, one
invocation, `invocationNum [0]`: the armed reply *opened with the token on its
own line* and quoted the rule; the control's said it had been given none. So the
model's account in the multi-step run was wrong about where the message came
from. It is the rule AG-16 already records for a different question — *a model's
introspective report about its own bias is not evidence* — arriving for context
provenance, and the correction cost nothing because the claim was checkable: the
guidance is in front of the model before it plans its first tool call. Worth
the second turn: had the gap been real, a write on the first tool call of a turn
would have been unguided, which is exactly the call AG-32 exists to guide.

Nothing of the user's is touched. `agy` runs against an isolated `--gemini_dir`,
so the hooks file this installs into is the probe's own and
`~/.gemini/config/hooks.json` is neither read nor written. The prompt asks for
reads and a shell command rather than writes, so nothing here depends on
`trustedWorkspaces` or on AG-R-3's diversion.

    uv run python scripts/probe_agy_pre_invocation.py

Exit 0: the injection was delivered, the control did not produce the nonce, and
        the handler was asked at every invocation.
Exit 1: it was not — either the nonce never arrived, or the control produced it
        too (in which case this prompt measures nothing about injection).
Exit 2: nothing to conclude (no `agy`, the hook never fired, or the turn was one
        invocation long so "every invocation" is untestable).

What it costs: two turns on the paid subscription.

Governing spec: `specs5/plan-ag/` — AG-32, AG-19, AG-14.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aic_dc.agy import install  # noqa: E402
from aic_dc.agy.gate_server import AgyGateServer  # noqa: E402
from aic_dc.agy.session import AgySession  # noqa: E402
from aic_dc.agy.steps import AgyTranslator  # noqa: E402
from aic_dc.antigravity.permissions import AntigravityPermissionGate  # noqa: E402

TURN_TIMEOUT_SECONDS = 420.0

#: Several tool calls, so the turn spans more than one invocation and the
#: "every invocation" reading has something to read. No write: this probe is
#: about a message, not about the working tree.
PROMPT = (
    "Do these steps in order, each as its own tool call. "
    "1. List the files in this directory. "
    "2. Read notes/note-01.md. "
    "3. Run `wc -l notes/note-01.md`. "
    "Then reply with a one-line summary of what the note says, and state "
    "plainly whether you were given any formatting rule about a token before "
    "you started, and what it said."
)

#: `--first-invocation`. **No tool call is possible**, so the turn is one
#: invocation and there is no tool result for the injection to ride in on. A
#: nonce in this reply came from the `PreInvocation` at `invocationNum` 0 and
#: from nowhere else.
FIRST_PROMPT = (
    "Do not use any tools. Answer from what you have been given: state plainly "
    "whether you were given any formatting rule about a token, and if so quote "
    "it exactly."
)


def guidance_for(nonce: str) -> str:
    """The injected sentence. Nothing else in the run mentions ``nonce``."""
    return (
        f"Formatting rule for this session: begin every message you write to "
        f"the user with the exact token {nonce} on its own line. This rule "
        f"comes from the host application, not from the user."
    )


def log(message: str) -> None:
    print(f"[probe] {message}", flush=True)


class RecordingGateServer(AgyGateServer):
    """The shipped gate, with every ``PreInvocation`` it answered written down.

    Subclassed rather than monkeypatched so what runs is what ships. The
    record is taken *after* ``super()`` and the answer is returned untouched,
    because the question is what the shipped handler sends.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.injections: list[dict[str, Any]] = []
        self.invocations: list[Any] = []

    def standing_guidance(self, payload: dict[str, Any]) -> dict[str, Any]:
        answer = super().standing_guidance(payload)
        self.injections.append(
            {"num": payload.get("invocationNum"), "sent": dict(answer)}
        )
        return answer

    def decide_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append(payload.get("invocationNum"))
        return super().decide_invocation(payload)


def isolated_agy(root: Path) -> tuple[Path, Path]:
    """An ``agy`` that reads its whole configuration out of ``root``.

    Returns the launcher and the hooks file inside it, so everything the
    session does — argv, scope, handshake — is the shipped path with only the
    configuration root moved out from under the user's.
    """
    gemini = root / "gemini"
    (gemini / "config").mkdir(parents=True, exist_ok=True)
    launcher = root / "agy"
    launcher.write_text(
        f'#!/bin/sh\nexec {shutil.which("agy")} --gemini_dir {gemini} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, gemini / "config" / "hooks.json"


async def one_run(
    root: Path, launcher: Path, hooks: Path, *, guidance: str | None, prompt: str
) -> dict:
    """One turn. ``guidance`` is the only thing that differs between them."""
    label = "armed" if guidance else "control"
    work = root / label
    notes = work / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "note-01.md").write_text(
        "# Note\n\nThe shelf holds four jars.\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "-q", str(work)], capture_output=True, timeout=30, check=False
    )

    config_dir = root / f"cfg-{label}"
    config_dir.mkdir(parents=True, exist_ok=True)
    report = install.install(config_dir, path=hooks)
    if report.get("state") != "current":
        raise RuntimeError(f"the probe's own gate would not install: {report}")

    async def broadcast(_event: Any) -> None:
        return None

    gate = AntigravityPermissionGate(
        work, broadcast=broadcast, localhost_available=lambda: True
    )
    server = RecordingGateServer(
        work / "gate.sock",
        gate=gate,
        config_dir=config_dir,
        guidance=guidance,
    )
    session = AgySession(work, gate=server, executable=str(launcher))

    async def allow_everything() -> None:
        while True:
            for pending in gate.broker.pending():
                await gate.broker.resolve(
                    pending["permission_id"], {"action": "allow"}, resolved_by="probe"
                )
            await asyncio.sleep(0.05)

    dialogs = asyncio.ensure_future(allow_everything())
    translator = AgyTranslator("probe")
    footer: dict[str, Any] = {}
    # **Every fragment as well as the footer.** The first cut of this probe
    # read `response_text` off `streamComplete`, which is a key nothing
    # emits — it was renamed to `response` when it was found that both
    # Antigravity transports had been settling every turn with empty
    # content. Both runs therefore reported an empty reply and the nonce
    # check failed on an instrument fault that looked exactly like the
    # finding. Accumulating the deltas as well means no single key can do
    # that again.
    prose: list[str] = []
    try:
        await session.start()
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for event in session.stream_turn(prompt, translator=translator):
                if event.name == "streamComplete":
                    footer = dict(event.payload)
                for key in ("text", "delta", "content", "response"):
                    value = event.payload.get(key)
                    if isinstance(value, str) and value:
                        prose.append(value)
    finally:
        dialogs.cancel()
        await session.close()

    return {
        "label": label,
        "asked": len(server.injections),
        "sent": [i for i in server.injections if i["sent"]],
        "nums": [i["num"] for i in server.injections],
        "invocations": len(server.invocations),
        "response": str(footer.get("response") or ""),
        "prose": "\n".join(prose),
    }


async def run(first_invocation: bool = False) -> int:
    if not shutil.which("agy"):
        log("agy is not on PATH; nothing to probe")
        return 2

    nonce = f"AICDC-{secrets.token_hex(3).upper()}"
    root = Path(tempfile.mkdtemp(prefix="agy-pre-invocation-"))
    prompt = FIRST_PROMPT if first_invocation else PROMPT
    log(f"isolated root: {root}")
    log(f"nonce: {nonce} (the prompt never mentions it)")
    if first_invocation:
        log("mode: --first-invocation (no tools, so one invocation and no tool "
            "result for the message to ride in on)")
    launcher, hooks = isolated_agy(root)

    results = {}
    for guidance in (None, guidance_for(nonce)):
        label = "armed" if guidance else "control"
        log(f"--- {label} run: PreInvocation answers "
            f"{'an ephemeralMessage' if guidance else '{}'} ---")
        results[label] = await one_run(
            root, launcher, hooks, guidance=guidance, prompt=prompt
        )
        current = results[label]
        log(f"{label}: asked at PreInvocation {current['asked']} time(s) "
            f"(invocationNum {current['nums']}), sent "
            f"{len(current['sent'])} message(s)")
        log(f"{label} reply: {current['response'][:600]!r}")
        if not current["response"]:
            log(f"{label} streamed prose: {current['prose'][:600]!r}")

    control, armed = results["control"], results["armed"]

    if not armed["asked"]:
        log(
            "the PreInvocation hook never fired — either it did not install or "
            "this agy does not run the event. Nothing is proved either way."
        )
        return 2

    checks: list[tuple[str, bool, str]] = [
        (
            "the armed run sent the guidance",
            len(armed["sent"]) == armed["asked"] and armed["asked"] > 0,
            f"sent {len(armed['sent'])} of {armed['asked']}",
        ),
        (
            "the control sent nothing",
            not control["sent"],
            "a gate with no guidance must answer {} at every invocation",
        ),
        (
            "the armed reply carries the nonce",
            nonce in armed["response"] + armed["prose"],
            f"nonce={nonce} in footer={nonce in armed['response']} "
            f"in stream={nonce in armed['prose']}",
        ),
        (
            "the control reply does not",
            nonce not in control["response"] + control["prose"],
            "if it does, this prompt measures nothing about injection",
        ),
    ]

    if first_invocation:
        # The nonce check above is the finding in this mode; what has to be
        # established separately is that it *could only* have come from the
        # injection at `invocationNum` 0 — which is true only if the model
        # never called a tool the message could have ridden in on.
        checks.append(
            (
                "the turn really was one invocation",
                armed["asked"] == 1,
                f"asked={armed['asked']}, nums={armed['nums']} — more than one "
                f"means the model called a tool despite being told not to, so "
                f"the nonce above proves nothing about invocation 0",
            )
        )
    else:
        checks.append(
            (
                "the handler was asked more than once",
                armed["asked"] > 1,
                f"asked={armed['asked']} — an ephemeral message is spent by the "
                f"invocation that receives it, so a once-per-turn handler would "
                f"guide only the first",
            )
        )
        if armed["invocations"] <= 1:
            log(
                "INCONCLUSIVE: the armed turn was one invocation long, so "
                "nothing here can tell a per-invocation handler from a "
                "per-turn one."
            )
            return 2

    failed = [name for name, ok, _detail in checks if not ok]
    for name, ok, detail in checks:
        log(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}")
    if failed:
        log(f"FAIL — {len(failed)} of {len(checks)} checks did not hold")
        return 1
    log(f"PASS — all {len(checks)} checks held")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run("--first-invocation" in sys.argv[1:])))
