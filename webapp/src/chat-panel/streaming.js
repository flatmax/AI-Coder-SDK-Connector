// Stream lifecycle handlers for the ChatPanel.
//
// Every event the Claude Code engine emits for a turn lands here, gets routed
// to the tab that owns the turn, and folds into that tab's block state. The
// module owns *when* state changes; `blocks.js` owns what the change means and
// `block-render.js` owns how it looks.
//
// Handlers, roughly in the order a turn produces them:
//
//   - `onSessionStarted`  — the engine's `init`: model, cwd, tools, mode
//   - `onStreamChunk`     — assistant text, block-keyed
//   - `onThinkingChunk`   — reasoning, same routing, different kind
//   - `onToolUse`         — a tool card
//   - `onPermissionRequest` / `onPermissionResolved` — the amber lock and what
//     came of it, matched to their card by `tool_use_id`
//   - `onToolResult`      — the result, attached by `tool_use_id`
//   - `onSubagentEvent`   — a `Task` row's description, status, usage
//   - `onStreamComplete`  — freeze the blocks onto a settled message, footer
//     and terminal-reason badge included
//   - `onRateLimit`, `onEngineHealth`, `onSystemEvent`, `onHookEvent` — out of
//     band, routed per specs5/5-webapp/chat.md § Engine Event Routing
//
// Why functional with `panel` as a parameter: matches the pattern established
// in tabs.js — the chat-panel module is too large to fit on one prototype, and
// Lit's reactive-property machinery doesn't compose cleanly with multi-class
// inheritance. Functional modules with explicit `panel` parameters keep the
// dependencies visible.
//
// Architectural contracts preserved here:
//
//   - Streaming state keyed by request ID. Each tab has its own `streams` Map
//     and its own block state; this module routes by request ID via
//     `findTabForRequest`.
//
//   - Chunks are cumulative *within a block*, not across a turn. The old
//     contract — every chunk carries the whole accumulated turn, so any one
//     chunk could rebuild the view — is gone. A chunk can rebuild its block
//     and nothing more, which is why `stageChunk` takes a block id and a `seq`
//     rather than a string.
//
//   - Chunks coalesced per animation frame. Rapid-fire chunks (every few ms)
//     don't trigger Lit re-renders faster than 60Hz. The synchronous drain
//     inside `onStreamChunk` is insurance against rAF starvation (tab
//     backgrounded, panel briefly display:none); both paths drain the same
//     staging map, so whichever runs second finds it empty.

import {
  applyPermissionOutcome,
  applyReplayBlocks,
  applySubagentEvent,
  applyToolResult,
  applyToolUse,
  collectFilesModified,
  drainChunks,
  freezeBlocks,
  markAwaitingPermission,
  resetTurnBlocks,
  stageChunk,
  subagentRowFor,
} from './blocks.js';
import {
  mirrorSubagentBlocks,
  settleLiveSubagentTabs,
  syncSubagentTab,
} from './subagent-tabs.js';
import { findTabForRequest, spawnAgentTabs } from './tabs.js';

// ---------------------------------------------------------------
// Turn outcome (LED row state)
// ---------------------------------------------------------------

/**
 * Derive the LED-row outcome for one completed turn.
 *
 * The old version read our own apply pipeline's `EditResult` list. There is no
 * such list any more — the agent owns its edits and reports them as tool
 * results — so the inputs are the engine's own verdict on the turn:
 *
 *   - `is_error` — the turn failed. Red, whatever else it managed first.
 *   - `terminal_reason` — a turn that stopped for a bad reason (`max_turns`,
 *     a refusal, an engine fault) is not clean even when `is_error` is false.
 *   - `permission_denials` — not an error. A denied call is the permission
 *     system working; the LED staying green is the honest signal.
 *
 * A cancelled turn is reported `clean`: the user stopped it deliberately and a
 * red LED would read as a fault. `files.length` fills `appliedCount`, which
 * the tooltip renders as "N edits applied" — with Claude Code that means "N
 * files the turn modified", which is the same question the tooltip was always
 * answering.
 *
 * Returns the shape stored on `tab.lastEditOutcome`:
 * `{status: 'clean'|'error', appliedCount, failureReason}`.
 */
export function computeTurnOutcome(result, files) {
  const modified = Array.isArray(files) ? files : [];
  const appliedCount = modified.length;
  if (!result || typeof result !== 'object') {
    return { status: 'clean', appliedCount, failureReason: null };
  }
  if (result.cancelled) {
    return { status: 'clean', appliedCount, failureReason: null };
  }
  if (result.is_error) {
    return {
      status: 'error',
      appliedCount,
      failureReason: engineErrorReason(result),
    };
  }
  const reason = result.terminal_reason;
  if (typeof reason === 'string' && reason && reason !== 'completed') {
    return {
      status: 'error',
      appliedCount,
      failureReason: reason.replace(/_/g, ' '),
    };
  }
  return { status: 'clean', appliedCount, failureReason: null };
}

/**
 * A short diagnostic for a failed turn.
 *
 * `errors` is the CLI's own list and reads best when present. `api_error_status`
 * is an HTTP status on a turn whose subtype is still "success" — the one case
 * where the interesting fact is a number rather than a sentence.
 */
export function engineErrorReason(result) {
  const errors = Array.isArray(result?.errors) ? result.errors : [];
  const first = errors.find((e) => typeof e === 'string' && e);
  if (first) return first;
  if (Number.isFinite(result?.api_error_status)) {
    return `API error ${result.api_error_status}`;
  }
  if (typeof result?.subtype === 'string' && result.subtype) {
    return result.subtype.replace(/_/g, ' ');
  }
  return 'the turn failed';
}

// ---------------------------------------------------------------
// Run timer (assistant elapsed-time display)
// ---------------------------------------------------------------

/**
 * Re-render cadence for the live run timer, in ms. 250ms is
 * frequent enough that the one-decimal seconds display
 * advances smoothly to the eye, but far cheaper than the
 * per-chunk render rate — each tick is a single
 * requestUpdate.
 */
const RUN_TIMER_TICK_MS = 250;

/**
 * Start the panel-level run-timer ticker.
 *
 * One interval drives every tab's live elapsed counter. The
 * render path computes elapsed from
 * ``Date.now() - tab.streamStartedAt`` on each pass, so the
 * tick only needs to kick a dirty-check — it never mutates
 * state itself.
 *
 * Idempotent: calling it while already running is a no-op.
 * Self-stopping: once no tab has a live ``streamStartedAt``
 * the interval clears itself, so an idle panel holds no
 * timer.
 */
export function startStreamTimerTick(panel) {
  if (panel._streamTimerInterval != null) return;
  panel._streamTimerInterval = setInterval(() => {
    let anyActive = false;
    for (const tab of panel._tabs.values()) {
      if (tab.streamStartedAt != null && tab.streaming) {
        anyActive = true;
        break;
      }
    }
    if (anyActive) {
      panel.requestUpdate();
    } else {
      stopStreamTimerTick(panel);
    }
  }, RUN_TIMER_TICK_MS);
}

/**
 * Stop the panel-level run-timer ticker. Called when no tab
 * has a live timer left, and on panel teardown from events.js's
 * detach path.
 */
export function stopStreamTimerTick(panel) {
  if (panel._streamTimerInterval != null) {
    clearInterval(panel._streamTimerInterval);
    panel._streamTimerInterval = null;
  }
}

/**
 * Stop the run-timer ticker if (and only if) no tab still has
 * a live timer. Called from the completion / error paths after
 * a tab's ``streamStartedAt`` is cleared so the interval doesn't
 * outlive the last running stream.
 */
export function maybeStopStreamTimerTick(panel) {
  for (const tab of panel._tabs.values()) {
    if (tab.streamStartedAt != null && tab.streaming) return;
  }
  stopStreamTimerTick(panel);
}

// ---------------------------------------------------------------
// Routing
// ---------------------------------------------------------------

/**
 * The tab that owns a request, including one whose turn has just finished.
 *
 * Post-turn housekeeping — a compaction boundary, a late system event, a
 * permission resolution racing the result message — runs asynchronously after
 * `streamComplete`, by which time `currentRequestId` is already null. The spec
 * is explicit that the handler accepts both the current and the most recently
 * completed request ID (specs5/5-webapp/chat.md § Engine Event Routing).
 *
 * Returns `{tabId, tab, live}` or null. `live` distinguishes "this turn is
 * still running" from "this is a straggler", which matters because a straggler
 * must not restart the streaming card.
 */
export function findTabForRecentRequest(panel, requestId) {
  if (!requestId) return null;
  const liveId = findTabForRequest(panel, requestId);
  if (liveId) {
    const tab = panel._tabs.get(liveId);
    return tab ? { tabId: liveId, tab, live: true } : null;
  }
  for (const [tabId, tab] of panel._tabs) {
    if (tab.lastRequestId === requestId) {
      return { tabId, tab, live: false };
    }
  }
  return null;
}

/**
 * Resolve the tab for a live turn event and note whether it is the active one.
 * Returns null for a request no tab owns — a collaborator's stream reaching
 * our panel, or an event for a turn we already tore down.
 */
function liveOwner(panel, requestId) {
  if (!requestId) return null;
  const tabId = findTabForRequest(panel, requestId);
  if (!tabId) return null;
  const tab = panel._tabs.get(tabId);
  if (!tab) return null;
  return { tabId, tab, active: tabId === panel._activeTabId };
}

/**
 * Mark a tab's blocks dirty.
 *
 * Block state mutates in place — `blocks.js` patches the same records the
 * renderer already holds, because that is what makes a chunk an O(1) update
 * instead of a list rebuild. Lit cannot see an in-place mutation, so a bare
 * `requestUpdate()` is the correct signal: it schedules unconditionally rather
 * than diffing a property that never changed identity.
 *
 * Every fold also mirrors the turn's subagent blocks into their own tabs
 * (`subagent-tabs.js`), because a block belongs to two places at once: the row
 * inside this turn and the feed the subagent has its own tab for.
 *
 * Only the active tab repaints — and the active tab may be a subagent's rather
 * than the one that owns the request, which is why the mirror pass reports
 * whether it changed anything on screen. A background tab's blocks are still
 * updated; they render when the user switches to it.
 */
function markBlocksDirty(panel, owner) {
  const mirrored = mirrorSubagentBlocks(panel, owner.tab);
  if (owner.active || mirrored) panel.requestUpdate();
}

// ---------------------------------------------------------------
// Text and thinking chunks
// ---------------------------------------------------------------

/**
 * Handle a `stream-chunk` window event: assistant text for one block.
 *
 * Payload is `{block_id, seq, content, done}` with content cumulative within
 * the block. Staged rather than applied so a burst coalesces into one repaint
 * per frame; a chunk whose `seq` is stale for its block is discarded by
 * `stageChunk` and never reaches the renderer.
 */
export function onStreamChunk(panel, event) {
  routeChunk(panel, event.detail || {}, 'text');
}

/**
 * Handle a `thinking-chunk` window event.
 *
 * Same routing and the same block-keyed staging as text — the only difference
 * is the kind, which decides whether the block renders as prose or as a
 * collapsed thinking region.
 */
export function onThinkingChunk(panel, event) {
  routeChunk(panel, event.detail || {}, 'thinking');
}

function routeChunk(panel, detail, kind) {
  const requestId = detail.requestId;
  // `chunk` is the block-keyed payload. `content` is accepted as an alias so a
  // shell that still forwards the old positional name keeps working during the
  // conversion rather than silently dropping every chunk.
  const payload = detail.chunk ?? detail.content;
  if (!requestId || !payload || typeof payload !== 'object') return;
  const owner = liveOwner(panel, requestId);
  if (!owner) return;
  const { tab } = owner;
  if (!stageChunk(tab.turnBlocks, payload, kind)) return;
  scheduleFlush(panel);
  // Apply synchronously in addition to scheduling the rAF coalesce. The rAF
  // caps re-render rate for rapid chunks; the sync path is insurance against
  // rAF starvation (backgrounded tab, panel briefly display:none). Both drain
  // the same staging map, so whichever runs second finds it empty.
  if (requestId === tab.currentRequestId && tab.streaming) {
    if (drainChunks(tab.turnBlocks)) markBlocksDirty(panel, owner);
  }
}

/**
 * Schedule (or coalesce) a deferred drain of every tab's staged chunks.
 *
 * One rAF active at a time; calls before it fires are no-ops, since the
 * pending rAF will see every write made in the meantime. Drains all tabs —
 * a background tab's blocks stay current without costing a repaint.
 */
export function scheduleFlush(panel) {
  if (panel._rafHandle != null) return;
  panel._rafHandle = requestAnimationFrame(() => {
    panel._rafHandle = null;
    let activeChanged = false;
    const activeTab = panel._tabs.get(panel._activeTabId);
    for (const tab of panel._tabs.values()) {
      if (!drainChunks(tab.turnBlocks)) continue;
      if (tab === activeTab) activeChanged = true;
      // A drained chunk can be the first one for a block produced inside a
      // subagent, so the mirror runs on the same frame — otherwise the
      // subagent's tab would sit on "Working…" until the next tool event.
      if (mirrorSubagentBlocks(panel, tab)) activeChanged = true;
    }
    if (activeChanged) panel.requestUpdate();
  });
}

// ---------------------------------------------------------------
// Tool cards
// ---------------------------------------------------------------

/**
 * Handle a `tool-use` window event: a tool card for the turn.
 *
 * Cards are keyed by the SDK's `tool_use_id` and appear in arrival order,
 * interleaved with the text and thinking blocks around them.
 */
export function onToolUse(panel, event) {
  const { requestId, data } = event.detail || {};
  const owner = liveOwner(panel, requestId);
  if (!owner || !data) return;
  if (applyToolUse(owner.tab.turnBlocks, data)) markBlocksDirty(panel, owner);
}

/**
 * Handle a `tool-result` window event.
 *
 * Attaches to its card by `tool_use_id`. A result for a card we never saw is
 * dropped by `applyToolResult` rather than rendered headless.
 */
export function onToolResult(panel, event) {
  const { requestId, data } = event.detail || {};
  const owner = liveOwner(panel, requestId);
  if (!owner || !data) return;
  if (applyToolResult(owner.tab.turnBlocks, data)) markBlocksDirty(panel, owner);
}

// ---------------------------------------------------------------
// Permission gating of tool cards
// ---------------------------------------------------------------

/**
 * Handle a `permission-request` window event.
 *
 * The dialog is its own component and owns the decision; this handler only
 * marks the tool card amber so the transcript shows *why* the turn stopped
 * moving. The payload carries `tool_use_id`, so no correlation table is needed.
 *
 * Session-wide rather than turn-scoped, so the request ID comes from the
 * payload and may name a turn that has just finished — a permission request
 * outliving its own `streamComplete` is possible when the engine is torn down
 * mid-ask.
 */
export function onPermissionRequest(panel, event) {
  const data = event.detail || {};
  const toolUseId = data.tool_use_id;
  if (!toolUseId) return;
  const owner = findTabForRecentRequest(panel, data.request_id);
  if (!owner) return;
  if (markAwaitingPermission(owner.tab.turnBlocks, toolUseId)) {
    if (owner.tabId === panel._activeTabId) panel.requestUpdate();
  }
}

/**
 * Handle a `permission-resolved` window event.
 *
 * Allow clears the lock and the call goes back to pending. Anything else — a
 * denial, a timeout, a shutdown — records the reason on the card, because the
 * agent was given that same reason and the transcript should show what it was
 * told.
 */
export function onPermissionResolved(panel, event) {
  const data = event.detail || {};
  if (!data.tool_use_id) return;
  const owner = findTabForRecentRequest(panel, data.request_id);
  if (!owner) return;
  if (applyPermissionOutcome(owner.tab.turnBlocks, data)) {
    if (owner.tabId === panel._activeTabId) panel.requestUpdate();
  }
}

/**
 * Handle a `permission-mode-changed` broadcast.
 *
 * The selector flips here and only here. It never updates optimistically: the
 * mode is engine state, a `set_permission_mode` call can be refused, and a
 * selector showing `acceptEdits` while the engine is in `default` is a lie
 * about what the next tool call will do
 * (specs5/5-webapp/chat.md § Permission Mode Selector).
 */
export function onPermissionModeChanged(panel, event) {
  const data = event.detail || {};
  const mode = typeof data.mode === 'string' ? data.mode : null;
  if (!mode) return;
  panel._permissionMode = mode;
  panel._permissionModePending = false;
}

// ---------------------------------------------------------------
// Session and subagents
// ---------------------------------------------------------------

/**
 * Handle a `session-started` window event — the engine's `init` message.
 *
 * Carries the model, cwd, the tool and MCP inventories, the slash commands the
 * CLI knows, and the permission mode the session actually started in. The mode
 * is adopted here rather than assumed, so a session resumed into `acceptEdits`
 * shows `acceptEdits`.
 */
export function onSessionStarted(panel, event) {
  const { data } = event.detail || {};
  if (!data || typeof data !== 'object') return;
  panel._sessionInfo = data;
  if (typeof data.permission_mode === 'string' && data.permission_mode) {
    panel._permissionMode = data.permission_mode;
  }
  panel.requestUpdate();
}

/**
 * Handle a `subagent-event` window event.
 *
 * Folds into the row for its task. Rows are patched, never replaced — fields
 * arrive spread across four message types and a later event with fewer fields
 * must not blank what an earlier one supplied.
 *
 * The row is only half of it. Every event also syncs the subagent's own tab in
 * the strip (`subagent-tabs.js`), so a subagent doing minutes of work has a
 * feed of its own instead of interleaving with the turn the user is reading —
 * specs5/5-webapp/subagent-browser.md § Tab Strip. Both views come from this
 * one event and the same block records; the row is the evidence the delegation
 * happened, the tab is where its work is legible.
 */
export function onSubagentEvent(panel, event) {
  const { requestId, data } = event.detail || {};
  const owner = liveOwner(panel, requestId);
  if (!owner || !data) return;
  const turn = owner.tab.turnBlocks;
  if (!applySubagentEvent(turn, data)) return;
  const synced = syncSubagentTab(
    panel,
    requestId,
    subagentRowFor(turn, data),
    owner.tab,
  );
  markBlocksDirty(panel, owner);
  if (!synced) return;
  // The strip, the LED row and the tab's own label all just changed, and none
  // of them is a reactive property — `_tabs` is a Map mutated in place.
  if (synced.created) startStreamTimerTick(panel);
  panel.requestUpdate();
}

// ---------------------------------------------------------------
// Out-of-band engine events
// ---------------------------------------------------------------

/**
 * Handle a `rate-limit` window event.
 *
 * Warning on `allowed_warning`, error on `rejected`, both naming when the
 * limit resets — a rate-limit notice with no reset time tells the user they
 * are blocked without telling them for how long, which is the least useful
 * half of the message (specs5/5-webapp/chat.md § Engine Event Routing).
 *
 * `allowed` is the ordinary case and says nothing.
 */
export function onRateLimit(panel, event) {
  const { data } = event.detail || {};
  if (!data || typeof data !== 'object') return;
  const status = data.status;
  if (status !== 'allowed_warning' && status !== 'rejected') return;
  const kind = typeof data.rate_limit_type === 'string' && data.rate_limit_type
    ? `${data.rate_limit_type} limit`
    : 'Rate limit';
  const resets = formatResetTime(data.resets_at);
  const when = resets ? ` — resets ${resets}` : '';
  if (status === 'rejected') {
    panel._emitToast(`⏱️ ${kind} reached${when}`, 'error');
  } else {
    panel._emitToast(`⏱️ Approaching the ${kind}${when}`, 'warning');
  }
}

/**
 * Unix seconds → a local wall-clock time.
 *
 * A clock time rather than a countdown: the toast does not tick, and "resets
 * in 47 minutes" is wrong the moment the user looks away.
 */
export function formatResetTime(resetsAt) {
  if (!Number.isFinite(resetsAt) || resetsAt <= 0) return '';
  const date = new Date(resetsAt * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return `at ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

/**
 * Handle an `engine-health` window event.
 *
 * No toast. A mirror gap means this turn did not reach the repo-local
 * transcript, which is a standing condition rather than an interruption — the
 * health banner owns saying so, and the affected turn's footer carries its own
 * marker.
 *
 * `detail` *is* the payload here, with no `{requestId, data}` envelope around
 * it: engine health is session-wide (`turn_scoped=False` on the engine side),
 * so the shell has no request id to pair it with.
 */
export function onEngineHealth(panel, event) {
  const data = event.detail;
  // Arrays are excluded rather than tolerated: this is a whole-record
  // replacement, so anything that is not a record would drop a real warning
  // in favour of nothing.
  if (!data || typeof data !== 'object' || Array.isArray(data)) return;
  panel._engineHealth = data;
}

/**
 * Handle a `system-event` window event.
 *
 * Almost all of these are diagnostics with no user-facing consequence. Two
 * are not:
 *
 *   - `pre_compact` — the engine is about to compact its context, which looks
 *     like a stall if unannounced. Transient local toast.
 *   - `conversation_reset` — the engine dropped the conversation underneath
 *     us. The user's next turn will start from nothing, so saying so is the
 *     difference between a surprise and an explanation.
 */
export function onSystemEvent(panel, event) {
  const { data } = event.detail || {};
  const subtype = data?.subtype;
  if (subtype === 'pre_compact') {
    panel._emitToast('🗜️ Compacting the conversation…', 'info');
    return;
  }
  if (subtype === 'conversation_reset') {
    panel._emitToast('The engine reset the conversation', 'warning');
  }
}

/**
 * Handle a `hook-event` window event.
 *
 * Hooks are the user's own configuration running, and narrating each one would
 * turn a configured workflow into a stream of notifications. One case earns a
 * word: a hook that *blocked* something explains a tool call the user is about
 * to see fail.
 *
 * `PreCompact` is deliberately not one of them, though the compaction toast is
 * real — it arrives through `onSystemEvent`, broadcast by our own PreCompact
 * hook. Toasting here as well would fire it twice for the same compaction, and
 * more than that in principle: `hookEvent` carries a `phase`, so one hook run
 * reports itself as `hook_started` and again as `hook_response`.
 */
export function onHookEvent(panel, event) {
  const { data } = event.detail || {};
  if (!data || typeof data !== 'object') return;
  if (data.outcome === 'block' || data.outcome === 'blocked') {
    const tool = data.tool_name ? ` ${data.tool_name}` : '';
    panel._emitToast(`🪝 A hook blocked${tool}`, 'warning');
  }
}

// ---------------------------------------------------------------
// Pre-spawn agents from broadcast
// ---------------------------------------------------------------

/**
 * Handle an `agents-spawned` window event.
 *
 * Dead on the Claude Code path: nothing emits `agentsSpawned`. The engine's
 * subagents are internal to a turn and announce themselves as `subagentEvent`,
 * which builds their tabs through `subagent-tabs.js` — a different producer for
 * a strip this one used to fill, and the reason it stays dead rather than being
 * repointed: an `agentsSpawned` tab is writable, and a subagent's is not.
 * Kept wired until the native engine is deleted in phase 3, so the phase-2 diff
 * is about the new path rather than about removing the old one.
 *
 * Payload: ``{turn_id, parent_request_id, agent_blocks}`` where
 * ``agent_blocks`` is ``[{id, task, agent_idx}, ...]``.
 */
export function onAgentsSpawned(panel, event) {
  const detail = event.detail || {};
  const { turn_id, parent_request_id, agent_blocks } = detail;
  if (typeof turn_id !== 'string' || !turn_id) return;
  if (typeof parent_request_id !== 'string') return;
  if (!Array.isArray(agent_blocks)) return;
  if (agent_blocks.length === 0) return;
  spawnAgentTabs(panel, turn_id, agent_blocks, parent_request_id);
  startStreamTimerTick(panel);
}

// ---------------------------------------------------------------
// Stream complete
// ---------------------------------------------------------------

/**
 * Handle a `stream-complete` window event.
 *
 * Freezes the turn: its blocks, its subagent rows, and the result summary the
 * footer renders, all copied onto one settled assistant message so a later
 * event cannot rewrite history the user has already read.
 *
 * `content` is the engine's `response`, which is the concatenation of the
 * turn's *text* blocks only. That matters beyond convenience — copy, paste and
 * read-aloud all read `content`, and thinking is excluded structurally rather
 * than by each of those remembering to filter it.
 *
 * A turn that produced no blocks at all still appends a message when it
 * errored: an empty transcript with a red LED gives the user nothing to read.
 */
export function onStreamComplete(panel, event) {
  const { requestId, result } = event.detail || {};
  if (!requestId) return;
  const ownerTabId = findTabForRequest(panel, requestId);
  if (!ownerTabId) return;
  const ownerTab = panel._tabs.get(ownerTabId);
  if (!ownerTab) return;
  const ownerIsActive = ownerTabId === panel._activeTabId;

  if (requestId === ownerTab.currentRequestId) {
    // Drain anything staged but not yet applied: the engine can send the
    // result immediately after its last chunk, before the rAF fires.
    drainChunks(ownerTab.turnBlocks);
    // Last chance to mirror: `resetTurnBlocks` below empties the list these
    // blocks are read from, and a subagent's final tool result can arrive on
    // the same tick as the turn's result.
    mirrorSubagentBlocks(panel, ownerTab);

    const turn = ownerTab.turnBlocks;
    const blocks = freezeBlocks(turn);
    const subagents = [...turn.subagents.values()].map((row) => ({ ...row }));
    // Union of the engine's own list and the one recovered from the blocks.
    // The result message is authoritative when it arrives, but a turn that
    // ended badly may carry tool results it never summarised.
    const files = [...new Set([
      ...(Array.isArray(result?.files_modified) ? result.files_modified : []),
      ...collectFilesModified(blocks),
    ])].filter((path) => typeof path === 'string' && path);

    const content = typeof result?.response === 'string' ? result.response : '';
    const startedAt = ownerTab.streamStartedAt;
    const durationField = typeof startedAt === 'number'
      ? { durationMs: Math.max(0, Date.now() - startedAt) }
      : {};

    // `user_message_id` identifies the turn for `rewind_files`. Stamped onto
    // the user message that started it, because that is the card the undo
    // affordance belongs on — "put the files back the way they were before I
    // asked this".
    if (typeof result?.user_message_id === 'string' && result.user_message_id) {
      stampUserMessageId(ownerTab, result.user_message_id);
    }

    if (blocks.length > 0 || content || result?.is_error) {
      ownerTab.messages = [
        ...ownerTab.messages,
        {
          role: 'assistant',
          content,
          blocks,
          subagents,
          files,
          turn: result && typeof result === 'object' ? { ...result } : {},
          terminalReason: result?.terminal_reason ?? null,
          ...durationField,
        },
      ];
    }

    if (result?.is_error) emitEngineErrorToast(panel, result);
    if (result?.deferred_tool_use) emitDeferredToolToast(panel, result);

    ownerTab.lastEditOutcome = computeTurnOutcome(result, files);
    // Every subagent tab still live settles with the turn. A subagent cannot
    // outlive the turn that spawned it, so one still marked live here is one
    // whose terminal event never arrived — its outcome is *unknown*, and the
    // spec is explicit that it must never be reported as completed
    // (specs5/5-webapp/subagent-browser.md § Status LEDs). The tabs stay in
    // the strip for the rest of the turn; the next send clears them.
    settleLiveSubagentTabs(
      panel,
      requestId,
      ownerTab.lastEditOutcome.status === 'error',
    );
    resetTurnBlocks(turn);
    ownerTab.streaming = false;
    ownerTab.streamingContent = '';
    ownerTab.currentRequestId = null;
    ownerTab.streamStartedAt = null;
    maybeStopStreamTimerTick(panel);
    // Remember the completed request ID so post-turn housekeeping — a
    // compaction boundary, a late permission resolution — still routes here.
    ownerTab.lastRequestId = requestId;
    panel.requestUpdate();
  }

  ownerTab.streams.delete(requestId);
  if (!ownerIsActive) panel.requestUpdate();
}

/**
 * Stamp `user_message_id` onto the most recent user message in a tab.
 *
 * Walks backwards to the last user message rather than assuming it is the last
 * message: by the time a result arrives the assistant message for the turn may
 * already be appended, and on a collaborator's client the ordering is whatever
 * the broadcasts delivered.
 */
function stampUserMessageId(tab, userMessageId) {
  for (let i = tab.messages.length - 1; i >= 0; i -= 1) {
    const message = tab.messages[i];
    if (message?.role !== 'user') continue;
    if (message.user_message_id) return;
    tab.messages = [
      ...tab.messages.slice(0, i),
      { ...message, user_message_id: userMessageId },
      ...tab.messages.slice(i + 1),
    ];
    return;
  }
}

/**
 * Toast for a turn the engine reported as failed.
 *
 * One toast, naming what the engine said. There is no provider-error
 * classifier on this path — the CLI owns the retry and the classification, and
 * inventing a taxonomy on top of `errors[]` would put our guess in front of
 * its answer.
 */
export function emitEngineErrorToast(panel, result) {
  panel._emitToast(`❌ ${engineErrorReason(result)}`, 'error');
}

/**
 * Toast for a turn that ended with a tool call deferred.
 *
 * The call did not run and will not run: the turn ended holding it. Silence
 * here reads as a turn that simply chose not to act.
 */
export function emitDeferredToolToast(panel, result) {
  const deferred = result?.deferred_tool_use;
  const name = deferred && typeof deferred === 'object' && deferred.name
    ? ` (${deferred.name})`
    : '';
  panel._emitToast(`⏸️ A tool call was deferred${name}`, 'warning');
}

// ---------------------------------------------------------------
// Reconnect replay
// ---------------------------------------------------------------

/**
 * Rebuild a tab's live turn from an `active_streams[]` entry.
 *
 * Replay is block state, not a chunk log — what the engine keeps is each
 * block's latest content, so a user who refreshed mid-turn sees the turn as it
 * stands rather than an empty card waiting for the next token. The next live
 * chunk for a block is compared against the snapshot's `seq`, so replay does
 * not reopen the door to stale chunks.
 */
export function resumeStreamBlocks(panel, tab, stream) {
  if (!tab || !stream || typeof stream !== 'object') return false;
  const requestId = stream.request_id;
  if (typeof requestId !== 'string' || !requestId) return false;
  applyReplayBlocks(tab.turnBlocks, stream.blocks);
  tab.streaming = true;
  tab.currentRequestId = requestId;
  tab.streams.set(requestId, { sticky: true });
  // The engine reports when the turn began; without it the elapsed counter
  // would restart from the reconnect and under-report the run.
  const startedAt = Number(stream.started_at);
  tab.streamStartedAt = Number.isFinite(startedAt) && startedAt > 0
    ? startedAt * 1000
    : Date.now();
  return true;
}

// The retry banner lived here until conversion phase 3. AC⚡DC's own
// completion wrapper slept between attempts and pushed `streamRetry` before
// each sleep, so the panel drew a countdown bar to prove the UI hadn't
// frozen. The CLI retries inside the subprocess and never narrates it; what
// it does report is a `rateLimit` message, which `onRateLimit` above turns
// into a notice carrying the real reset time. A countdown we cannot source
// would be an invented number.

// ---------------------------------------------------------------
// Passive observer for user messages
// ---------------------------------------------------------------

/**
 * Handle a `user-message` window event.
 *
 * The server broadcasts user messages to every client. If we are the sender we
 * already added it optimistically in `send`, so the echo is ignored. If we are
 * a collaborator (no in-flight request) the message is added so it appears
 * before the streaming response arrives, with the same file hints the sender
 * saw.
 */
export function onUserMessage(panel, event) {
  if (panel._currentRequestId) return;
  const data = event.detail || {};
  const content = data.content ?? '';
  if (!content) return;
  const files = Array.isArray(data.files) ? data.files : undefined;
  panel.messages = [
    ...panel.messages,
    {
      role: 'user',
      content,
      ...(files && files.length ? { files } : {}),
      ...(data.request_id ? { request_id: data.request_id } : {}),
    },
  ];
}

// ---------------------------------------------------------------
// Stream-start error handling
// ---------------------------------------------------------------

/**
 * Handle a synchronous error response from ``chat_streaming``.
 *
 * The service resolves with an error dict rather than rejecting, for the gates
 * it can check before launching: no engine, a turn already in flight, a
 * non-localhost caller, an empty message. Appends the reason as an assistant
 * message so the failure sits inline with the user message that caused it,
 * clears streaming state, and leaves the tab open to retry.
 *
 * Owning-tab outcome write: ``send()`` flips ``panel._streaming`` (the
 * active-tab reactive surface) but doesn't touch the per-tab ``tab.streaming``
 * / ``tab.lastEditOutcome`` state the LED row reads. Without writing those
 * here, a stream-start error leaves the LED at its prior outcome — green if a
 * previous turn succeeded — even though this turn never started.
 */
export function handleStreamStartError(panel, requestId, errorMsg) {
  panel._streaming = false;
  panel._streamingContent = '';
  panel._currentRequestId = null;
  panel._streams.delete(requestId);
  panel._streamStartedAt = null;

  const ownerTab = panel._tabs.get(panel._activeTabId);
  if (ownerTab) {
    ownerTab.streaming = false;
    ownerTab.streamingContent = '';
    ownerTab.currentRequestId = null;
    ownerTab.streams.delete(requestId);
    ownerTab.streamStartedAt = null;
    resetTurnBlocks(ownerTab.turnBlocks);
    ownerTab.lastEditOutcome = {
      status: 'error',
      appliedCount: 0,
      failureReason: errorMsg || 'the turn failed to start',
    };
  }
  maybeStopStreamTimerTick(panel);

  // "A turn is already in flight" fires when the user sends before the
  // post-reconnect resume has finished adopting the running turn. Narrow
  // window, but real on a slow connection — and the useful thing to say is
  // what to do about it, not just that it happened.
  let displayMsg = errorMsg;
  if (typeof errorMsg === 'string' && /already in flight|already running/i.test(errorMsg)) {
    displayMsg = (
      `${errorMsg}\n\n`
      + 'A previous turn is still running on the server. Wait for it to '
      + 'finish — the panel picks it up when the next chunk arrives — or '
      + 'interrupt it with Stop.'
    );
    panel._emitToast(
      'A turn is still running — wait for it or press Stop',
      'warning',
    );
  }
  panel.messages = [
    ...panel.messages,
    { role: 'assistant', content: `**Error:** ${displayMsg}` },
  ];
  panel.requestUpdate();
}

/**
 * Report a slash command the engine has no meaning for.
 *
 * `chat_streaming` refuses these rather than sending them as prose — a
 * `/compact` typed at the box must not reach the model as the word "/compact"
 * (specs5/5-webapp/chat.md § Invariants). The equivalent, when the service
 * names one, is the actionable half of the message.
 */
export function handleUnsupportedSlash(panel, result) {
  const command = typeof result?.command === 'string' ? result.command : '';
  const message = typeof result?.message === 'string' && result.message
    ? result.message
    : `${command || 'That command'} isn't supported here.`;
  const equivalent = typeof result?.equivalent === 'string' && result.equivalent
    ? `\n\nUse ${result.equivalent} instead.`
    : '';
  panel.messages = [
    ...panel.messages,
    { role: 'system', content: `${message}${equivalent}` },
  ];
  panel._emitToast(message, 'warning');
}
