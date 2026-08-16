// ChatPanel — the primary interaction surface in
// the Files tab.
//
// This file is deliberately slim. Every concern
// the component owns (state, rendering, event
// handling, streaming, search, URLs, tabs,
// input) lives in a sibling module under
// `webapp/src/chat-panel/`. This file is the
// integration point — it pulls those modules
// together into a Lit element, registers the
// custom element, and exposes the public surface
// the rest of the webapp (and the test suite)
// imports.
//
// Module map:
//
//   helpers.js     — pure utilities, constants,
//                    localStorage shims, and
//                    request-ID generation
//   properties.js  — `static properties` block
//   styles.js      — `static styles` block
//   state.js       — per-tab state factory +
//                    reactive-accessor installer
//   tabs.js        — tab strip rendering, spawn,
//                    overflow menu, Alt+`
//   blocks.js      — the Claude Code block model:
//                    what a turn *is*
//   block-render.js— what a turn *looks like*
//   streaming.js   — every engine channel folded
//                    into block state; turn
//                    completion; toasts
//   permission-mode.js — the safety-posture
//                    selector and its authority
//                    probe
//   search.js      — message + file search
//                    controllers
//   input.js       — input handling, paste,
//                    images, lightbox, speech,
//                    snippets, file chips
//   rendering.js   — render() entry + per-region
//                    helpers
//   events.js      — connect/disconnect, all
//                    window event listeners,
//                    engine-state hydration,
//                    snippet load, commit result
//
// Architectural contracts preserved here (the
// modules cooperate to honour these — the
// component class itself does no business logic):
//
//   - **Streaming state keyed by request ID**
//     (specs4/0-overview/implementation-guide.md
//     D10): each tab has its own `_streams` Map;
//     routing happens by request ID via
//     `findTabForRequest`.
//
//   - **A turn is a list of blocks, not a string.**
//     Text, thinking, tool calls, tool results and
//     whole subagents interleave, and each block
//     has its own identity (`{request_id}:b{n}`,
//     or the SDK's `tool_use_id` for tools). The
//     tab's `turnBlocks` object holds the turn in
//     flight; completion freezes it onto the
//     settled message and resets it.
//
//   - **Content is cumulative within a block and
//     never across a turn.** A chunk replaces its
//     block's content rather than appending, so a
//     dropped chunk is harmless — the next one
//     carries a superset. `seq` decides which of
//     two chunks for the same block is newer;
//     anything not newer is discarded.
//
//   - **Chunks coalesced per animation frame**: a
//     pending map of block id → latest content is
//     drained by one rAF callback, so the render
//     rate is capped regardless of chunk rate.
//
//   - **The permission gate is never optimistic.**
//     The mode selector moves when the engine says
//     it moved, not when the user clicks. See
//     permission-mode.js.
//
//   - **Per-tab state lives in `_tabs`, accessed
//     via prototype-installed getter/setter
//     pairs**. Lit's `noAccessor: true` flag
//     opts out of its default accessor
//     installation (see properties.js).

import { LitElement } from 'lit';

import { RpcMixin } from '../rpc-mixin.js';
import { speechPlayer } from '../speech-player.js';
// Side-effect imports — these modules register
// custom elements (`<ac-history-browser>`,
// `<ac-input-history>`, `<ac-speech-to-text>`)
// that the render template uses. Without these
// imports the elements would render as unknown
// HTML.
//
// `<ac-url-chips>` used to be here. Phase 2 took the
// chips off the input area — they fetched URLs into
// the native engine's context, and Claude Code has
// WebFetch. See rendering.js.
import '../history-browser.js';
import '../input-history.js';
import '../speech-to-text.js';

import {
  attachEventListeners,
  bindEventHandlers,
  detachEventListeners,
  loadEngineState,
  loadSnippets,
  onUpdated,
  rehydrateLiveAgents,
  switchMode,
} from './events.js';
import { INITIAL_PERMISSION_MODE, probeModeAuthority } from './permission-mode.js';
import {
  _AGENT_LABEL_MAX_LENGTH,
  _DRAWER_STORAGE_KEY,
  _EXPERIMENTAL_ENABLED,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _SEARCH_WHOLE_WORD_KEY,
  _loadDrawerOpen,
  _loadSearchToggle,
  _saveDrawerOpen,
  _saveSearchToggle,
  deriveAgentTabLabel,
  generateRequestId,
  parseAgentTabId,
} from './helpers.js';
import {
  _DRAFT_STORAGE_KEY,
  _loadDraft,
  cancel,
  onHistoryCancel,
  onHistorySelect,
  onNewSession,
  onOpenHistory,
  onRecognitionError,
  onTranscript,
  send,
} from './input.js';
import { PROPERTIES } from './properties.js';
import { render as renderTemplate } from './rendering.js';
import {
  activateFileSearch as activateFileSearchImpl,
  scrollFileSearchToFile as scrollFileSearchToFileImpl,
  scrollToCurrentMatch,
  setSearchMode,
} from './search.js';
import { installReactiveAccessors, makeTabState } from './state.js';
import { STYLES } from './styles.js';
import { installTabHandlers, onTabClose } from './tabs.js';

export class ChatPanel extends RpcMixin(LitElement) {
  static properties = PROPERTIES;
  static styles = STYLES;

  constructor() {
    super();
    // ---------------------------------------------------------
    // Per-tab state (D21 — agent tab strip foundation)
    // ---------------------------------------------------------
    //
    // Every field that used to live on `this` directly and
    // changes per-conversation now lives inside a tab state
    // object, keyed by tab ID. Single-agent operation has
    // exactly one entry, `"main"`. Future parallel-agent
    // spawning adds one entry per agent under the same
    // Map.
    //
    // `_activeTabIdValue` is the backing storage for the
    // `_activeTabId` getter/setter defined on the
    // prototype below (see `_installActiveTabAccessor`).
    // The setter dispatches `active-tab-changed` on
    // change so sibling components can re-sync per-tab
    // state.
    this._activeTabIdValue = 'main';
    this._tabs = new Map();
    this._tabs.set('main', makeTabState());

    // Human-readable labels for the tab strip. Keyed by
    // tab ID, stored separately from `_tabs` so agent
    // tabs can have descriptive labels derived from
    // their task text without renaming their state-
    // storage key. Main's label is fixed.
    this._tabLabels = new Map();
    this._tabLabels.set('main', 'Main');

    // Per-agent mode strings — one of 'code', 'doc',
    // 'code+xref', 'doc+xref'. Populated by
    // spawnAgentTabs from the agentsSpawned payload's
    // resolved mode field. Stable for the agent's
    // lifetime (mode is fixed at spawn time per spec
    // ``specs4/7-future/parallel-agents.md``).
    // Surfaced in the tab strip tooltip and the LED
    // row's hover state. Main tab has no entry — the
    // orchestrator's mode is shown via the action-bar
    // mode toggle, not via tooltip.
    this._tabModes = new Map();

    // Overflow menu open state. Reactive (declared in
    // properties.js) rather than per-tab because it's a
    // UI-level dropdown — every tab sees the same menu.
    this._tabStripOverflowOpen = false;

    // ---------------------------------------------------------
    // Cross-tab / component-scoped state
    // ---------------------------------------------------------
    // Not per-conversation — global to the chat panel
    // (main-only concerns, handler bindings, files-tab
    // pushes).

    // Commit state. `_committing` flips true on click,
    // false when the `commit-result` window event fires.
    // Review state defaults false and is driven by the
    // parent component via property push. Both are
    // main-conversation concerns — agents never commit,
    // agents never enter review mode.
    this._committing = false;
    this.reviewActive = false;

    // Mode state. Hydrated from get_current_state on RPC
    // ready and kept in sync via the `mode-changed` window
    // event — both dormant since phase 3, the preset selector
    // that replaces this is CC-12. The cross-reference half
    // of this pair went in phase 4: both indexes are always
    // available to the agent as tools, so there is nothing to
    // switch.
    this._mode = 'code';

    // Text-to-speech: index of the message currently
    // being read aloud, or -1 when idle. Single global
    // synthesis queue, so this is component-scoped.
    this._speakingMsgIndex = -1;

    // Repo files list — pushed by files-tab for file
    // mention detection. Global to the chat panel.
    this.repoFiles = [];

    // rAF handle for chunk coalescing. One rAF active
    // at a time across all tabs.
    this._rafHandle = null;

    // ---------------------------------------------------------
    // Claude Code engine state
    // ---------------------------------------------------------

    // The engine's safety posture, and whether this client may change it.
    // Both start at their most conservative honest value: the mode that asks
    // about everything, and "we are probably the host" — see properties.js for
    // why one of those defaults narrows and the other doesn't.
    this._permissionMode = INITIAL_PERMISSION_MODE;
    this._permissionModePending = false;
    this._canSetPermissionMode = true;
    this._sessionInfo = null;
    this._engineHealth = null;

    // Per-block expansion state: Map<block_id, boolean>. Not reactive — it is
    // mutated in place, so a dirty-check on identity would never fire;
    // `block-render.js` calls `requestUpdate()` after each toggle instead.
    //
    // One Map for the whole panel rather than one per tab, because block ids
    // are already globally unique (`{request_id}:b{n}`, or the SDK's
    // `tool_use_id`). That is also what makes expansion survive the turn
    // settling: the Map outlives `turnBlocks`, so a thinking region the user
    // opened stays open once the message is frozen into the list.
    this._blockExpansion = new Map();

    // Resolved image pointers: Map<`session|uuid|block`, {dataUri}|{error}>.
    // A restored prompt carries pointers rather than bytes, and the tiles are
    // filled in from here as `history_image` answers each one — see
    // `image-refs.js`. Not reactive for the same reason as `_blockExpansion`:
    // it is mutated in place, so the hydrator calls `requestUpdate()` itself.
    // Keyed by the pointer's own fields, so an image re-attached, resumed
    // away from and resumed back to is fetched once.
    this._imageRefData = new Map();
    // Bumped by every wholesale message replace. A hydration in flight when
    // the user resumes a different session belongs to nobody, and its tiles
    // are no longer on screen.
    this._restoreGeneration = 0;

    // Bind handlers. tabs.js owns the overflow + Alt+`
    // closures; events.js owns window-event handlers;
    // input.js owns the speech-to-text + history
    // delegators (the renderer wires them via
    // `panel._onTranscript` etc., so we install thin
    // bound forwarders here).
    installTabHandlers(this);
    bindEventHandlers(this);
    this._onTranscript = (e) => onTranscript(this, e);
    this._onRecognitionError = (e) => onRecognitionError(this, e);
    this._onHistoryClose = () => { this._historyOpen = false; };
    this._onHistorySessionLoaded = () => {
      this._historyOpen = false;
    };
    // Bound mode helpers — the search bar's render path
    // calls these via `panel._switchMode(mode)` etc.
    this._switchMode = (mode) => switchMode(this, mode);
    // Bound tab-close — used by streaming.js's stale-
    // agent recovery path (it calls `panel._onTabClose`
    // when the backend reports `agent not found`).
    this._onTabClose = (tabId) => onTabClose(this, tabId);
  }

  // ---------------------------------------------------------------
  // Subagent control
  // ---------------------------------------------------------------

  /**
   * Kill one live subagent. Called by the Stop button on a subagent row
   * (block-render.js, which invokes it as `panel._stopSubagent?.(row)`).
   *
   * A prototype method rather than a bound closure in the constructor because
   * nothing detaches it — the render path reaches it through `panel`, so
   * there is no listener identity to keep stable.
   *
   * Optimism is deliberately absent: the row keeps rendering as live until
   * the engine reports `status: "killed"` through `subagentEvent`. `stop_task`
   * returns `{status: "stopping"}`, not `stopped`, and a row that greyed out
   * on the request would claim a subagent had stopped while its tools were
   * still writing files.
   */
  async _stopSubagent(row) {
    const taskId = row?.task_id;
    if (typeof taskId !== 'string' || !taskId) return;
    if (!this.rpcConnected) {
      this._emitToast('Not connected to the server', 'warning');
      return;
    }
    try {
      const result = await this.rpcExtract(
        'ClaudeCodeService.stop_task', taskId,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(`Could not stop: ${result.error}`, 'warning');
      }
    } catch (err) {
      this._emitToast(
        `Could not stop the subagent: ${err?.message || err}`,
        'error',
      );
    }
  }

  // ---------------------------------------------------------------
  // Active-tab accessor
  // ---------------------------------------------------------------
  //
  // Special-cased here rather than in state.js because
  // `_activeTabId` is the KEY into `_tabs`, not a per-
  // tab field itself. The setter dispatches
  // `active-tab-changed` on real transitions.
  //
  // It used to snapshot and restore URL chip state across
  // the switch as well — a singleton `<ac-url-chips>`
  // element showed the active tab's chips, so its `_chips`
  // Map had to be swapped by hand. Phase 2 removed the
  // chips, and with them the swap.
  //
  // Block state needs no equivalent: each tab owns its own
  // `turnBlocks` object (state.js) and the renderer reads
  // whichever tab is active, so there is no shared element
  // holding one tab's data at a time.

  get _activeTabId() {
    return this._activeTabIdValue;
  }

  set _activeTabId(value) {
    const oldValue = this._activeTabIdValue;
    if (oldValue === value) return;
    this._activeTabIdValue = value;
    this.requestUpdate('_activeTabId', oldValue);
    // Notify listeners of the transition. bubbles +
    // composed so the event crosses the shadow DOM
    // boundary.
    this.dispatchEvent(
      new CustomEvent('active-tab-changed', {
        detail: {
          tabId: value,
          previousTabId: oldValue,
        },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ---------------------------------------------------------------
  // Toast emission
  // ---------------------------------------------------------------

  /**
   * Emit a toast event. Modules that need to surface
   * user feedback go through this rather than
   * dispatching directly so the channel stays
   * consistent (e.g. for future toast deduplication).
   */
  _emitToast(message, type = 'info') {
    window.dispatchEvent(
      new CustomEvent('ac-toast', {
        detail: { message, type },
        bubbles: false,
      }),
    );
  }

  // ---------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------

  connectedCallback() {
    super.connectedCallback();
    attachEventListeners(this);
    // Restore any persisted draft. Done here
    // rather than in the constructor because
    // `_input` is a forwarding accessor backed
    // by the main tab's state — the per-tab
    // state is set up in the constructor, so
    // this is safe either place, but
    // connectedCallback also covers the case
    // where the panel is detached and re-
    // attached without re-construction. The
    // textarea is hydrated from `_input` via
    // the `.value=${panel._input}` binding in
    // the render template, so no manual sync
    // is needed.
    const draft = _loadDraft();
    if (draft && !this._input) {
      this._input = draft;
    }
    // Listen for view-agents-requested events
    // dispatched by the renderViewAgentsAffordance
    // button. The event is composed + bubbles so
    // it crosses the shadow boundary; we listen
    // on the panel itself rather than on window
    // so a future host outside the chat panel
    // can intercept too. Bound in constructor by
    // installTabHandlers.
    this.addEventListener(
      'view-agents-requested', this._onViewAgentsRequested,
    );
  }

  firstUpdated() {
    // After the textarea exists in the shadow
    // DOM, force its inline height to match the
    // restored draft. The render binding sets
    // `.value` reactively, but auto-resize only
    // runs on `input` events — without this,
    // a multi-line restored draft renders in a
    // single-row textarea until the user types.
    if (this._input) {
      const ta = this.shadowRoot?.querySelector(
        '.input-textarea',
      );
      if (ta) {
        ta.style.height = 'auto';
        ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`;
      }
    }
  }

  disconnectedCallback() {
    this.removeEventListener(
      'view-agents-requested', this._onViewAgentsRequested,
    );
    detachEventListeners(this);
    // Stop any in-flight text-to-speech so it doesn't keep
    // reading after the panel is torn down (tab close,
    // navigation, test teardown) — mirrors the mic-release
    // cleanup in speech-to-text.js. Stopping the player
    // also dismisses the floating transport. The
    // speech-player-state listener (now detached) won't
    // reset the index, so clear it directly too.
    speechPlayer.stop();
    this._speakingMsgIndex = -1;
    super.disconnectedCallback();
  }

  onRpcReady() {
    // Fetch snippets + hydrate engine state once the
    // proxy is published. RpcMixin defers this hook to
    // the next microtask so every sibling component
    // has received the proxy before any of them issues
    // requests — we're safe to call straight away.
    loadSnippets(this);
    // The engine's session outlives the websocket, so this is the reconnect
    // path as much as the startup one: permission mode, engine health, and any
    // turn still in flight all come from here.
    loadEngineState(this);
    // Whether this client may change the permission mode. Separate call
    // because it asks the collab service, not the engine — and it must not be
    // gated on the engine answering, or a participant would briefly see an
    // enabled selector.
    probeModeAuthority(this);
    // Rehydrate live agent tabs from the backend's
    // _agent_contexts registry. Per spec
    // specs4/5-webapp/agent-browser.md § Refresh and
    // Reconnect, the backend's agent registry survives
    // browser refresh and WebSocket reconnect; the
    // frontend tab strip does not, so onRpcReady is
    // the recovery point.
    rehydrateLiveAgents(this);
  }

  updated(changedProps) {
    onUpdated(this, changedProps);
  }

  // ---------------------------------------------------------------
  // Public methods
  // ---------------------------------------------------------------
  //
  // The shell calls these directly (Ctrl+Shift+F
  // routing) and the files-tab calls
  // `scrollFileSearchToFile` for picker-driven
  // overlay scrolling.

  activateFileSearch(prefill = '') {
    activateFileSearchImpl(this, prefill);
  }

  scrollFileSearchToFile(filePath) {
    scrollFileSearchToFileImpl(this, filePath);
  }

  // ---------------------------------------------------------------
  // Test-surface forwarders
  // ---------------------------------------------------------------
  //
  // The old chat-panel.js exposed `_makeTabState`,
  // `_send`, and `_cancel` as instance methods. The
  // refactor moved their implementations to
  // `./state.js` (factory) and `./input.js`
  // (functional handlers taking `panel` as their
  // first argument), neither of which lands on the
  // instance.
  //
  // Existing test files seed agent tabs via
  // `panel._tabs.set(id, panel._makeTabState())` and
  // exercise the send path via `panel._send()` /
  // `panel._cancel()`. Rather than rewrite every
  // call site, expose thin forwarders here. Costs
  // nothing in production (one extra function call)
  // and keeps the test surface stable.

  _makeTabState() {
    return makeTabState();
  }

  _send() {
    return send(this);
  }

  _cancel() {
    return cancel(this);
  }

  _setSearchMode(mode) {
    return setSearchMode(this, mode);
  }

  _onNewSession() {
    return onNewSession(this);
  }

  _onOpenHistory() {
    return onOpenHistory(this);
  }

  _scrollToCurrentMatch() {
    return scrollToCurrentMatch(this);
  }

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------

  render() {
    return renderTemplate(this);
  }
}

// Install per-tab forwarding accessors onto the
// prototype. Done at module load so every instance
// shares the same accessor descriptors (Lit's reactive-
// property contract is preserved via
// requestUpdate calls inside the setters — see
// state.js for details).
installReactiveAccessors(ChatPanel.prototype);

customElements.define('ac-chat-panel', ChatPanel);

// ---------------------------------------------------------------
// Public re-exports
// ---------------------------------------------------------------
//
// Tests and a few sibling components import these
// helpers directly. The original chat-panel.js
// re-exported them at the bottom of the file; we
// preserve the surface here so commit 13's import
// retargeting is purely path-level (`'./chat-panel.js'`
// → `'./chat-panel/index.js'`).

export {
  generateRequestId,
  deriveAgentTabLabel,
  parseAgentTabId,
  _AGENT_LABEL_MAX_LENGTH,
  _loadDrawerOpen,
  _saveDrawerOpen,
  _DRAWER_STORAGE_KEY,
  _DRAFT_STORAGE_KEY,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _SEARCH_WHOLE_WORD_KEY,
  _loadSearchToggle,
  _saveSearchToggle,
};