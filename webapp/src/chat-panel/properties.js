// Static properties declaration for the ChatPanel
// component, extracted from chat-panel.js.
//
// Lit reads `static properties` once at class
// definition time to install reactive-property
// metadata. The block is large (~30 properties, each
// with extensive doc comments explaining per-tab
// scoping and lifecycle semantics) so it lives here
// as a plain exported object.
//
// `noAccessor: true` is set on every per-tab
// property because the actual getter/setter pairs
// are installed onto the prototype by
// `installReactiveAccessors` in state.js — Lit must
// not install its default accessors or the tab-Map
// indirection breaks.
//
// Component-scoped reactive properties (those that
// don't move with the active tab — `repoFiles`,
// `reviewActive`, `_committing`, `_mode`,
// `_tabStripOverflowOpen`)
// use Lit's default accessor path and don't carry
// `noAccessor: true`.

export const PROPERTIES = {
  // Per-tab reactive properties — every field marked
  // `noAccessor: true` has a custom getter/setter on the
  // class body that forwards to the active tab's state
  // (D21 per-tab refactor). Lit honours `noAccessor` by
  // skipping its descriptor installation and relying on
  // our setter to call requestUpdate.
  //
  // Non-per-tab reactive properties (`repoFiles`,
  // `reviewActive`) use the normal Lit accessor path.

  /**
   * Which tab is currently visible (D21 A3). Setter
   * dispatches `active-tab-changed` on change so
   * sibling components (files-tab picker, tab strip
   * UI) can re-sync their per-tab state. Single-tab
   * operation keeps this fixed at `"main"`; the
   * reactive plumbing is wired now so Phase C's
   * spawn path doesn't re-touch the switching
   * logic.
   */
  _activeTabId: { type: String, state: true, noAccessor: true },

  /**
   * Whether the tab strip's overflow menu is open
   * (D21 Phase B2). The menu is a dropdown anchored
   * to the three-dots button at the right edge of
   * the strip; it lists every tab by label for
   * direct-jump navigation. Non-per-tab because
   * it's a UI-level dropdown, not a conversation-
   * level concern — every tab sees the same menu.
   * Closed by default; toggled by button click or
   * menu-item click; dismissed by outside-click or
   * Escape.
   */
  _tabStripOverflowOpen: { type: Boolean, state: true },

  /**
   * Messages as `{role, content, system_event?}` dicts.
   * Replaced wholesale on session load; appended during
   * normal conversation. Always a new array on change so
   * Lit's default identity check triggers re-render.
   */
  messages: { type: Array, noAccessor: true },
  /**
   * Flat list of repo-relative file paths. The files-tab
   * orchestrator pushes this down via direct assignment
   * when the file tree loads (the direct-update pattern —
   * see files-tab/index.js). Assistant messages are
   * post-processed to wrap matching substrings in
   * clickable `.file-mention` spans; see
   * `_renderAssistantBody`.
   *
   * Empty array (default) disables mention detection
   * entirely — `findFileMentions` short-circuits on empty
   * lists so the cost is nil until the files-tab wires
   * up.
   *
   * Not per-tab — repo-level state, global across tabs.
   */
  repoFiles: { type: Array },
  /**
   * The `/` palette's command list, as `list_commands`
   * returned it. Null until the first `/` is typed — the
   * list comes from the CLI's initialize handshake, so
   * fetching it eagerly would either race the engine coming
   * up or cache an empty answer from before it did.
   *
   * Not per-tab: the engine advertises one command list and
   * every tab composes against the same one.
   */
  _slashCommands: { type: Array, state: true },
  /** Current textarea content. Cleared on send. */
  _input: { type: String, state: true, noAccessor: true },
  /**
   * True while a user-initiated stream is in flight. Drives
   * the Send/Stop toggle and disables the input.
   */
  _streaming: { type: Boolean, state: true, noAccessor: true },
  /**
   * Rendered content of the active streaming assistant
   * message. Updated per animation frame, not per chunk, so
   * Lit re-render rate is capped at ~60Hz.
   */
  _streamingContent: {
    type: String,
    state: true,
    noAccessor: true,
  },
  /**
   * Whether the history browser modal is open. Toggled by
   * the "History" button and by the modal's close/load
   * events.
   */
  _historyOpen: {
    type: Boolean,
    state: true,
    noAccessor: true,
  },
  /**
   * Images currently attached to the composition, as
   * data URIs. Accumulated from pastes and re-attaches;
   * cleared when the message is sent. Capped at
   * MAX_IMAGES_PER_MESSAGE; over-limit adds produce a
   * warning toast and are ignored.
   */
  _pendingImages: {
    type: Array,
    state: true,
    noAccessor: true,
  },
  /**
   * When non-null, the lightbox is open showing this data
   * URI. Set by clicking a message thumbnail or a pending
   * preview; cleared by Escape or backdrop click.
   */
  _lightboxImage: {
    type: String,
    state: true,
    noAccessor: true,
  },
  /** Current search query text. Empty = no active search. */
  _searchQuery: { type: String, state: true, noAccessor: true },
  /** Ignore-case search toggle. Persisted to localStorage. */
  _searchIgnoreCase: {
    type: Boolean,
    state: true,
    noAccessor: true,
  },
  /** Regex search toggle. Persisted to localStorage. */
  _searchRegex: {
    type: Boolean,
    state: true,
    noAccessor: true,
  },
  /** Whole-word search toggle. Persisted to localStorage. */
  _searchWholeWord: {
    type: Boolean,
    state: true,
    noAccessor: true,
  },
  /**
   * Index into the matches array of the currently-highlighted
   * match. -1 when no matches or no active search. Wraps
   * on Enter/Shift+Enter navigation.
   */
  _searchCurrentIndex: {
    type: Number,
    state: true,
    noAccessor: true,
  },
  /**
   * Search mode — 'message' (default) searches chat
   * messages; 'file' searches repository content via the
   * Repo.search_files RPC. Toggled via the mode button in
   * the action bar and by the activateFileSearch() public
   * method (called from Ctrl+Shift+F at the shell level).
   */
  _searchMode: { type: String, state: true, noAccessor: true },
  /**
   * Flat list of file search results, shape from the RPC:
   * [{file, matches: [{line_num, line, context_before,
   * context_after}]}]. Empty until the first debounced RPC
   * call completes.
   */
  _fileSearchResults: {
    type: Array,
    state: true,
    noAccessor: true,
  },
  /** True while a file-search RPC call is in flight. */
  _fileSearchLoading: {
    type: Boolean,
    state: true,
    noAccessor: true,
  },
  /**
   * Flat index into the results' matches — each file's
   * matches contribute N slots, enumerated top-to-bottom.
   * A value of 0 means the first match of the first file.
   * -1 means no focus (empty results).
   */
  _fileSearchFocusedIndex: {
    type: Number,
    state: true,
    noAccessor: true,
  },
  /**
   * True while a commit_all background task is in flight.
   * Drives the commit button's spinner state and disables
   * both commit and reset until the completion event fires.
   * Cleared by the `commit-result` window event handler.
   *
   * Not per-tab — commits are main-conversation-only.
   */
  _committing: { type: Boolean, state: true },
  /**
   * True when review mode is active. Pushed down from the
   * files-tab orchestrator when the server's review state
   * is populated. Disables the commit button — review is
   * read-only per specs4/4-features/code-review.md § Read-Only
   * Mode. Reset is NOT disabled in review mode; a user may
   * legitimately want to discard review-mode modifications.
   *
   * Defaults to false so component works standalone before
   * the files-tab wires up the push.
   *
   * Not per-tab — review is main-conversation-only.
   */
  reviewActive: { type: Boolean },
  // `_reasoningEnabled` and `_reasoningEffort` were declared here until
  // conversion phase 3. They fed the native `chat_streaming`'s `reasoning`
  // and `effort` arguments, neither of which is on the new signature — the
  // CLI decides its own thinking depth, and effort is an `engine.json` value
  // read when the subprocess starts. Their controls came off the action bar
  // in phase 2; the state, handlers and localStorage shims go here.
  /**
   * Current primary mode — 'code' or 'doc'. Component-
   * scoped (not per-tab) for now: the backend has one
   * authoritative mode and every tab follows it. When
   * backend gains per-agent mode, this moves into
   * `_makeTabState()` and the read/write paths thread
   * through agent_tag. Defaults to 'code' to match the
   * backend.
   *
   * `_crossRefEnabled` sat beside it until conversion phase
   * 4. Both indexes are permanently available to the agent as
   * MCP tools now, so the overlay it toggled has nothing left
   * to overlay (specs5/5-webapp/chat.md § Preset Selector).
   */
  _mode: { type: String, state: true },
  /**
   * Index of the message currently being read aloud via
   * the Web Speech synthesis API, or -1 when nothing is
   * speaking. Drives the 🔊/⏹ toggle state on each
   * message's speaker button. Component-scoped (not
   * per-tab) because `speechSynthesis` is a single
   * window-level queue — only one message speaks at a
   * time across the whole panel. See speech-synthesis.js
   * and `speakMessage` in input.js.
   */
  _speakingMsgIndex: { type: Number, state: true },

  // ---------------------------------------------------------------
  // Claude Code engine state
  // ---------------------------------------------------------------
  //
  // All five are component-scoped, and for the same reason: the engine has one
  // session, one permission mode and one health state, shared by every tab.
  // Making them per-tab would let two tabs disagree about whether the next edit
  // will ask, which is precisely the confusion the selector exists to remove.

  /**
   * The engine's current permission mode — one of the values in
   * `PERMISSION_MODES` (permission-mode.js), which mirror the engine's own
   * tuple in `src/aic_dc/claude_code/session.py`.
   *
   * Only ever written from the engine's word: `sessionStarted`,
   * `permissionModeChanged`, or the `get_current_state` hydration. Never
   * optimistically from a click — see permission-mode.js for why a selector
   * that runs ahead of the engine is worse than one that lags it.
   */
  _permissionMode: { type: String, state: true },
  /**
   * The postures the *running* engine accepts, as it reports them.
   *
   * `null` until `get_current_state` answers, and for any engine that does
   * not report a list — both of which mean "show the full table", which is
   * what every build before this one did.
   *
   * It exists because the table in permission-mode.js is Claude's six and
   * Antigravity accepts two, so the selector was offering four options that
   * returned `unsupported` when picked. Read from the engine rather than
   * mapped from its name (AG-R-4), and re-read on `engineChanged` because a
   * switch changes the answer.
   */
  _permissionModes: { type: Array, state: true },
  /**
   * True while a `set_permission_mode` call is in flight. Disables the selector
   * so two overlapping changes can't race, and shows a pending marker so the
   * lag between click and flip reads as "waiting" rather than "ignored".
   * Cleared by the `permissionModeChanged` broadcast on success, or by the
   * call's own error path on failure.
   */
  _permissionModePending: { type: Boolean, state: true },
  /**
   * Whether this client is allowed to change the permission mode — false for a
   * non-localhost collab participant, whom the engine rejects outright.
   *
   * Starts `true` and is narrowed by `probeModeAuthority` / the `role-changed`
   * event. Optimistic on purpose: the engine holds the real gate
   * (`ClaudeCodeService.set_permission_mode` is localhost-only), so being wrong
   * here costs a rejected call and a toast, never an unauthorised change.
   */
  _canSetPermissionMode: { type: Boolean, state: true },
  /**
   * The `sessionStarted` payload — session id, model, cwd, tools, permission
   * mode. Held for the header/HUD to read; phase 6 gives it a real home.
   * Null before the first turn.
   */
  _sessionInfo: { type: Object, state: true },
  /**
   * Latest `engineHealth` payload. Stashed rather than toasted: engine health
   * changes on reconnect and mid-turn, and a toast per transition would drown
   * the ones that matter. Null until the engine reports.
   */
  _engineHealth: { type: Object, state: true },
  /**
   * The `healthKey` of the problem set the user dismissed, or null. Keyed by
   * problem rather than by session so the banner stays quiet about what has
   * been read and speaks up about what has not — see `health-banner.js`.
   */
  _healthDismissed: { type: String, state: true },
  /**
   * True when the turn footer's mirror-gap marker asked for the banner. Forces
   * it open even with nothing wrong, because that click is a question and an
   * empty answer is still an answer.
   */
  _healthForced: { type: Boolean, state: true },
  /**
   * `list_engines()`'s answer — `{active, available, mountable}` — or null
   * before it has been read.
   *
   * The one place this panel is allowed to hold an engine *name*, and it is
   * held to render it and to send it back to `switch_engine`, never to decide
   * what to draw. What to draw is the capability descriptor's business
   * (AG-R-4), and `engine-notice.js` keeps the two apart.
   */
  _engines: { type: Object, state: true },
  /**
   * The `engineNoticeKey` of the engine-gap set the user dismissed, or null.
   * Keyed by engine and gaps rather than by session, so a dismissal covers
   * what was read and not the next engine's different gaps.
   */
  _engineNoticeDismissed: { type: String, state: true },
  /**
   * True when the engine chip asked for the notice. Forces it open even on an
   * engine with nothing missing, because that click is a question — "which
   * engine is this?" — and "this one, and it is complete" is still an answer.
   */
  _engineNoticeForced: { type: Boolean, state: true },
  /** True while a `switch_engine` call from the notice is in flight. */
  _engineSwitchPending: { type: Boolean, state: true },
  /**
   * Why the last switch was refused, or ''. Shown rather than logged:
   * `switch_engine` declines for reasons the user can act on — a turn is still
   * running, that engine has no credential here — and a button that did
   * nothing would be indistinguishable from a broken one.
   */
  _engineSwitchError: { type: String, state: true },
};