"""Symbol index — tree-sitter based code analysis.

Layer 2.1 delivers the parser and data model. Extractors, cache,
formatter, and orchestrator land in subsequent sub-layers.

Governing spec: ``specs4/2-indexing/symbol-index.md``.
"""

from aic_dc.symbol_index.cache import SymbolCache
from aic_dc.symbol_index.compact_format import CompactFormatter
from aic_dc.symbol_index.models import (
    CallSite,
    FileSymbols,
    Import,
    Symbol,
)
from aic_dc.symbol_index.parser import (
    LANGUAGE_MAP,
    TreeSitterParser,
    language_for_file,
)
from aic_dc.symbol_index.reference_index import ReferenceIndex

__all__ = [
    "CallSite",
    "CompactFormatter",
    "FileSymbols",
    "Import",
    "LANGUAGE_MAP",
    "ReferenceIndex",
    "Symbol",
    "SymbolCache",
    "TreeSitterParser",
    "language_for_file",
]