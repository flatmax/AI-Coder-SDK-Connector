# Work Log

Active reimplementation work. This file holds in-flight plans, known bugs, deferred work, and process notes that don't yet belong in the historical archive at `specs4/impl-history/`.

When a plan completes and its audit trail stabilises, move it to [`plans-archive.md`](plans-archive.md). When a decision is settled, add it to [`decisions.md`](decisions.md).

## Build order

Per `specs4/0-overview/implementation-guide.md#build-order-suggestion`:

0. **Layer 0 — scaffolding** — package skeleton, CLI entry, build config, webapp shell, tests
1. **Layer 1 — foundation** — RPC transport (jrpc-oo), configuration, repository
2. **Layer 2 — indexing** — symbol index, doc index, reference graph, keyword enrichment
3. **Layer 3 — LLM engine** — context, history, cache tiering, prompt assembly, streaming, edits, modes
4. **Layer 4 — features** — URL content, images, review, collaboration, doc convert
5. **Layer 5 — webapp** — shell, chat, viewers, file picker, search, settings
6. **Layer 6 — deployment** — build, startup, packaging

Each layer depends only on layers below. Complete and test each layer before proceeding.

## Recently completed — preservation pass (2025-01)

Before deleting specs3 and the source tree, a preservation pass added byte-level / numeric-constant twins for the webapp specs and mirrored LLM prompt text into `specs-reference/3-llm/prompts/`.

### Twin files added

Seven new reference twins covering detail that would otherwise be lost when specs3 and the source tree are deleted:

- `specs-reference/5-webapp/svg-viewer.md` — SvgEditor constants, per-element drag dispatch, path parser, coordinate math, handle role identifiers, auto-generated ID filter regex, marquee visual feedback, copy-as-PNG pipeline, keyboard focus guard
- `specs-reference/5-webapp/chat.md` — Request ID format, finish-reason badges, compaction event stage routing, retry prompt templates, file mention accumulation, system event message templates, scroll constants, content-visibility thresholds, input history cap, image limits, edit block diff highlighting, post-response compaction timing, localStorage keys, cross-component paste suppression flag
- `specs-reference/5-webapp/file-picker.md` — localStorage keys, panel width constraints, context menu action IDs, inline input modes, shift+click vs regular click semantics, three-state checkbox cycle, deleted file exclusion cleanup
- `specs-reference/5-webapp/shell.md` — dialog dimensions, resize handle table, drag threshold, startup overlay timing, global keyboard shortcuts, synchronous selection capture rule, window resize throttling, repo-scoped localStorage keys for file/viewport restore
- `specs-reference/5-webapp/file-navigation.md` — spatial layout constants, animation timings, PLACEMENT_ORDER / REPLACEMENT_ORDER arrays, DIR_OFFSET table, node data model, event detail flags, file type color palette, Alt+Arrow capture-phase listener rule
- `specs-reference/5-webapp/viewers-hud.md` — Token HUD auto-hide timing, geometry, cache hit rate colors, budget bar thresholds, tier color palette, category colors, content group type icons, provider-cache-rate precedence, terminal HUD format, localStorage keys
- `specs-reference/3-llm/prompts.md` — index documenting contracts per prompt file; points at sibling files in `specs-reference/3-llm/prompts/` for content

### Prompt files as a synced mirror

Rather than embedding verbatim prompt text inside `prompts.md` (which would have required extensive four-backtick fence escaping for files containing edit-block markers, and would have created a drift risk on every prompt improvement), the prompts live as standalone sibling files:

```
specs-reference/3-llm/
  prompts.md              — index + contracts-per-file documentation
  prompts/
    system.md
    system_doc.md
    review.md
    commit.md
    compaction.md
    system_reminder.md
    snippets.json
    llm.json
    app.json
```

`scripts/sync_prompts.py` copies the nine files from `src/aic_dc/config/` into `specs-reference/3-llm/prompts/`, comparing bytes before writing so unchanged files are no-ops. Re-run after any prompt edit; commit both sides of the diff together. When the source tree is deleted, the mirror becomes authoritative.

The script itself avoids the edit-block self-reference problem by reading bytes off disk and writing them verbatim — it never constructs marker sequences in its own source code.

### Implementation guide updates

- `specs4/0-overview/implementation-guide.md` "Where specs4 Is Incomplete Without specs-reference" table updated: the prompt-text row now points at `specs-reference/3-llm/prompts/` (the directory) and mentions the sync script. The "Use specs-reference/3-llm/prompts.md for" heading was updated to reflect the index/mirror split.
- `specs-reference/README.md` "What Stays Outside" section updated: prompt text is no longer called out as "stays in the source tree." A new "Synced Mirror" section documents the `prompts/` directory as the special case.

### specs3 retirement

With prompts mirrored and the seven webapp reference twins in place, the [Where specs4 Is Incomplete Without specs-reference](../0-overview/implementation-guide.md) table has no remaining rows pointing into specs3. specs3 can be deleted with `git rm -r specs3/`.

### Other observations

- `specs4/1-foundation/jrpc-oo.md` was noted as a potential gap but left as-is per user request.
- No twin was created for several spec4 files judged self-sufficient during the pass: `specs4/2-indexing/reference-graph.md`, `specs4/2-indexing/keyword-enrichment.md`, `specs4/3-llm/modes.md`, `specs4/4-features/code-review.md`, `specs4/4-features/images.md`, `specs4/4-features/url-content.md`, `specs4/5-webapp/agent-browser.md`, `specs4/5-webapp/search.md`, `specs4/5-webapp/settings.md`, `specs4/5-webapp/speech.md`, `specs4/5-webapp/tex-preview.md`, `specs4/6-deployment/packaging.md`. If a byte-level detail turns out to be missing during reimplementation, grep the source tree (before deletion) or add an ad-hoc twin and update the implementation guide's table.
- Future missing twins follow the same mechanical rule: `specs4/{path}/{name}.md` ↔ `specs-reference/{path}/{name}.md`; add a row to the implementation guide's table; twin gets byte-level / numeric detail only, not behavior.

## Next tasks

Layers 0–4 complete. Layer 5 is substantially delivered — core interaction loop (chat + selection + viewing + editing + search + review) fully functional. Remaining Layer 5 work, in order of readiness:

1. **UI polish work plan** — delivered. All four commits shipped (viewer relayout on dialog/window resize, Alt+1..4 / Alt+M shortcuts, file picker left-panel resizer, specs4 docs catch-up for dialog chrome). See [`plans-archive.md`](plans-archive.md#ui-polish-work-plan--complete).
2. **Doc Convert tab — commit 6** — delivered. Scope identified post-Commit 5: clickable output paths in the summary view's progress rows. Before Commit 6, users finishing a conversion had to close the summary, switch to the Files tab, navigate to the output file, and click it — four clicks for the core post-conversion task ("review the diff"). Commit 6 upgrades successful progress rows' output-path text to a button element that dispatches `navigate-file` so the app shell routes to the diff viewer directly. Failed and skipped rows keep plain-text detail since there's no output to navigate to. Closes out the post-conversion workflow specs4/4-features/doc-convert.md describes end-to-end. The Doc Convert frontend is feature-complete.
3. **Agent-mode UI plan** — Increment A delivered (see D30 in [`decisions.md`](decisions.md) and the dedicated plan section below). Increments B–E (live tabs, refresh rehydration, per-turn historical view, cross-turn view) are queued; B+C+D form the next coherent push when agent-mode UI work begins.
4. **Collaboration UI — on pause.** Backend collab (Layer 4.4/4.5) is fully in place; the frontend surface (admission flow pending screen, admission toast, participant UI restrictions, connected-users indicator, collab popover with share link) is deliberately deferred. Revisit when someone actually wants to run a multi-client session; building the UI on spec without a real testing workflow would accumulate staleness. **Reaffirmed 2026-08-27** — still on pause, with the live consequence now written down ([`../next.md`](../next.md) § E): the participant-restriction half *is* built, so with collaboration enabled every connection after the first is auto-denied by the 120-second pending timeout.

**Layer 6 (build & deployment) — was on pause; off pause as of 2026-08-27.** Phase 7 is the active work and the checklist is [`../next.md`](../next.md) § A2. What follows is the reasoning that parked it, left as written. PyInstaller packaging, release workflow, Vite → webapp-dist bundling, and version baking are all deferred. The system needs more hardening at the application layer first — sharper edges in the existing features, deeper test coverage on the paths users actually exercise, and any latent correctness bugs surfaced in day-to-day use — before packaging cost becomes worth paying. Revisit after a hardening pass decides what's actually ready to ship. Related deferrals that land with Layer 6: the webapp-dist bundling rule (D7 in [`decisions.md`](decisions.md)), the version-baking mechanism described in `specs4/6-deployment/build.md § Version Baking`, and the GitHub Actions release matrix described in `specs-reference/6-deployment/build.md § PyInstaller command — release build`.

## Agent-mode UI plan

The agent-mode foundation has shipped piecewise across Layers 3–5: edit-protocol marker tolerance (D20), `_stream_chat` ConversationScope refactor (D24), execution plane (D25), and now `agent_blocks` persistence (D30). What remains is the user-facing UI surface that consumes this infrastructure.

Five increments, sequenced by value-vs-effort. Each is a standalone deliverable; together they realise the chat-panel-as-agent-browser model specs4/5-webapp/agent-browser.md describes.

A separate plan — "Agents as first-class persistent entities" — sits below this one. It picks up where the UI plan ends, addressing the architectural gap that agent state currently doesn't survive session-reset or session-reload the way main's state does.

### ~~Increment A — `agent_blocks` persistence (no UI)~~ — delivered

Backend-only foundation. Persists the per-turn `{id, agent_idx}` mapping on every orchestrator-spawned assistant record so future cross-turn reconstruction views can recover an agent's full session transcript without scanning archive contents. Zero user-visible change. Pure forward-compatibility — every agent-mode turn from this point forward is reconstructable.

Delivered via `HistoryStore.append_message` accepting `agent_blocks`, `_stream_chat` parsing the response a second time at persistence-write time and threading the summary through `archival_append`. See D30 in [`decisions.md`](decisions.md) for the full rationale and `specs4/3-llm/history.md` § Cross-Turn Agent Reconstruction for the contract.

### ~~Increment B — Live agent tabs (current turn)~~ — already delivered (audit recovery, post-A)

Plan-status correction. When the agent-mode UI plan was first written, this increment was tagged "biggest piece, not started." A subsequent audit of the actual webapp source — prompted by the question of whether to start Increment B — revealed the work was substantially complete already, shipped piecemeal across earlier sessions before the plan was written.

What the audit found in `webapp/src/chat-panel/` and `webapp/src/files-tab/`:

- **Tab strip rendering** — `renderTabStrip()` in `tabs.js` produces the strip with active-class, streaming indicators, close buttons, mode-aware tooltips, and an overflow menu for many tabs. Hidden when only the main tab exists; appears the moment a second tab materialises.
- **Per-tab state** — `_tabs: Map<id, state>` on the chat panel, with `makeTabState()` factory and `installReactiveAccessors` (in `state.js`) installing prototype getters/setters that forward every reactive property to the active tab's slot. `noAccessor: true` in `properties.js` opts out of Lit's default accessor installation.
- **Tab activation** — click handler in `tabs.js`, Alt+` cycling via `onChatTabShortcut`, LED-row click via `led-row.js`'s `scrollTabIntoView`. The setter on `_activeTabId` snapshots / restores per-tab URL chip state across the singleton `aic-url-chips` element so chip state per tab survives switching.
- **`agentsSpawned` event** — handler in `streaming.js` (`onAgentsSpawned`) calls `spawnAgentTabs` in `tabs.js`, creating tabs SYNCHRONOUSLY before child stream chunks arrive (the spec's tab-creation-ordering invariant — without this, fast-completing agents' chunks are dropped because no tab claims the child request ID yet).
- **Streaming routing** — `findTabForRequest` in `tabs.js` matches by exact ID first, then by parent-prefix (`{parent}-agent-NN`). Both `onStreamChunk` and `onStreamComplete` route through this. Pending-chunk coalescing per animation frame is per-tab.
- **`agent_tag` routing** — `send()` in `input.js` reads the active tab id, passes it through `parseAgentTabId` (`null` for main, the id otherwise), and threads the result as `chat_streaming`'s `agent_tag` argument. Stale-agent error response (`{error: "agent not found"}`) closes the tab locally and toasts the user.
- **LED row** — `led-row.js`'s `renderLedRow()` produces one dot for main plus one per agent tab. State is cyan (streaming) / green (clean) / red (error) per `getLedState()`; tooltip via `formatLedTooltip()` carries id + mode + outcome ("running" / "completed (N edits applied)" / "<failure reason>"). Click activates the tab and scrolls the strip into view.
- **File picker per-tab scope** — `_selectedFilesByTab` and `_excludedFilesByTab` Maps in `files-tab/index.js`; `active-tab-changed` listener swaps picker state to the new tab; `applySelection` and `applyExclusion` route to `LLMService.set_selected_files` for main / `set_agent_selected_files(agent_id, files)` for agent tabs (mirror for exclusion).
- **Close affordance** — `onTabClose` in `tabs.js` removes the tab from `_tabs` and `_tabLabels`, switches to main if the closed tab was active, fires the `close-tab` event, and calls `LLMService.close_agent_context(agent_id)` fire-and-forget.

Test coverage in `webapp/src/chat-panel/tabs.test.js` (~1100 lines), `streaming.test.js` (~1300 lines), `state.test.js`, `led-row.test.js`, and `webapp/src/files-tab/per-tab.test.js` pins every contract item above. Hundreds of test cases.

Why this section says "already delivered" rather than just striking it through: the level of detail above is the audit trail. A future session reading the plan should not re-do the audit to confirm — the catalog of what was found, where it lives, and how tests pin it is the documentation that prevents wasted re-implementation work. The lesson for the plan-writing process is to read the source before writing scope statements; symbol maps showed the existence of `spawnAgentTabs`, `parseAgentTabId`, `_makeTabState`, `installReactiveAccessors`, `findTabForRequest`, and `renderTabStrip` but the original plan still labelled the whole increment "not started." The audit corrected that.

### ~~Increment C — Tab rehydration on refresh / reconnect~~ — already delivered (audit recovery, post-A)

Same audit, same finding: implemented and tested. `rehydrateLiveAgents` lives in `webapp/src/chat-panel/events.js`; it's called from `onRpcReady` after the proxy publishes. The function calls `LLMService.list_live_agents()` (returns one entry per registered agent), passes the entries to `rehydrateAgentTabs` in `tabs.js` to materialise writable tabs, then loads conversation content per turn via `get_turn_archive(turn_id)` filtered to each agent's `agent_idx`. Per-tab LED outcome is recomputed from archive content via `computeOutcomeFromArchive()` (last assistant record's edit_results determine clean/error; cyan can't be recovered because the frontend can't subscribe mid-stream).

Tab IDs use the agent's LLM-chosen string id (per D26 flat identity); `deriveAgentTabLabelFromEntry` produces the user-facing label, recognising positional ids (`agent-NN`) and rendering them as `Agent NN` for visual consistency with spawn-time labels while preserving descriptive ids (`frontend-chat`) verbatim. Rehydration is idempotent — agents already in `_tabs` (e.g. from an earlier `agentsSpawned` in the same connection) are skipped.

The handler is fire-and-forget on errors: a failed `list_live_agents` call logs at console-error level (skipping the routine "method not found" case for stripped-down test fixtures) but never surfaces a toast — running on every connect, transient failures shouldn't punish the user with a notification on every reload.

### ~~Increment D — Per-turn historical view~~ — delivered

Implements agent-browser.md § "Historical Turns". Three commits:

- **Commit 1 (`7774c18`)** — field threading. `turn_id` and `agent_blocks` ride through stream-complete, session-changed, state-loaded, and compaction-event mutation paths. Without this, persisted-then-reloaded messages lost the metadata the renderer needs.
- **Commit 2 (`d3b203f`)** — view-agents affordance. Assistant messages from previous agentic turns render a "View agents (N)" button below the body. Visibility gated on partial-overlap (at least one agent no longer live in the tab strip), so the active turn — whose agents are already reachable via the strip — doesn't grow a duplicate button.
- **Commit 3 (`65b82da`)** — load handler. Click fetches the archive via `get_turn_archive(turn_id)`, creates read-only tabs with `historical:{turn_id}/{agent_id}` ids, marks them `readOnly: true`, disables the input on those tabs, and switches to the first new tab. Idempotent re-clicks clear prior historical tabs so the strip doesn't accumulate.

Deferred:
- **Scroll-away cleanup.** Spec says historical tabs should disappear when the user scrolls away from their parent message. Currently the user closes them explicitly via the existing tab close button or by triggering another View Agents click (which clears the strip first). Not critical to D's value; revisit if the manual-close UX produces complaints.

### ~~Increment D.5 — Inline agent-spawn card rendering~~ — delivered

Closes a gap that D and the original A–E plan both missed: when the orchestrator emits a `🟧🟧🟧 AGENT … 🟩🟩🟩 AGEND` block in its assistant response, the chat panel now renders it as a card inline in the message body — symmetric to edit-block cards. Before this, the markers fell through the segmenter as plain `text` segments and rendered as preformatted prose, which read as visual garbage in the orchestrator's output.

Three files, one new module, no backend changes:

- **`webapp/src/edit-blocks.js`** — `segmentResponse` learned a `reading-agent` state and emits `agent` / `agent-pending` segments alongside the existing `text` / `edit` / `edit-pending` types. Body parsed into `{id, task, mode}` via a new `_parseAgentBody` helper that uses a field-name allowlist (`id` / `task` / `mode`) so multi-line task bodies containing `Requirements:` / `Notes:` / `Examples:` headings don't truncate. Mirrors the backend `EditParser`'s allowlist contract from `specs-reference/3-llm/edit-protocol.md`.

- **`webapp/src/agent-block-render.js`** (new) — pure rendering helpers symmetric to `edit-block-render.js`. Exports `renderAgentCard(segment, status)` plus piecewise helpers (`renderAgentId`, `renderModePill`, `renderTaskBody`, `renderStatusBadge`, `resolveDisplayStatus`) and the `STATUS_META` table for tests. Status enum is `pending` / `streaming` / `complete` / `error` per `specs4/7-future/parallel-agents.md` § Frontend agent-block rendering. Long task bodies (more than ~6 lines or 600 chars) wrap in `<details>` so the card doesn't dominate the message.

- **`webapp/src/chat-panel/rendering.js`** — `renderAssistantBody` dispatches `agent` / `agent-pending` segments to `renderAgentCard`. Wraps the unsafeHTML output in a Lit `<div class="agent-block-wrapper">` with a delegated click handler `_onAgentCardClick` that reads `data-agent-id` off the chip and flips `panel._activeTabId` directly when a tab with that id exists. No round-trip through `events.js` — same in-file pattern the file summary chips use.

- **`webapp/src/chat-panel/styles.js`** — agent-card styling. Magenta accent (`rgba(210, 168, 255, ...)`) distinct from the edit-block blue so users can tell at a glance whether a card is "the LLM proposes a file edit" or "the LLM spawned a worker agent". Status-badge variants for the four statuses with a `agent-pulse` keyframe for the streaming state. Long-task `<details>` styling.

Status binding reads per-tab streaming state via the same Map the LED row uses: tab id == agent id under D26's flat-identity contract, so `_resolveAgentStatus` is a direct `panel._tabs.get(id)` lookup with no parsing. `tab.streaming` → `streaming`; `tab.lastEditOutcome.status === 'clean'` → `complete`; `tab.lastEditOutcome.status === 'error'` → `error`; tab present but no completion yet → `pending`; tab missing entirely (historical message whose agent has been closed) → `pending`. Card status reflects state at render time; live updates piggyback on the message card's existing re-render triggers (chunk arrivals, completion events) — no separate subscription.

What this delivery does NOT cover:

- **Tests for the new behaviour.** `agent-block-render.test.js` and the new `reading-agent` cases in `edit-blocks.test.js` should land as a follow-up.
- **Status-update push.** A historical-but-still-live agent's card may show stale status until the parent message re-renders for some other reason. A `requestUpdate` on `agentsSpawned` and on each agent's `streamComplete` would fix this; deferred until a real case appears where the staleness is visible.

### Increment E — Cross-turn agent history view

Implements the feature D30's `agent_blocks` persistence enables. UI affordance: a control on a live agent's tab (e.g., "show full history across all turns") that walks the main store, finds turns where that agent's `id` appears in `agent_blocks`, and presents a unified view. Or equivalently: a "filter by agent id" view in the history browser.

Scope:
- Backend RPC `get_agent_history(agent_id)` that scans `history.jsonl` for assistant records with non-empty `agent_blocks` containing the matching id, then for each match calls `get_turn_archive(turn_id)` filtered to that turn's `agent_idx`. Returns the concatenated record list in turn order (chronological by user-message timestamp).
- Frontend affordance — exact UX TBD based on B-D usage. Could be a button on the agent tab, a filter in the history browser, or both.

Highest-spec, lowest-volume use case. Defer until B-D are in real use; once you've used the per-turn view for a few weeks, you'll know exactly what cross-turn affordance is missing — and `agent_blocks` will already be in the JSONL waiting.

### Delivery order

Increments A, B, and C delivered. (B and C were marked "not started" in the original plan but a post-A audit found both already shipped — see their respective delivery-note sections.) D is the next concrete piece of work; it depends on B's tab construction infrastructure (which is in place). E follows once D has shipped and seen real use. Each remaining increment gets its own delivery-note section here when it lands.

## Agents as first-class persistent entities

The agent-mode UI plan above (A–E) makes agents *visible* in the chat panel — tabs, LEDs, archives, historical views. This plan addresses the orthogonal architectural gap: agents currently aren't *persistent* the way main's session is. Three concrete symptoms today:

1. **Clicking "new session" doesn't reset agents.** The button is rendered on every tab including agents but only ever resets main. Confusing UX. Bug surfaced 2025-01.
2. **Reloading an old session restores main's history but no agents.** Agent archives sit on disk untouched. Even though `agent_blocks` (per D30) records which agents participated in each turn, no code path uses that to reconstruct agents.
3. **Mid-session per-agent mode changes are not supported.** The spec (system_agentic_appendix.md) explicitly says mode is fixed for the life of an agent. To change mode, the user must close the agent and respawn under a new id. This was a deliberate trade for simplicity — but it means a code-mode agent can't be promoted to code+xref to consult docs without losing its conversation history.

The architectural shift: treat agents the way we treat main. Their mode can change. Their state persists across session-reset and session-reload. Reconstructing an agent from disk produces a live, writable ContextManager — same as main's session-reload behaviour.

Five increments, ordered cheap-to-architectural so each commit ships a working improvement and the system stays internally consistent at every step. Each commit lands with tests; spec updates land with the commits that change behaviour.

### ~~Increment 1 — Hide tab-scoped buttons that don't apply to agents~~ — delivered

Pure UI fix, no backend, no spec change. The chat panel's action bar renders new-session, history-browser, mode toggle, cross-reference toggle, and snippet drawer on every tab. Several of those don't make sense on agent tabs.

Audit of what was already gated and what wasn't, conducted on the live source:

- **Mode toggle (code/doc segmented)** — already gated on `panel._activeTabId === 'main'` from earlier work. ✓ no change needed.
- **Cross-reference toggle** — already gated on `panel._activeTabId === 'main'`. ✓ no change needed.
- **New-session button (✨)** — was ungated. Calls `onNewSession` which clears main's session id and history. Wrong on agent tabs. Now gated.
- **History button (📜)** — was ungated. Opens the history-browser modal which loads sessions over main's context. Wrong on agent tabs. Now gated.
- **Reasoning toggle (🧠)** — deliberately left alone. Per-call setting that applies to whichever tab is sending; agents can benefit from reasoning too. The experimental flag still gates whether it renders at all.
- **Snippet drawer button (✂️)** — deliberately left alone. Snippets are per-mode and useful on every conversation type.
- **Search bar** — deliberately left alone. Works on every tab (message search within the active tab; file search globally).

Implementation: extended the existing `panel._searchMode === 'file' || ...` gate around the new-session/history `action-group` to also include `panel._activeTabId !== 'main'`. Both buttons sit inside the same div and route through the same divider, so a single compound condition gates the whole group cleanly.

Test coverage in `webapp/src/chat-panel/action-bar.test.js` (16 tests across six describe blocks): main-tab visibility for all four button groups, agent-tab hiding of the four hide-target buttons, search-bar and snippet-drawer survival on agents, read-only historical-tab hiding (inherits the same rules since the tab id isn't `'main'`), file-search mode preserving its pre-existing hide, compound gate behavior (file-search + agent both true), tab-switching reactivity in both directions, reasoning-toggle symmetric-rendering invariant (if it renders on main, it renders on agents — pins the deliberate non-gate).

Why Increment 1 *first*: the audit-then-action sequence reveals that the spec gap was narrower than the original plan implied. Half the controls the plan called out were already correctly gated. The remaining gap was a single render expression covering two adjacent buttons. The 30-second user-visible benefit (no more "I clicked new-session and nothing happened") is realised via a one-line gate change plus tests pinning the contract.

### ~~Increment 2 — `new_session` closes all live agents~~ — delivered

Backend-only change with broadcast-driven frontend visibility. Reverses the D24/D25 "agents survive new_session" policy after audit revealed the resulting UX bug: the new-session button was rendered on every tab including agents but only ever reset main, leaving users on agent tabs clicking and seeing nothing happen. Increment 1 hid the button on agent tabs (palliative); Increment 2 fixes the underlying policy.

Implementation in `src/aic_dc/llm/_rpc_state.py:new_session`:

1. Clear `_active_agent_streams` first — signals any in-flight agent task to stop on its next chunk check, before its scope vanishes underneath it.
2. Snapshot the agent ids, then `_agent_contexts.clear()` to free `ContextManager` + `StabilityTracker` + `file_context` per agent.
3. Broadcast `agentClosed {agent_id}` for each snapshot id BEFORE `sessionChanged`. Order matters: `sessionChanged` triggers the chat panel to reload main's empty history, so agent tabs need to be gone first to avoid the "live tab with empty history" flash.
4. Per-turn archive files on disk survive — closing frees memory; transcripts stay readable via `get_turn_archive` for any turn the agent participated in.

Tests in `tests/test_llm_service/test_sessions.py § TestNewSessionClosesAgents` cover empty-registry no-op, single/multi-agent close, agentClosed payload shape (`{agent_id: str}` — pinned so future additions trip the test rather than silently break the frontend handler), broadcast ordering, in-flight stream cancellation, post-close agent-not-found behaviour, and archive-file preservation. The existing `test_new_session_preserves_agent_scopes` in `test_agent_lifecycle.py` was updated to `test_new_session_closes_all_live_agents` reflecting the new contract.

Spec updates deferred — `specs4/5-webapp/agent-browser.md § Tab Lifetime` and `specs4/7-future/parallel-agents.md § Agent lifetime` both currently document the old "agents survive" policy and need updating. Filing as a follow-up doc-mode pass since the behavioural test suite is now the authoritative specification.

Frontend impact — none required for Increment 2 itself. The chat panel needs an `agentClosed` event handler that calls `_onTabClose(detail.agent_id)` to remove the tab and free per-tab state. Without that handler, agent tabs persist on the frontend after `new_session` until the user closes them manually (the backend has freed the scope, so any RPC routed to those tabs returns `{error: "agent not found"}` — which the existing stale-tag handling in `chat-panel/streaming.js` already converts to a tab-close + toast). The frontend behaviour is therefore correct-but-unpolished without the handler; landing the handler is part of the next webapp commit.

### Increment 3 — Persist per-agent state on disk

Forward-compatible foundation for Increments 4 and 5. Doesn't change runtime behaviour today; lays the bytes that future reconstruction reads.

Two sub-changes:

#### ~~3a — Extend `agent_blocks` with initial state~~ — delivered

The orchestrator's assistant record now persists per-agent state alongside identity. Extends D30's `{id, agent_idx}` shape with three optional fields:

- `mode` — one of `code` / `doc` / `code+xref` / `doc+xref`. Captured at write time so reconstruction (Increment 5) installs the right system prompt when rebuilding the agent's ContextManager.
- `cross_reference_enabled` — bool. Redundant with the mode string's `+xref` suffix but stored explicitly so reconstruction reads one field rather than parsing the mode. Future per-agent xref toggle (Increment 4) writes this directly.
- `model` — provider-qualified id like `anthropic/claude-sonnet-4-5`. Today every agent inherits the orchestrator's model; field is forward-compat for a future per-agent override and ensures reconstruction routes an agent's continuation to the same provider it spawned against.

Implementation in two places:

1. **`HistoryStore.append_message`** (`src/aic_dc/history_store.py`) — `agent_blocks` filter extended to accept and round-trip the three new fields. Defensive validation: unknown mode strings dropped (so a future write-side mode value can't corrupt records the read-side doesn't recognise), strict bool check on cross-ref (rejects 0/1 ints), empty-string model dropped. Required `id` and `agent_idx` contract unchanged — optional fields don't promote malformed entries to valid.

2. **`stream_chat`** (`src/aic_dc/llm/_streaming.py`) — persistence-write site builds the per-block summary with mode resolution mirroring the `agentsSpawned` broadcast path (existing agents on retask keep their current mode; fresh spawns resolve via `_resolve_agent_mode` against the orchestrator's scope). Model read from `service._config.model` rather than from the agent's ContextManager since fresh-spawn scopes don't exist yet at this persistence point. Defensive: a config read failure must not block persistence — reconstruction tolerates a missing model field.

Tests: `TestAgentBlocksOptionalFields` in `test_history_store.py` pins the persistence contract (round-trip, partial fields, all four valid modes, defensive filtering for unknown/non-bool/non-string values, backwards-compat with pre-3a bare `{id, agent_idx}` shape). `TestAgentBlocksPersistence` in `test_agent_spawn.py` extended with four end-to-end tests through `_stream_chat`: explicit + inherited mode persist correctly, cross-ref flag both values round-trip, model field carries the orchestrator's config value, retasked agent persists the existing agent's mode (not the orchestrator's drifted mode).

Backwards-compat: D30 records (bare `{id, agent_idx}`) load correctly; reconstruction tolerates absent fields by falling back to current orchestrator state. Records with the new fields load on systems that don't yet read them — JSON dict round-trips ignore unknown keys.

#### 3b — Write per-agent mode-change events to the archive

Lands with Increment 4. When per-agent mode toggles ship, each toggle writes a system-event message to the agent's `.aic-dc/agents/{turn_id}/agent-NN.jsonl` archive marking the transition. Reconstruction replays these events to arrive at the agent's final mode rather than relying solely on the spawn-time mode in `agent_blocks`.

Scope:
- `HistoryStore` — new `append_agent_system_event(turn_id, agent_idx, event_type, payload)` helper for mode-change events.
- Tests pinning the on-disk shape.

No frontend change. No reconstruction yet — just the data.

### Increment 4 — Per-agent mode toggle

The user-visible feature: the mode toggle on an agent tab actually changes that agent's mode.

Split into two parts. 4a (backend) lands first; 4b (frontend) follows.

#### ~~4a — Backend: per-agent mode RPCs~~ — delivered

Two new RPCs in `_rpc_state.py`:

- **`LLMService.switch_agent_mode(agent_id, mode)`** — accepts the four combined mode strings (`code` / `doc` / `code+xref` / `doc+xref`) and flattens them into the agent's ContextManager's two axes. Validates the id, the mode shape, and the mid-stream guard (rejects when `agent_id in _active_agent_streams`). On a real change: applies the mode and cross-ref flags, rebuilds the agent's `StabilityTracker` (every tier prefix invalidated by the new prompt + index combination), writes a mode-change system event to the agent's archive via `scope.archival_append`, broadcasts `agentModeChanged`. No-op short-circuit when the new state matches the current state — saves a needless tracker rebuild and skips the event/broadcast.

- **`LLMService.set_agent_cross_reference(agent_id, enabled)`** — same shape, axis-isolated. Critical contract: toggling cross-ref MUST NOT touch the primary mode. Pinned by `test_disables_xref_preserving_mode` which exercises a `doc+xref → doc` transition and asserts `Mode.DOC` survives.

Helper functions in the same module: `_parse_agent_mode_string` (wire string → `(Mode, bool)`), `_format_agent_mode` (the inverse, used for archive event content), `_rebuild_agent_tracker` (replace `scope.tracker` with a fresh `StabilityTracker` and re-attach via `scope.context.set_stability_tracker`).

Archive system events follow the format `"Mode changed: {old} → {new}."` for both RPCs — reconstruction (Increment 5) can parse one format. Sink failures are logged at WARNING and swallowed; the in-memory mode change is authoritative.

Both RPCs are localhost-only, matching the rest of the agent-keyed surface. `LLMService` exposes thin delegators (`switch_agent_mode`, `set_agent_cross_reference`).

The orchestrator's prompt-time descriptor (per `specs4/7-future/parallel-agents.md` § Per-agent state descriptor) already reads each agent's mode from its live ContextManager via `build_agent_descriptor`, so a successful switch is visible to main on its very next turn without any further wiring. The "verify" item from the original 4a scope is satisfied by inspection of `_agents.py:build_agent_descriptor` which calls `cm.mode` and `cm.cross_reference_enabled` — both now fresh after a switch.

Tests: `TestSwitchAgentMode` (12 tests) and `TestSetAgentCrossReference` (12 tests) in `test_agent_lifecycle.py` cover happy paths for every transition, no-op short-circuits, unknown-agent errors, malformed-mode errors (string and non-string), mid-stream rejection, tracker rebuild side-effect, ContextManager attachment update, archive event persistence with content matching the format above, broadcast payload shape (`{agent_id, mode, cross_reference_enabled}`), no-broadcast on no-op. Companion `*LocalhostOnly` classes pin the restriction gate.

What 4a does NOT deliver: per-agent mode-change events being interleaved with normal agent conversation in the archive. The archive write happens via `archival_append` which targets the agent's `agent-NN.jsonl` file directly — so the sequence is correct and chronological, but the agent's NEXT turn's records (when the user replies) come AFTER the mode-change event, not woven in. This matches the design — system events are operational records, not conversational turns.

What 4a sets up for 4b: the RPC surface is stable, the broadcast event name (`agentModeChanged`) is fixed, and the frontend can route a single click to `switch_agent_mode` for combined transitions OR to `set_agent_cross_reference` for the overlay-only toggle. Same pattern as the main-tab segmented + overlay UI.

#### ~~4b — Frontend: render mode toggle on agent tabs and route to per-agent RPCs~~ — delivered

Reverse of Increment 1's hide for the mode toggle group. The mode toggle now appears on every tab (main, agent, historical); the new-session and history buttons stay main-only because their semantics don't translate (see Increment 1 for the original gate).

Implementation:

- **`webapp/src/chat-panel/rendering.js`** — Extracted the existing inline toggle JSX into a new `renderModeToggle(panel)` helper. The helper resolves state via `_resolveActiveTabMode(panel)` which reads from `_mode` / `_crossRefEnabled` for main and from `_tabModes.get(activeTabId)` for agents. The combined mode strings (`code` / `doc` / `code+xref` / `doc+xref`) parse cleanly with `endsWith('+xref')` + `replace('+xref', '')`. Disabled rules baked into the helper:
  - RPC disconnected → every tab disabled
  - Main + non-localhost → disabled (collab participants can't switch host's mode)
  - Agent + streaming → disabled (matches backend's mid-stream rejection in `LLMService.switch_agent_mode`)
  - Historical (read-only) tab → disabled (the ContextManager no longer exists)
  - Tooltips adapt: agent + streaming shows "Wait for the agent to finish before switching mode" rather than a generic mode description.

- **`webapp/src/chat-panel/events.js`** — Split `switchMode` and `toggleCrossRef` into main / agent dispatchers. The exported entry points (`switchMode(panel, mode)` and `toggleCrossRef(panel)`) are still called from the rendered template; they now branch on `panel._activeTabId === 'main'` and route to `_switchMainMode` / `_switchAgentMode` (or the cross-ref equivalents). Agent dispatchers compute the combined mode string from `_tabModes` (preserving xref state across primary-axis switches) and call `LLMService.switch_agent_mode` / `LLMService.set_agent_cross_reference`. New `onAgentModeChanged(panel, event)` handler updates `_tabModes` and `requestUpdate()`s on the `agent-mode-changed` window event. Wired in `bindEventHandlers` / `attachEventListeners` / `detachEventListeners`.

- **`webapp/src/app-shell/index.js`** — New `agentModeChanged(data)` server-push method. The backend's `LLMService.switch_agent_mode` and `set_agent_cross_reference` broadcast `agentModeChanged` per Increment 4a; the AppShell translates it to a window-level `agent-mode-changed` event so the chat panel's listener fires.

- **`webapp/src/chat-panel/action-bar.test.js`** — Updated Increment 1's hide assertions for the mode toggle. The "on agent tab" describe block now asserts the mode toggle and cross-reference toggle are PRESENT (was: hidden). The historical-tab block now asserts the toggle renders but every button is `disabled`. New-session, history, and search-bar tests are unchanged — those gates are still correct.

What 4b deliberately does NOT deliver:

- **Optimistic UI updates.** Click → RPC → broadcast → re-render is the full loop. The user sees the new state on the broadcast, not on the click. Matches main's pattern. Without optimistic updates, a failing RPC (mid-stream rejection, restricted caller, network error) leaves the toggle in its actual state rather than briefly showing the user-attempted state then snapping back.

- **Spec updates.** `specs4/5-webapp/agent-browser.md` should document the per-agent toggle; `src/aic_dc/config/system_agentic_appendix.md` should relax the "mode is fixed for the life of the agent" claim. Filed as a follow-up doc-mode pass — the behavioural test suite is now the authoritative spec for the mid-session mode-change behaviour.

What the user can now do (the scenario from the original 4b plan):

1. Spawn an agent in `code` mode (orchestrator's current mode at spawn time).
2. Switch to the agent's tab. Click 🔀 (cross-ref toggle).
3. Backend's `LLMService.set_agent_cross_reference` rebuilds the agent's tracker, archives the change, broadcasts `agentModeChanged`.
4. Frontend updates `_tabModes` for that agent → re-renders the toggle in the active state → tab strip tooltip updates to `<id> (code+xref)`.
5. Reply to the agent. The next LLM call assembles the agent's prompt with both indexes active.

The orchestrator's prompt-time descriptor reads each agent's mode from the agent's live ContextManager (per Increment 4a's verification), so main sees the new mode in its context on its next turn — no additional plumbing needed.

After 4b lands, Increment 5 (reconstruct agents on session-load) is the remaining piece. Increments 1–4 between them ship the full per-agent persistent-entity surface for the live session; Increment 5 makes that surface survive session restore.

### ~~Increment 5 — Reconstruct agents on session-load~~ — delivered

Three commits across one session: backend reconstruction skeleton with spawn-time baseline (`2284b5b`), archive replay of mode-change events (`312828b`), and `agentsRehydrated` broadcast wiring through to the chat panel's tab-materialisation path (`5ebc844`).

The capstone. Loading an old session now restores not just main's history but every agent that participated — as live, writable scopes reachable via the agent-keyed RPC surface, with their final modes intact (mid-session toggles replayed from archive events) and their full conversation history pre-populated. Reconstructed agents are indistinguishable from agents that have been continuously alive since their first spawn turn — the user can reply, the orchestrator can retask them, the LED row reflects their state. Provider cache starts cold (the rebuilt StabilityTracker has no tier assignments) but everything else works.

Specs landed alongside the implementation: `specs4/3-llm/history.md § Session-Load Reconstruction` (the full nine-step algorithm with replay-from-archive as authoritative source) and `specs4/7-future/parallel-agents.md § Agent lifetime` (paragraph noting session-load as a reconstruction event symmetric with refresh / reconnect).

Replay strategy is **(b)** per spec: walk every `system_event: true` record in the agent's concatenated archive looking for `"Mode changed: {old} → {new}."` content, parse the trailing target, update running state. The spawn-time `agent_blocks` entry serves as the replay's starting baseline only — a mid-session toggle from `code` to `doc+xref` reconstructs as `doc+xref`, not as `code`. Strategy (a) (use spawn-time mode without replay) was rejected because it would silently lose every retask toggle on session reload.

Per-commit delivery record:

**Commit 1 — Reconstruction skeleton with spawn-time baseline only** (`2284b5b`).

`reconstruct_agent_scope` in `src/aic_dc/llm/_agents.py` constructs a ContextManager via the existing `build_agent_context_manager` factory, pre-populates history from concatenated archive content, attaches a fresh StabilityTracker, and registers the scope in `service._agent_contexts[agent_id]`. `_reconstruct_agents_from_session` in `src/aic_dc/llm/_rpc_history.py` walks the session's full records (NOT the context-load shape, which strips `agent_blocks`), groups by agent id keeping the latest record per id (retask wins), concatenates archive content across every turn the id appeared in, and resolves mode from the latest spawn entry. Wired into `load_session_into_context` between the history-set step and the `sessionChanged` broadcast; idempotent against partial registration.

After Commit 1, agents reappeared in `_agent_contexts` after session-load with their conversation history but with the spawn-time mode rather than the post-toggle mode — known-wrong intermediate state per replay strategy.

**Commit 2 — Replay mode-change events on top of spawn-time baseline** (`312828b`).

`_replay_mode_events` in `src/aic_dc/llm/_agents.py` walks `archive_messages` for `system_event: true` records whose content matches the strict format `"Mode changed: {old} → {new}."` produced by `switch_agent_mode` and `set_agent_cross_reference`. Each valid event advances running `(Mode, cross_ref)` state to the parsed target; malformed events skip without raising. `reconstruct_agent_scope` now calls the replay before constructing the ContextManager, so the scope's mode is the post-replay result, not the spawn-time baseline.

The strict format (prefix + arrow + terminating period) is deliberate. A loose match like `"Mode changed: code → doc"` (no terminator) would tolerate writer-side regressions silently — pinning the exact format means a future change to the writer surfaces as a quietly-lost replay rather than continuing to "work" with subtly wrong state.

**Commit 3 — `agentsRehydrated` broadcast + frontend wiring + integration test** (`5ebc844`).

`load_session_into_context` captures `pre_existing_ids = set(_agent_contexts.keys())` before reconstruction, then broadcasts `agentsRehydrated` with the diff against the post-reconstruction registry — only the just-reconstructed ids appear in the payload, not the full registry. Pre-existing agents (from the current session, before the load) stay live but don't re-trigger frontend tab creation. Fired AFTER `sessionChanged` so the chat panel's session-changed handler (which clears messages + streaming state) runs first; reverse order would briefly render the new agent tabs against the old session's main-tab content.

Frontend wiring: `agentsRehydrated` server-push method in `webapp/src/app-shell/index.js` re-dispatches as a window-level `agents-rehydrated` event. `onAgentsRehydrated` in `webapp/src/chat-panel/events.js` calls the existing `rehydrateLiveAgents(panel)` — same path Increment C uses on `onRpcReady` after a browser refresh. The frontend doesn't need to filter by id; `rehydrateLiveAgents` itself queries `list_live_agents()` and creates tabs idempotently.

Integration test `test_full_round_trip_two_service_instances` runs the end-to-end loop: service A spawns an agent (registering the scope, persisting the spawn record with `agent_blocks`, seeding the agent's first user message via `scope.context.add_message`), toggles its mode (writing the mode-change archive event), then a fresh service B with the same history store (simulating a server restart) loads the session and reconstructs. Asserts the agent is in `service_b._agent_contexts`, mode is the post-replay value (Mode.DOC), conversation history contains both the initial task and the mode-change event, and `agentsRehydrated` fired on service B with the agent's id.

What this delivery does NOT cover:
- Per-agent selected-files / excluded-files reconstruction — deferred (always ephemeral, even refresh loses them today).
- Reconstructing a session whose agents' archive files were partially deleted — remaining content reconstructs normally; deleted turns contribute nothing. Replay walks whatever archive content exists.
- Cross-session agent migration — sessions remain isolation boundaries.
- Frontend test for the `agents-rehydrated` → tab materialisation path. Backend behaviour is authoritative and covered by integration tests; the frontend handler is a one-line forwarder to the already-tested `rehydrateLiveAgents` path. Filed for a future commit if the manual UX exposes a regression.

### Delivery order

Increments 1 → 2 → 3 → 4 → 5. Each is a standalone commit (or small commit cluster). The chain dependency:

- 1 unblocks visible UI work without committing to a backend strategy.
- 2 fixes the immediate confusion you reported. Independent of 3+.
- 3 lays bytes for 4 and 5. Doesn't change runtime behaviour, so it can ship without coordinated frontend work.
- 4 needs 3a (initial mode persistence) and 3b (mode-change archive events) to be useful long-term. Without 3, mode changes work in-memory but vanish on reset. We could ship 4 without 3 but it'd be a half-feature.
- 5 needs 3 fully, plus the rebuilt-tracker logic. The reconstruction code is the most novel part of the plan.

Each increment gets its own delivery-note section here when it lands. Strike-through and commit hash, same convention as the agent-mode UI plan.

### What this plan does NOT cover

- **Per-agent selected-files / excluded-files persistence.** Currently lost on every refresh. Worth fixing eventually; not blocking the core "agents as first-class" shift. Separate small commit when needed.
- **Per-agent model selection.** Could a user run agent-0 against Claude Opus and agent-1 against GPT-4? `agent_blocks` extension in 3a includes the model field for forward-compat, but no UI ships in this plan. Separate feature when desired.
- **Cross-session agent migration.** Could the user pull an agent from session A into session B as a new conversation? No — sessions are isolation boundaries. Mixing agents across them isn't part of the model.
- **Agent-to-agent communication.** Agents converse with the user (via their tab) and with the main LLM (via assimilation). Not with each other. Out of scope here, out of scope in `parallel-agents.md`.

## Known bugs — per-tab state

### ~~Agent tab shows duplicated user prompt + stuck cursor after agent completes~~ — fixed

**Symptom.** When the orchestrator spawned agents, the agent's tab showed the user prompt twice — once before the streamed response, once after — and the streaming cursor remained visible indefinitely even though the backend log confirmed `finish_reason='stop'` had fired. After hard browser reload (which loads from archive via `get_agent_history`), the persisted state showed exactly one user prompt + one assistant response per agent, confirming the duplicates were frontend-only.

In two-turn agent sessions the bug compounded:

```
- user prompt 1
- agent response 1
- user prompt 1 (duplicate)
- user prompt 2
- agent response 2
- user prompt 2 (duplicate)
```

Hard reload shrunk this to four items (the persisted truth).

**Root cause.** `spawnAgentTabs` was invoked twice per agentic turn:

1. Eagerly from `onAgentsSpawned` when the backend's `agentsSpawned` broadcast arrived (BEFORE child streams dispatched, so tabs could claim child request IDs in time to route chunks).
2. As a fallback inside main's `onStreamComplete` handler when `result.agent_blocks` was non-empty (intended for older backends that only surface agent blocks via `streamComplete`).

Modern backends emit BOTH events for every agentic turn. The second call hit the retask branch (added in the earlier per-tab streaming routing fix) for every tab — because the tabs already existed from call 1 — and the retask branch appended the user task to `existing.messages` and re-set `existing.currentRequestId = childId`, `existing.streaming = true`. By the time the fallback fired, the agent's child stream had already completed and cleared those flags, so the re-arm produced a cursor that would never advance.

**Fix.** Memoise on `parent_request_id` inside `spawnAgentTabs`. The first invocation for a given parent request runs the create/retask logic; subsequent invocations with the same parent request id no-op. Turn boundaries are distinguished by parent request id (each turn has its own), so retask in turn 2 still appends correctly while the duplicate fallback inside turn 2 no-ops. The memo set is session-scoped — `new_session` doesn't clear it, but parent request ids carry an epoch prefix and don't collide across sessions in practice.

**Why memoise rather than remove the fallback.** The fallback path's stated purpose (older-backend support) is preserved — for a backend that emits only `streamComplete` and not `agentsSpawned`, the first invocation IS the fallback, and it runs normally because no prior call recorded the parent request id. The memo only suppresses redundant work, not necessary work.

**Spec updates.** `specs4/5-webapp/agent-browser.md` § Tab Creation Ordering gained a new "Idempotency under the dual-event design" paragraph documenting the dual-call architecture and the memoise-on-parent-request-id contract. See D32 in [`decisions.md`](decisions.md) for the deeper architectural rationale.

### URL fetch result lands in wrong tab when user switches tabs mid-fetch

**Symptom.** A URL fetch is initiated from agent tab A (user clicks "Fetch" on a chip). While the RPC is in flight (GitHub repo clone + symbol map generation can take 10+ seconds), the user switches to agent tab B. When the fetch resolves, `chipsEl.markFetched(url, result)` runs against whichever tab's chip state is currently installed on the singleton `aic-url-chips` element — which is B, not A. The user sees a chip for a URL they never fetched on B, and A's own chip stays in `fetching` state indefinitely.

**Root cause.** Per-tab URL chip state (D23 Commit 4) is swapped in/out of the singleton `aic-url-chips` element on tab switch via `_snapshotUrlChipsForTab` / `_restoreUrlChipsForTab`. The fetch RPC closure in `_onUrlFetchRequested` captures the `chipsEl` reference, not the tab ID — so when the promise resolves, the mutation lands on whichever tab is currently showing.

**Fix shape (when this becomes a real pain point).** Capture the originating tab ID at fetch-initiation time, look up that tab's state slot when the promise resolves, and mutate the state slot directly (rather than via the singleton element). If the originating tab is still active, also mutate the live element. If it's inactive, the snapshot carries the updated state and restoration on next tab switch surfaces it. Same pattern for `markErrored`.

**Why deferred.** The bug only fires when the user actively switches tabs during a multi-second fetch — rare outside of GitHub repo clones. The common case (stay on the tab while URL fetches complete) works correctly. Fixing it requires threading the tab ID through three async paths (`markFetching`, `markFetched`/`markErrored`, and the chat-panel-level view-content dialog's fallback fetch) and adding a per-tab chip-mutation helper that operates on snapshots rather than the live element.

**Grep for `TODO(url-fetch-cross-tab)` in `chat-panel.js` when attacking this.**

## Deferred cleanup

Temporary scaffolding installed to keep a test/output path quiet, with the fix scheduled for a specific future phase. Grep `TODO(phase-` across the tree to find markers.

- **`webapp/src/app-shell.test.js` — `describe('setupDone')` console.error silence.** The `beforeEach`/`afterEach` pair in the setupDone describe block installs a `vi.spyOn(console, 'error').mockImplementation(() => {})` to swallow errors from the files-tab's `onRpcReady` handler when it tries `Repo.get_file_tree` on a fake proxy that doesn't implement it. The errors are genuine — the files-tab genuinely can't fetch the tree — but they're out of scope for app-shell tests which focus on shell-level wire-up, not files-tab RPC behavior. **Remove when:** Phase 2d expands these shell tests (or adds a separate integration test class) that publishes a richer fake proxy including `Repo.get_file_tree`, at which point the files-tab's RPC call succeeds and the console.error goes away naturally. The TODO comment in the test file references `TODO(phase-2d)` so it shows up in that phase's grep sweep.

## Specified but not yet built

Entries land here when a spec commits to something the implementation has not caught up to; they move
to § Landed since when it does. **What is still open across the whole suite, in order, is
[`../next.md`](../next.md)** — this section is one tab's worth of it, kept here because the drift it
records is worth more than the to-do list it leaves behind.

### The Settings tab spec describes a tab three times its size

Found while building the model panel, by reading
[`../5-webapp/settings.md`](../5-webapp/settings.md) against `webapp/src/settings-tab.js` line by line.
The tab renders a toolbar, a one-row info banner, the model panel, a two-card grid and an inline
editor. The spec describes all of that plus five preference cards, four session controls, a
retired-files note, and a per-field save disposition. None of the latter existed. Six have since been
settled: two were built (the save disposition and restart session) and four turned out to belong to
another surface — engine health and MCP status to the Context tab, the permission chime and the live
permission mode away from the preference cards entirely.

This was not a case of the spec running ahead of a build — `/permissions` had been *routing* to one of
the missing controls, and the route's own copy promised "the Settings tab's permission-mode control plus
the rules list". Correcting that copy is what exposed the rest. So the entry is worth more than a
to-do list: it is the record of a spec section that drifted far enough to make a shipped command lie.

**Three different problems, needing three different fixes.** Lumping them together is what let the
whole section rot unnoticed.

**(a) Right feature, wrong tab — a spec fix, not a code fix.** ~~These exist and work; the spec files
them under Settings and they live in the Context tab or the permission dialog.~~ **Settled 2026-08-26 —
see § Landed since.** `get_config_info` returns `{"config_dir": ...}` and nothing else, so the § Info
Banner bullets asking for credential source and CLI version described a banner that could not render them
from the RPC it reads.

| Specified as | Actually in | Read from |
|---|---|---|
| § Info Banner "credential source", "resolved `claude` path and version" | Context tab; also the chat panel's health banner | `get_engine_health` |
| § Session Controls "Engine health" | Context tab | `get_engine_health` |
| § Session Controls "MCP servers" status | Context tab § Session — which is where `/mcp` already routes | `get_mcp_status` |
| § Preference Cards "Permission chime" | The permission dialog | `localStorage` |

The resolution ~~is almost certainly~~ **was** to move these in the spec rather than build second copies:
a second engine-health panel would be a second thing to keep true, and `/mcp` routing to the Context tab
is already the better answer. Recorded rather than done at the time because it was nine items of spec
arbitration and the call on each was the author's, not the implementer's.

**(b) Backend built, no caller — the shape `set_model` was in.** ~~Two MCP control RPCs exist in
`service.py`, both localhost-gated, both with **zero callers anywhere in `webapp/src`**.~~ **Built
2026-08-26 — see § Landed since.** Both are now actions on the Context tab's server rows.

| RPC | What it does | Surface it got |
|---|---|---|
| `reconnect_mcp_server(name)` | Re-dials one server | Offered on a `failed` / `needs-auth` row |
| `toggle_mcp_server(name, enabled)` | Enables or disables one server | The same row, confirmed when enabling |

This was precisely the position `set_model` occupied before that session: working control requests that
nothing could reach. Unlike `set_model` they did not even need a new surface — the Context tab already
lists every server with its connection state and token cost, so both are actions on a row that was
already on screen.

`toggle_mcp_server` got the moment's thought its docstring asks for — *"Enabling a server hands the agent
a new set of tools; the host is the one who decides which tools exist"* — and the answer was to keep the
toggle but split the two directions, since only one of them grants anything. What that thought actually
turned on was a fact neither the docstring nor this entry had — and the fact this entry first reached for
was wrong. See § Landed since below: `aic-dc` *is* in the list, and it is the one row that gets no toggle.

**(c) Neither side exists.** Specified, with nothing behind it on either the engine or the browser:

- ~~**Restart session.** No RPC, no control.~~ **Built 2026-08-26 — see § Landed since**, together with
  the per-field save disposition it was paired with. The two were one item in practice: § The Applies
  Column Is Load-Bearing says a save touching a next-session field "offers the only thing that would
  apply it", so a disposition with no restart to offer would have named the problem and left it, and a
  restart with no disposition would have had nothing to name.
- ~~**Per-field save disposition.** § Save Behavior has the save reply driving an "applied / applies next
  session" summary.~~ **Built 2026-08-26 — see § Landed since.** The invariant that depended on it —
  *"a save never shows an unqualified success for a field that did not apply"* — is now enforced at the
  toast, not only in the panel.
- **Thinking display toggle** — no `thinking_display` reference anywhere in `webapp/src`.
- **Doc enrichment toggle** — no `keywords_enabled` reference anywhere in `webapp/src`.
- **Deny-read scope reset** — no `aic-dc-deny-read-scope` key anywhere in the tree.
- **The permission chime's mute.** Added to this list by (a)'s arbitration rather than found in it: the
  dialog *reads* `aic-dc.permission-chime` and nothing writes it, so the mute is reachable only from a
  devtools console. It was a Settings preference card, which is why the gap read as "specified, not built"
  on a tab that should never have held it. Now owned by
  [`../5-webapp/permission-dialog.md` § Attention](../5-webapp/permission-dialog.md), beside the thing
  that rings.
- **Session storage size** — nothing to call. The backend measures the session directory only as a
  turn-time warning (`_disk_warning`), not as a readable RPC.
- ~~**The retired-files note.** § Deleted cards argues for it at some length — a user who customised
  `system_extra.md` over months and finds the card gone "deserves to know why" — and then no note is
  rendered.~~ **Built 2026-08-28 — see § Landed since.** It was the cheapest item in this list and the
  only one whose absence the spec had already called a mistake, which is a combination that should have
  got it built two phases earlier than it was.

**Where to start.** ~~(b) is an afternoon and closes a real gap.~~ **(b) is done.** ~~Then (a), because a
spec that names the wrong tab is how `/permissions` came to lie and the same trap is still set for eight
more items~~ **— (a) is done too** — and note that (b) turned up a ninth of exactly that kind, since this
section's own table filed the MCP server list under Settings when the right home for it was the Context
tab all along. ~~**(c) is what is left**, and within it the restart-session pair first, since two
invariants currently have nothing behind them.~~ **That pair is built.** What is left of (c) is the three
preference-card items (thinking display, doc enrichment, deny-read scope), session-storage size — which
still has no RPC to call — and the retired-files note, which remains the cheapest thing on this list.

**How to keep this from recurring.** The drift was invisible because nothing reads a spec section
against the component that implements it. The two mechanisms that *did* catch things this session were
both cheap and both mechanical: `test_every_rpc_is_classified` refused `get_model` until it was
classified, and the `/permissions` route made a claim specific enough to check by opening the tab. A
`test_every_rpc_has_a_caller_or_is_listed_as_dormant`, in the same shape as the classification test,
would have caught `reconnect_mcp_server` and `toggle_mcp_server` years earlier.

The precedent for the *annotation* half is [`../plan/delivery.md` § Dormant, annotated, not
deleted](../plan/delivery.md) — but note it tracks the opposite direction of dead wiring: push receivers
with no emitter, and browser call sites into a retired `LLMService`. A caller-less **RPC** is the mirror
image and appears on no list, which is exactly why two of them sat unnoticed. Modelled on that section
rather than already covered by it.

**✅ Built 2026-08-28 as `tests/test_rpc_surface.py`** (next.md § C5), and the guess in the paragraph
above was low. It covers all five registered services rather than the one this section was about, and
the audit that had to happen first found **ten more callerless RPCs, not two** — plus, running the same
scan backwards, the one browser call into a namespace no service registers, which is the *other*
direction this section says it does not cover. So one test now watches both. Of 100 exposed methods, 66
have a browser caller, 22 are internal-only and 12 are dormant; the reasoning lives in
[`../1-foundation/rpc-inventory.md`](../1-foundation/rpc-inventory.md) § *Who Calls These* and the two
findings worth building are next.md §§ C7–C8.

**The part worth carrying forward is why reading had not already found these.** Two docstrings had
noticed and stopped: `Settings.is_reloadable` reasons its way to the right answer mid-sentence
("actually wait, jrpc-oo exposes everything non-underscored"), and `shutdown` explains why its gate does
not obstruct a caller that does not exist. Both are careful prose by someone looking straight at it.
The classification tests work because they refuse to be read past, and that is the difference.

### Landed since

- **A tool card's header is two columns, and nothing is pinned right** — 2026-08-28, asked for in the same
  conversation as the entry below and superseding its `.tool-header-end` group. The user's question was
  whether the time chip could move under the tool name, "so that we have only two columns, left (tool tag,
  time chips) and right which is the assistant / user entries in the chat", with the observation that closed
  the argument: *everything is line wrapped now, so there is rarely a one line entry*. The rail's second line
  is therefore free, and my first answer — that stacking would cost every card a line — was priced against a
  one-line card that had stopped existing the day before.

  **The measurement that made it worth doing is not the one either of us was arguing about.** Chrome pinned
  to the right is subtracted from *every* line of the summary, not just the line it sits on, because the
  summary is one box and a box that ends before the time chip ends before it all the way down. Measured in a
  520px pane across eight cards, the old layout gave the summary between **180px and 402px** depending on how
  much chrome its card carried — an MCP call with a server chip, a long name, a gated marker and a clock got
  180 — and every card started its text at a different x. The rail gives all eight **384px from one x**.
  So the honest gain is not the ~8px a plain `Bash` card wins on the right; it is that the cards that were
  squeezed worst stopped being squeezed at all, and that the column is a column.

  **The rail is sized by the tool name, not by the clock.** Sized to the clock (5.5rem) it looked right until
  `TodoWrite` and `NotebookEdit`, which are wider than a time-of-day, dropped to a rail line of their own and
  left a caret and a status dot stranded on the line above. A name whose line number depends on its character
  count is the one thing in the rail that cannot wrap, because the name is what a reader scans the rail *for*.
  7rem holds the longest built-in beside the caret and the dot. MCP names are unbounded and wrap; a server
  chip wider than the rail now breaks inside itself rather than overflowing into the summary's column, which
  is the one place on the card that must have no chrome in it.

  **Under 360px the rail lies down.** The first version of this was a straight loss at the width the previous
  day's work had gone to trouble over: 7rem out of a 300px pane is over a third of it, and the summary
  measured **164px against the 231px** the flex layout gave it, because that layout could drop a squeezed
  summary onto a line of its own and a fixed column cannot. So below a card width of 360px the header
  collapses to one column with the metadata as a row above the summary — 282px there, better than either.
  It is a container query, not a media query: the pane is a dialog the user drags, so the viewport's width
  is not the question being asked. `.tool-card` carries `container-type: inline-size`, which is safe on a
  card because a card's width comes from the column it sits in and never from its contents. The override
  block sits last in the section, since a container query adds no specificity and source order is all that
  decides — the first placement put it before the `.tool-time` rule it overrides, and the clock went on
  stacking in the flattened row until a screenshot showed it.

  **The clock and the elapsed stack instead of sharing a line.** `12:29:29 PM · 2m 41s` wants about 110px
  of the rail's 112 — a two-pixel fit, and no fit at all on a card restored from another day, whose chip
  carries a date in front of the clock — and while the rail was still 88px the middle dot was left dangling
  at the end of a wrapped line, which is what the change was made to stop. The two spans keep a space between them in the markup — it draws nothing, since
  whitespace between flex items is discarded, and it is the only thing stopping "14:32:07" and "2m 41s"
  running together into one word for anything reading text content.

  The guards are structural where they can be: the header's children are asserted to be exactly the rail and
  the summary, every piece of chrome is asserted to be *inside* the rail, and an untimed ungated card is
  asserted to draw one rail line rather than an empty second one. Only the fixed track width is read from
  `STYLES.cssText`, per § C4. Read in Chrome at 520, 400 and 300px, including an expanded `Edit` card, to
  check that containment left the diff body alone.

- **A tool card's header wraps its summary instead of eliding it** — 2026-08-28, reported from a screenshot
  rather than found in the queue. A `Read` card read `file_path=/home/…/repos/softwaredesignlifec…`, which
  names no file: the header is the only place a *collapsed* card says what the call was about, it carries no
  tooltip, and the card's own body is a JSON echo, so the identifying tail of a path was not reachable
  anywhere. Every summary wraps now — [`../5-webapp/chat.md`](../5-webapp/chat.md) § *Card Anatomy* holds the
  rule.

  **The exception was already there and its argument was general.** `Bash` summaries had wrapped since phase
  2, for the stated reason that "eliding it eats exactly the tail that says what it does: which paths, which
  flags, which test file" — a sentence with nothing in it about `Bash`. The same entry then kept every other
  tool on one line to buy a uniform row height. So this was not a new decision so much as noticing that one
  had been made twice, in opposite directions, two paragraphs apart
  ([`../plan/delivery.md`](../plan/delivery.md) § *`Bash` summaries wrap; nothing else does*, now annotated).
  Three `[data-tool='Bash']` blocks over five selectors are gone, and what they said is what `.tool-summary`
  and `.tool-header` say now for every tool.

  **The line clamp went too, and that is the part worth arguing.** A three-row `-webkit-line-clamp` is the
  same ellipsis three rows lower. The engine's 200-character `TOOL_INPUT_SUMMARY_CHARS` cap already bounds
  how tall a card can get, so the clamp was a second bound on a quantity that had one.

  **A browser found the bug the suite could not.** `overflow-wrap: anywhere` on a `flex: 1` item was correct
  and not sufficient: the summary's basis was 0, so the row never wrapped and the summary absorbed every
  shortfall instead — at `DIALOG_MIN_WIDTH` (300px) an MCP card, which spends its row on a server chip, a
  long tool name, a gated marker and a time chip, squeezed the summary to about ten pixels and rendered a
  hundred-character path as **one broken character per line** — a column taller than the viewport. That is worse than the
  ellipsis it replaced, it is reachable by dragging the dialog, and jsdom cannot see it. Two changes fixed
  it: a `10rem` flex basis, which is what makes the summary the item that *does not fit* and therefore the
  one that wraps, and `flex-wrap` on the header. Read at 520, 400 and 300px in Chrome against a scratch page
  that mounted the real renderer with the real stylesheet.

  **One DOM change fell out of the same check.** The gated marker, the time chip and the caret are now one
  `.tool-header-end` element rather than three siblings, because three siblings wrap one at a time and the
  first thing a narrow pane did was strand the caret alone on a line of its own. As a group they travel
  together and an auto margin pins them right, which is also what stopped the gated marker reading as part
  of the wrapped path beside it. **Superseded within the day** by the entry above: the group is gone and its
  three items are in the rail. The lesson survived the element — the caret is still kept where it cannot
  strand, which is now first rather than grouped.

  The guard is read from `STYLES.cssText`, the convention § C4 established for rules jsdom cannot execute,
  and it was checked to fail with the elision restored. It pins the absence of `line-clamp` and of any
  `data-tool='Bash'` rule across the whole sheet rather than the presence of properties in one rule, because
  the way this regresses is someone re-adding the special case, not someone editing this declaration.

- **A file chip is named the way the rest of the app names files** — 2026-08-28, § C4. Every path on a
  tool card is absolute, because the CLI's file tools require it, and the chips displayed it: a label
  spending its width on a prefix identical for every file in the repo, with the part that identifies the
  file at the end that falls off. The label is now the repo-relative name, on the tool-card footer chips,
  the turn footer's "files modified" list and the "Files Referenced" chips alike, with the engine's path
  on the tooltip and the accessible name. [`../5-webapp/shell.md`](../5-webapp/shell.md) § *The Same Rule
  Names Files On Screen* holds the rule.

  **The item had been open eleven days on a question that was already answered.** It was filed as a
  display decision with three options — basename, root-relative, middle-elided — and no reason to prefer
  one, so it kept being the thing to do later. The Context tab's memory-file table had stated the rule in
  a code comment **the day before** (`daa7fa9`, 2026-08-16; the chip fix that deferred the question is
  `218f89d`, 2026-08-17): *"A file inside the repo is named the way every other view in this app names
  files — relative to the root — with the engine's absolute path on the row's tooltip. One outside it
  keeps the absolute path, because that is the only name it has here."* The permission dialog already
  follows it, because the backend relativises before sending. And that rule *is* `toRepoPath`, which was
  built for navigation in `218f89d` and behaves exactly that way. So the work was applying an existing
  function, there is **no second helper for display**, and the two cannot drift apart. **An item can be
  blocked by its own framing:** "which of these three" had no answer and "what does this app already do"
  had one.

  **Three things the work found, none of them the item.** The chip had **no width budget at all** — no
  `max-width`, no ellipsis, unlike the `.file-chip` it sits beside, whose comment says "Full path is in
  the tooltip" — so one long path stretched the footer row. That is fixed and pinned from the stylesheet
  source, the way the slash palette pins its hint-width rules and for the same stated reason: jsdom does
  no layout, so only the rules' presence is checked. The shell's `_repoRoot` property is **gone rather
  than duplicated** — the root moved into the module holding the rule, because the chip renderers take a
  path and no host, and the cheap route would have been a third holder of the same string reached through
  the existing `state-loaded` event. And the "Files Referenced" list **deduplicated on the raw path**: its
  two sources spell paths differently — prose mentions are matched against the picker's list and so are
  relative, edit-block headers carry whatever the model wrote — so a file named both ways was already two
  entries, invisible until § C4 gave both the same label and would have rendered them as two identical
  chips. The key is now the relative name; the entry keeps the path as found, which is what leaves the
  absolute one available to the tooltip.

  **One path is left absolute on purpose.** The card *header*'s input summary still reads
  `file_path=/home/you/repo/…`. It is built server-side by `summarise_tool_input`, a `key=value` join over
  whatever keys the input happens to have, so it has no idea which of them are paths; giving it that idea
  means a per-tool table of path keys — which `permissions.py` already keeps a private copy of, and which
  would be a *third* mechanism answering "absolute → relative" against a suite that already has an item
  open about there being two. It belongs to that convergence, and it is stated in
  [`../5-webapp/chat.md`](../5-webapp/chat.md) § *Card Anatomy* rather than left to be noticed.

  Fifteen mutations, each checked to fail a test. On the module holding the root: dropping
  `setRepoRoot`'s guard (a snapshot without a `repo_root` then un-sets one an earlier snapshot
  established), making it write-once (a reconnect to a different repo then keeps the old root), making the
  test hook a no-op, defaulting `toRepoPath`'s root to `''` instead of the published one, and having
  `getRepoRoot` report `''`. On the chips: labelling with the raw path, putting the *relative* path on the
  tooltip, dispatching the label instead of the path, and the same three for the summary chips plus
  storing the relative form in the entry. On the stylesheet: removing the width budget. Two more re-pin
  behaviour that only moved house — deleting the snapshot's `setRepoRoot` call, and un-normalising
  `navigate-file` — and one pins the test suite itself: without `resetRepoRoot` in the shared
  `beforeEach`/`afterEach`, a test that publishes a root decides how every later test's paths are
  labelled, and the case asserting an absolute path *stays* absolute is the one that breaks.

- **The engine gets a graceful teardown, bounded, before the hard exit** — 2026-08-28, § C8, the last of
  § C5's findings to close. `ClaudeCodeService.shutdown` had no caller for its whole life while its
  docstring reasoned about one: the localhost gate "does not get in the way of the real caller ... so an
  in-process teardown hook passes". There was no hook. `main._shut_the_engine_down` is now that caller.

  **The analysis said delete it, and checking the specs reversed the decision.** Three of `shutdown`'s
  four steps are meaningless before `os._exit` — cancelling turn tasks, closing an executor and
  disconnecting a session all describe a process that is about to stop existing, and the two consequences
  that genuinely outlive it (the CLI child, the resumed session's temp config dir) were already handled by
  hand for exactly that reason. Step by step it reads as dead code. What it misses is two calls further
  down: `cancel_all` → `_deny_unanswered` → `_announce` **pushes the denial to the browser**, and the
  browser outlives the server. [`../5-webapp/permission-dialog.md`](../5-webapp/permission-dialog.md)
  § *Multiple Clients* enumerates four `cause` values a denial can name and `shutdown` was the one nothing
  could produce, so a user who stopped the server with a dialog open kept a live-looking dialog that never
  resolved. **The lesson is the order of operations**: reading the method bottom-up gave the wrong answer,
  and the spec that consumed its output gave the right one.

  **A courtesy, never a condition of exiting.** 2 seconds, then abandoned; every failure including the
  timeout swallowed at debug; a second Ctrl-C skips it entirely. The handler moved from `signal.signal` to
  `loop.add_signal_handler` for one reason — a C-level handler cannot await a coroutine — and the hard
  `os._exit` stays, because asyncio's runner cleanup is what hangs on `_heavy_init`'s model load. What the
  loop gets is one bounded task, not its own shutdown.

  **Two residues, both stated where they bite.** Windows has no graceful step at all, because
  `add_signal_handler` raises `NotImplementedError` on the proactor loop — the same platform split as
  `_kill_vite`'s process group, logged rather than worked around. And `is_caller_localhost` reads the
  *current* RPC caller, so a remote participant's call caught mid-dispatch by the signal can have the gate
  refuse the host's own teardown; the caller logs a warning instead of bypassing the gate, because a
  teardown path that does not consult the gate is a worse thing to own than a rare log line.
  [`../4-features/collaboration.md`](../4-features/collaboration.md) carries that one.

  **§ C5's own list did not catch this closure, and that is a finding about the list.** Wiring
  `set_viewer_state` a few hours earlier failed a test loudly (§ C7 below); wiring `shutdown` failed
  nothing. `DORMANT` is asserted against browser callers in both directions, but the Python direction is
  only asserted for `INTERNAL_ONLY`, where an entry names a file and the call in it is checked — so a
  dormant method that gains a *Python* caller keeps a stale entry until somebody moves it. This one moved
  by hand, after checking rather than assuming. The asymmetry is now written above `DORMANT`, with why the
  obvious repo-wide `.method(` scan is not the fix: `shutdown` is its own counter-example, since
  `doc_index/background.py`'s `self._executor.shutdown(wait=False)` is a different method spelled the same
  way. `DocIndexBuilder.close()` is still reached only through `shutdown`, so it is now reached at all.

  **The wiring was untested until it was extracted, and extracting it is what made the gap visible.**
  The graceful step is a module-level function and was pinned from the start; the arrangement that runs
  it — two `add_signal_handler` calls, the second-signal escape, the Windows fallback — was a closure
  inside `main()`, which the suite never drives. So `_install_exit_handlers(loop, service, teardown)` came
  out to module level, taking the teardown as a parameter because that is the only part that genuinely
  needs `main`'s locals (`vite_process`). Its tests fire **real signals at the test process** rather than
  calling the callback: what is worth pinning is that a Ctrl-C reaches the coroutine at all, and a
  hand-called callback would pass against `signal.signal` wiring that cannot await one.

  Twelve mutations, each checked to fail a test. On the step: dropping the `wait_for` bound (a hung engine
  then hangs the exit), narrowing the `except` so a raise escapes, dropping the early return after the
  log, dropping the `restricted` check, and warning on every answer. On the wiring: installing SIGINT and
  forgetting SIGTERM, tearing down before awaiting the graceful step rather than after, dropping the
  second-signal escape, never setting the `exiting` flag, dropping the `NotImplementedError` fallback, and
  using C-level `signal.signal` handlers instead of loop ones.

  Three findings came out of that pass rather than out of reading:

  - **A survivor that was the code's fault.** An `if service is None: return` guard whose state is
    unreachable, since the handlers are installed after the service is built. Both it and the test that
    asserted otherwise — "the signal can arrive before phase 1 has built one", which is false — were
    deleted rather than kept as unpinnable code.
  - **A test that passed for the wrong reason.** The second-Ctrl-C test polled for 5 seconds against a
    2-second grace period, so it passed with the escape deleted: the *first* signal's timeout produced the
    teardown. The window is now 0.2s, with the margin asserted rather than assumed, and the docstring says
    what it caught.
  - **A latent dependence on `os._exit` never returning.** `on_signal` fell through after the second
    teardown and queued another graceful step against an engine already being torn down — invisible in
    production precisely because `teardown` does not come back. A `return` makes the behaviour not depend
    on that, and the test now pins it by counting the engine's calls rather than the teardowns.

  Two stale names went with it: `resume_cleanup` and `session.py` both referred to `main._signal_handler`,
  which no longer exists, and both said the exit path "never reaches `disconnect()`" — it now reaches it
  on a 2-second budget on POSIX, which is a weaker claim than the by-hand cleanup needs, so the cleanup
  stays and says why. A third, unrelated, was found while making
  [`../6-deployment/startup.md`](../6-deployment/startup.md) § *Graceful Shutdown* true:
  `_cleanup_tex_preview_dir`'s docstring claimed a startup call that has never existed.

- **The agent is told which file the user has open** — 2026-08-28, § C7, the first of § C5's findings to
  close. `ViewerFraming` had two arrival paths and a writer on neither, so `Turn.viewer` was always
  `None` and the `ui_state` tool answered "nothing is open in the user's viewer pane" for the entire life
  of the app. `webapp/src/app-shell/viewer-framing.js` now pushes `set_viewer_state` from the shell's
  `active-file-changed` handler.

  **One writer, and the choice of which one is the whole design.** `chat_streaming`'s `viewer` argument
  stays null; the service's existing fallback feeds both readers from the single push. Answering in both
  places would have given one field two sources that can disagree — the shape § C3 keeps finding — and
  the per-turn argument is the worse source, because it only knows about turns that start in this
  browser, which is the case `ui_state` exists to answer. The server side needed no change: the fallback,
  and `test_the_last_push_frames_a_turn_that_sends_no_viewer`, had been asserting against a writer that
  did not exist.

  **`active-file-changed` over `navigate-file`**, because it reports what a viewer *has* open rather than
  what it was asked to open — a fetch can fail, and routing diverges when an SVG carries a scroll hint.
  Three cases follow from the event's own shape and each is pinned: repeats for one path are deduped (the
  SVG viewer re-emits on a same-file `openFile` on purpose, so the shell re-runs its visibility routing);
  a `null` from the *hidden* viewer is ignored, since something is still on screen; and the SVG viewer's
  synthesised `virtual://svg-compare/…` path reports as nothing open, because it is on screen but is not
  a file anything can read. A reconnect re-pushes — server-side viewer state is in memory.

  **The selection range is not wired, and that is stated rather than absorbed.** `set_viewer_state` takes
  `start_line` / `end_line` and `build_framing` renders them, but no selection plumbing exists in either
  viewer; it would take a debounced Monaco `onDidChangeCursorSelection` chain per editor, which is new
  surface, and a range that lags the cursor points the agent at lines the user is not on — the failure
  `chat-panel/input.js`'s own comment refused to risk. The file is the part worth having. § C7 carries
  the residue and nothing schedules it.

  **§ C5's test fired, which is the first evidence it works.** Adding the caller failed
  `test_a_listed_method_is_not_called_from_the_browser` and the message named the new call's file and
  line, so the stale `DORMANT` entry went in the same commit rather than surviving as an assertion that
  the gap it had just closed was still open. `navigate_file`, the other half of what § C7 named, is
  re-attributed to § E's collaboration pause where it belongs.

  Eleven mutations, each checked to fail a test: dropping the dedupe, dropping the hidden-viewer gate,
  treating `virtual://` as a real file, recording a push before it lands, dropping either RPC-availability
  guard, resending without standing the dedupe down, unwiring the handler, moving the report after the
  null-path return, and dropping the reconnect re-push. Two survived the first pass and both were the
  code's fault, not the tests': a redundant `if (!open) return` in the resend that the dedupe already
  covered (deleted), and a `typeof` guard whose only observable effect is *not* logging, which the test
  now asserts.

- **Every RPC is accounted for as called, internal or dormant** — 2026-08-28, § C5. `test_rpc_surface.py`
  partitions the 100 methods the five registered services expose and asserts the partition, so the next
  public method added to any of them fails a test until somebody answers "is this meant to be reachable
  from a browser?".

  **The audit was the deliverable and the test is what is left of it.** `add_service` publishes every
  public method, so the surface is not a list anybody wrote — 22 of the 100 turn out to be internal
  helpers that a browser can call, and their docstrings describe an internal contract. That is not a
  defect to fix; it is a fact to record, which is why `INTERNAL_ONLY` names the Python caller and a test
  checks that the named file still contains the call. A helper whose caller moves on becomes dormant
  without anything else noticing.

  **Twelve are dormant, and three of those were news.** `set_viewer_state` and the hardcoded `null` in
  `chat-panel/input.js` mean `ViewerFraming` has two arrival paths and a writer on neither, so the
  `ui_state` tool's `viewer` key is permanently null (§ C7 — *closed the same day; see the entry above,
  which leaves eleven dormant*). `shutdown` has no caller and a docstring
  that reasons about one (§ C8). `get_review_file_diff` is dead on both services it exists on, because
  review mode's soft reset makes the diff viewer's ordinary HEAD-versus-working-tree pair *be* the review
  diff — recorded in the inventory rather than queued, since deleting it is a separate decision from
  knowing it is dead.

  **Deliberately not fixed here.** The audit's job was to find and arbitrate, and turning three findings
  into three fixes in the same sitting is how an audit stops being repeatable. Each one is either queued
  with the decision it needs stated, or recorded with the reason it needs none.

  Every assertion was checked to fail without the code it pins — seven mutations, one per test: dropping
  a `DORMANT` entry, listing a live method as dormant, naming a deleted method, pointing an
  `INTERNAL_ONLY` entry at the wrong file, dropping the CC-12 exception, leaving a stale exception, and
  widening the scan's skip rule so it loses real call sites.

- **A control-request timeout stops printing a traceback** — 2026-08-28, the server-side half of § C1.

  The client gate above needs health to say the engine is gone, and there is one failure mode that never
  says it. **The SDK routes control responses on a detached reader task**, started once per session
  (`Query.start` → `spawn_detached(self._read_messages())`), not on the per-turn pump — which is why
  `get_context_usage` works between turns at all. When that reader dies it sends one error into the
  message stream and exits; after that every control request waits out its full 60 seconds forever, and
  `connected` stays true because nothing disconnected. The `CLIJSONDecodeError` from an oversized line
  that closed open item 1 is this path, and it is the one that produced the four tracebacks.

  **The obvious fix is ruled out by a measurement already in the specs.** Treating a timeout as evidence
  of a dead engine would kill working sessions: the call is measured at 3-14s and exceeds 60s often
  enough to log eight timeouts in one healthy half-hour run, so a threshold on consecutive timeouts
  would be a guess at a number the same paragraph says is routinely passed. Detecting the dead reader
  honestly means reading the SDK's private `_read_task`. So the residue is accepted and *stated* —
  [`../5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md) § *When the Engine Is Gone* — in the shape
  CC-18 established: the absence is written down rather than left silent, and pinned by tests.

  What is treated is the symptom the queue actually complained about. `_log_control_failure` logs a
  timeout as one sentence and everything else with its stack; the traceback was pure SDK plumbing
  between the service and an `anyio.fail_after`, and a polled caller repeated it. **Two details decide
  whether this is worth anything:** it keys on the chained `TimeoutError` rather than on the message,
  because the SDK raises a *bare* `Exception` with no class of its own and the wording is prose it may
  reword — a check a reword could silence is worse than none; and it fails towards noise, so anything
  unclassified keeps its stack. Applied to all eight control-request handlers rather than the polled
  one, since the reasoning is about control requests. `reconnect_mcp_server` and `toggle_mcp_server`
  had already logged this shape without a stack, so the rule was there incidentally and is now general.

- **A lost engine stops being polled, and the HUD has somewhere to sit** — 2026-08-28, closing
  [`../next.md`](../next.md) § C1 and [`../plan/README.md`](../plan/README.md) open item 2.

  The usage HUD called `get_context_usage` once per turn with no reference to whether there was an engine
  to call. Against a lost session that is a wasted round trip; against the worse case — a message pump
  that died *without* the session being marked lost, which leaves a client that still looks usable and a
  subprocess whose reply nobody is reading — it is a 60-second control-request timeout logged with a
  traceback, four of them in one log. None of it told anyone anything: the health banner had already
  reported the loss, in the engine's own words.

  **The fix is four lines of behaviour and one definition, and the definition was the work.** The HUD now
  listens for the pushed `engineHealth` record and treats the engine as gone when `connected` is false
  *and* `last_error` is non-empty. Gating on `connected` alone is the obvious version and it is wrong in
  a way that would have looked like a different bug: the field is false before the first prompt too, so
  the HUD would have stopped fetching on a freshly loaded page and never started. `last_error` is what
  separates "died" from "not yet", and it is reused rather than invented — `health-banner.js` already
  draws the line there, for the surface this note points the user at. One definition, two readers.

  **Two signals, and neither is redundant.** The push covers every turn after the loss but arrives after
  the `streamComplete` of the turn that *caused* it, so it cannot stop that turn's own fetch; the reply's
  `reason: 'no-engine'` closes the gate on that one. A reply cannot pre-empt the request it rides on, and
  a push cannot overtake the event ahead of it — so the two cases are disjoint and each needs its own
  signal. The `reason` field already existed for the Context tab's error note and needed nothing new.

  The state clears on a `connected: true` push, on a breakdown that arrives anyway, and on
  `session-changed` — that last one because starting or resuming is precisely what the note tells the
  user to do, and a flag surviving it would report a dead engine at a live one until a push happened
  along. The note is amber rather than error red (a condition, not a failed request), replaces the last
  good breakdown rather than sitting beside it (numbers from before the loss describe a window no engine
  holds), and says only that there is nothing to read — the banner owns the why. The turn's own receipt
  is untouched: a turn that failed after spending something still reports what it cost.

  **A spec claim fell out of writing it.** [`../5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md)
  § *Data Flow* said the HUD "renders entirely from the `streamComplete` payload", with a matching
  invariant that it "renders without a follow-up RPC", describing a design where the context percentage
  arrived on `postResponseComplete`. That payload has no such key and `usage-hud.js` has made the call
  since phase 3, so the claim was false for three phases and nothing caught it — the section documenting
  the gate could not be written next to one denying the call exists. Both are corrected. Eleven tests,
  each checked to fail without the code it pins, including the discriminator.

- **The compaction indicator survives a reload: a broadcast is not a record** — 2026-08-28, closing
  [`../next.md`](../next.md) § C6 and emptying [`../known-issues.md`](../known-issues.md).

  **The defect was small and the shape of it was not.** Every signal driving the indicator is live: it
  says what the engine is doing *now*, to whoever happens to be connected. Refresh during the pause and
  all of them have already been and gone, so the component that existed to explain a long silence was
  erased by the one action a user watching a silent screen is most likely to take — and what they got
  back was a session that looked idle while the engine was still summarising. This is the same class as
  the compaction divider phase 2 shipped client-side only, which suggests the lesson is worth naming: a
  UI state driven only by a push is a UI state that does not survive a reload, and reload is not an
  exotic case.

  The fix is that `EngineSession` now *holds* the fact rather than only forwarding it.
  `_fold_session_state` — already the place where translated events complete against session state —
  sets a monotonic start on `compaction_started` and clears it on `compaction_ended`, and
  `get_current_state` carries `compaction`. The event is still forwarded unchanged, so the live path is
  untouched; this only adds the snapshot.

  Four decisions inside it, each of which the obvious version gets wrong:

  - **The server computes elapsed seconds; it does not send a start timestamp.** A timestamp makes the
    browser difference two clocks, and a collaborating client can be on another machine.
  - **Only the status frames move it.** `compact_boundary` is the transcript's record and can arrive for
    a microcompaction that never paused anything, so ending on it would clear a state it never set —
    and starting on it would hand a reloading browser an indicator with no end frame coming.
  - **A turn ending clears it, and so does disconnect.** A compaction never outlives its turn; without
    that, a turn dying mid-compaction leaves every later browser a spinner for a pause that ended when
    the engine went away.
  - **The client's ceiling budgets the whole compaction**, not its own view of it. A restore 170 seconds
    in gets the remaining 10, not a fresh 180.

  The restore is treated as *confirmed*, because the server only ever sets it from the engine's own
  status frame and never from the ambiguous `PreCompact` hook — so it cannot be a speculative background
  precompute and must not vanish at the short unconfirmed ceiling. The trigger is deliberately absent: it
  belongs to the hook, not the frame, so a restored indicator says how long and not why.

  `test_current_state_has_every_key_the_frontend_reads` asserts an exact key set and caught the addition,
  which is the test doing its job rather than an obstacle.

- **`EngineHealth.mcp` deleted: a field that always answered `[]` was answering the wrong question** —
  2026-08-28, closing [`../next.md`](../next.md) § B5. Declared, serialised by `to_dict()`, assigned by
  nothing in `src/` for three phases.

  **The size was two lines; the choice was the work.** Write it or delete it, and deleting won because
  the question it looked like it answered already had a better answer. `get_mcp_status()` asks the CLI
  and is *allowed to fail visibly*, which is exactly what a status pill needs — it can be absent. A
  field that always answers `[]` cannot fail, and an empty list does not say "no servers", it says "no
  answer". That conflation is what made the Context tab's own MCP claim wrong for a week before anyone
  checked.

  **The whole suite passed unchanged when the field went**, which is the clearest evidence available
  that nothing had been reading it — and is itself the argument against declared-and-empty fields: for
  three phases the codebase could not tell the difference between this field working and this field not
  existing.

  Three tests pin the absence, and the third is the general form: every key in the health payload must
  map to a dataclass field or a named computed property, so a key cannot again be serialised out of
  nothing at all. `degradations` is untouched and is *not* a second attempt at the same thing — it
  records what the session started **without**, one sentence per loss, and it has a writer.

- **The retired-files note: the leaving-alone was right, the silence was the mistake** — built
  2026-08-28, closing [`../next.md`](../next.md) § B2 and the last entry of *(c)* below that belongs
  to this tab's own surface. Phase 3 retired eight config files, left them on disk deliberately —
  `system_extra.md` can hold months of a user's own prompt work, and deleting it would be irreversible
  and pointless since nothing reads it either way — and then never told anyone. Six cards vanished from
  the Settings tab with no explanation for three phases.

  **The design decision was relevance, and it is what made this more than a paragraph of static text.**
  A note is only owed to an install that *has* one of these files; a fresh install never had the cards,
  so explaining their disappearance would be explaining something the reader never witnessed. So
  `ConfigManager.retired_files_present()` stats the eight names and `get_config_info` carries the
  result — a new key on the banner's existing RPC rather than an RPC of its own, because it is one more
  fact about the same directory and a list that is usually empty does not deserve a round trip. It also
  keeps the RPC inventory unchanged, which is the classification burden § C5 is about.

  **The dismissal is keyed on the file list, not a boolean.** If a later upgrade retires something new,
  that name has never been explained to this user and the note is owed again — a flag would swallow it,
  and the bug would be invisible because the failure mode is silence, which is the exact failure this
  item exists to fix.

  Two smaller things worth keeping: the note **says the files will not be deleted**, because that is the
  reassurance the leave-alone rule exists to provide and a note that only said "obsolete" would read as
  a deletion warning; and `retired_files_present` uses `is_file`, so a directory that happens to be
  called `review.md` is not offered as a prompt somebody wrote. The upgrade-preserves-them rule is now
  pinned by a test *next to* the reporting, because if an upgrade ever started cleaning these up the
  note would quietly stop having anything to say.

  Found while there and fixed: `specs-reference/1-foundation/rpc-inventory.md` still described
  `get_config_info` as returning `{model, config_dir, cli_path}`, two of which had been gone since the
  conversion.

- **Phase 8: index freshness after `Bash`, by asking the disk instead of the command line** — built
  2026-08-28, closing [`../next.md`](../next.md) § A1 and deciding
  [`../plan/decisions.md#cc-18`](../plan/decisions.md). Phase 4's largest known hole, and the last
  phase-shaped correctness item in the suite.

  **None of CC-18's four options was taken, because the question had a false premise.** The table
  offered a filesystem watcher, parsing paths out of the command line, re-indexing after every `Bash`,
  or documenting the gap — and all four assume freshness needs a *new* source of truth. It did not.
  `BaseCache.get(path, mtime)` has returned `None` on a stale entry since Layer 2.7, so per-file
  staleness was always computable; it had simply never been asked as a question in its own right, only
  as a side effect of re-indexing a file someone already knew had changed. `SymbolIndex.find_stale_files`
  is that question, and it is forty lines of `stat` and dict lookup.

  **Two costs defused rather than paid.** The `Bash` hook sets a boolean and does nothing else, so an
  `ls` re-indexes nothing — the sweep only runs when something *reads* an index, which is
  `Reindexer.flush()`, which every index-reading MCP tool already awaited. And `reindex_files` is called
  only when the sweep returns a non-empty set, so its two whole-index passes (call-site re-resolution,
  reference-graph rebuild) are never paid for a sweep that found nothing. No watcher, no debounce
  tuning, no gitignore logic, no cross-platform behaviour.

  **The gap is a decision, and it is pinned.** A file a shell command *creates* holds no cached mtime to
  disagree with, so the sweep cannot see it; catching it means re-walking the repo per sweep, which is
  the cost being avoided. Modification and deletion — `sed -i`, formatters, `git checkout` over a tracked
  file, `mv` away — are covered. `test_a_file_the_index_never_knew_is_not_reported` asserts the
  limitation so it stays a decision rather than becoming a surprise, and
  [`../2-indexing/symbol-index.md`](../2-indexing/symbol-index.md) § *Freshness After a Shell Command*
  states it.

  **Tested against tree-sitter, not against a mock.** The unit tests run on a `FakeIndex`, which proves
  the wiring and not the effect — the failure mode this project has hit twice. So there is also an
  end-to-end pair on a real `SymbolIndex`: a `sed -i` behind the index's back, then the hook, then the
  flush, asserting `after` is in the symbol map and `before` is gone — **plus the counter-test that runs
  the identical edit with no `Bash` hook and asserts the map is still wrong**, so the positive test
  cannot pass for free. 3460 tests green.

- **Phase 7 (d): the release path is verified by running it, not by reading it** — built 2026-08-27,
  closing [`../next.md`](../next.md) § A2 (d). Three things landed; the interesting part is what the
  third one found.

  **`aic-dc --check-engine`.** The build already asserted the bundled `claude` was *in* the onefile
  archive. That cannot answer whether the extracted copy runs, and the two are separate claims for a
  concrete reason: `--collect-all` files are data, and data files carry no permission bits. The new flag
  resolves the binary the SDK would actually spawn — via the same `resolve_cli` the app calls, so a green
  check and a working launch cannot disagree — prints it, and exits **1** when nothing resolves, **2**
  when something resolved and would not run. It needs no credentials and asserts nothing about them,
  because a runner has no login and a check that demanded one could not run there. Exiting non-zero does
  not contradict [`../6-deployment/startup.md`](../6-deployment/startup.md) § *Engine Health in the
  Overlay*, which governs the *launch* path; a diagnostic that cannot fail reports nothing.

  **A container, because a runner is not a fresh machine.** Phase 7's criterion is "a fresh machine can
  install and run without a manual `npm i -g @anthropic-ai/claude-code`", and the runner has Node,
  Python, a uv environment and the repo. The Linux leg now runs the artefact in `ubuntu:24.04` with
  `claude`, `node`, `npm` and `python3` asserted absent *first* — a check that could pass by finding a
  system engine is not a check. Verified locally against a real 237 MiB build: the container populated
  the user config directory on first run, resolved `_MEI*/claude_agent_sdk/_bundled/claude`, and got
  `2.1.229` out of it. Bonus coverage nobody planned — that first-run config population is a packaging
  invariant that had never been watched happening on a machine that had never run the app.

  **The find: the release binary could not fail.** `src/aic_dc/__main__.py` is the script PyInstaller
  builds from, and it called `main()` and discarded the return value. Every exit code the CLI computed
  was therefore invisible to a shell, so the CI step whose entire output is an exit status would have
  passed whether or not the artefact had a working engine. Confirmed both ways on the rebuilt artefact:
  a bad `cli_path` in a container returned 0 before the one-line fix and 1 after it. **This is the second
  inert check in one phase** — § A2 (a)'s `--collect-all` for uninstalled packages was the first — and
  both were found by running the thing, not by reading it. The pattern is worth naming: a verification
  step is code, and untested code does not work.

  **The wheel carries the webapp.** `_find_webapp_dist`'s third priority is installed package data at
  `aic_dc/webapp_dist`, and nothing had ever put anything there, so pip installs fell through to the
  GitHub Pages fallback for no visible reason. The include is conditional via `hatch_build.py` rather
  than a declarative `force-include`, which fails the build when `webapp/dist` is missing — and it is
  missing in every dev checkout until someone builds a frontend they may not be working on. That would
  have turned `uv sync`, the first command in the contributing path, into an error. The residual risk is
  a release built without the Vite step shipping a silently webapp-less wheel, so CI asserts on the
  built wheel instead of trusting the ordering of its own steps. Both branches tested: wheel with the
  webapp (13 entries), wheel without (builds clean, backend only), and an editable install with the
  webapp present.

- **A tool card says when it was invoked, and counts up while it hasn't answered** — built 2026-08-27
  from § Known issues: *"a time chip for when the particular mcp task was invoked. This allows me to see
  if the task has stalled."*

  **The gap was that a pending card carried no time at all.** A finished card has `duration_ms` in its
  footer; a card still running has nothing and never will if it hangs, so a `Bash` that wedged and a
  `Bash` that answered in 30ms rendered identically. The header therefore answers **when**, not how long —
  duplicating the duration on a finished card would have added a number without adding a fact.

  **Two clocks, and neither is derivable from the other.** `TurnTranslator` already took an injectable
  monotonic `clock` for durations; it now takes a `wall_clock` beside it. A duration must be monotonic or
  an NTP correction mid-call can report a result arriving before its own request, while `invoked_at` has
  to be a time a reader can compare against the clock on their wall — and `time.monotonic` is a
  process-local number no browser can turn into a time of day. So the card gets a *new* field rather than
  reusing `started_at`, which would have been the same name meaning two things one layer apart.

  **"The ticker is running" is the licence to show a live number.** The elapsed is computed in
  `renderToolTime` from the panel's existing 250ms run-timer interval — no second interval — and is
  withheld entirely when `_streamTimerInterval` is null. That makes a frozen counter structurally
  impossible rather than merely unlikely: the thing that displays the number and the thing that advances
  it are the same condition. Writing it exposed a one-frame hole in that argument — `stopStreamTimerTick`
  cleared the interval without re-rendering, so a stranded card's elapsed could outlive its licence until
  the panel next happened to redraw. It now kicks a final `requestUpdate()` on the way out.

  Two more ways the number could lie, both closed: it is clamped at zero, because engine and browser may
  be different machines with skewed clocks; and a `denied` card gets none, since time since a call was
  *proposed* measures how long the user took to say no — a fact about the reader in a tool card's
  clothes.

  **The disk path had to be the transcript's own timestamp, not a re-render of one.** `history.py`
  `_open_card` passes `entry["timestamp"]` through verbatim, so a card keeps its invocation time across a
  refresh; reformatting could only lose precision or invent it. An entry with no timestamp yields `""`
  and renders no chip — absent stays absent, which is the invariant the browser half enforces too
  (`invokedAtMs` returns null for anything unparseable rather than falling back to now).

  **Verified in the browser, not only in the suite.** The disk path across 113 restored cards; the live
  tick on a genuinely stranded pending `Bash`, advancing `2m 46s → 2m 51s` and then withdrawing when the
  interval was cleared. `specs-reference/3-engine/session.md`'s `ToolCard` schema gained `invoked_at` —
  and `server_tool`, which was pre-existing drift found while adding it.

- **A save now says what it did not apply, and a restart applies it** — item (c)'s restart-session pair,
  built 2026-08-26. Two invariants that had nothing behind them now do.

  **What the shape turned on is that a save applies nothing live.** The obvious design — report `model`
  and `permission_mode` as applied, since they have live setters — would have been false: those setters
  are `set_model` and `set_permission_mode`, and `save_config_content` calls neither. So the disposition
  reports *every* `engine.json` field as next-session, and carries a separate `live_control` map naming
  the control that would apply one now (the model panel, the composer's selector). A pointer, not a
  receipt. `EngineConfig.LIVE_CONTROLS` holds that map beside the field declarations rather than in
  `settings.py`, so a renamed field cannot leave a stale name two layers up.

  **`commit_model` is the field that tested the rule.** Nothing sets it live, so it is next-session like
  the rest — but it is also the one `engine.json` value read at *call* time (`commit.py`), which means a
  restart applies it by replacing `service.engine_config` rather than by rebuilding options. Classifying
  it correctly needed reading the call site, not the field list.

  **The tab joins "applied", not the save.** For `app.json` the save asks the tab to reload, and a reload
  can fail — so the summary reports a field as applied only after that call came back true. A failed
  reload gets its own sentence: changed on disk, not in force, and *not* waiting for a restart either,
  because a restart is not what applies it. That third case was missed in the first draft, where a failed
  reload rendered as "nothing changed" — the one wrong answer available.

  **The restart resumes the conversation.** `_resume_request = (session_id, False)` — same session, no
  fork — so the transcript and the model's context survive; the CLI's cost ledger does not, and the
  confirmation says both. It also says, unconditionally, that a model or mode set by hand this session
  goes back to what the file says. Unconditional because the alternative was a preview RPC to detect an
  override the save did not touch: one honest sentence beats a second round trip that would be wrong
  whenever a `set_model` happened between the preview and the confirm.

  **The first draft broadcast `sessionChanged` and would have blanked every client's transcript.** Caught
  by reading the receiver rather than the emitter: `onSessionChanged` replaces `panel.messages` from
  `data.messages || []`, so an event sent to mean "the engine is new" reads as "the session has no
  messages". Replaced with conditional `permissionModeChanged` / `modelChanged`, each fired only if the
  value actually moved, and no session event at all — the session on screen is still this one.

  **Two refusals, one shortcut.** A turn in flight, like `new_session` and `resume_session`. An active
  review, because review holds `plan` and restores the entering mode when it ends, so a restart would put
  the file's mode in force under a UI still showing review's posture. And a cold engine adopts the re-read
  config in place — a shortcut, not an early return, since the config was loaded at startup and a cold
  session still holds the old one. `EngineSession.adopt_config` refuses to run while connected, which is
  what makes "the options it reports are the options it built" checkable rather than asserted.

  `test_every_rpc_is_classified` did its job again: it failed on `restart_session` until it was filed, and
  filing it is where the localhost gate comes from. See
  [`../3-engine/session.md` § Restart is the only thing that applies an option](../3-engine/session.md#restart-is-the-only-thing-that-applies-an-option)
  and [`../5-webapp/settings.md` § Save Behavior](../5-webapp/settings.md).

- **The Settings spec stopped describing other tabs' work** — item (a) of the section above, arbitrated
  2026-08-26. Six features were **deleted outright** from
  [`../5-webapp/settings.md`](../5-webapp/settings.md) rather than annotated with where they really live:
  engine health, credential source, the resolved `claude` path and version, MCP server status with its
  reconnect/enable controls, the permission chime, and the live permission mode. Annotating was the other
  option on the table and it was the wrong one — a spec section that describes a surface it does not own is
  how the drift started, and an annotated bullet is still a bullet somebody will implement.

  **What made the deletions safe to do was checking the receiving spec first, not the code.** Every one of
  the six is already specified where it is built —
  [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md),
  [`../5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md),
  [`../5-webapp/permission-dialog.md`](../5-webapp/permission-dialog.md),
  [`../5-webapp/chat.md`](../5-webapp/chat.md) — so deleting was moving a description, not losing one. Had
  any been implemented-but-unspecified, deleting would have made a built feature undescribed anywhere,
  which is the same drift running the other way.

  **The scope trap this ran into is worth the sentence.** § Session Controls held four bullets and it was
  tempting to delete the section: two of them (engine health, MCP servers) were (a) items, but **restart
  session and session-storage size are (c)** — unbuilt, specified only here, and restart-session is what
  § The Applies Column Is Load-Bearing depends on for its central claim. Deleting the group as a unit
  would have quietly dropped a (c) item and left an invariant elsewhere depending on a section that no
  longer existed. Same shape in § Invariants, where the non-localhost invariant named restart *and*
  permission mode together: the first half was vacuous-until-built and stayed — and was built later the
  same day, which is the argument for keeping it — the second is not this tab's and went.

  **The arbitration found one live defect, and it was the one that started the section.** `/permissions`
  routed to a bare `tab:settings` — the tab, not the thing it names. Fixed by naming the field instead of
  building a control for the route to point at: the target is now `tab:settings#permission-mode`, and the
  Settings tab answers that anchor by opening the `engine.json` card and selecting the `permission_mode`
  line. That is a third meaning for a `#section` — a segment on the Context tab, a panel to scroll and
  mark for `/model`, a *line in a file* here — and it is the one that makes the grammar carry its weight,
  because the alternative was a duplicate next-session control on Settings existing solely so a route had
  a target. `fieldLineRange` reads the textarea rather than the loaded content, so a reader who adds the
  key after being told it is absent lands on the line they just wrote; and an absent key is answered
  ("the engine's own default is in force") rather than mimed with a mark over nothing.

  `test_every_context_tab_route_names_a_section` widened to `test_every_tab_route_names_a_section` — a tab
  is not a destination, and the two tabs reach that conclusion for different reasons (Context remembers
  the wrong section; Settings opens onto a card grid).

- **The two MCP controls got a caller** — built, as item (b) of the section above. `reconnect_mcp_server`
  and `toggle_mcp_server` are now actions in the Context tab's server-group body, under the connection
  facts they act on.

  **The fact that decided the design was one nobody in this file had — and the first version of it was
  wrong.** The question on the table was whether `toggle_mcp_server` should get a button at all, given its
  docstring's argument that the host decides which tools exist. It looked like a narrower question than it
  was: [`../plan/sdk-surface.md`](../plan/sdk-surface.md) recorded from a 2026-08-15 live run that
  `get_mcp_status` lists only *configured stdio/http* servers, so the toggle's only subject on this machine
  would be the user's own `chrome-devtools`. That is what this entry originally said, and the design was
  settled on it: keep the toggle, put the friction only on enabling, which is the direction that grants.

  **Then the browser check found `aic-dc` in the list**, `scope: "dynamic"`, six tools, stable across
  samples — same CLI 2.1.229 the contrary claim was verified against. The absence was an artefact of
  *when the status was sampled*: `bridge_smoke.py` calls `get_mcp_status()` in the instant after
  `connect()` returns, and at that instant the list is unpopulated — only stdio servers, `pending`, 0
  tools. A probe registering a trivial SDK server and polling every 1.5s had it at t=1.5s and stable
  thereafter. Corrected in `sdk-surface.md`, `delivery.md`, `settings.md`, and in `bridge_smoke.py`'s own
  comment, which is where the claim entered the specs.

  **The first attempt at that correction blamed the wrong cause** — it said the 2026-08-15 run had built
  the session without `mcp_servers=`. True of the three throwaway probes written while building this,
  false of `bridge_smoke.py`, which registers the bridge on line 248 and is the script the verification
  used. Caught only because a question about tooling prompted actually reading the script instead of
  assuming what it did. Two wrong claims in one payload, both from reasoning about a harness rather than
  running it.

  What the correction cost was not cosmetic. Measured on the scratch backend rather than reasoned about:
  `toggle_mcp_server('aic-dc', false)` replies `{"status": "ok", "enabled": false}` and takes the tool
  count 6 → 0 while the pill still reads `connected`; then **both** ways back refuse with
  `SDK servers should be handled in print.ts` — the re-enable and `reconnect_mcp_server` alike. So the
  shipped design would have put a one-click, unconfirmed, unrecoverable action on the one server whose
  tools the agent itself runs on, and the enable-only confirmation was no protection because the
  reversibility it assumed does not exist there.

  **So a `dynamic` row gets neither control**, and says why. This is keyed off the CLI's own `scope`
  rather than off the name `aic-dc`, so any SDK server we register later is covered. The
  enable-confirms/disable-does-not split stands for real stdio and http servers, where disabling *is*
  reversible — the exemption removes the case that broke the premise instead of rewriting the rule.

  Two things worth keeping from how this went. The design rested on a spec claim that was load-bearing
  and false, and only rendering it in a browser against a live engine found that out — the unit tests
  were green against fixtures that encoded the same wrong assumption. And the claim was false in the
  direction that is hardest to catch: **an absence, asserted from a harness that could not have produced
  the presence.**

  **Reconnect is deliberately narrow** — `failed` and `needs-auth` only, which is the loop the SDK's own
  docstring example walks. `pending` is mid-dial and re-dialling races the attempt in flight; `disabled`
  wants Enable. And the toast says `reconnecting`, the reply's own word, rather than claiming a
  connection: the outcome belongs to the pill on the refresh that follows, and this is the one row that
  exists because a server was wrong about being fine.

  **What had to be measured rather than reasoned**, on the `set_model` precedent — three probes against a
  live session, because the first two disagreed with each other. Unlike `set_model`, both controls apply
  **immediately**: disabling a settled `connected` server returned `disabled` with 0 tools on the next
  `get_mcp_status` and took `get_context_usage` from 29 tools / 9,071 tokens to `mcpTools=0/0`. So no
  "applies next turn" sentence is owed, and a control call re-reads the whole breakdown rather than just
  the status — refreshing the pill and leaving the numbers is how a green pill ends up over a stale total.

  The disagreement between the probes was itself the finding: **a disable issued while the initial dial is
  still in flight is silently reverted** by the connection completing. The probe that disabled ~1s after
  connect found the server `connected` with all 29 tools eight seconds later, and the probe that waited
  for a settled state found `disabled` holding across 11s. Not guarded against — the actions live behind
  a collapsed group and the tab's first breakdown fetch is slower than the window in which it is possible
  — but recorded, because "Disable didn't work" is otherwise unfalsifiable. The same 3-14s breakdown cost
  works in our favour on the way back: the dial takes 1-3s, so the refresh cannot return before the
  server has finished connecting.

  Two smaller things the work settled. The toggle is session-scoped, not a settings edit —
  `~/.claude.json`'s `mcpServers` entry was unchanged after both directions with no `disabled` key added.
  And `test_the_methods_the_frontend_calls_exist` had been listing both RPCs as methods the frontend
  calls, for a frontend that called neither; that assertion is now true rather than aspirational. See
  [`../5-webapp/viewers-hud.md` § Session Section](../5-webapp/viewers-hud.md).

  **Not built, and deliberately:** the `test_every_rpc_has_a_caller_or_is_listed_as_dormant` this section
  argues for. It is the right mechanism and these two were its motivating cases, but writing it means
  auditing every RPC for callers and arbitrating each caller-less one — the work-log's own claim that it
  "would have caught these two years earlier" implies there are others. That is its own task, not a
  rider on this one.

- **A model-selector surface, and `/model` routed to it** — built. `set_model` had been localhost-only
  with no caller and `/model` was passthrough, for a reason this entry stated correctly: there was
  nothing to route it *to*, and routing to a tab with no model control would have opened the wrong
  thing confidently.

  The surface is the Settings tab's own panel, above the card grid and explicitly outside it, because
  every card there applies to the next session and `set_model` applies now. It reads a new cheap
  `get_model()` — the alias in force, the CLI's `alias → resolvedModel` mapping from the handshake, and
  the advertised list — rather than `get_current_state`, which would have shipped the whole rendered
  transcript to answer one string. `get_model` is classified read-only: switching is gated, but a
  participant who cannot see which model is answering cannot tell why a turn came back cheaper, faster
  or worse than the last one.

  Three things the surface refuses to invent. A null alias stays null instead of becoming the string
  `"default"` — "nothing pinned" and "`default` pinned" land in the same place today and need not after
  a CLI upgrade. A resolution absent from the handshake stays absent, because this repo does not resolve
  aliases and a guessed model id is one somebody would quote back while deciding what to spend. An
  empty model list before the first turn is reported as "the engine has not connected yet", which is
  the ordinary pre-first-turn state rather than an error — the same lazy-connect window that makes
  `list_commands` answer `partial: true`.

  The control moves on the RPC reply rather than the click, which is a documented departure from the
  permission-mode control's never-optimistic rule and is allowed because `Session.set_model` records the
  alias only after the control request returned. `modelChanged` still broadcasts, for the windows that
  did not make the call. The chat panel's gesture latch was kept even though there is no dialog to
  intercept a phantom `change` here — which is the stronger reason to keep it, since what it guards is
  the host's bill.

  The one thing that had to be measured rather than reasoned was *when* a mid-turn switch takes effect.
  `set_model('haiku')` fired 22.8s into a live 34s turn answered in 252ms and did not interrupt it — and
  the turn went on billing opus, including a usage report 124ms after the switch was broadcast. The next
  turn billed haiku. So the panel says "a switch takes effect from your next turn", unconditionally,
  because the reader most likely to reach for a cheaper model mid-turn is the one watching an expensive
  turn run away, and letting them think they had just stopped it would be the worst thing this panel
  could do.

  Two committed falsehoods fell out of the work. `/permissions` advertised "the Settings tab's
  permission-mode control plus the rules list" and neither exists — the live mode is the selector beside
  the composer, and the tab holds only `engine.json`'s next-session value. And a routed command's
  argument never travelled, silently; `/model sonnet` now answers with what it dropped and why. See
  [`3-engine/session.md` § Slash Commands](../3-engine/session.md#slash-commands) and
  [`5-webapp/settings.md` § Model Panel](../5-webapp/settings.md#model-panel).

- **Sections on a route target** — built, and it was a correctness fix rather than the polish this
  entry first called it. The earlier framing said the five Context routes landed "on the right tab and
  the wrong scroll position". They did not: the Context tab is a segmented control that *remembers the
  section its reader last chose*, so a bare `tab:context` opened onto whatever that was. For `/mcp` and
  `/agents` the usual answer was Usage, which carries no MCP status and no agent list — two commands
  opening the right tab and never showing the thing they name, silently, looking like they had worked.
  A target may now name a section after a `#` (`tab:context#session`), the shell forwards it through a
  duck-typed `showSection(id)` in the same shape as the existing `onTabVisible()` hook, and the fragment
  is split off *before* the surface switch so an unknown section cannot make a known tab unreachable.
  `showSection` deliberately does **not** persist: the stored key means "the section the user was last
  reading", and a command choosing it is not the reader choosing it. See
  [`3-engine/session.md` § Target grammar](../3-engine/session.md#target-grammar).

- **`during_turn` on `list_commands` entries** — built. `SLASH_ROUTES` carries the flag, `list_commands`
  and `_routed_commands` emit it, the chat panel binds `panel._streaming` onto `aic-slash-palette`, and
  the palette renders blocked rows dimmed with "when the turn ends" on them, skips them with the arrow
  keys, and refuses Enter and click. The composer's palette button is no longer disabled while
  streaming. Correcting the spec was the larger part of the work: the claim that *everything* reaching
  the CLI is `during_turn: false` had been generalised into "only local UI can run mid-turn", which is
  wrong — the SDK's fifteen client methods include exactly one that starts a turn, and `can_use_tool`
  already answers on the concurrent channel while a turn blocks on it. See
  [`3-engine/session.md` § Mid-turn availability](../3-engine/session.md#mid-turn-availability).

## Resumption protocol

If a response drops mid-layer, the next response begins by:

1. Reading the files currently in context (not relying on memory of what was delivered).
2. Identifying the last known good state — the latest complete file, the latest test that passed.
3. Continuing from there with one file per response when length is tight.

Do not rewrite files that are already complete. Do not quote large sections of previously-delivered content verbatim to "re-establish context" — the context window already carries the file state.

## Layer-transition checklist

Before declaring a layer complete:

- All test files in the layer pass locally (`uv run pytest tests/test_<module>.py` per module).
- `uv run pytest` passes overall — no regression in prior layers.
- `uv run ruff check src tests` has no errors (warnings OK in early layers).
- The work log marks the layer complete and opens the next layer's checklist (this file when active; archived plans / decisions when stable).
- Any deviation from specs4 is recorded as a decision (D-N) in [`decisions.md`](decisions.md).