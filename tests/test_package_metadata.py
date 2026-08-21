"""Tests for package metadata and bundled defaults.

Guards the contract that aic_dc is importable, exposes a version string,
and ships all the default configuration files that later layers depend on.
A packaging regression (missing force-include, renamed file, broken VERSION
read) surfaces here rather than in a downstream layer's tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import aic_dc


CONFIG_DIR = Path(aic_dc.__file__).parent / "config"


def test_version_is_non_empty_string() -> None:
    """__version__ is always a non-empty string.

    Source installs see the literal 'dev' marker from the shipped VERSION
    file. Release builds bake a timestamp+SHA string. Either way, reading
    it must succeed and yield something printable.
    """
    assert isinstance(aic_dc.__version__, str)
    assert aic_dc.__version__ != ""
    # Version should be ASCII printable — no surprise control chars from a
    # mis-encoded VERSION file.
    assert aic_dc.__version__.isprintable()


def test_version_file_is_shipped() -> None:
    """The VERSION file is present in the installed package.

    This is the file _read_version() consults. Absence would mean the
    package was installed without its data files.
    """
    version_file = Path(aic_dc.__file__).parent / "VERSION"
    assert version_file.is_file()
    content = version_file.read_text(encoding="utf-8").strip()
    # Source tree ships 'dev'; release builds bake a timestamp+SHA string
    # matching YYYY.MM.DD-HH.MM-<sha>. Accept either shape.
    # Source tree ships ``dev`` or a ``dev-{label}`` variant
    # (e.g., ``dev-d27`` to identify a development branch);
    # release builds bake a timestamp+SHA string matching
    # ``YYYY.MM.DD-HH.MM-<sha>``. Accept any of the three.
    is_dev = bool(re.fullmatch(r"dev(-[A-Za-z0-9_.-]+)?", content))
    is_release = bool(
        re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}-[0-9a-f]{7,40}", content)
    )
    assert is_dev or is_release, f"unexpected VERSION content: {content!r}"


def test_config_dir_exists() -> None:
    """The bundled config directory is present next to the package."""
    assert CONFIG_DIR.is_dir(), f"expected config dir at {CONFIG_DIR}"


def test_all_expected_config_files_present() -> None:
    """Every config file specs5 references is shipped with the package.

    The names here are the union of files listed in:
      - specs5/1-foundation/configuration.md (config file set)
      - specs5/6-deployment/packaging.md (managed + user files)

    We check containment, not equality — extra files in the bundled
    config directory (experiments, transitional files during a
    refactor) are tolerated. If you add a *required* config file,
    add it both here and in the packaging spec.
    """
    expected = {
        "engine.json",
        "app.json",
        "snippets.json",
        "commit.md",
    }
    actual = {p.name for p in CONFIG_DIR.iterdir() if p.is_file()}
    missing = expected - actual
    assert not missing, f"missing config files: {sorted(missing)}"


def test_the_engines_own_files_are_not_shipped() -> None:
    """The six native-engine files must not be in the bundle.

    This is the one place where a leftover is actively harmful rather
    than merely untidy. ``ConfigManager``'s upgrade pass copies every
    managed file it finds in the bundle into the user's config dir, so a
    stale ``system.md`` here would be re-installed on every launch — and
    a user seeing a system prompt in their config dir would reasonably
    conclude the app sends it. Nothing reads any of these now.
    """
    retired = {
        "llm.json",
        "system.md",
        "system_doc.md",
        "review.md",
        "compaction.md",
        "system_reminder.md",
    }
    actual = {p.name for p in CONFIG_DIR.iterdir() if p.is_file()}
    assert not (retired & actual), (
        f"retired config files still bundled: {sorted(retired & actual)}"
    )


def test_engine_config_is_valid_json_with_required_keys() -> None:
    """engine.json parses and carries the fields EngineConfig reads.

    Replaces the ``llm.json`` guard. The keys changed shape completely:
    no provider-qualified model pair and no ``env`` dict, because the CLI
    resolves its own credentials — writing them from here would silently
    change which account a turn bills to
    (``specs5/1-foundation/configuration.md`` § No credentials, and no
    environment export).
    """
    data = json.loads((CONFIG_DIR / "engine.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # Required keys per specs5/1-foundation/configuration.md.
    for key in (
        "model",
        "permission_mode",
        "effort",
        "thinking_display",
        "max_budget_usd",
        "cli_path",
    ):
        assert key in data, f"missing engine.json key: {key}"
    # No credential surface, by design.
    assert "env" not in data
    assert "smaller_model" not in data


def test_app_config_is_valid_json_with_required_sections() -> None:
    """app.json parses and has the sections downstream layers consume."""
    data = json.loads((CONFIG_DIR / "app.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # Required top-level sections per specs5/1-foundation/configuration.md.
    # ``url_cache`` and ``history_compaction`` went with the engine that
    # fetched URLs and compacted its own history.
    for section in ("doc_convert", "doc_index"):
        assert section in data, f"missing app.json section: {section}"
        assert isinstance(data[section], dict)


def test_snippets_json_has_all_three_modes() -> None:
    """snippets.json uses the nested per-mode structure.

    specs4/1-foundation/configuration.md#snippets defines keys for code,
    review, and doc modes. Each value is a list of snippet objects with
    icon, tooltip, and message fields.
    """
    data = json.loads((CONFIG_DIR / "snippets.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    for mode in ("code", "review", "doc"):
        assert mode in data, f"snippets.json missing mode: {mode}"
        assert isinstance(data[mode], list)
        assert len(data[mode]) > 0, f"snippets.json[{mode}] is empty"
        for snippet in data[mode]:
            assert isinstance(snippet, dict)
            # Required keys per spec.
            assert "icon" in snippet
            assert "tooltip" in snippet
            assert "message" in snippet


def test_the_commit_prompt_is_non_empty() -> None:
    """The one surviving prompt ships real content, not a zero-byte stub.

    It survives because generating a commit message is a request AIC⚡DC
    makes on its own behalf — a one-shot query with its own instructions,
    not a turn in the user's conversation with the agent.
    """
    content = (CONFIG_DIR / "commit.md").read_text(encoding="utf-8")
    # Strip whitespace so a file containing only newlines fails the check.
    assert content.strip(), "commit.md is empty or whitespace-only"


def test_no_shipped_file_describes_an_edit_protocol() -> None:
    """No bundled file teaches an edit-block format any more.

    The emoji delimiters (🟧🟧🟧 EDIT / 🟨🟨🟨 REPL / 🟩🟩🟩 END) were
    AIC⚡DC's own protocol: the model emitted them in prose and a parser
    turned them into writes. The CLI edits files with its own ``Write``
    and ``Edit`` tools, so a shipped file still specifying the markers
    would be instructing the agent to route edits through a parser that
    no longer exists.

    ``edit-block-render.js`` stays on the frontend — it renders the
    blocks in *archived* history, which must keep displaying correctly.
    """
    for path in CONFIG_DIR.iterdir():
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("🟧🟧🟧 EDIT", "🟨🟨🟨 REPL", "🟩🟩🟩 END", "««« EDIT"):
            assert marker not in content, (
                f"{path.name} still specifies the {marker!r} edit marker"
            )