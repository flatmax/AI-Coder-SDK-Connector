"""Tests that the bundled default config values are sane.

Goes further than test_package_metadata: validates that numeric values
are in sensible ranges, that the engine file defers to the CLI rather
than second-guessing it, and that snippet content isn't obviously
broken. These are the values a fresh user sees; shipping nonsense
defaults wastes their first session.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import ac_dc


CONFIG_DIR = Path(ac_dc.__file__).parent / "config"


def _load_json(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


# ---- engine.json ---------------------------------------------------------


def test_engine_defaults_are_all_null() -> None:
    """Every shipped engine option is null — the CLI decides.

    ``engine.json`` replaced ``llm.json``, and the shape of the question
    changed with it. The old file named a provider-qualified model and an
    env dict because AC⚡DC called the provider itself. The CLI resolves
    its own credentials and its own default model, so a shipped value
    here would override the user's own ``claude`` configuration with a
    guess made at packaging time
    (``specs5/1-foundation/configuration.md`` § The Engine File).
    """
    data = _load_json("engine.json")
    assert data, "engine.json must ship the keys, so the editor shows them"
    for key, value in data.items():
        assert value is None, f"{key}={value!r} — shipped defaults must be null"


def test_engine_defaults_cover_every_field() -> None:
    """The shipped keys are exactly the ones EngineConfig reads.

    A key the loader ignores is a setting the user can edit with no
    effect; a field absent from the file is one they will not discover.
    """
    from ac_dc.claude_code.engine_config import EngineConfig

    fields = {f.name for f in dataclasses.fields(EngineConfig)}
    assert set(_load_json("engine.json")) == fields


def test_engine_defaults_survive_the_loader() -> None:
    """Loading the shipped file yields an all-null config, not a warning."""
    from ac_dc.claude_code.engine_config import EngineConfig

    loaded = EngineConfig.load(CONFIG_DIR)
    assert loaded == EngineConfig()


# ---- app.json ------------------------------------------------------------


def test_app_config_has_only_the_two_live_sections() -> None:
    """app.json is the two indexes and nothing else.

    Five sections went with the native engine — ``url_cache``,
    ``history_compaction``, ``agents``, ``reasoning``, ``cache_warmup``
    and ``cache_tiering``. A section nothing reads is a knob that lies:
    the user edits it, the app accepts the edit, and nothing changes.
    """
    assert set(_load_json("app.json")) == {"doc_convert", "doc_index"}


def test_doc_convert_section_fields() -> None:
    """doc_convert has the fields the converter tab will consult."""
    cfg = _load_json("app.json")["doc_convert"]
    assert isinstance(cfg["enabled"], bool)
    exts = cfg["extensions"]
    assert isinstance(exts, list)
    assert exts, "doc_convert.extensions is empty"
    for ext in exts:
        assert isinstance(ext, str)
        assert ext.startswith("."), f"extension {ext!r} should start with a dot"
    max_mb = cfg["max_source_size_mb"]
    assert isinstance(max_mb, (int, float))
    assert max_mb > 0, f"max_source_size_mb must be > 0, got {max_mb}"


def test_doc_index_section_fields() -> None:
    """doc_index has the fields the keyword enricher reads."""
    cfg = _load_json("app.json")["doc_index"]
    required = {
        "keyword_model",
        "keywords_enabled",
        "keywords_top_n",
        "keywords_ngram_range",
        "keywords_min_section_chars",
        "keywords_min_score",
        "keywords_diversity",
        "keywords_tfidf_fallback_chars",
        "keywords_max_doc_freq",
    }
    missing = required - set(cfg.keys())
    assert not missing, f"doc_index missing keys: {sorted(missing)}"
    assert isinstance(cfg["keyword_model"], str) and cfg["keyword_model"]
    assert isinstance(cfg["keywords_enabled"], bool)
    assert isinstance(cfg["keywords_top_n"], int) and cfg["keywords_top_n"] > 0
    ngram = cfg["keywords_ngram_range"]
    assert isinstance(ngram, list) and len(ngram) == 2
    assert all(isinstance(n, int) and n > 0 for n in ngram)
    assert ngram[0] <= ngram[1], f"ngram range {ngram} is inverted"
    for key in ("keywords_min_score", "keywords_diversity", "keywords_max_doc_freq"):
        value = cfg[key]
        assert isinstance(value, (int, float))
        assert 0.0 <= value <= 1.0, f"{key}={value} is outside [0.0, 1.0]"
    for key in ("keywords_min_section_chars", "keywords_tfidf_fallback_chars"):
        value = cfg[key]
        assert isinstance(value, int) and value > 0


# ---- snippets.json -------------------------------------------------------


def test_snippet_icons_and_messages_are_non_empty() -> None:
    """Every snippet has meaningful icon and message content."""
    data = _load_json("snippets.json")
    for mode in ("code", "review", "doc"):
        for i, snippet in enumerate(data[mode]):
            for field in ("icon", "tooltip", "message"):
                value = snippet[field]
                assert isinstance(value, str), (
                    f"{mode}[{i}].{field} is not a string: {value!r}"
                )
                assert value.strip(), f"{mode}[{i}].{field} is empty or whitespace"


def test_snippet_messages_do_not_reference_old_delimiters() -> None:
    """Snippet messages don't leak the specs3 guillemet delimiters."""
    data = _load_json("snippets.json")
    for mode in ("code", "review", "doc"):
        for i, snippet in enumerate(data[mode]):
            msg = snippet["message"]
            assert "\u00ab\u00ab\u00ab EDIT" not in msg, (
                f"{mode}[{i}] snippet message references old start marker"
            )
            assert "\u00bb\u00bb\u00bb EDIT END" not in msg, (
                f"{mode}[{i}] snippet message references old end marker"
            )


# ---- Prompt content sanity ----------------------------------------------


def test_the_commit_prompt_is_the_only_prompt_shipped() -> None:
    """Five prompt files went with the engine that assembled them.

    ``system.md``, ``system_doc.md``, ``review.md``, ``compaction.md``
    and ``system_reminder.md`` all described how to be a coding agent —
    the workflow, the edit protocol, the context-trust rules. The CLI
    has its own, and shipping ours alongside would give the user a file
    to edit that no request reads.
    """
    prompts = {p.name for p in CONFIG_DIR.glob("*.md")}
    assert prompts == {"commit.md"}


def test_commit_prompt_mentions_conventional_commit_style() -> None:
    """commit.md instructs conventional commit format."""
    content = (CONFIG_DIR / "commit.md").read_text(encoding="utf-8")
    lower = content.lower()
    assert "conventional" in lower or "type" in lower and "scope" in lower
    assert "imperative" in lower


def test_commit_prompt_asks_for_a_bare_message() -> None:
    """No fencing, no preamble — the output goes straight into git.

    ``ac_dc.claude_code.commit`` strips a wrapping fence defensively, but
    the prompt is where the contract is stated: anything the model adds
    around the message would otherwise land in permanent history.
    """
    lower = (CONFIG_DIR / "commit.md").read_text(encoding="utf-8").lower()
    assert "only the commit message" in lower
    assert "fencing" in lower or "fence" in lower