// Per-tab state machine for the ChatPanel component.
//
// Two exports:
//
//   - `makeTabState()` — factory returning a fresh
//     state object. Called once at construction for
//     the main tab; called again per spawned agent.
//
//   - `installReactiveAccessors(proto)` — installs
//     getter/setter pairs onto a class prototype.
//     Each pair forwards to the active tab's state
//     object (via `this._tabs.get(this._activeTabId)`)
//     and, for reactive properties, calls
//     `requestUpdate(name, oldValue)` so Lit's
//     dirty-check fires on mutation.
//
// Why prototype-installed accessors instead of
// declaring them on the class body: the original
// chat-panel.js had ~30 near-identical getter/setter
// pairs taking up several hundred lines. Generating
// them programmatically from a list keeps the
// component class slim and the per-tab fan-out
// declarative.
//
// `noAccessor: true` in `properties.js` matters
// here. Lit normally installs its own accessors that
// store values directly on the instance; we need
// those values to live in `_tabs.get(id)` instead.
// `noAccessor: true` tells Lit "don't install your
// default accessors, the user is providing their
// own", which is exactly what
// `installReactiveAccessors` does.
//
// Reactive vs non-reactive split:
//
//   - REACTIVE_FIELDS: declared in `properties.js`,
//     drive Lit re-renders. Setter calls
//     `requestUpdate(name, oldValue)`.
//
//   - NON_REACTIVE_FIELDS: per-tab storage for
//     handler-scoped state (debounce timers, stream
//     internals, scroll flags). Setters skip
//     `requestUpdate` because their mutation alone
//     doesn't change rendered output — code paths
//     that DO need a re-render also write a reactive
//     field, which carries the requestUpdate.

import { makeTurnBlocks } from './blocks.js';
import {
  _loadDrawerOpen,
  _loadSearchToggle,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _SEARCH_WHOLE_WORD_KEY,
} from './helpers.js';

/**
 * Factory: build a fresh tab state object.
 *
 * Field groupings (informational — the flat object is
 * what callers use):
 *
 *   Conversation — messages, input, pendingImages
 *   Streaming    — streaming, streamingContent,
 *                  currentRequestId, lastRequestId,
 *                  streams, pendingChunks
 *   Search       — searchQuery, searchIgnoreCase,
 *                  searchRegex, searchWholeWord,
 *                  searchCurrentIndex, searchMode,
 *                  fileSearchResults, fileSearchLoading,
 *                  fileSearchFocusedIndex,
 *                  fileSearchGeneration,
 *                  fileSearchDebounceTimer,
 *                  fileSearchScrollPaused
 *   UI           — historyOpen, snippetDrawerOpen,
 *                  lightboxImage, snippets
 *   Misc         — autoScroll, suppressNextPaste,
 *                  activeMention
 *
 * Persisted toggles (drawer state, search toggles)
 * load their initial values from localStorage so
 * each new tab inherits the user's last choice.
 * Per-tab divergence after that is intentional —
 * an agent tab's search settings may legitimately
 * differ from main's.
 */
export function makeTabState() {
  return {
    // Conversation
    messages: [],
    input: '',
    pendingImages: [],
    // Streaming
    streaming: false,
    streamingContent: '',
    currentRequestId: null,
    lastRequestId: null,
    streams: new Map(),
    pendingChunks: new Map(),
    // Block state for the turn currently in flight. Owned by `blocks.js`,
    // mutated in place so an in-flight rAF closure and the renderer always see
    // the same object. Frozen onto the settled assistant message at
    // completion, then reset — a tab never carries two turns' live blocks.
    turnBlocks: makeTurnBlocks(),
    // Run-timer start stamp. Set to Date.now() the
    // moment a turn's stream is armed on this tab
    // (send, resume-after-reconnect, agent spawn /
    // retask) and reset to null when the stream
    // completes. The streaming card reads it every
    // tick to render the live "how long the assistant
    // has been running" elapsed counter; the settled
    // assistant message gets the frozen duration baked
    // onto its record at completion (msg.durationMs).
    // null when no stream is in flight on this tab.
    streamStartedAt: null,
    // Search — toggle defaults loaded from localStorage
    // so the user's last chosen search mode survives
    // reload.
    searchQuery: '',
    searchIgnoreCase: _loadSearchToggle(
      _SEARCH_IGNORE_CASE_KEY,
      true,
    ),
    searchRegex: _loadSearchToggle(_SEARCH_REGEX_KEY, false),
    searchWholeWord: _loadSearchToggle(
      _SEARCH_WHOLE_WORD_KEY,
      false,
    ),
    searchCurrentIndex: -1,
    searchMode: 'message',
    fileSearchResults: [],
    fileSearchLoading: false,
    fileSearchFocusedIndex: -1,
    fileSearchGeneration: 0,
    fileSearchDebounceTimer: null,
    fileSearchScrollPaused: false,
    // UI
    historyOpen: false,
    snippetDrawerOpen: _loadDrawerOpen(),
    lightboxImage: null,
    snippets: [],
    // Misc non-reactive flags / state
    autoScroll: true,
    suppressNextPaste: false,
    activeMention: null,
    // Last-completion outcome for this tab. Drives the
    // LED row's green/red state for agent tabs (cyan
    // flashing comes from the live `streaming` flag
    // above, so `lastEditOutcome` only needs to record
    // the post-stream resting state).
    //
    // Shape:
    //   null — never streamed, or fresh stream in
    //          flight. LEDs default to cyan flashing
    //          while streaming, no LED at rest.
    //   { status: 'clean', appliedCount, failureReason: null }
    //   { status: 'error', appliedCount, failureReason }
    //
    // `appliedCount` is the count of EditResult entries
    // with status === 'applied'. `failureReason` carries
    // the human-readable diagnostic — provider error
    // message, anchor-not-found, ambiguous, or the
    // assimilation-failed marker. Per spec
    // ``specs4/5-webapp/agent-browser.md`` § Status LEDs.
    //
    // Reset to null when a fresh stream starts on this
    // tab so a previous failure doesn't show stale red
    // on the next turn.
    lastEditOutcome: null,
    // Read-only flag for subagent transcripts read off disk (those
    // populated via `view-subagents-requested`). Per spec
    // ``specs5/5-webapp/subagent-browser.md`` — there is no channel to a
    // subagent at all, live or finished, so the tab carries no input
    // surface: not a greyed-out textarea, which would imply a channel that
    // might open under some condition, but none at all. Main defaults to
    // false; only transcript tabs flip this to true.
    readOnly: false,
    // The subagent this tab is a feed for, or null on every other tab —
    // Main, an agent tab, an archived transcript. Presence of this field is
    // what marks a tab as a live subagent's, so the strip, the LED row and
    // `clearSubagentTabs` all test it rather than parsing the tab id.
    //
    // Shape (a copy of the subagent row, plus the tab's own bookkeeping):
    //   rowKey       the key `blocks.js` filed the row under — stable even
    //                when a later event supplies an `agent_id` the first
    //                one lacked, which is why lookups prefer it
    //   agent_id     the SDK transcript key, once reported
    //   task_id      what `stop_task` takes
    //   tool_use_id  the parent `Task` call — the id blocks produced inside
    //                this subagent carry as their `agent_id`
    //   requestId    the *parent* turn's request id. Never
    //                `currentRequestId`: a second tab claiming the parent's
    //                request would steal Main's chunks in
    //                `findTabForRequest`.
    //   description / task_type / subagent_type / status / last_tool_name /
    //   usage / summary
    //                as reported, patched cumulatively across events —
    //                except `description`, where the first non-empty one
    //                wins: the CLI reuses the field for the live activity
    //                string, which is already the `last_tool_name` chip.
    //                `task_type` is the transport ("local_agent");
    //                `subagent_type` is the agent's kind ("Explore") and is
    //                the one anything user-facing labels with.
    //   terminal     the engine's own verdict that the task ended
    //   settled      this tab's feed has stopped
    //   unknown      the parent turn ended while it was still live, so its
    //                outcome is unreported — never shown as completed
    //   errored      that turn also failed, which makes the LED red
    //   feedMessage  the blocks have been attached to a message already
    //
    // Written by `subagent-tabs.js`; read by the strip, the LED row and the
    // send gate. Per specs5/5-webapp/subagent-browser.md.
    subagent: null,
  };
}

/**
 * Reactive per-tab fields. Each entry is
 * `[propertyName, tabFieldName]`. The property name
 * is what the component code reads/writes via
 * `this.<name>`; the tab-field name is the
 * corresponding key inside the tab state object.
 *
 * Most properties are 1:1 with their tab field
 * minus the leading underscore — the underscore is
 * a Lit convention for "internal state" properties
 * but the tab-state object doesn't carry that
 * convention. Mapping is explicit so a future
 * rename in either direction stays surgical.
 */
const REACTIVE_FIELDS = [
  ['messages', 'messages'],
  ['_input', 'input'],
  ['_streaming', 'streaming'],
  ['_streamingContent', 'streamingContent'],
  ['_historyOpen', 'historyOpen'],
  ['_snippetDrawerOpen', 'snippetDrawerOpen'],
  ['_snippets', 'snippets'],
  ['_pendingImages', 'pendingImages'],
  ['_lightboxImage', 'lightboxImage'],
  ['_searchQuery', 'searchQuery'],
  ['_searchIgnoreCase', 'searchIgnoreCase'],
  ['_searchRegex', 'searchRegex'],
  ['_searchWholeWord', 'searchWholeWord'],
  ['_searchCurrentIndex', 'searchCurrentIndex'],
  ['_searchMode', 'searchMode'],
  ['_fileSearchResults', 'fileSearchResults'],
  ['_fileSearchLoading', 'fileSearchLoading'],
  ['_fileSearchFocusedIndex', 'fileSearchFocusedIndex'],
];

/**
 * Non-reactive per-tab fields. These back code
 * paths that should NOT trigger a Lit re-render on
 * mutation — streaming internals, event-handler
 * scoped flags, transient timer handles. They need
 * tab-scoped storage but no `requestUpdate` call.
 */
const NON_REACTIVE_FIELDS = [
  ['_streams', 'streams'],
  ['_currentRequestId', 'currentRequestId'],
  ['_lastRequestId', 'lastRequestId'],
  ['_pendingChunks', 'pendingChunks'],
  // Live block state. Non-reactive on purpose: the object is mutated in place
  // and its identity never changes, so a reactive setter's dirty-check would
  // never fire. `streaming.js` calls `requestUpdate()` directly after each
  // fold, which schedules unconditionally.
  ['_turnBlocks', 'turnBlocks'],
  // Run-timer start stamp. Non-reactive because the
  // live elapsed counter re-renders off the panel-
  // level 250ms ticker (see startStreamTimerTick in
  // streaming.js), not off a per-write requestUpdate.
  ['_streamStartedAt', 'streamStartedAt'],
  ['_autoScroll', 'autoScroll'],
  ['_suppressNextPaste', 'suppressNextPaste'],
  ['_activeMention', 'activeMention'],
  ['_fileSearchGeneration', 'fileSearchGeneration'],
  ['_fileSearchDebounceTimer', 'fileSearchDebounceTimer'],
  ['_fileSearchScrollPaused', 'fileSearchScrollPaused'],
];

/**
 * Install per-tab forwarding accessors onto a class
 * prototype.
 *
 * Called once at class-definition time. Walks the
 * REACTIVE_FIELDS and NON_REACTIVE_FIELDS lists and
 * defines a getter/setter pair for each. Reactive
 * setters call `requestUpdate(name, oldValue)` so
 * Lit's dirty-check fires; non-reactive setters
 * skip it.
 *
 * The closure captures `tabField` so each accessor
 * routes to the right slot of the tab state
 * object. `this._tabs.get(this._activeTabId)` is
 * the tab state at call time — the active tab can
 * change between successive accesses, which is
 * exactly the behaviour we want (e.g. switching
 * tabs makes `this.messages` return the newly-
 * active tab's messages).
 *
 * @param {Function} proto — the class prototype to
 *   modify. The chat-panel component class passes
 *   `ChatPanel.prototype`.
 */
export function installReactiveAccessors(proto) {
  for (const [propName, tabField] of REACTIVE_FIELDS) {
    Object.defineProperty(proto, propName, {
      configurable: true,
      enumerable: true,
      get() {
        return this._tabs.get(this._activeTabId)[tabField];
      },
      set(value) {
        const tab = this._tabs.get(this._activeTabId);
        const oldValue = tab[tabField];
        tab[tabField] = value;
        this.requestUpdate(propName, oldValue);
      },
    });
  }
  for (const [propName, tabField] of NON_REACTIVE_FIELDS) {
    Object.defineProperty(proto, propName, {
      configurable: true,
      enumerable: true,
      get() {
        return this._tabs.get(this._activeTabId)[tabField];
      },
      set(value) {
        this._tabs.get(this._activeTabId)[tabField] = value;
      },
    });
  }
}