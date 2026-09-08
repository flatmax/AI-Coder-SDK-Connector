"""Tests for aic_dc.claude_code.cli_surface — the question-preview gate.

``specs5/next.md`` § D3: *"The question-preview `--without` A/B is not
automated. ``scripts/question_preview_smoke.py`` supports it and the specs
record its result, but nothing re-runs it when the CLI ships a new build —
and what it measures is exactly the kind of detail a version bump moves."*

This file is what re-runs it, and it does not run the A/B. The live A/B
established two facts about the CLI, and both are **in the binary**: the
per-format prompt block the variable selects, and the schema field that
exists either way and defers its format to that block. Reading them out of
the bytes is deterministic where the A/B is not — a live run asks a model to
fill an optional field, so a null result is inconclusive rather than
failing, which is why the script's own docstring has a ``--neutral`` mode
and why running it after an upgrade was "a judgement call, not a check".

So the division is:

- **Here, offline, every suite run:** the four facts the contract rests on,
  each failing by name with what its absence would mean.
- **``scripts/question_preview_smoke.py``, live, by hand:** that the answer
  and its annotation are *accepted* by the CLI, which no static read can
  establish — a ``FakeSession`` accepts any shape we invent.

Offline: no client, no CLI spawned, no credentials. It scans a file.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from aic_dc.claude_code import cli_surface
from aic_dc.claude_code.cli_surface import (
    ACCEPTED_FORMATS,
    MEASURED_AGAINST,
    PREVIEW_CLAIMS,
    SDK_ENTRYPOINT,
    SDK_ENTRYPOINT_PREFIX,
    bundled_cli_path,
    preview_contract_report,
)
from aic_dc.claude_code.engine_config import EngineConfig
from aic_dc.claude_code.options import (
    QUESTION_PREVIEW_ENV,
    QUESTION_PREVIEW_FORMAT,
    build_option_kwargs,
)

CLAIMS = tuple(PREVIEW_CLAIMS)


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    """Drop the marker cache between tests.

    The scan is ``lru_cache``d because the gate reads it once per claim;
    that cache would otherwise leak one test's synthetic binary into the
    next, and a synthetic one is how the failure paths below are proved.
    """
    cli_surface._present_markers.cache_clear()
    yield
    cli_surface._present_markers.cache_clear()


@pytest.fixture(scope="module")
def report():
    """The real scan, or a skip naming why there was nothing to scan.

    A skip rather than a failure, because a checkout with no bundled
    binary has not disproved anything — but *only* here. The claims about
    our own side below never skip, so an install with no binary still has
    a gate over everything that does not need one.
    """
    if bundled_cli_path() is None:
        pytest.skip(
            "no CLI bundled with the installed claude-agent-sdk, so there is "
            "nothing to read the contract out of. The claims about our own "
            "side still run."
        )
    result = preview_contract_report()
    if not result["scanned"]:
        pytest.skip(f"could not scan {result['cli_path']}")
    return result


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class TestTheContractStillHolds:
    """Each fact the preview feature rests on, failing by name."""

    @pytest.mark.parametrize("claim", CLAIMS)
    def test_claim_holds(self, claim, report):
        entry = report["claims"][claim]
        assert entry["holds"], (
            f"The CLI no longer carries the marker for {claim!r}.\n"
            f"This claim establishes: {entry['establishes']}.\n"
            f"{entry['absence_means']}\n"
            f"Read against CLI {MEASURED_AGAINST}; the installed SDK pins "
            f"{report['cli_pin']} at {report['cli_path']}.\n"
            "This is a CLI release moving something, not a bug in this test. "
            "See aic_dc/claude_code/cli_surface.py, and re-run "
            "scripts/question_preview_smoke.py if the live half is in doubt."
        )

    def test_nothing_is_broken(self, report):
        """The same gate as a list, so a failure names every claim at once.

        The parametrised test above fails one case per claim, which is the
        readable form when one moves. This one is for the release that
        moves several: a single message holding the whole triage list.
        """
        assert not report["broken"], (
            "The CLI has moved several parts of the question-preview "
            f"contract at once: {', '.join(report['broken'])}. That pattern "
            "usually means the feature was reworked rather than reworded — "
            "read the binary before editing markers one at a time."
        )


# ---------------------------------------------------------------------------
# Our own side of the same contract — never skipped
# ---------------------------------------------------------------------------


class TestOurSideAgrees:
    """The three ways this could break without the CLI changing at all."""

    def test_we_set_the_variable_the_cli_reads(self):
        assert list(QUESTION_PREVIEW_ENV) == [
            PREVIEW_CLAIMS["env_var"]["markers"][0]
        ], (
            "options.QUESTION_PREVIEW_ENV and cli_surface's env_var claim "
            "name different variables, so one of them is being read against "
            "a binary that does not care."
        )

    def test_the_format_we_ask_for_is_one_the_cli_accepts(self):
        """A typo here is silent, which is the whole reason to assert it.

        The CLI's branch is ``if(v==="markdown"||v==="html")`` with an
        entrypoint-dependent fallback — so ``"markdwon"`` does not raise,
        it falls through to the SDK exemption and leaves the format
        unchosen. The dialog would then render whatever the model guessed,
        which is the state this variable exists to end.
        """
        assert QUESTION_PREVIEW_FORMAT in ACCEPTED_FORMATS, (
            f"options sets {QUESTION_PREVIEW_FORMAT!r}, which the CLI does "
            f"not accept ({', '.join(ACCEPTED_FORMATS)}). It would not error "
            "— it would leave the format nobody's decision."
        )

    def test_we_never_set_the_entrypoint(self, tmp_path):
        """``options.env`` is merged *after* the SDK's own ``sdk-py``.

        ``subprocess_cli`` builds ``{**inherited, ENTRYPOINT: "sdk-py",
        **options.env}``, so ours wins on a collision. Setting it to
        anything outside the ``sdk-`` prefix would flip the exemption
        branch and make an unset format mean "markdown by default" instead
        of "unchosen" — quietly reversing the reasoning in
        QUESTION_PREVIEW_FORMAT's comment.
        """
        env = build_option_kwargs(
            repo_root=tmp_path, config=EngineConfig()
        )["env"]
        assert "CLAUDE_CODE_ENTRYPOINT" not in env, (
            "build_option_kwargs sets CLAUDE_CODE_ENTRYPOINT, which "
            "overrides the SDK's sdk-py stamp and changes what an unset "
            "preview format means."
        )

    def test_the_sdk_still_stamps_an_sdk_prefixed_entrypoint(self):
        """The other end of the same chain, read from the wheel.

        The CLI exempts entrypoints beginning ``sdk-`` from its default
        format. That only helps if the Python SDK still identifies itself
        that way — and it does so inline in a method rather than through a
        constant, so this reads the source rather than importing a name.
        """
        from claude_agent_sdk._internal.transport import subprocess_cli

        source = inspect.getsource(subprocess_cli)
        assert f'"CLAUDE_CODE_ENTRYPOINT": "{SDK_ENTRYPOINT}"' in source, (
            f"the SDK no longer stamps CLAUDE_CODE_ENTRYPOINT={SDK_ENTRYPOINT}. "
            "If it now sends something outside the CLI's sdk- exemption, an "
            "unset preview format stops meaning 'unchosen'."
        )
        assert SDK_ENTRYPOINT.startswith(SDK_ENTRYPOINT_PREFIX)


# ---------------------------------------------------------------------------
# The probe's own honesty — a gate that cannot fail is not a gate
# ---------------------------------------------------------------------------


class TestTheGateCanActuallyFail:
    """Proved against synthetic binaries, because the real one passes.

    Without these, a scan that silently found nothing and a scan that
    found everything produce the same green run — which is the shape of
    every inert check this project has already shipped twice (``next.md``
    § A2 (a) and (d)).
    """

    def _binary(self, tmp_path: Path, markers: list[str]) -> Path:
        path = tmp_path / "claude-ish"
        path.write_bytes(b"padding" + "".join(markers).encode() + b"padding")
        return path

    def test_a_missing_marker_reads_as_broken(self, tmp_path):
        kept = [
            claim["markers"][0]
            for name, claim in PREVIEW_CLAIMS.items()
            if name != "schema_field"
        ]
        result = preview_contract_report(self._binary(tmp_path, kept))
        assert result["scanned"] is True
        assert result["broken"] == ["schema_field"]
        assert result["claims"]["schema_field"]["holds"] is False
        assert result["claims"]["env_var"]["holds"] is True

    def test_an_unreadable_binary_is_not_a_broken_contract(self, tmp_path):
        """``holds`` is ``None`` and ``broken`` is empty, so nothing lies.

        The distinction the report exists to keep: a binary that could not
        be read has not disproved a claim. Reporting ``False`` here would
        make a stripped install look like a CLI regression, and — worse —
        make a real regression indistinguishable from a missing file.
        """
        result = preview_contract_report(tmp_path / "not-a-file")
        assert result["scanned"] is False
        assert result["broken"] == []
        assert all(c["holds"] is None for c in result["claims"].values())

    def test_an_empty_binary_is_unreadable_rather_than_failing(self, tmp_path):
        """``mmap`` refuses a zero-length file with ``ValueError``.

        Caught alongside ``OSError`` because an empty file is the same
        answer as an absent one and must not surface as a broken claim.
        """
        empty = tmp_path / "empty"
        empty.write_bytes(b"")
        result = preview_contract_report(empty)
        assert result["scanned"] is False
        assert result["broken"] == []

    def test_each_claim_carries_what_its_absence_would_mean(self):
        """The failure message is the whole value of the table.

        A claim reduced to a boolean sends the next reader to the binary
        with no idea what they are looking for. Every entry owes a reason
        and a consequence.
        """
        for name, claim in PREVIEW_CLAIMS.items():
            assert claim["markers"], f"{name} has no marker"
            assert claim["establishes"], f"{name} says nothing about what it proves"
            assert claim["absence_means"], f"{name} says nothing about its absence"
            assert claim["shape"] in ("literal", "code"), name

    def test_markers_are_long_enough_not_to_match_by_accident(self):
        """A short marker in a 300 MiB binary is a coin flip.

        The floor is arbitrary but the failure it prevents is not: a
        marker that matches unrelated bytes reads as a healthy contract
        forever, which is worse than having no gate at all.
        """
        for name, claim in PREVIEW_CLAIMS.items():
            for marker in claim["markers"]:
                assert len(marker) >= 15, (
                    f"{name}'s marker {marker!r} is short enough to match "
                    "something unrelated in a 300 MiB binary."
                )

    def test_only_the_entrypoint_claim_leans_on_code(self):
        """Prose survives minification; identifiers do not.

        Recorded as a test rather than a comment because the temptation
        when a marker goes red is to replace it with the surrounding
        minified code, which would go red again on the next release for no
        reason. One code-shaped marker is a considered exception; a second
        one needs the same argument made again.
        """
        code_shaped = sorted(
            name
            for name, claim in PREVIEW_CLAIMS.items()
            if claim["shape"] == "code"
        )
        assert code_shaped == ["entrypoint_exemption"], (
            "cli_surface has grown a code-shaped marker beyond the one this "
            f"file argues for: {code_shaped}. Anchor on the CLI's own prose "
            "instead — a minifier renames identifiers and does not touch "
            "string literals."
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_the_measured_version_is_recorded_and_not_gated_on(self):
        """A version bump alone must not fail this file.

        The claims are the check. Gating on the number would turn every
        SDK release into a red test with no defect behind it, which is how
        a tripwire gets disabled.
        """
        assert MEASURED_AGAINST
        report = preview_contract_report(Path("/nonexistent"))
        assert report["measured_against"] == MEASURED_AGAINST
        assert report["broken"] == []

    def test_the_report_names_the_binary_it_read(self, tmp_path):
        """Otherwise a failure cannot be reproduced by hand."""
        binary = tmp_path / "claude-ish"
        binary.write_bytes(b"nothing here")
        assert preview_contract_report(binary)["cli_path"] == str(binary)
