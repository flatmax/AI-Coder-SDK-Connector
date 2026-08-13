# Symbol Index

A tree-sitter based code analysis engine. Extracts classes, functions, variables, and imports from source files to produce a compact textual symbol map, served to the agent as an MCP tool and to the editor as language features (hover, go-to-definition, completions).

## Architecture

- Orchestrator coordinates parser, per-language extractors, cache, import resolver, reference graph, and formatter
- Two formatter outputs: compact (no line numbers, for the `symbol_map` and `file_symbols` tools) and LSP (with line numbers, for the editor)
- Singleton tree-sitter parser with multi-language support
- Per-file mtime-based cache

## Data Model

- Symbol — name, kind, file path, range, parameters, return type, bases, children, async flag, call sites, instance variables
- Call site — name, line, conditional flag, resolved target symbol and file
- Import — module, names, alias, level (0 absolute, 1+ relative), line
- FileSymbols — file path, top-level symbols, imports, flattened all-symbols list

## Supported Languages

- Python
- JavaScript (also covers `.jsx` and `.mjs`)
- TypeScript (also covers `.tsx`)
- C / C++
- MATLAB / Octave

## Grammar Acquisition

- Tree-sitter grammars come from per-language pip packages
- Each language is loaded lazily on first use
- Missing grammar packages cause that language to be silently unavailable
- The TypeScript package exposes grammars under a different function name than other languages — loader must probe for both

## Regex-Based Extractors

- A language may declare its extractor does not require a parse tree
- MATLAB uses this mechanism (no maintained tree-sitter grammar)
- Regex extractors produce the same FileSymbols output as tree-sitter extractors

## Per-Language Extraction Concerns

- Method vs function distinction (parent context)
- Async detection (keyword or node marker)
- Parameters — defaults, types, varargs
- Instance variables from constructor body
- Properties via decorators or keywords
- Inheritance via heritage nodes

## MATLAB Extractor Specifics

- Recognizes `classdef` with inheritance, functions with output args, imports, top-level variables
- Function body analysis extracts call sites and local/read variables
- Large builtin exclusion list filters common MATLAB functions from call site and variable detection
- `end` nesting is tracked for block scoping
- Functions inside `classdef` become methods

## Import Resolution

- Python — absolute paths, package `__init__.py`, relative paths with level-aware parent traversal
- JavaScript/TypeScript — relative resolution with extension probing, `index.*` fallback for directories
- C/C++ — `#include` search across repo

## Reference Index

- Tracks cross-file symbol usage by scanning identifier nodes
- Builtin identifiers (language keywords, common names) are excluded
- Queries: references to symbol, files referencing a file, dependencies of a file, reference count, bidirectional edges, connected components

## Compact Format — Symbol Map

- Human-readable, token-efficient
- Two variants: context (no line numbers) and LSP (with line numbers)
- Legend header establishes abbreviations: class, method, function, async variants, variable, property, import, return type, optional, refs, calls, more, ditto, test summary, path aliases
- File entries show path, incoming reference count, imports, top-level symbols with children nested by indent
- Path aliases — frequent prefixes get short aliases computed from reference frequency
- Ditto marks — repeated reference lists are collapsed
- Test file collapsing — test files show only summary counts (classes, methods, fixtures)
- Stable ordering — files maintain position across regenerations, so a repeated `symbol_map` call is diffable and hits the agent's own prompt cache
- Instance variables listed as indented nested entries under their class

## Chunked Output

- Symbol map can be split into chunks with a continuation token, because a large repo's map exceeds a
  reasonable tool response size. Chunk boundaries are file boundaries, never mid-file

## Per-File Blocks

- Individual file symbol blocks can be generated independently
- Stable signature hash enables cheap change detection: a re-index that produces an identical hash for a file needs no downstream work

## Indexing Pipeline

- Per-file — check cache, parse, extract, post-process (method detection, params, async, instance vars), resolve imports, store in cache
- Multi-file — index each file cache-aware, remove stale entries from memory and cache, resolve cross-file call targets, build reference index

## Stale Entry Cleanup

- Files in the in-memory index but not in the current file list are removed from memory and invalidated in the cache
- This handles files deleted from disk or removed from git tracking, including by the agent's own `Bash` calls
- Must run before an index-reading tool answers, or a deleted file reappears in the map and the agent is sent to read a path that no longer exists

## Consumers

The index has three consumers, none of which is prompt assembly:

1. **Monaco language features** — hover, go-to-definition, find-references, and completions in the
   diff viewer, via the LSP-shaped RPCs. This is the primary consumer and the reason the index
   survived the engine conversion.
2. **The agent, via the MCP bridge** — `symbol_map`, `file_symbols`, and `find_references` tools. See
   [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md).
3. **Browser navigation surfaces** — file picker outlines and the file-navigation grid.

The agent and the browser read the **same index instance**. A symbol resolvable in Monaco is
resolvable by the agent, and vice versa; there is no second copy and no divergence to reconcile.

## Snapshot Discipline

Re-indexing happens at **tool-call boundaries**. Within the execution window of a single index query
the index is a **read-only snapshot**: symbol map queries, per-file block lookups, and reference graph
queries all return consistent data.

The old contract was per-request, which no longer holds: a single agentic turn may rewrite dozens of
files, and there is no request boundary between the rewrite and the agent's next question about the
code. Concretely, the contract is now:

- A file-mutating tool call triggers an incremental re-index of the paths it touched, debounced so a
  burst of writes does not thrash.
- A pending re-index is **flushed synchronously before any index-reading tool returns**.

The flush is not an optimisation detail — without it, an agent that just edited a file gets a map
that predates its own edit and reasons confidently from stale structure. Misleading the agent with our
own tool is worse than having no tool.

The index is not thread-safe for concurrent writes. Only one re-indexing pass runs at a time, and it
runs on the event loop thread (or in an executor with a barrier). Concurrent reads from multiple
threads within a query window are safe because the index is not being mutated during that window.

## LSP Queries

- Hover — symbol signature, parameters, return type
- Definition — file and range via call site or import resolution
- References — list of file and range pairs
- Completions — label, kind, detail, filtered by prefix

## Symbol at Position

- Search through symbols by line/column range
- For nested symbols, return the deepest match
- If on a call site, match against the function's call sites list
- If on an import statement, match by line and resolve via import resolver

## Definition Resolution

- Call site — use resolved target file and symbol
- Import statement — resolve via import resolver, return synthetic call site pointing at target file
- Local symbol — return its own definition range

## Caching

- Per-file in-memory cache, mtime-based invalidation
- No on-disk map snapshot. The map is assembled in memory on demand; the per-file cache is what makes that cheap
- Import resolution cache cleared when new files are detected

## Indexing Exclusions

- Hidden directories
- Common build/dependency directories (node_modules, `__pycache__`, venvs, dist, build, .git)
- The application's own working directory

## Invariants

- A file's mtime-unchanged entry is never re-parsed
- Stale entries are removed from both memory and cache on each full index pass
- The signature hash is deterministic — identical symbol structure produces identical hash
- The symbol map output for unchanged files is byte-stable across regenerations