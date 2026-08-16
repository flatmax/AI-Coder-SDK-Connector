// Window-event wiring + lifecycle hooks for the
// ChatPanel.
//
// Owns:
//
//   - connectedCallback / disconnectedCallback
//     attach/detach for every window-level event
//     the panel listens to
//   - Stream-related event delegation (the actual
//     handlers live in streaming.js — this module
//     just routes)
//   - Session lifecycle (`session-changed`,
//     `state-loaded`) — replaces messages, resets
//     streaming state, seeds input history
//   - Compaction events (URL fetch progress,
//     history compaction)
//   - Mode toggles (RPC calls + broadcast sync)
//   - Commit result handler (appends system event
//     to the conversation)
//   - Snippet loading + reload on mode/review
//     changes
//   - Mode hydration from `get_current_state`
//   - Alt+` chat-tab cycling document listener
//   - `updated()` lifecycle hook helpers (auto-
//     scroll, lightbox focus)
//
// Why the lifecycle methods don't live on the
// component class: Lit's
// `connectedCallback`/`disconnectedCallback` need
// to be on the prototype because they're called
// by the framework. The component class
// (`index.js`) provides one-line forwarders that
// call `attachEventListeners(this)` and
// `detachEventListeners(this)` — keeping the
// wiring tables here means the events file is the
// one place to look when adding a new event.

import { hydrateImageRefs, imageRefKey, imageRefsOf } from '../image-refs.js';
import { SPEECH_STATE_EVENT } from '../speech-player.js';
import { compactionSummary } from './block-render.js';
import { resetTurnBlocks } from './blocks.js';
import {
  INITIAL_PERMISSION_MODE,
  onRoleChanged,
  probeModeAuthority,
} from './permission-mode.js';
import { restoreMessage } from './restore.js';
import { clearHistoricalTabs, onChatTabShortcut, onTabClose } from './tabs.js';
import {
  onAgentsSpawned,
  onEngineHealth,
  onHookEvent,
  onPermissionModeChanged,
  onPermissionRequest,
  onPermissionResolved,
  onRateLimit,
  onSessionStarted,
  onStreamChunk,
  onStreamComplete,
  onSubagentEvent,
  onSystemEvent,
  onThinkingChunk,
  onToolResult,
  onToolUse,
  onUserMessage,
  resumeStreamBlocks,
  startStreamTimerTick,
  stopStreamTimerTick,
} from './streaming.js';

// ---------------------------------------------------------------
// Event handler binding
// ---------------------------------------------------------------

/**
 * Bind every window-event handler the chat panel
 * needs. Called once at construction time
 * (before connectedCallback) so the references
 * are stable across attach/detach cycles.
 *
 * Each `_on*` field on the panel is a stable
 * function reference. addEventListener +
 * removeEventListener pairs in attach/detach
 * use those references directly so listener
 * cleanup actually fires.
 */
export function bindEventHandlers(panel) {
  panel._onStreamChunk = (e) => onStreamChunk(panel, e);
  panel._onStreamComplete = (e) => onStreamComplete(panel, e);
  panel._onUserMessage = (e) => onUserMessage(panel, e);
  panel._onUserMessageImages = (e) => onUserMessageImages(panel, e);
  // Claude Code engine channels. Every one is a distinct window event
  // because the engine reports them as distinct pushes; folding them into
  // one "engine-event" channel with a discriminator would only move the
  // switch statement from the wiring table into the handler.
  panel._onThinkingChunk = (e) => onThinkingChunk(panel, e);
  panel._onToolUse = (e) => onToolUse(panel, e);
  panel._onToolResult = (e) => onToolResult(panel, e);
  panel._onSessionStarted = (e) => onSessionStarted(panel, e);
  panel._onSubagentEvent = (e) => onSubagentEvent(panel, e);
  panel._onHookEvent = (e) => onHookEvent(panel, e);
  panel._onRateLimit = (e) => onRateLimit(panel, e);
  panel._onEngineHealth = (e) => onEngineHealth(panel, e);
  panel._onSystemEvent = (e) => onSystemEvent(panel, e);
  panel._onPermissionRequest = (e) => onPermissionRequest(panel, e);
  panel._onPermissionResolved = (e) => onPermissionResolved(panel, e);
  panel._onPermissionModeChanged = (e) => onPermissionModeChanged(panel, e);
  panel._onRoleChanged = (e) => onRoleChanged(panel, e);
  panel._onAgentsSpawned = (e) => onAgentsSpawned(panel, e);
  panel._onAgentsRehydrated = (e) => onAgentsRehydrated(panel, e);
  panel._onAgentClosed = (e) => onAgentClosed(panel, e);
  panel._onSessionChanged = (e) => onSessionChanged(panel, e);
  panel._onStateLoaded = (e) => onStateLoaded(panel, e);
  panel._onPostResponseComplete = (e) => onPostResponseComplete(panel, e);
  panel._onCompactionEvent = (e) => onCompactionEvent(panel, e);
  panel._onModeOrReviewChanged = () => onModeOrReviewChanged(panel);
  panel._onModeChanged = (e) => onModeChanged(panel, e);
  panel._onAgentModeChanged = (e) => onAgentModeChanged(panel, e);
  panel._onCommitResult = (e) => onCommitResult(panel, e);
  panel._onChatTabShortcutBound = (e) => onChatTabShortcut(panel, e);
  panel._onSpeechPlayerState = (e) => onSpeechPlayerState(panel, e);
}

// ---------------------------------------------------------------
// Text-to-speech state sync
// ---------------------------------------------------------------

/**
 * Sync the per-message speaker toggle from the shared
 * speech player's state.
 *
 * The 🔊/⏹ button on each message card reflects
 * `panel._speakingMsgIndex` (rendering.js). Playback is
 * owned by the module-level `speechPlayer` (speech-player.js)
 * and driven from anywhere — a card's speaker button, the
 * floating transport's stop button, or the queue finishing
 * on its own. Rather than have each of those touch the
 * panel's state, the player emits `speech-player-state` and
 * we mirror its `ownerKey` (the message index passed by
 * `speakMessage`) here.
 *
 * When playback is inactive, or owned by a different panel
 * instance / non-numeric key, the index resets to -1 so no
 * button shows the stop state.
 */
export function onSpeechPlayerState(panel, event) {
  const state = event?.detail;
  const next =
    state && state.active && typeof state.ownerKey === 'number'
      ? state.ownerKey
      : -1;
  if (panel._speakingMsgIndex !== next) {
    panel._speakingMsgIndex = next;
  }
}

// ---------------------------------------------------------------
// Stream resumption after reconnect
// ---------------------------------------------------------------

/**
 * Resume in-flight streams reported by
 * ``get_current_state.active_streams``.
 *
 * Each entry is the engine's ``ActiveStream`` shape —
 * ``{request_id, session_id, started_at, blocks}`` — where ``blocks`` is the
 * turn's *block state*, one entry per block:
 * ``{block_id, kind, seq, content, tool}``.
 *
 * That it is state and not a chunk log is the whole design. A reconnecting
 * client replays the current content of each block, in one pass, and is then
 * exactly where a client that never disconnected would be. Replaying a chunk
 * log would mean re-applying supersessions in order and hoping none were
 * dropped, for the same end result.
 *
 * There is no ``agent_id`` in the payload, and never more than one entry: the
 * engine runs one CLI session with one turn in flight, so the resumed turn is
 * always the main tab's. Subagent work inside that turn arrives as blocks
 * carrying their parent ``Task`` call's id and nests under its card — it is
 * part of this turn, not a separate stream. (Phase 3's agent tabs are a
 * frontend grouping of the same single stream, not concurrent streams.)
 *
 * The engine keeps broadcasting to every connected websocket for the
 * remainder of the turn, so the refreshed browser gets the NEXT chunk
 * normally; the replayed block state bridges the gap until it arrives, which
 * for a long tool call can be tens of seconds.
 *
 * Defensive against malformed entries — anything missing a ``request_id``, or
 * whose blocks aren't a list, is skipped silently. The contract is
 * "active_streams may be missing or empty"; a malformed entry gets the same
 * treatment.
 */
export function resumeActiveStreams(panel, activeStreams) {
  if (!Array.isArray(activeStreams)) return;
  if (activeStreams.length === 0) return;
  const tab = panel._tabs.get('main');
  if (!tab) return;
  let resumed = false;
  for (const entry of activeStreams) {
    if (resumeStreamBlocks(panel, tab, entry)) resumed = true;
  }
  if (!resumed) return;
  if (panel._activeTabId === 'main') panel.requestUpdate();
  // Kick the ticker — `resumeStreamBlocks` armed the run timer.
  startStreamTimerTick(panel);
}

// ---------------------------------------------------------------
// Lifecycle attach/detach
// ---------------------------------------------------------------

/**
 * Attach every window-level listener. Called
 * from the component's connectedCallback.
 *
 * Listener channels (informational — the wiring
 * is one block below):
 *
 *   stream-chunk / stream-complete — server-push
 *     stream events, routed by request ID to the
 *     owning tab.
 *
 *   thinking-chunk / tool-use / tool-result /
 *   subagent-event — the rest of the Claude Code
 *     block stream. Same request-ID routing; each
 *     folds into the owning tab's `turnBlocks`
 *     (see blocks.js) rather than into prose.
 *
 *   session-started / engine-health / rate-limit /
 *   system-event / hook-event — engine reports that
 *     are not transcript content. Mostly stashed or
 *     toasted; see the handlers in streaming.js for
 *     which ones earn a toast and why the rest
 *     deliberately don't.
 *
 *   permission-request / permission-resolved —
 *     the gate. Session-wide, not turn-scoped:
 *     every client sees the dialog, because the
 *     turn is stalled and whoever is at a keyboard
 *     should be able to answer.
 *
 *   permission-mode-changed / role-changed — the
 *     safety posture and this client's authority
 *     over it. The selector only ever moves on
 *     these, never on its own click.
 *
 *   user-message — server broadcasts user
 *     messages to all clients; passive observers
 *     append, sender skips its own echo.
 *
 *   user-message-images — the pointers to that
 *     message's images, which only exist once the
 *     CLI has written the entry they live in.
 *     Attached to the message the request id names.
 *
 *   agents-spawned — backend pre-spawn signal so
 *     child stream chunks have somewhere to land.
 *
 *   session-changed — explicit session swap
 *     (new_session, load_session_into_context).
 *
 *   state-loaded — initial snapshot from
 *     get_current_state on connect/reconnect.
 *     Backend auto-restores the most recent
 *     session on boot; without consuming this
 *     event the chat panel would render empty.
 *
 *   post-response-complete — the quiet point
 *     after a turn. Read for one field: the
 *     session-directory size warning, whose other
 *     carrier is the snapshot above.
 *
 *   compaction-event — URL fetch progress, history
 *     compaction stages, doc-enrichment stages
 *     (the last group is dropped here — the
 *     header progress bar handles them).
 *
 *   mode-changed / review-started / review-ended —
 *     mode-aware snippet reload + local mode
 *     state sync.
 *
 *   commit-result — broadcast from background
 *     commit task; appends system event to
 *     conversation, flips _committing off.
 */
export function attachEventListeners(panel) {
  window.addEventListener('stream-chunk', panel._onStreamChunk);
  window.addEventListener('stream-complete', panel._onStreamComplete);
  window.addEventListener('user-message', panel._onUserMessage);
  window.addEventListener('user-message-images', panel._onUserMessageImages);
  // Claude Code engine channels (see the block comment above).
  window.addEventListener('thinking-chunk', panel._onThinkingChunk);
  window.addEventListener('tool-use', panel._onToolUse);
  window.addEventListener('tool-result', panel._onToolResult);
  window.addEventListener('session-started', panel._onSessionStarted);
  window.addEventListener('subagent-event', panel._onSubagentEvent);
  window.addEventListener('hook-event', panel._onHookEvent);
  window.addEventListener('rate-limit', panel._onRateLimit);
  window.addEventListener('engine-health', panel._onEngineHealth);
  window.addEventListener('system-event', panel._onSystemEvent);
  window.addEventListener('permission-request', panel._onPermissionRequest);
  window.addEventListener('permission-resolved', panel._onPermissionResolved);
  window.addEventListener(
    'permission-mode-changed', panel._onPermissionModeChanged,
  );
  // Collab authority can change mid-session (the host turns collab on or
  // off), and with it whether this client may set the permission mode.
  window.addEventListener('role-changed', panel._onRoleChanged);
  window.addEventListener('session-changed', panel._onSessionChanged);
  window.addEventListener('agents-spawned', panel._onAgentsSpawned);
  window.addEventListener(
    'agents-rehydrated', panel._onAgentsRehydrated,
  );
  window.addEventListener('agent-closed', panel._onAgentClosed);
  // D2 — chat-tab keyboard shortcuts. Alt+`
  // cycles to the next tab, Alt+Shift+` to the
  // previous. Installed at the document level
  // (bubble phase) so the shortcut works
  // regardless of focus location within the
  // chat panel, but does NOT intercept typing
  // in the textarea — backtick is a normal
  // character and the chat panel itself doesn't
  // consume it. Alt+` is not claimed by the
  // app shell's shortcuts (those own Alt+1..4
  // and Alt+M).
  document.addEventListener('keydown', panel._onChatTabShortcutBound);
  // `state-loaded` fires once on connect carrying
  // the full backend state snapshot. Distinct
  // from `session-changed`, which fires when the
  // active session is explicitly replaced. On
  // startup the backend silently auto-restores
  // the most recent prior session; without this
  // listener the chat panel would show an empty
  // message list even though the backend already
  // has the prior conversation in its context.
  window.addEventListener('state-loaded', panel._onStateLoaded);
  window.addEventListener(
    'post-response-complete', panel._onPostResponseComplete,
  );
  window.addEventListener(
    'compaction-event',
    panel._onCompactionEvent,
  );
  window.addEventListener('mode-changed', panel._onModeOrReviewChanged);
  window.addEventListener('mode-changed', panel._onModeChanged);
  window.addEventListener(
    'agent-mode-changed', panel._onAgentModeChanged,
  );
  window.addEventListener(
    'review-started',
    panel._onModeOrReviewChanged,
  );
  window.addEventListener(
    'review-ended',
    panel._onModeOrReviewChanged,
  );
  window.addEventListener('commit-result', panel._onCommitResult);
  // Mirror the shared speech player's state onto the
  // per-message speaker toggle (see onSpeechPlayerState).
  window.addEventListener(
    SPEECH_STATE_EVENT,
    panel._onSpeechPlayerState,
  );
}

/**
 * Detach every listener bound by
 * `attachEventListeners`. Called from
 * `disconnectedCallback`. Also tears down any
 * capture-phase listeners installed by the
 * overflow menu (defensive — if the menu was
 * open at unmount, releasing the document
 * listeners prevents the stale handler from
 * keeping the panel reachable).
 *
 * Cancels any pending rAF or debounce timers
 * scoped to the panel itself (the per-tab
 * debounce timer held by `_fileSearchDebounceTimer`
 * gets cleared too — a brief delay between
 * disconnect and re-attach could otherwise produce
 * a stale re-render).
 */
export function detachEventListeners(panel) {
  document.removeEventListener('keydown', panel._onChatTabShortcutBound);
  window.removeEventListener('stream-chunk', panel._onStreamChunk);
  window.removeEventListener('stream-complete', panel._onStreamComplete);
  window.removeEventListener('user-message', panel._onUserMessage);
  window.removeEventListener('user-message-images', panel._onUserMessageImages);
  window.removeEventListener('thinking-chunk', panel._onThinkingChunk);
  window.removeEventListener('tool-use', panel._onToolUse);
  window.removeEventListener('tool-result', panel._onToolResult);
  window.removeEventListener('session-started', panel._onSessionStarted);
  window.removeEventListener('subagent-event', panel._onSubagentEvent);
  window.removeEventListener('hook-event', panel._onHookEvent);
  window.removeEventListener('rate-limit', panel._onRateLimit);
  window.removeEventListener('engine-health', panel._onEngineHealth);
  window.removeEventListener('system-event', panel._onSystemEvent);
  window.removeEventListener('permission-request', panel._onPermissionRequest);
  window.removeEventListener(
    'permission-resolved', panel._onPermissionResolved,
  );
  window.removeEventListener(
    'permission-mode-changed', panel._onPermissionModeChanged,
  );
  window.removeEventListener('role-changed', panel._onRoleChanged);
  window.removeEventListener('session-changed', panel._onSessionChanged);
  window.removeEventListener('agents-spawned', panel._onAgentsSpawned);
  window.removeEventListener(
    'agents-rehydrated', panel._onAgentsRehydrated,
  );
  window.removeEventListener('agent-closed', panel._onAgentClosed);
  window.removeEventListener('state-loaded', panel._onStateLoaded);
  window.removeEventListener(
    'post-response-complete', panel._onPostResponseComplete,
  );
  window.removeEventListener(
    'compaction-event',
    panel._onCompactionEvent,
  );
  window.removeEventListener(
    'mode-changed',
    panel._onModeOrReviewChanged,
  );
  window.removeEventListener('mode-changed', panel._onModeChanged);
  window.removeEventListener(
    'agent-mode-changed', panel._onAgentModeChanged,
  );
  window.removeEventListener(
    'review-started',
    panel._onModeOrReviewChanged,
  );
  window.removeEventListener(
    'review-ended',
    panel._onModeOrReviewChanged,
  );
  window.removeEventListener('commit-result', panel._onCommitResult);
  window.removeEventListener(
    SPEECH_STATE_EVENT,
    panel._onSpeechPlayerState,
  );
  // Defensive — if the overflow menu was open
  // at unmount, release the document listeners
  // so they don't keep a stale handler alive.
  // Closing via the setter would be cleaner but
  // also touches reactive state on an already-
  // tearing-down component.
  document.removeEventListener(
    'click',
    panel._onOverflowOutsideClick,
    true,
  );
  document.removeEventListener(
    'keydown',
    panel._onOverflowKeyDown,
    true,
  );
  if (panel._rafHandle != null) {
    cancelAnimationFrame(panel._rafHandle);
    panel._rafHandle = null;
  }
  if (panel._fileSearchDebounceTimer != null) {
    clearTimeout(panel._fileSearchDebounceTimer);
    panel._fileSearchDebounceTimer = null;
  }
  // The run-timer ticker — a live stream at
  // unmount would otherwise leave its interval firing
  // requestUpdate on a detached component.
  stopStreamTimerTick(panel);
}

// ---------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------

/**
 * Handle a `session-changed` event. Session load
 * or new-session — replace the message list
 * wholesale.
 *
 * Resets transient state — a session switch
 * cancels any in-flight stream from the caller's
 * perspective (the backend's stream may still be
 * running but we're no longer interested).
 */
export function onSessionChanged(panel, event) {
  const data = event.detail || {};
  const msgs = Array.isArray(data.messages) ? data.messages : [];
  panel.messages = msgs.map(restoreMessage);
  panel._streaming = false;
  panel._streamingContent = '';
  panel._currentRequestId = null;
  panel._streams.clear();
  panel._pendingChunks.clear();
  // Session swap abandons any in-flight stream — clear the
  // run timer and stop the ticker so it doesn't keep
  // re-rendering against a stamp whose stream is gone.
  panel._streamStartedAt = null;
  stopStreamTimerTick(panel);
  panel._autoScroll = true;
  // A session swap abandons the turn in flight, so the block state it was
  // accumulating is no longer anyone's. Reset every tab's, not just the
  // active one — a stale half-turn left on a background tab would surface
  // the moment the user switched to it.
  for (const tab of panel._tabs.values()) {
    resetTurnBlocks(tab.turnBlocks);
  }
  // Subagent transcripts belong to the session that was on screen. Keeping
  // them across a resume would leave a tab labelled with one session's task
  // showing a transcript from another, so the strip drops to Main alone
  // (specs5/5-webapp/subagent-browser.md § Tab Lifetime). This also abandons
  // a load still in flight for the session being left.
  clearHistoricalTabs(panel);
  // Seed input history from the loaded session's user messages — from the
  // restored list, not the raw one, so the recall filter sees the same
  // `system_event` marks the renderer does.
  seedInputHistory(panel, panel.messages);
  restoreImages(panel);
}

/**
 * Handle the `state-loaded` event dispatched by
 * AppShell after `get_current_state` returns on
 * startup / reconnect.
 *
 * The backend auto-restores the most recent
 * prior session when it boots, so
 * `get_current_state` includes the restored
 * message list. Without consuming this event,
 * the chat panel would render empty even though
 * the backend's context already has the prior
 * conversation loaded.
 *
 * Guarded against wiping an in-flight stream:
 * if the user reconnects mid-stream (rare but
 * possible), we skip the replace. The stream's
 * own completion will bring the UI back into
 * sync.
 *
 * Also guarded against an empty snapshot. A transcript the
 * service could not read renders as an empty conversation
 * rather than a failed snapshot, so a `state-loaded` arriving
 * after the user has said something would otherwise replace a
 * live conversation with nothing. An empty snapshot is always a
 * no-op: absence of a transcript is not evidence there was no
 * conversation.
 */
export function onStateLoaded(panel, event) {
  const state = event.detail || {};
  restoreStateSnapshot(panel, state);
  // Last, and outside the restore, because the restore *replaces*
  // `panel.messages` — a warning appended before it would be thrown away
  // with the list it was appended to, and the server's one-shot has already
  // been spent by then.
  noteDiskWarning(panel, state.disk_warning);
}

/** The transcript half of a `state-loaded` snapshot. */
function restoreStateSnapshot(panel, state) {
  // Capture whether we were already streaming
  // BEFORE the resume call flips the flag. Used
  // below to gate message restore — a collaborator
  // with their own stream in flight mustn't have
  // their messages clobbered by a state-loaded
  // triggered by some other event.
  const wasStreaming = panel._streaming;
  // Resume any in-flight streams the backend
  // reports. Per spec ``specs4/3-llm/streaming``
  // § Passive Stream Adoption — the originating
  // client after refresh re-attaches to the
  // live stream rather than blocking on the
  // single-stream guard.
  if (!wasStreaming) {
    resumeActiveStreams(panel, state.active_streams);
  }
  if (wasStreaming) return;
  const msgs = Array.isArray(state.messages) ? state.messages : [];
  // Only overwrite when we actually have something to restore.
  if (msgs.length === 0) return;
  // Same restore as `onSessionChanged`: this is the same transcript,
  // reached by reconnecting rather than by resuming.
  panel.messages = msgs.map(restoreMessage);
  seedInputHistory(panel, panel.messages);
  restoreImages(panel);
}

/**
 * Resolve the image pointers in the message list that was just restored.
 *
 * Deliberately not awaited by either caller: the transcript is readable while
 * the bytes are still arriving, and a session with twenty screenshots in it
 * would otherwise hold the whole restore behind twenty disk reads.
 *
 * The generation bump is what makes an in-flight hydration abandonable. Two
 * restores in a row — a reconnect landing while a resume is still fetching —
 * would otherwise have the first one's tiles written into the cache for a
 * transcript nobody is looking at any more. The cache itself is not cleared:
 * pointers are keyed by session, so entries fetched for the session being
 * left are exactly what makes returning to it instant.
 */
export function restoreImages(panel) {
  const gen = ++panel._restoreGeneration;
  const messages = panel.messages;
  hydrateImageRefs(panel, messages, {
    cache: panel._imageRefData,
    isStale: () => gen !== panel._restoreGeneration,
    label: 'chat',
  });
}

/**
 * Handle a `user-message-images` window event — the addresses of a prompt's
 * images, arriving after the message they belong to.
 *
 * The `userMessage` broadcast goes out before the turn starts, when the
 * pasted images have no addresses yet: a pointer is `{session_id,
 * entry_uuid, block}` and the entry is written by the CLI, mid-turn, some
 * time later. So the pointers follow as their own event and are attached
 * here, to the message carrying that request id.
 *
 * Which means this is a *collaborator's* handler, by construction rather
 * than by a sender check. Only `onUserMessage` stamps a `request_id` onto a
 * message, and it only runs on a client that did not send the prompt — the
 * sender's optimistic message holds the data URIs it pasted and needs
 * nothing fetched, and a message restored from disk after a reconnect
 * already came with its pointers.
 *
 * Pointers already present are not re-added: two events naming the same
 * block would otherwise draw the same screenshot twice.
 */
export function onUserMessageImages(panel, event) {
  const { requestId, data } = event.detail || {};
  if (!requestId) return;
  const refs = Array.isArray(data?.image_refs)
    ? data.image_refs.filter((ref) => ref && typeof ref === 'object')
    : [];
  if (refs.length === 0) return;
  for (let i = panel.messages.length - 1; i >= 0; i -= 1) {
    const msg = panel.messages[i];
    if (msg?.role !== 'user' || msg.request_id !== requestId) continue;
    const known = new Set(imageRefsOf(msg).map(imageRefKey));
    const added = refs.filter((ref) => !known.has(imageRefKey(ref)));
    if (added.length === 0) return;
    panel.messages = [
      ...panel.messages.slice(0, i),
      { ...msg, image_refs: [...imageRefsOf(msg), ...added] },
      ...panel.messages.slice(i + 1),
    ];
    restoreImages(panel);
    return;
  }
}

/**
 * Seed the input-history component with user
 * messages from a just-loaded session. Called
 * after messages are replaced so up-arrow recall
 * works for messages from the loaded
 * conversation, not just messages typed since
 * mount.
 *
 * Takes the *restored* list, the one `restoreMessage` produced and the
 * renderer reads. Recall and rendering then agree on which entries the user
 * actually typed: a compaction divider or a compact summary is a user-role
 * record the CLI wrote, and offering it back on up-arrow would put the
 * agent's own words in the composer.
 */
function seedInputHistory(panel, msgs) {
  const history = panel.shadowRoot?.querySelector(
    'ac-input-history',
  );
  if (!history) {
    // Component isn't mounted yet. Defer until
    // it is. Adding entries is cheap, so we can
    // safely retry once Lit commits.
    panel.updateComplete.then(() => {
      const h = panel.shadowRoot?.querySelector('ac-input-history');
      if (h) seedIntoHistory(h, msgs);
    });
    return;
  }
  seedIntoHistory(history, msgs);
}

function seedIntoHistory(historyEl, msgs) {
  for (const m of msgs) {
    if (m.role !== 'user' || m.system_event) continue;
    // `restoreMessage` has already folded multimodal content down to a
    // string plus a sibling `images` array, so there is one shape here.
    if (typeof m.content !== 'string') continue;
    const text = m.content;
    const images = Array.isArray(m.images)
      ? m.images.filter((s) => typeof s === 'string' && s)
      : [];
    if (text.trim() || images.length > 0) {
      historyEl.addEntry(text, images);
    }
  }
}

// ---------------------------------------------------------------
// Compaction events
// ---------------------------------------------------------------

/**
 * Handle a compaction / progress event from the
 * server.
 *
 * These events arrive on the same channel as
 * stream-chunk / stream-complete but carry a
 * `stage` field identifying what's happening:
 *
 *   - `compact_boundary` — the engine compacted
 *     its own context mid-turn. Appends a divider
 *     to the transcript carrying the before/after
 *     token counts.
 *
 * That is the whole vocabulary now. Five further
 * stages — `url_fetch`, `url_ready`, `compacting`,
 * `compacted` and `compaction_error` — were handled
 * here until conversion phase 3 deleted the native
 * engine that broadcast them. The first two belonged
 * to URL curation (CC-9); the last three to a
 * compaction this app performed on a history it
 * owned. The CLI compacts its own context and tells
 * us after the fact, which is what `compact_boundary`
 * is.
 *
 * `compact_boundary` deliberately did NOT take
 * the old `compacted` branch. Compaction is the
 * engine's, it happens to the engine's context,
 * and the transcript on this page is not affected
 * by it — what the divider says is "the agent's
 * memory of everything above this line is now a
 * summary", which is a fact about the model, not
 * about the page. Replacing the message list
 * would throw away the conversation the user is
 * reading to reflect a change that never touched
 * it. Per specs5/5-webapp/chat.md § Engine Event
 * Routing.
 *
 * Request ID filtering: compaction runs AFTER
 * stream-complete has fired, so
 * `_currentRequestId` is already null by the
 * time compaction events arrive. We also accept
 * events matching `_lastRequestId` (the most
 * recently completed request). Events for
 * unknown request IDs are silently dropped.
 *
 * Doc enrichment stages (`doc_enrichment_*`) are
 * ignored here. Per specs4/5-webapp/shell.md
 * they drive a header progress bar, not a
 * chat-panel toast.
 */
export function onCompactionEvent(panel, event) {
  const { requestId, event: payload } = event.detail || {};
  if (!payload || typeof payload !== 'object') return;
  const stage = payload.stage;
  if (!stage) return;
  // Request ID filter — accept current and
  // most-recent, drop anything else. Missing
  // requestId is accepted too (some progress
  // events may not carry one).
  if (
    requestId &&
    requestId !== panel._currentRequestId &&
    requestId !== panel._lastRequestId
  ) {
    return;
  }
  switch (stage) {
    case 'compact_boundary': {
      // A divider, not a replacement. The engine broadcasts this to every
      // connected client, and there is no optimistic local add to dedupe
      // against, so each client appends exactly once — same arrangement as
      // `commitResult`.
      //
      // `content` carries the plain-text form because three things downstream
      // read messages without knowing about `compaction`: chat search, the copy
      // button, and the history browser. The renderer keys off `compaction`
      // and ignores `content`.
      const summary = compactionSummary(payload);
      panel.messages = [
        ...panel.messages,
        {
          role: 'user',
          content: summary.text,
          system_event: true,
          compaction: {
            pre_tokens: summary.pre,
            post_tokens: summary.post,
            trigger: summary.trigger,
          },
        },
      ];
      return;
    }
    default:
      // Unknown stage — silent drop. Doc
      // enrichment stages fall through here (by
      // design), as do the five retired native-engine
      // stages if a stale backend still broadcasts
      // them: acting on `compacted` would replace the
      // transcript the user is reading with a
      // summary produced by an engine that is gone.
      return;
  }
}

// ---------------------------------------------------------------
// Mode + snippets
// ---------------------------------------------------------------
//
// The cross-reference toggle and its two RPC helpers stood here
// until conversion phase 4. Both indexes are always available
// to the agent as MCP tools, so there is nothing to switch on —
// specs5/5-webapp/chat.md § Preset Selector.

/**
 * Mode or review state changed — snippets are
 * mode-aware (code / doc / review), so refetch.
 * The fetch is idempotent and cheap; a stray
 * event that doesn't actually change the mode
 * just re-sets the same list.
 */
export function onModeOrReviewChanged(panel) {
  loadSnippets(panel);
}

/**
 * Sync mode state from the broadcast. Fires for
 * our own switches and for collaborators'. The
 * ``cross_ref_enabled`` field the native engine
 * also sent is ignored — see the note above.
 */
export function onModeChanged(panel, event) {
  const detail = event.detail || {};
  if (typeof detail.mode === 'string') {
    panel._mode = detail.mode;
  }
}

/**
 * Switch primary mode via RPC.
 *
 * Routes by active tab — main targets the
 * orchestrator's ContextManager via
 * ``LLMService.switch_mode``; agent tabs target
 * the per-agent ContextManager via
 * ``LLMService.switch_agent_mode``. Per
 * Increment 4b — per-agent mode is independent
 * of the orchestrator's mode, so the toggle on
 * an agent tab must NOT touch main's state.
 *
 * For main: backend broadcasts ``mode-changed``
 * which our handler picks up. For agents: backend
 * broadcasts ``agent-mode-changed`` which
 * :func:`onAgentModeChanged` picks up.
 *
 * The optimistic-update path is the same in
 * both cases — don't mutate local state, wait
 * for the broadcast.
 */
export async function switchMode(panel, mode) {
  if (mode !== 'code' && mode !== 'doc') return;
  if (!panel.rpcConnected) return;
  if (panel._activeTabId === 'main') {
    if (mode === panel._mode) return;
    return _switchMainMode(panel, mode);
  }
  return _switchAgentMode(panel, mode);
}

async function _switchMainMode(panel, mode) {
  try {
    const result = await panel.rpcExtract(
      'LLMService.switch_mode', mode,
    );
    if (result && typeof result === 'object' && result.error) {
      const reason = result.reason || result.error;
      panel._emitToast(`Mode switch failed: ${reason}`, 'warning');
    }
  } catch (err) {
    panel._emitToast(
      `Mode switch failed: ${err?.message || 'RPC error'}`,
      'error',
    );
  }
}

/**
 * Per-agent mode switch.
 *
 * No-op when the new mode equals the current —
 * saves a needless RPC and matches the backend's
 * no-op short-circuit.
 *
 * A ``+xref`` suffix on a mode read out of
 * ``_tabModes`` is dropped rather than preserved:
 * the axis it named was retired in phase 4, and
 * only an archived tab entry can still carry it.
 */
async function _switchAgentMode(panel, mode) {
  const agentId = panel._activeTabId;
  const current = panel._tabModes?.get(agentId) || 'code';
  if (mode === current) return;
  try {
    const result = await panel.rpcExtract(
      'LLMService.switch_agent_mode', agentId, mode,
    );
    if (result && typeof result === 'object' && result.error) {
      const reason = result.reason || result.error;
      panel._emitToast(
        `Agent mode switch failed: ${reason}`,
        'warning',
      );
    }
  } catch (err) {
    panel._emitToast(
      `Agent mode switch failed: ${err?.message || 'RPC error'}`,
      'error',
    );
  }
}

/**
 * Handle ``agent-mode-changed`` window events.
 *
 * Updates ``_tabModes`` for the affected agent
 * and forces a re-render so the toggle reflects
 * the new state. Detail shape is
 * ``{agent_id, mode, ...}``; only the first two
 * fields are read. Dormant since phase 3 —
 * nothing broadcasts this any more, and the shape
 * is kept for CC-8 rather than half-deleted.
 *
 * Defensive against unknown agent ids — a stale
 * broadcast for an agent the user just closed
 * is silently dropped (the tab no longer
 * exists, so updating its mode would just
 * leave a dangling map entry).
 */
export function onAgentModeChanged(panel, event) {
  const detail = event?.detail;
  if (!detail || typeof detail !== 'object') return;
  const agentId = detail.agent_id;
  const mode = detail.mode;
  if (typeof agentId !== 'string' || !agentId) return;
  if (typeof mode !== 'string' || !mode) return;
  if (!panel._tabs.has(agentId)) return;
  panel._tabModes.set(agentId, mode);
  panel.requestUpdate();
}

/**
 * Hydrate engine state from ``ClaudeCodeService.get_current_state``.
 *
 * Called from ``onRpcReady``, which is also the reconnect path — the engine's
 * session outlives the websocket, so a refreshed browser has to ask rather
 * than assume. Four things come back and each is here for a reason:
 *
 *   ``permission_mode`` — the safety posture. Hydrating it is not cosmetic:
 *     the selector renders on first paint with `INITIAL_PERMISSION_MODE`, and
 *     a session actually sitting in `acceptEdits` would otherwise show as
 *     "Ask" until the next broadcast, telling the user the opposite of what
 *     the next edit will do.
 *
 *   ``active_streams`` — a turn in flight at reconnect. Replayed as block
 *     state by :func:`resumeActiveStreams`.
 *
 *   ``pending_permissions`` — deliberately ignored here. The dialog hydrates
 *     its own queue from the same call (see permission-dialog/), and a second
 *     copy in the panel could only ever disagree with it.
 *
 *   ``engine_health`` — startup failures, credential and version warnings,
 *     transcript mirror gaps. Stashed, not toasted (see `onEngineHealth`).
 *
 * Silent on failure. This runs on every connect, and a transient RPC error
 * that resolves itself on the next broadcast doesn't deserve a toast — but a
 * *missing* method does get logged, because that means the engine service
 * isn't registered and nothing else will work either.
 */
export async function loadEngineState(panel) {
  if (!panel.rpcConnected) return;
  let state;
  try {
    state = await panel.rpcExtract('ClaudeCodeService.get_current_state');
  } catch (err) {
    console.error('[chat] ClaudeCodeService.get_current_state failed', err);
    return;
  }
  if (!state || typeof state !== 'object' || state.error) return;
  if (typeof state.permission_mode === 'string' && state.permission_mode) {
    panel._permissionMode = state.permission_mode;
  } else if (!panel._permissionMode) {
    panel._permissionMode = INITIAL_PERMISSION_MODE;
  }
  if (state.engine_health && typeof state.engine_health === 'object') {
    panel._engineHealth = state.engine_health;
  }
  noteDiskWarning(panel, state.disk_warning);
  resumeActiveStreams(panel, state.active_streams);
}

/**
 * Append the session-directory size warning a snapshot or a finished turn
 * carried, if it carried one — which is almost never.
 *
 * A transcript notice rather than a toast. The sentence names a threshold, a
 * cause and what to do about it, which is more reading than a three-second
 * toast allows, and as a system event it goes through the markdown renderer
 * so the directory it names comes out as code. It sits among the commit
 * notices, blocks nothing, and scrolls away.
 *
 * No "have I said this already" guard here. The server owns the one-shot —
 * one flag behind both carriers, spent by whichever notices first
 * (`specs-reference/3-engine/session.md` § `EngineState`) — so a second owner
 * of that rule in the browser could only disagree with it, and would swallow
 * the honest second warning from a server that has restarted.
 */
export function noteDiskWarning(panel, warning) {
  if (typeof warning !== 'string' || !warning) return;
  panel.messages = [
    ...panel.messages,
    { role: 'user', content: warning, system_event: true },
  ];
}

/**
 * Handle a `post-response-complete` window event — the quiet point after a
 * turn, once the service's post-turn housekeeping has settled.
 *
 * Only the disk warning is read from it, and only because this is the other
 * half of "checked at startup and after each turn"
 * (`specs5/3-engine/history.md` § Numeric constants): a session that crosses
 * the threshold while the browser is open would otherwise hear nothing until
 * the next reload. The payload's other two fields already have owners — the
 * context tab refetches its own breakdown when a turn ends, and the file tree
 * reloads on `filesModified`.
 *
 * Not request-scoped: the warning is about the whole session directory, not
 * about the turn that happened to notice it, so a collaborator's turn is as
 * good a messenger as our own.
 */
export function onPostResponseComplete(panel, event) {
  const { data } = event.detail || {};
  if (!data || typeof data !== 'object') return;
  noteDiskWarning(panel, data.disk_warning);
}

/**
 * Fetch snippets from the server. Fire-and-
 * forget; errors leave the snippet list
 * unchanged (preserving any previously-loaded
 * snippets) and log to console. The drawer
 * renders a placeholder when the list is empty,
 * so pre-load state and post-error state look
 * the same from the user's perspective.
 *
 * Distinguishes "method not on proxy" (expected
 * when the backend is a stripped-down test
 * fixture or an older server) from a real
 * failure (network, server error). Only the
 * latter is worth surfacing.
 */
export async function loadSnippets(panel) {
  if (!panel.rpcConnected) return;
  try {
    const snippets = await panel.rpcExtract(
      'ClaudeCodeService.get_snippets',
    );
    panel._snippets = Array.isArray(snippets) ? snippets : [];
  } catch (err) {
    const message = err?.message || '';
    if (!message.includes('method not found')) {
      console.error('[chat] get_snippets failed', err);
    }
  }
}

/**
 * Rehydrate live agent tabs from the backend's
 * ``_agent_contexts`` registry. Called from
 * ``onRpcReady`` so the chat panel reconstructs
 * writable tabs after browser refresh or
 * WebSocket reconnect.
 *
 * Per spec ``specs4/5-webapp/agent-browser.md``
 * § Refresh and Reconnect — the backend's agent
 * registry survives the websocket roundtrip;
 * the frontend tab strip does not. ``onRpcReady``
 * is the recovery point.
 *
 * Two-phase:
 *
 *   1. ``list_live_agents()`` — metadata only,
 *      one entry per registered agent.
 *      Synchronous tab creation (writable, empty
 *      message list).
 *   2. ``get_turn_archive(turn_id)`` per unique
 *      turn — returns conversation content for
 *      every agent in that turn. Filtered per
 *      tab by ``agent_idx``.
 *
 * Tabs render immediately after phase 1 so the
 * user sees the strip without waiting for
 * archive loads. Conversation messages
 * materialise as each archive call returns.
 *
 * Errors are logged but never surfaced as
 * toasts — this runs on every connect and a
 * transient failure shouldn't punish the user
 * with a notification on every reload. An
 * agent's tab without a populated message list
 * still works: the user can reply, and the
 * backend ContextManager has the full context.
 */
/**
 * Handle ``agents-rehydrated`` window events.
 *
 * Dispatched by the AppShell after the backend's
 * :func:`load_session_into_context` reconstructs agent
 * scopes from the session's archive. The detail carries
 * ``{agent_ids: [...]}`` listing which ids the backend
 * just registered, but the frontend doesn't need to
 * filter — :func:`rehydrateLiveAgents` calls
 * ``list_live_agents()`` and reconstructs every live
 * agent's tab. Tabs already present from the prior
 * connection are skipped idempotently.
 *
 * The ``session-changed`` handler runs before this one
 * (per the broadcast order in
 * :func:`load_session_into_context`), so by the time
 * we materialise tabs the message list and streaming
 * state have already been reset for the new session.
 */
export function onAgentsRehydrated(panel, event) {
  // The detail's agent_ids is informational — the
  // rehydrate path itself queries the backend. We don't
  // gate on it (an empty list would skip the call but
  // the handler still runs harmlessly through
  // list_live_agents → empty entries → no tabs).
  rehydrateLiveAgents(panel);
}

/**
 * Handle ``agent-closed`` window events.
 *
 * Dispatched by the AppShell when the backend frees an
 * agent's scope server-side — currently from
 * :func:`new_session` (which closes every live agent
 * per Increment 2 of the "Agents as first-class
 * persistent entities" plan) and from
 * :func:`close_agent_context`.
 *
 * Detail shape: ``{agent_id: string}``. We route the id
 * to :func:`onTabClose` which removes the tab from
 * ``_tabs`` and ``_tabLabels``, switches to main if the
 * closed tab was active, and frees per-tab UI state.
 *
 * Defensive against unknown ids — :func:`onTabClose`
 * already short-circuits when the tab isn't in the
 * registry. Defensive against malformed detail —
 * non-string id silently skipped (matches the agent-
 * mode-changed handler's defensive shape).
 *
 * Note that :func:`onTabClose` itself fires the close-
 * agent-context RPC. That's a no-op when the backend
 * has already freed the scope (the unknown-id branch
 * returns ``{closed: false}``), so the round-trip is
 * harmless even though it's redundant on this path.
 * The alternative (gate the RPC call inside
 * :func:`onTabClose`) would either require a new
 * "from-broadcast" flag or a registry probe; neither
 * pays for itself.
 */
export function onAgentClosed(panel, event) {
  const detail = event?.detail;
  if (!detail || typeof detail !== 'object') return;
  const agentId = detail.agent_id;
  if (typeof agentId !== 'string' || !agentId) return;
  onTabClose(panel, agentId);
}

export async function rehydrateLiveAgents(panel) {
  if (!panel.rpcConnected) return;
  let entries;
  try {
    entries = await panel.rpcExtract(
      'LLMService.list_live_agents',
    );
  } catch (err) {
    const message = err?.message || '';
    if (!message.includes('method not found')) {
      console.error('[chat] list_live_agents failed', err);
    }
    return;
  }
  if (!Array.isArray(entries) || entries.length === 0) return;

  // Lazy import to avoid a tabs.js → events.js cycle.
  const { rehydrateAgentTabs } = await import('./tabs.js');
  const created = rehydrateAgentTabs(panel, entries);
  if (created.length === 0) return;

  // One get_agent_history call per rehydrated agent.
  // Reads the agent's full reconstructed conversation
  // from its ContextManager — for session-reconstructed
  // agents that's the concatenation across every turn
  // they participated in. The earlier per-turn approach
  // (get_turn_archive(turn_id) filtered to agent_idx)
  // only returned the latest turn's messages, so multi-
  // turn agents lost all but their most recent turn
  // from the rehydrated tab.
  for (const entry of created) {
    loadAgentHistory(panel, entry);
  }
}

/**
 * Load full conversation history for one rehydrated
 * agent.
 *
 * Fire-and-forget — kicks off the async fetch and
 * returns. The tab's message list populates when the
 * RPC resolves; Lit's reactive update pipeline
 * handles the UI refresh.
 *
 * Uses ``get_agent_history`` rather than
 * ``get_turn_archive`` so multi-turn agents (those
 * that participated in several turns over the
 * session, reconstructed via the session-load path)
 * surface their full conversation, not just the
 * latest turn's messages. The backend's reconstruction
 * has already concatenated archive content across
 * every participating turn into the agent's
 * ContextManager; this RPC reads from there.
 *
 * Each record matches :class:`ContextManager`'s
 * history shape — ``{role, content, ...}`` plus
 * optional ``system_event``, ``images``, etc.
 * (essentially the same shape ``get_turn_archive``
 * records use, since the reconstruction path
 * sources from those archives).
 */
async function loadAgentHistory(panel, entry) {
  if (!panel.rpcConnected) return;
  const tabId = entry.id;
  if (typeof tabId !== 'string' || !tabId) return;

  let history;
  try {
    history = await panel.rpcExtract(
      'LLMService.get_agent_history',
      tabId,
    );
  } catch (err) {
    console.error(
      `[chat] get_agent_history(${tabId}) failed`, err,
    );
    return;
  }
  if (!Array.isArray(history) || history.length === 0) return;

  const tab = panel._tabs.get(tabId);
  if (!tab) return;

  tab.messages = history.map((r) => {
    const msg = { role: r.role, content: r.content ?? '' };
    if (Array.isArray(r.images) && r.images.length > 0) {
      msg.images = r.images;
    }
    if (r.system_event) msg.system_event = true;
    return msg;
  });
  // Recompute lastEditOutcome from the loaded history
  // so the LED row resolves to green/red per spec
  // § Refresh and Reconnect "What is genuinely lost"
  // — cyan is never recovered, but green/red is
  // recomputable.
  tab.lastEditOutcome = computeOutcomeFromArchive(history);
  panel.requestUpdate();
}

/**
 * Recompute a tab's last-completion outcome from its
 * persisted archive records.
 *
 * Mirrors ``computeLastEditOutcome`` in streaming.js
 * but reads from archive records rather than a fresh
 * stream-complete payload. Two signals:
 *
 *   - The last assistant message — if it carries
 *     edit results metadata with any failed entry,
 *     the outcome is red.
 *   - Stream-error metadata persisted on the
 *     assistant message — also red.
 *
 * Otherwise green. Cyan (active stream) is never
 * recovered across refresh per spec.
 *
 * Returns null when no assistant message exists yet
 * (fresh agent that's only seen its initial user
 * message). Null leaves the LED at its rest state
 * rather than asserting a misleading green.
 */
function computeOutcomeFromArchive(records) {
  let lastAssistant = null;
  for (const r of records) {
    if (r && r.role === 'assistant') lastAssistant = r;
  }
  if (!lastAssistant) return null;
  const editResults = Array.isArray(lastAssistant.edit_results)
    ? lastAssistant.edit_results
    : [];
  const error = lastAssistant.error;
  let appliedCount = 0;
  let firstFailure = null;
  for (const r of editResults) {
    if (!r || typeof r !== 'object') continue;
    if (r.status === 'applied') appliedCount += 1;
    else if (r.status === 'failed' && !firstFailure) {
      firstFailure = r;
    }
  }
  if (error || firstFailure) {
    let reason = 'archived failure';
    if (error) {
      reason = typeof error === 'string' ? error : 'stream failed';
    } else if (firstFailure) {
      const msg = firstFailure.message || '';
      const file = firstFailure.file || '';
      if (msg) reason = file ? `${file}: ${msg}` : msg;
      else reason = firstFailure.error_type || 'edit failed';
    }
    return {
      status: 'error',
      appliedCount,
      failureReason: reason,
    };
  }
  return {
    status: 'clean',
    appliedCount,
    failureReason: null,
  };
}

// ---------------------------------------------------------------
// Commit result
// ---------------------------------------------------------------

/**
 * Handle the `commit-result` window event
 * dispatched by AppShell when the backend's
 * background commit_all task finishes.
 *
 * Two jobs:
 *   1. Flip `_committing` off so the commit
 *      button returns to idle.
 *   2. Append the commit's system event message
 *      to the local `messages` array so the
 *      user sees it in the chat. The server
 *      already persisted the same text to the
 *      history store, so a subsequent session
 *      reload picks it up too — this handler is
 *      what makes it appear in the current
 *      session's UI without waiting for a
 *      reload.
 *
 * Per specs-reference/5-webapp/chat.md § System
 * Event Messages — commit events render as
 * role=user with system_event=true, distinct
 * styling.
 *
 * Server broadcasts `commitResult` to all
 * connected clients (not just the initiator),
 * so every client appends exactly once per
 * commit. Unlike `userMessage`, there's no
 * dedupe needed — commits don't stream and
 * there's no optimistic local-add on the
 * initiator.
 */
export function onCommitResult(panel, event) {
  panel._committing = false;
  const detail = event?.detail;
  if (!detail || typeof detail !== 'object') return;
  // Error path — don't append a message; the
  // shell has already surfaced a toast. The
  // frontend error state stops here.
  if (detail.error) return;
  const text = detail.system_event_message;
  if (typeof text !== 'string' || !text) return;
  panel.messages = [
    ...panel.messages,
    { role: 'user', content: text, system_event: true },
  ];
}

// ---------------------------------------------------------------
// updated() lifecycle helpers
// ---------------------------------------------------------------

/**
 * Run side-effects after a Lit `updated` cycle:
 *
 *   - Auto-scroll to bottom when engaged
 *   - Focus the lightbox backdrop on open so
 *     Escape works without a click first
 *
 * Called from the component class's `updated`
 * hook. Kept here rather than on the prototype
 * so the wiring lives next to the events that
 * trigger it.
 */
export function onUpdated(panel, changedProps) {
  if (panel._autoScroll) {
    // scrollToBottom is in input.js — import
    // there would create a cycle. The component
    // class injects it as a callable, but to
    // avoid yet another binding we duplicate
    // the trivial double-rAF path here.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const container = panel.shadowRoot?.querySelector(
          '.messages',
        );
        if (container) {
          container.scrollTop = container.scrollHeight;
        }
      });
    });
  }
  // Focus the lightbox backdrop when it opens
  // so Escape works without the user having to
  // click first. Using `changedProps.has` checks
  // the transition, not the current value, so
  // we don't re-focus on every render while the
  // lightbox is open.
  if (
    changedProps.has('_lightboxImage') &&
    panel._lightboxImage &&
    !changedProps.get('_lightboxImage')
  ) {
    panel.updateComplete.then(() => {
      const backdrop =
        panel.shadowRoot?.querySelector('.lightbox-backdrop');
      if (backdrop) backdrop.focus();
    });
  }
}