"""AIC-DC — AI-Coder-SDK-Connector.

A browser UI over Claude Code: the repository layer, both indexes,
document conversion and collaboration are AIC⚡DC's; the conversation is
the Claude Code CLI's, driven through the Claude Agent SDK.

The package root deliberately re-exports nothing but the version. It used
to hoist the native engine's central types — the token counter, the
context manager, the stability tracker, the history compactor — because
they were constructed everywhere. All four are gone with the engine, and
the surviving subsystems are reached by their own module paths
(``aic_dc.repo``, ``aic_dc.symbol_index``, ``aic_dc.doc_index``,
``aic_dc.claude_code``), which keeps importing this package free of any
transitive cost.
"""

from pathlib import Path


def _read_version() -> str:
    """Read the baked VERSION file, falling back to a dev marker.

    The VERSION file is written at build time by the release workflow with a
    timestamp + short SHA. In source-tree runs it contains the literal
    string ``dev``.
    """
    version_file = Path(__file__).parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


__version__ = _read_version()

__all__ = ["__version__"]
