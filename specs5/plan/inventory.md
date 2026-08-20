# Keep / Delete / Add Inventory

File-by-file disposition for the conversion. Line counts are from the pre-conversion tree
(`53,586` lines of Python across `src/`, `110,910` lines of JS across `webapp/src/`, `16,115`
lines of Markdown across `specs5/`).

Legend: **KEEP** unchanged · **ADAPT** survives with changes · **DELETE** removed ·
**NEW** written from scratch.

---

## Totals

| | Python (`src/`) | Frontend (`webapp/src/`) | Specs (`specs5/`) |
|---|---|---|---|
| DELETE | ~20,700 lines | ~4,900 lines | 12 files |
| ADAPT | ~3,200 lines | ~7,200 lines | 18 files |
| KEEP | ~29,700 lines | ~98,800 lines | 21 files |
| NEW | ~2,400 lines (est.) | ~2,600 lines (est.) | 8 files |

The engine is roughly 37% of the Python tree and 4% of the frontend. The asymmetry is the point:
the frontend is the product, the engine was the tax.

---

## Python — DELETE

The whole native engine. Nothing here has a consumer after phase 3.

| File | Lines | Why it goes |
|---|---:|---|
| `src/ac_dc/llm_service.py` | 2042 | The service class itself. Replaced by `claude_code/service.py`. |
| `src/ac_dc/llm/_streaming.py` | 1538 | LiteLLM streaming loop, retry policy, usage extraction, URL fetch during stream. |
| `src/ac_dc/llm/_breakdown.py` | 1492 | Context breakdown + terminal HUD. Replaced by `get_context_usage()` rendering. |
| `src/ac_dc/llm/_cache_warmer.py` | 1477 | Idle warm-up calls. No provider cache of ours to keep hot. |
| `src/ac_dc/stability_tracker.py` | 1436 | Four-tier tracker, N-values, cascade, promotion/demotion log. |
| `src/ac_dc/context_manager.py` | 1392 | Prompt state holder. Claude Code owns conversation state. |
| `src/ac_dc/llm/_helpers.py` | 1280 | LiteLLM error classification, finish-reason extraction, agent-tag parsing, max-token resolution. |
| `src/ac_dc/llm/_agents.py` | 1146 | `🟧🟧🟧 AGENT` spawn/gather/assimilate. Replaced by the SDK `Task` tool (CC-8). |
| `src/ac_dc/edit_protocol.py` | 973 | Emoji block parser (CC-7). |
| `src/ac_dc/llm/_rpc_state.py` | 853 | Mode switching, cross-reference, index exclusion, agent selection RPCs. Partially re-homed. |
| `src/ac_dc/llm/_stability.py` | 711 | Tracker wiring and cross-reference seeding. |
| `src/ac_dc/url_service/` (4 files) | ~2,180 | Fetchers, cache, service, LiteLLM summariser (CC-9). |
| `src/ac_dc/edit_pipeline.py` | 665 | Anchored-match application, five failure classes (CC-7). |
| `src/ac_dc/history_compactor.py` | 657 | Topic-boundary detection, verbatim windows, summarise-vs-truncate (CC-3). |
| `src/ac_dc/llm/_assembly.py` | 608 | Tiered and flat message assembly, cache-control placement. |
| `src/ac_dc/llm/_rebuild.py` | 594 | Manual cache rebuild and orphan redistribution. |
| `src/ac_dc/token_counter.py` | 577 | Model-aware tokenizer wrapper and ceilings. |
| `src/ac_dc/cache_membrane.py` | 540 | Membrane / flux controller (D35–D37). |
| `src/ac_dc/llm/_lifecycle.py` | 547 | Post-response housekeeping, file-context sync. Partially re-homed. |
| `src/ac_dc/llm/_commit.py` | 388 | LLM commit-message generation. Re-homed as an agent prompt, not a direct call. |
| `src/ac_dc/agent_factory.py` | ~250 | Agent `ContextManager` construction. |
| `src/ac_dc/file_context.py` | ~300 | In-memory `{path: content}` map. |
| `src/ac_dc/llm/_rpc_urls.py`, `_rpc_streaming.py`, `_construction.py`, `_types.py`, `__init__.py` | ~1,000 | Remaining engine RPC surface and re-export hub. |
| `src/ac_dc/history_store.py` | 1148 | Moved here from ADAPT by [CC-19](decisions.md#cc-19). A store cannot impose a record shape on pass-through entries, so this file's schema is not a head start on the `SessionStore` — it is a second one. Its three jobs re-home to a derived index, read-time rendering, and `events.jsonl`. |

**Tests deleted with them:** `test_context_manager.py`, `test_edit_pipeline.py`,
`test_edit_protocol.py`, `test_file_context.py`, `test_history_compactor.py`,
`test_history_store.py`, `test_prompt_assembly.py`, `test_stability_tracker/`,
`test_token_counter.py`, `test_url_content.py`, `test_agent_factory.py`, `test_llm_service/`,
`test_thinking_kwargs.py`.

**Dependency removed:** `litellm` and its transitive tree.

---

## Python — ADAPT

| File | Lines | Change |
|---|---:|---|
| `src/ac_dc/main.py` | 856 | Construct `ClaudeCodeService` instead of `LLMService`; drop stability init, doc-index-for-prompt scheduling, and the `_post_write_callback` into the engine. Keep symbol/doc index build, static server, collab wiring. |
| `src/ac_dc/config.py` | 1485 | Loses every prompt-composition helper and the whole prompt file set (CC-11); loses cache tuning, compaction config, agent gate, warmup config. Keeps config-dir resolution, version-aware upgrade, managed/user split, snippets. Expect ~600 lines. |
| `src/ac_dc/settings.py` | 428 | Config whitelist shrinks to the surviving files; `refresh_system_prompt` removed. |
| `src/ac_dc/llm/_review.py` | 504 | Git-side review state moves to `src/ac_dc/repo/review.py`; the prompt-swap and review-context-assembly halves are deleted (CC-13). |
| `src/ac_dc/llm/_rpc_history.py` | 466 | Session and history RPCs re-pointed at the SDK's session functions and the `*_from_store` parsers. Method names survive; every return shape changes ([CC-19](decisions.md#cc-19)). |
| `src/ac_dc/llm/_doc_index_background.py` | 577 | Survives as the doc-index build/enrichment scheduler, moved under `doc_index/`. Loses the "deferred enrichment after edit blocks" coupling. |
| `src/ac_dc/llm/_rpc_lifecycle.py` | 498 | `get_current_state`, localhost gate, navigate, TeX, snippets survive; deferred-init/stability parts go. |

---

## Python — KEEP unchanged

`rpc.py`, `collab.py`, `cli.py`, `logging_setup.py`, `base_cache.py`, `base_formatter.py`,
`repo/` (all), `symbol_index/` (all), `doc_index/` (all), `doc_convert/` (all).

That is the repository layer, both indexes, document conversion, collaboration, and the
transport — ~29,700 lines, untouched.

---

## Python — NEW

`src/ac_dc/claude_code/`:

| File | Purpose | Spec |
|---|---|---|
| `service.py` | The jrpc-oo service replacing `LLMService`. Owns the client, the request-ID map, and the RPC surface. | [`../3-engine/session.md`](../3-engine/session.md) |
| `session.py` | `ClaudeSDKClient` lifecycle: connect, options assembly, query, `receive_response()` pump, interrupt, disconnect, resume/fork. | [`../3-engine/session.md`](../3-engine/session.md) |
| `messages.py` | SDK message taxonomy → server-push events. The one place that knows about `AssistantMessage`, `ToolUseBlock`, `StreamEvent`, `ResultMessage`, the four `Task*Message` subclasses, `SystemMessage(subtype="compact_boundary")`, and `RateLimitEvent`. | [`../3-engine/session.md`](../3-engine/session.md) |
| `permissions.py` | `can_use_tool` callback, the awaitable browser round-trip, decision persistence, timeout/deny policy. | [`../3-engine/permissions.md`](../3-engine/permissions.md) |
| `hooks.py` | `PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `PreCompact` / `Stop` / `SubagentStart` / `SubagentStop` handlers that drive UI broadcasts and re-indexing. | [`../3-engine/tool-surface.md`](../3-engine/tool-surface.md) |
| `mcp_server.py` | In-process SDK MCP server `ac-dc` exposing the indexes, review state, and repo facts as tools. | [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md) |
| `session_store.py` | `SessionStore` protocol implementation over `.ac-dc4/sessions/`. All six methods, entries verbatim, verified against the SDK's conformance harness. | [`../3-engine/history.md`](../3-engine/history.md) |
| `events_log.py` | `.ac-dc4/events.jsonl` — our own operational events, append-only, keyed by session and request ID. Separate from the store because the store is never given an entry the CLI did not write. | [`../3-engine/history.md`](../3-engine/history.md) |
| `history_index.py` | The derived index under `.ac-dc4/index/`: search postings, session summaries, request ID ↔ session mapping. Rebuildable from the transcript, so deleting it is supported. | [`../3-engine/history.md`](../3-engine/history.md) |
| `context_usage.py` | `get_context_usage()` fetch, shaping, and caching for the Context tab. | [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md) |

---

## Frontend — DELETE

| File | Why |
|---|---|
| `webapp/src/edit-blocks.js` (+ tests) | Emoji edit-block parsing (CC-7). Replaced by tool-use cards and the permission diff. |
| `webapp/src/agent-block-render.js` | `🟧🟧🟧 AGENT` block rendering (CC-8). |
| `webapp/src/cache-warmup-progress.js` | Cache warmer progress bar. |
| `webapp/src/compaction-progress.js` (+ test) | Our compaction toast. Replaced by a transcript divider driven by `SystemMessage(subtype="compact_boundary")`. |
| `webapp/src/url-chips.js` (+ test) | URL curation UI (CC-9). |
| `webapp/src/token-hud.js` | Tier bars, N/threshold labels, stability bars. Rebuilt from scratch (CC-4). |
| `webapp/src/context-tab.js` | Budget/Cache sub-views, rebuild button, tier groups. Rebuilt from scratch (CC-4). |
| `webapp/src/chat-panel/urls.js` (+ test) | URL chip integration in the input area. |

Two files an earlier draft of this table listed for deletion are **not** deletable, found while
implementing phase 2:

- **`edit-block-render.js` stays.** Its diff renderer is what draws the body of a tool card for `Edit`,
  `MultiEdit`, `Write` and `NotebookEdit` — `chat-panel/block-render.js` imports it. The emoji *parsing*
  goes with `edit-blocks.js`; the rendering is the thing the tool cards were supposed to replace it with,
  and it already does the job.
- **`url-helpers.js` stays, and has nothing to do with URL curation.** It is `main.js`'s WebSocket
  port-and-URI parser. Deleting it breaks the transport bootstrap, which is a failure mode with no
  visible connection to the URL-chip work the row grouped it with.

---

## Frontend — ADAPT

| File / dir | Change |
|---|---|
| `webapp/src/chat-panel/index.js` + `rendering.js` | Message rendering gains tool-use cards, tool-result cards, thinking blocks, todo lists, and a permission-request card. Loses edit-block segmentation, edit summary banners, and the three retry prompts. |
| `webapp/src/chat-panel/streaming.js` | Chunk coalescing survives; the accumulate-full-content contract changes to accumulate-per-block. `StreamEvent` partials feed the same animation-frame throttle. |
| `webapp/src/chat-panel/tabs.js` | Agent tabs re-keyed on SDK `agent_id` (CC-8). |
| `webapp/src/chat-panel/events.js`, `state.js` | Server-push handler set changes with the RPC inventory. |
| `webapp/src/app-shell/index.js` | The `AcApp` callback surface is re-specified — see [`../1-foundation/rpc-inventory.md`](../1-foundation/rpc-inventory.md). |
| `webapp/src/app-shell/mode.js` | Mode toggle becomes a preset selector (CC-12). |
| `webapp/src/settings-tab.js` | Config cards shrink to the surviving files; gains permission-mode, model, effort, and budget controls. |
| `webapp/src/file-picker/` | Third checkbox state re-labelled from "exclude from index" to "deny agent read" (CC-14), then the checkbox column deleted outright and the gesture moved to shift+click on the row ([CC-21](decisions.md#cc-21)). `files-tab/selection.js` goes with it. |
| `webapp/src/history-browser.js` | Reads the one transcript through the parsed shapes; gains resume-vs-fork affordances. Loses the `files_modified`, `edit_results` and "show agents" panels — the records behind them retire with `history_store.py` ([CC-19](decisions.md#cc-19)). |

## Frontend — NEW

| File | Purpose |
|---|---|
| `webapp/src/permission-dialog/` | The `can_use_tool` surface. One module per concern: queue ordering, body renderers, the decision row, the Monaco diff, constants, styles. A single file was the plan; the queue, the settling interval and the diff editor's lifecycle each earned their own. |
| `webapp/src/chat-panel/blocks.js` | The turn-block model: one block per `content_block`, tool status, todo state, tool paths. |
| `webapp/src/chat-panel/block-render.js` | Templates for every block kind — text, thinking, tool cards (including the diff bodies, via `edit-block-render.js`), todo lists, subagent rows, the turn footer. Replaces the planned `tool-card.js` and `todo-list.js`, which were never separate components: a card and a checklist are two templates over the same block list, and splitting them would have meant three modules sharing one expansion map. |
| `webapp/src/chat-panel/permission-mode.js` | The permission-mode control and its state. |
| `webapp/src/context-usage-tab.js` | The rebuilt Context tab (CC-4). |
| `webapp/src/usage-hud.js` | The rebuilt HUD: per-turn cost, per-model usage, context percentage, rate-limit state. |

## Frontend — KEEP unchanged

`diff-viewer/`, `svg-editor/`, `svg-viewer.js`, `files-tab/`, `file-nav.js`, `commit-graph.js`,
`monaco-setup.js`, `monaco-worker.js`, `lsp-providers.js`, `markdown*.js`, `speech-*.js`,
`tex-preview.js`, `doc-convert-tab.js`, `doc-index-progress.js`, `viewer-routing.js`,
`rpc.js`, `rpc-mixin.js`, `message-search.js`, `input-history.js`, `image-utils.js`,
`file-mentions.js` — ~98,800 lines.

`lsp-providers.js` is worth calling out: it is the reason CC-2 keeps the symbol index. It is
unchanged, but its backend now has no other reason to exist.

---

## Specs — disposition

### DELETE

`3-llm/cache-tiering.md` (521) · `3-llm/streaming.md` (345) · `3-llm/history.md` (277) ·
`3-llm/edit-protocol.md` (241) · `3-llm/prompt-assembly.md` (227) · `3-llm/context-model.md` (205) ·
`3-llm/modes.md` (175) · `4-features/url-content.md` (245) · `7-future/parallel-agents.md` ·
`7-future/cache-tiering-piggyback-promotion.md` · `7-future/reasoning.md` ·
`7-future/mcp-integration.md`

The `7-future/` deletions are all *implemented by the platform* rather than abandoned: parallel
agents → `Task` tool, reasoning → SDK `thinking` / `effort` config, MCP integration → the bridge in
`3-engine/`. Their design reasoning is preserved in git history and, where still relevant, folded
into the replacement spec.

**Companion twins deleted with them:** `specs-reference/3-llm/cache-tiering.md`,
`context-model.md`, `edit-protocol.md`, `prompt-assembly.md`, `streaming.md`, `prompts.md`,
`prompts/`.

### NEW — `3-engine/`

`session.md` · `permissions.md` · `tool-surface.md` · `history.md` · `context-visibility.md` ·
`mcp-bridge.md`, plus companion twins under `specs-reference/3-engine/` for the RPC payload shapes
and the on-disk session-store schema.

### ADAPT

`README.md` · `0-overview/architecture.md` · `0-overview/glossary.md` ·
`0-overview/implementation-guide.md` · `1-foundation/rpc-inventory.md` ·
`1-foundation/configuration.md` · `2-indexing/symbol-index.md` · `2-indexing/document-index.md` ·
`2-indexing/reference-graph.md` · `4-features/images.md` · `4-features/code-review.md` ·
`4-features/collaboration.md` · `5-webapp/chat.md` · `5-webapp/agent-browser.md` →
`subagent-browser.md` · `5-webapp/viewers-hud.md` · `5-webapp/settings.md` · `5-webapp/shell.md` ·
`5-webapp/file-picker.md` · `6-deployment/packaging.md` · `6-deployment/startup.md` ·
`6-deployment/build.md`

### KEEP unchanged

`1-foundation/jrpc-oo.md` · `1-foundation/rpc-transport.md` · `1-foundation/repository.md` ·
`2-indexing/keyword-enrichment.md` · `4-features/doc-convert.md` · `5-webapp/diff-viewer.md` ·
`5-webapp/svg-viewer.md` · `5-webapp/file-navigation.md` · `5-webapp/search.md` ·
`5-webapp/speech.md` · `5-webapp/tex-preview.md` · `impl-history/*`

---

## Verification gates

A phase is not complete until its gate passes.

| Gate | Command / check |
|---|---|
| No LiteLLM | `grep -rn "litellm" src/ tests/ pyproject.toml` returns nothing. |
| No emoji edit protocol | `grep -rn "🟧🟧🟧\|🟨🟨🟨\|🟩🟩🟩" src/ webapp/src/` returns nothing. |
| No tier vocabulary in live code | `grep -rniE "\b(L0|L1|L2|L3)\b|stability_tracker|cache_membrane" src/ webapp/src/` returns nothing. |
| Indexes still serve the browser | Monaco hover, go-to-definition, references, completions all resolve in a repo with mixed Python/JS/C++. |
| `SessionStore` conforms | `claude_agent_sdk.testing.run_session_store_conformance(make_store)` asserts all **14** contracts with an empty `skip_optional` — a green run over fewer contracts means an optional method was silently skipped, not that it passed. |
| Context tab is truthful | Tab totals match `/context` run in the CLI against the same session. |
| Permissions cannot be answered remotely | A non-localhost client's attempt to resolve a permission request is rejected and logged. |
