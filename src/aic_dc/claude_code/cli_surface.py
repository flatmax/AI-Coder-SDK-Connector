"""What the CLI *binary* promises about option previews, read from its bytes.

:mod:`~aic_dc.claude_code.sdk_surface` reads the SDK wheel and says, in as
many words, what it cannot reach: "a feature that lands entirely in the CLI
— which is a separate Node binary on its own release train, and the larger
half of the product." It answers that with a live initialize diff. This
module answers a different part of it **statically**, by scanning the
binary, and it exists for one contract in particular.

The question-preview contract
-----------------------------
``options.py`` sets ``CLAUDE_CODE_QUESTION_PREVIEW_FORMAT=markdown``, and
the permission dialog renders what comes back as markdown. Four facts have
to hold for that to be the right thing to do, and **all four are the
CLI's**:

1. The CLI reads that variable name.
2. It accepts exactly ``markdown`` and ``html`` — anything else, including
   a typo of ours, falls through to the *entrypoint* default rather than
   erroring.
3. The prompt block the variable buys exists in a per-format pair, so
   choosing a format chooses a block, and setting nothing selects none.
4. ``preview`` is in the tool's input schema unconditionally, and the
   schema's own description defers the format to the tool description —
   which is why fact 3 matters at all.

None of the four is in the SDK's Python surface, so ``test_claude_code_
options`` cannot see them; none arrives as a build break; and a CLI release
can move any of them silently. That is exactly what happened in reverse
once already: this repo asserted in four places that previews existed
*because* we asked for them, and a live A/B disproved it
(``specs5/plan/delivery.md`` § *Interlude — the examples the question was
asking about*).

Why the markers are string literals
-----------------------------------
The binary is a minified bundle inside a native executable. Identifiers are
generated — the branch that resolves the format reads ``if(i==="markdown"
||i==="html")``, and ``i`` is whatever the minifier chose that day. String
*literals* are not renamed, so every marker below is one: either English the
CLI shows a model, or a variable name it reads from the environment. One
claim is anchored on code instead and is marked ``shape: "code"``; its
absence means "read the branch again", not "the contract broke".

What this cannot do
-------------------
It reads **presence, never behaviour**. A marker still in the binary on a
dead code path reads as present, and the block's *wording* changing while
its meaning holds reads as absent. It also cannot establish that the
answer we send back is accepted — that needs a real turn, and
``scripts/question_preview_smoke.py`` is where it lives. This closes the
half that a version bump moves; the live script keeps the half it cannot.

Governing specs: ``specs5/5-webapp/permission-dialog.md``,
``specs-reference/3-engine/permissions.md`` § *The format is the host's
choice*.
"""

from __future__ import annotations

import functools
import logging
import mmap
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The CLI version the four claims below were read against, by hand, in the
#: sitting that produced them. Recorded so a failure can say "this held at
#: X and does not at Y" rather than only "missing".
#:
#: Deliberately **not** something the gate compares against: a bump that
#: leaves all four claims standing needs no action, and failing on the
#: number alone would make every SDK release a red test with no defect
#: behind it. The claims are the check; this is provenance.
MEASURED_AGAINST = "2.1.229"

#: The values the CLI accepts for the format variable. Anything else is
#: not an error — see :data:`PREVIEW_CLAIMS`' ``format_gate`` entry.
ACCEPTED_FORMATS = ("markdown", "html")

#: The entrypoint the Python SDK stamps on every spawn, and the prefix the
#: CLI tests it against. ``subprocess_cli`` sets the first unconditionally
#: *before* merging ``options.env``, so it is ours to break and nobody
#: else's — which is what ``test_claude_code_cli_surface`` asserts about our
#: own side.
SDK_ENTRYPOINT = "sdk-py"
SDK_ENTRYPOINT_PREFIX = "sdk-"

#: The four facts above, split into the seven markers that carry them —
#: each with what it establishes and what its absence would mean. Fact 3
#: needs two claims (a block per format is what makes the variable a
#: choice) and fact 4 needs two (the field, and the sentence that defers
#: its format), which is why there are more claims than facts.
#:
#: A claim holds when **any** of its markers is present: alternatives are
#: spelling variants of one string, not separate facts.
#:
#: Keep the markers long. A short one goes green on an unrelated match,
#: which is the failure mode that makes a tripwire worse than nothing.
PREVIEW_CLAIMS: dict[str, dict[str, Any]] = {
    "env_var": {
        "markers": ("CLAUDE_CODE_QUESTION_PREVIEW_FORMAT",),
        "establishes": "the CLI reads the variable options.py sets",
        "absence_means": "options.py sets a variable nothing reads, and the "
        "preview format goes back to being the model's guess. Find the new "
        "name and update QUESTION_PREVIEW_ENV, or drop the env entry.",
        "shape": "literal",
    },
    "format_gate": {
        # The branch itself is `if(i==="markdown"||i==="html")` — the
        # identifier is generated, so what is anchored on is the pair of
        # per-format prompt blocks it selects between. Two format-specific
        # blocks existing *is* the gate: one block, or none, would mean the
        # format no longer chooses anything.
        "markers": (
            "Preview content is rendered as markdown in a monospace box",
        ),
        "establishes": "a markdown-specific prompt block exists, so asking "
        "for markdown asks for something",
        "absence_means": "the markdown block is gone or reworded. If the "
        "block is gone the variable buys nothing and the dialog's renderer "
        "is guessing again; if it is reworded, update the marker.",
        "shape": "literal",
    },
    "format_is_a_choice": {
        "markers": (
            "Preview content must be a self-contained HTML fragment",
        ),
        "establishes": "an html-specific block exists too, so the variable "
        "selects between formats rather than merely switching one on",
        "absence_means": "there is no longer a second format to choose. "
        "QUESTION_PREVIEW_FORMAT's comment argues markdown *over* html and "
        "would be arguing against nothing; re-read it before editing.",
        "shape": "literal",
    },
    "schema_field": {
        "markers": (
            "Optional preview content rendered when this option is focused",
        ),
        "establishes": "`preview` is in the option schema with its own "
        "description, unconditionally",
        "absence_means": "the field moved or went behind a gate. The dialog's "
        "whole compare layout is downstream of options carrying this key.",
        "shape": "literal",
    },
    "schema_defers_format": {
        "markers": (
            "See the tool description for the expected content format",
        ),
        "establishes": "the schema hands the format question to the tool "
        "description — the sentence that makes the env var load-bearing",
        "absence_means": "the schema may now state the format itself, in "
        "which case setting the variable is no longer what makes the format "
        "ours and QUESTION_PREVIEW_FORMAT's reasoning needs re-reading.",
        "shape": "literal",
    },
    "annotations_shape": {
        "markers": (
            "The preview content of the selected option, if the question "
            "used previews",
        ),
        "establishes": "`annotations[…].preview` is still the key "
        "build_answer_input fills",
        "absence_means": "the answer shape moved. permissions.py's "
        "build_answer_input would be sending a key the CLI ignores, which "
        "presents as a note that never reaches the model.",
        "shape": "literal",
    },
    "entrypoint_exemption": {
        # Code-shaped, and the one claim here that a formatting change in
        # the bundler could break on its own. `startsWith` is a built-in
        # property name and "sdk-" is a literal, so neither is renamed;
        # the quote style is the part that can move, hence three spellings.
        "markers": (
            'startsWith("sdk-")',
            "startsWith('sdk-')",
            "startsWith(`sdk-`)",
        ),
        "establishes": "the default-format branch exempts SDK entrypoints, "
        "so with the variable unset an sdk-py session gets no block at all "
        "— which is why 'unset' means 'nobody chose' rather than 'markdown'",
        "absence_means": "read the branch before concluding anything: this "
        "is the one marker here that is code rather than prose. If the "
        "exemption is genuinely gone, unset now means the CLI's own default "
        "and the env var becomes a preference rather than a decision.",
        "shape": "code",
    },
}


def bundled_cli_path() -> Path | None:
    """The binary the SDK ships, or ``None`` if this wheel has none.

    Deliberately the *bundled* copy rather than
    ``resolve_cli().path``. The gate wants a deterministic subject: a
    machine with ``engine.json``'s ``cli_path`` pointed somewhere else
    would otherwise scan a binary CI never sees, and a test that passes or
    fails on a reader's local configuration is the machine-dependence this
    suite has already been bitten by once. The bundled copy is also what
    the SDK's own resolver prefers, so on an unconfigured install the two
    are the same file.

    Reuses ``health``'s locator rather than re-deriving the path: that
    module already owns the private-layout read and its fallback.
    """
    try:
        from aic_dc.claude_code.health import _bundled_cli_path

        return _bundled_cli_path()
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("Could not locate the bundled CLI")
        return None


@functools.lru_cache(maxsize=4)
def _present_markers(path: str) -> frozenset[str]:
    """Which of every claim's markers occur in the binary at ``path``.

    ``mmap`` rather than ``read_bytes``: the file is ~300 MiB and every
    marker is a substring search, so paging it through the OS cache costs
    no resident memory and one pass per marker is cheap next to reading it
    all in. Cached because the report is read more than once per process
    (a test parametrised per claim, and the tab).
    """
    wanted = {
        marker
        for claim in PREVIEW_CLAIMS.values()
        for marker in claim["markers"]
    }
    found: set[str] = set()
    try:
        with open(path, "rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                for marker in wanted:
                    if data.find(marker.encode()) >= 0:
                        found.add(marker)
    except (OSError, ValueError) as exc:
        # ValueError covers an empty file, which mmap refuses. Both mean
        # "no answer" rather than "the claim failed", and the report says
        # so through `scanned` rather than by reporting empty claims.
        logger.debug("Could not scan %s: %s", path, exc)
        return frozenset()
    return frozenset(found)


def preview_contract_report(cli_path: str | Path | None = None) -> dict[str, Any]:
    """Per-claim presence in the CLI binary, plus what was scanned.

    ``holds`` is ``None``, not ``False``, when nothing could be scanned.
    A binary that is absent has not disproved anything, and a gate that
    could not read its subject must not report the contract broken — the
    caller decides whether an unreadable subject is a skip or a failure.
    """
    path = Path(cli_path) if cli_path else bundled_cli_path()
    present = _present_markers(str(path)) if path else frozenset()
    scanned = bool(path) and bool(present)
    claims = {}
    for name, claim in PREVIEW_CLAIMS.items():
        hit = next((m for m in claim["markers"] if m in present), None)
        claims[name] = {
            "holds": (hit is not None) if scanned else None,
            "marker": hit,
            "shape": claim["shape"],
            "establishes": claim["establishes"],
            "absence_means": claim["absence_means"],
        }
    return {
        "scanned": scanned,
        "cli_path": str(path) if path else None,
        "measured_against": MEASURED_AGAINST,
        "cli_pin": _cli_pin(),
        "claims": claims,
        #: Gathered so the test and any future tab agree on what "broken"
        #: means, and so the failure message is the triage list rather
        #: than a boolean.
        "broken": sorted(n for n, c in claims.items() if c["holds"] is False),
    }


def _cli_pin() -> str:
    """The CLI version the installed SDK bundles, via ``health``."""
    try:
        from aic_dc.claude_code import health

        return health.sdk_cli_pin()
    except Exception:  # noqa: BLE001 - a probe, never a control path
        logger.debug("Could not read the SDK's CLI pin")
        return "unknown"
