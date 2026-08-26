// SettingsTab — config editing and hot-reload.
//
// Layer 5 Phase 3.3 — Settings tab.
//
// Renders a card grid of whitelisted config types. Clicking
// a card opens its content in an inline monospace textarea.
// Save writes via Settings.save_config_content; app.json
// auto-triggers its reload RPC on save.
//
// The card set shrank with the native engine. The five prompt
// files (system, system extra, doc, review, compaction) were
// AIC⚡DC's own prompt assembly, and llm.json its provider
// credentials — neither exists now. What replaced them is
// engine.json, which is editable but deliberately NOT
// reloadable: the model, permission mode and CLI path are
// read when the subprocess starts, so a mid-session reload
// would report success while the running engine kept its
// original values.
//
// The model panel is the exception to that sentence, and the
// reason it is here. `specs5/5-webapp/settings.md § Info
// Banner` asks for the "resolved model — what the CLI actually
// resolved, which can legitimately differ from the configured
// alias", and for a long time nothing could answer it: the
// banner's old model name came from `get_config_info`, which is
// the *file's* request, and that field was removed rather than
// left to mislead. `ClaudeCodeService.get_model` answers it
// properly — the session's alias paired with the CLI's own
// resolution of it — and `set_model` is a control request, so
// unlike everything else on this tab it takes effect on the
// running session. It is also where `/model` lands
// (specs5/3-engine/session.md § Slash Commands).
//
// `/permissions` lands here too, and lands somewhere narrower:
// the mode the *next* session starts in is a field inside
// engine.json rather than a control, so the route opens that
// card and selects the `permission_mode` line. The running
// session's mode is the composer's own selector and is not this
// tab's (specs5/5-webapp/settings.md § Preference Cards).
//
// Governing spec: specs5/1-foundation/configuration.md

import { LitElement, css, html } from 'lit';
import { RpcMixin } from './rpc-mixin.js';

/**
 * Config cards — one per whitelisted type. The `key` field
 * matches the backend's CONFIG_TYPES keys. `reloadable`
 * controls whether save auto-triggers a reload RPC.
 *
 * Every card opens the textarea editor. There was a
 * `renderer: 'toggle'` variant for one card — **Agentic
 * coding**, gating whether AIC⚡DC told the model about its
 * `🟧🟧🟧 AGENT` spawn protocol — and it went with the
 * protocol: the agent's `Task` tool is part of the platform
 * and is not ours to switch off. A user who wants to
 * constrain delegation writes a `Task` deny rule in project
 * settings, which the permission layer honours like any
 * other rule. The switch machinery went with it; the
 * preference cards in
 * `specs5/5-webapp/settings.md § Preference Cards` will need
 * their own, sized to what they actually bind to.
 */
const CONFIG_CARDS = [
  { key: 'engine', icon: '🤖', label: 'Engine Config', format: 'json', reloadable: false },
  { key: 'app', icon: '⚙️', label: 'App Config', format: 'json', reloadable: true },
];

/**
 * The CLI's own alias for "whatever you would have picked anyway".
 *
 * A session's model is null when nothing named one: engine.json may omit it, in
 * which case no model argument was passed and the CLI used its own default.
 * That lands where this alias lands, and the CLI advertises the alias as a
 * `models` entry with a resolution filled in — so a null model shows as this
 * entry rather than as a blank select. The distinction is not thrown away: the
 * note under the control says the file pins nothing, because somebody who
 * wants a model pinned needs to know that it is not.
 */
export const DEFAULT_MODEL_ALIAS = 'default';

/** How long the panel stays marked after a `/model` route lands on it. */
const _FLASH_MS = 2200;

/**
 * The engine's `models` list, normalised, and guaranteed to contain `current`.
 *
 * Same lesson as the chat panel's `permissionModeOptions`: a `<select>` whose
 * value is not among its options renders as the *first* option, so an alias the
 * engine does not advertise — a custom one from engine.json, a session resumed
 * from a newer CLI — would silently read as whatever happens to be at the top
 * of the list. Appended rather than dropped, and marked as unlisted.
 *
 * An empty list stays empty. The engine connects lazily, so having nothing to
 * offer is the ordinary state before the first turn, and a one-item menu built
 * from the alias would look like a choice the user had.
 */
export function modelEntries(models, current) {
  const entries = [];
  for (const m of Array.isArray(models) ? models : []) {
    if (!m || typeof m !== 'object') continue;
    const value = typeof m.value === 'string' ? m.value : '';
    if (!value) continue;
    entries.push({
      value,
      label: typeof m.displayName === 'string' && m.displayName
        ? m.displayName
        : value,
      resolved: typeof m.resolvedModel === 'string' ? m.resolvedModel : '',
      detail: typeof m.description === 'string' ? m.description : '',
    });
  }
  if (entries.length === 0) return entries;
  const alias = current || DEFAULT_MODEL_ALIAS;
  if (!entries.some((e) => e.value === alias)) {
    entries.push({
      value: alias,
      label: alias,
      resolved: '',
      detail: 'In force for this session, but not in the engine list.',
    });
  }
  return entries;
}

/**
 * The span of the line that sets `field` in a JSON document, or null.
 *
 * Text, not `JSON.parse`: the editor holds whatever the user has typed, which
 * may not parse at all, and a field is worth pointing at precisely while the
 * file is being edited. The span runs from the key's opening quote to the end of
 * its line, so what gets selected reads as the setting — `"permission_mode":
 * "acceptEdits",` — rather than as an offset.
 *
 * A key, not a value that happens to contain the name: the quotes must be
 * followed by a colon. Without that check, a `"cli_path": "/opt/permission_mode"`
 * would be the line the reader is sent to.
 *
 * @param {string} content
 * @param {string} field
 * @returns {{start: number, end: number}|null}
 */
export function fieldLineRange(content, field) {
  if (typeof content !== 'string' || !field) return null;
  const needle = `"${field}"`;
  let at = 0;
  for (const line of content.split('\n')) {
    const col = line.indexOf(needle);
    if (col !== -1 && line.slice(col + needle.length).trimStart().startsWith(':')) {
      return { start: at + col, end: at + line.length };
    }
    at += line.length + 1;
  }
  return null;
}

export class SettingsTab extends RpcMixin(LitElement) {
  static properties = {
    /** Info banner data from get_config_info. */
    _info: { type: Object, state: true },
    /** Currently-open card key, or null. */
    _activeKey: { type: String, state: true },
    /** Content loaded into the editor textarea. */
    _editorContent: { type: String, state: true },
    /** Whether the editor is loading content. */
    _loading: { type: Boolean, state: true },
    /** Whether a save is in flight. */
    _saving: { type: Boolean, state: true },
    /**
     * The models the engine advertises, from `get_model().models`. Empty
     * before the engine's handshake, which is every load before the first
     * turn — an ordinary state, not a failure.
     */
    _models: { type: Array, state: true },
    /**
     * The model alias in force, or '' when the session pins none.
     *
     * Written from `get_model`, from `set_model`'s reply, and from a
     * `modelChanged` broadcast — never from the `<select>`'s own value on
     * change. The select is a request; this is the answer.
     */
    _model: { type: String, state: true },
    /** What the engine says `_model` resolves to, or '' if it has not said. */
    _resolved: { type: String, state: true },
    /** True while a `set_model` call is in flight. */
    _modelPending: { type: Boolean, state: true },
    /**
     * False once `Collab.get_collab_role` reports this client is not the host.
     * `set_model` is localhost-only, so an enabled control for a participant
     * would be one that always fails. Only ever narrows what is offered — the
     * engine keeps the real gate.
     */
    _canSetModel: { type: Boolean, state: true },
    /**
     * True for a moment after `/model` routed here, so the panel the command
     * meant is visibly the one it landed on. A route can arrive with this tab
     * already open and already scrolled to the panel, where scrolling alone
     * changes nothing on screen and the command looks like it did nothing.
     */
    _modelFlash: { type: Boolean, state: true },
    /** The same mark, for the editor a field-naming route opened. */
    _editorFlash: { type: Boolean, state: true },
  };

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 0;
      background: var(--bg-primary, #0d1117);
      color: var(--text-primary, #c9d1d9);
      font-size: 0.9375rem;
      overflow-y: auto;
      padding: 1rem;
    }

    .toolbar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }
    .toolbar .minimize-right {
      margin-left: auto;
    }
    .back-btn {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.875rem;
      line-height: 1;
      font-family: inherit;
    }
    .back-btn:hover {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
      border-color: rgba(240, 246, 252, 0.3);
    }

    .info-banner {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.8125rem;
      color: var(--text-secondary, #8b949e);
    }
    .info-banner strong {
      color: var(--text-primary, #c9d1d9);
    }
    .info-row {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.25rem;
    }
    .info-label {
      opacity: 0.7;
      min-width: 5rem;
    }

    /*
      The model panel. Same card chrome as the info banner so it
      reads as part of the header rather than as a config card —
      it is not one: every card below opens an editor on a file,
      and this control reaches the running session.
    */
    .model-panel {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.8125rem;
      transition: border-color 240ms ease, background 240ms ease;
    }
    /*
      The mark a routed command leaves. Border and background
      only, no animation: the tab may already be scrolled here,
      and a moving thing is the one way to be sure the reader
      sees that something answered.
    */
    .model-panel.flash {
      border-color: var(--accent-primary, #58a6ff);
      background: rgba(88, 166, 255, 0.08);
    }
    .model-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .model-title {
      font-weight: 600;
      color: var(--text-primary, #c9d1d9);
    }
    .model-select {
      background: rgba(13, 17, 23, 0.8);
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-primary, #c9d1d9);
      border-radius: 4px;
      padding: 0.25rem 0.4rem;
      font-family: inherit;
      font-size: 0.8125rem;
      max-width: 16rem;
    }
    .model-select:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .model-pending {
      color: var(--text-secondary, #8b949e);
    }
    .model-resolved {
      margin-top: 0.4rem;
      color: var(--text-secondary, #8b949e);
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.75rem;
      word-break: break-all;
    }
    .model-note {
      margin: 0.4rem 0 0;
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      line-height: 1.45;
    }
    .model-note code {
      font-family: 'SFMono-Regular', Consolas, monospace;
      background: rgba(240, 246, 252, 0.06);
      border-radius: 3px;
      padding: 0 0.2rem;
    }

    .card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }
    .card {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 5px;
      padding: 0.4rem 0.5rem;
      cursor: pointer;
      text-align: center;
      transition: border-color 120ms ease, background 120ms ease;
    }
    .card:hover {
      background: rgba(240, 246, 252, 0.04);
      border-color: rgba(240, 246, 252, 0.2);
    }
    .card.active {
      border-color: var(--accent-primary, #58a6ff);
      background: rgba(88, 166, 255, 0.08);
    }
    .card-icon {
      font-size: 1.1rem;
      display: block;
      margin-bottom: 0.2rem;
      line-height: 1;
    }
    .card-label {
      font-size: 0.75rem;
      color: var(--text-secondary, #8b949e);
      line-height: 1.2;
    }

    .editor-area {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      overflow: hidden;
      transition: border-color 240ms ease;
    }
    /* Same mark as the model panel's, for the same reason. */
    .editor-area.flash {
      border-color: var(--accent-primary, #58a6ff);
    }
    .editor-toolbar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 0.75rem;
      background: rgba(22, 27, 34, 0.6);
      border-bottom: 1px solid rgba(240, 246, 252, 0.08);
    }
    .editor-toolbar .toolbar-label {
      font-size: 0.8125rem;
      font-weight: 600;
      flex: 1;
    }
    .toolbar-button {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-primary, #c9d1d9);
      padding: 0.3rem 0.6rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.75rem;
      font-family: inherit;
    }
    .toolbar-button:hover {
      background: rgba(240, 246, 252, 0.06);
      border-color: rgba(240, 246, 252, 0.3);
    }
    .toolbar-button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .toolbar-button.primary {
      background: var(--accent-primary, #58a6ff);
      border-color: var(--accent-primary, #58a6ff);
      color: #0d1117;
    }
    .toolbar-button.primary:hover {
      filter: brightness(1.1);
    }
    .editor-textarea {
      flex: 1;
      min-height: 200px;
      width: 100%;
      box-sizing: border-box;
      resize: none;
      padding: 0.75rem;
      background: rgba(13, 17, 23, 0.8);
      border: none;
      color: var(--text-primary, #c9d1d9);
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.8125rem;
      line-height: 1.5;
    }
    .editor-textarea:focus {
      outline: none;
    }
    .loading-note {
      padding: 2rem;
      text-align: center;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
    }
  `;

  constructor() {
    super();
    this._info = null;
    this._activeKey = null;
    this._editorContent = '';
    this._loading = false;
    this._saving = false;
    this._models = [];
    this._model = '';
    this._resolved = '';
    this._modelPending = false;
    this._canSetModel = true;
    this._modelFlash = false;
    this._editorFlash = false;
    /** Pending flash-clear timer, so a second route restarts it. */
    this._flashTimer = null;
    // Bound so add/removeEventListener find the same reference.
    this._onModelChanged = this._onModelChanged.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    // Another window's switch. The window that made the call has the answer in
    // its reply and does not need this, but it arrives there too and says the
    // same thing, so no guard is needed.
    window.addEventListener('model-changed', this._onModelChanged);
  }

  disconnectedCallback() {
    window.removeEventListener('model-changed', this._onModelChanged);
    if (this._flashTimer) {
      clearTimeout(this._flashTimer);
      this._flashTimer = null;
    }
    this._modelFlash = false;
    this._editorFlash = false;
    super.disconnectedCallback();
  }

  onRpcReady() {
    this._loadInfo();
    this._loadModel();
  }

  /**
   * Re-read the model when this tab comes back on screen.
   *
   * The list is empty until the engine's handshake, and the engine connects on
   * the first turn — so a tab opened before that turn holds an empty menu with
   * nothing to tell it otherwise. `get_model` is answered from the stored
   * initialize reply, so this costs a round trip and no control request.
   */
  onTabVisible() {
    this._loadModel();
  }

  async _loadInfo() {
    if (!this.rpcConnected) return;
    try {
      const result = await this.rpcExtract('Settings.get_config_info');
      this._info = result && typeof result === 'object' ? result : null;
    } catch (err) {
      console.warn('[settings] get_config_info failed', err);
    }
  }

  /**
   * Read the model in force and the menu the engine offers.
   *
   * One RPC, because the two halves are one fact and `get_model` pairs them on
   * the server where the list already is. A failure is warned and left alone
   * rather than blanking what is on screen: the alias last read is still the
   * best answer available, and a panel that empties itself on a transient RPC
   * error would claim the session has no model.
   */
  async _loadModel() {
    if (!this.rpcConnected) return;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.get_model');
      if (!res || typeof res !== 'object' || res.error) {
        console.warn('[settings] get_model failed', res?.error);
        return;
      }
      this._model = typeof res.model === 'string' ? res.model : '';
      this._resolved = typeof res.resolved === 'string' ? res.resolved : '';
      this._models = Array.isArray(res.models) ? res.models : [];
    } catch (err) {
      console.warn('[settings] get_model failed', err);
      return;
    }
    this._probeModelAuthority();
  }

  /**
   * Find out whether this client may change the model.
   *
   * Same probe and the same defaults as the chat panel's
   * `probeModeAuthority`: no collab service registered means single-user, which
   * means we are the host. It only narrows the UI — `set_model` enforces the
   * real gate, so being wrong here costs a rejected call, not an unauthorised
   * one.
   */
  async _probeModelAuthority() {
    try {
      const role = await this.rpcExtract('Collab.get_collab_role');
      if (role && typeof role === 'object' && !role.error) {
        this._canSetModel = role.is_localhost !== false;
        return;
      }
    } catch (_) {
      // No collab service — single-user, and we are the host.
    }
    this._canSetModel = true;
  }

  /** Another window changed the model. */
  _onModelChanged(event) {
    const model = event?.detail?.model;
    if (typeof model !== 'string' && model !== null) return;
    this._model = model || '';
    this._resolved = this._resolvedFor(this._model);
    this._modelPending = false;
  }

  /** What the engine's list says an alias resolves to, or ''. */
  _resolvedFor(alias) {
    const entry = modelEntries(this._models, alias)
      .find((e) => e.value === (alias || DEFAULT_MODEL_ALIAS));
    return entry?.resolved || '';
  }

  /**
   * Handle a selection.
   *
   * The `<select>`'s DOM value goes straight back to the alias in force, before
   * the RPC resolves — the native element flips itself and Lit will not put it
   * back, because `_model` has not changed yet. The real flip comes from the
   * reply, which is authoritative: the service records the new alias only after
   * the engine's control request came back.
   *
   * The gesture latch is the one from the chat panel's permission-mode
   * selector, and it is here for the same reason a switch that costs money
   * should not be reachable by an event nobody raised — a browser restoring
   * form state on load raises a trusted `change` with no pointer or key in
   * front of it. See `webapp/src/chat-panel/permission-mode.js` for the full
   * account; the difference is that a model switch has no confirmation to
   * intercept it, so the latch is the only guard there is.
   */
  _onModelSelect(event) {
    const select = event?.target;
    const picked = select?.value;
    const current = this._model || DEFAULT_MODEL_ALIAS;
    if (select) select.value = current;
    const gesture = this._modelGesture === true;
    this._modelGesture = false;
    if (!gesture) return;
    if (typeof picked !== 'string' || !picked || picked === current) return;
    this._setModel(picked);
  }

  /** Record that a user is actually operating the control. */
  _noteModelGesture() {
    this._modelGesture = true;
  }

  /**
   * Ask the engine to switch models.
   *
   * `_modelPending` disables the control while the call is out: two switches in
   * flight would race, and the reply that landed last would win regardless of
   * which the user picked last.
   */
  async _setModel(value) {
    if (!this.rpcConnected || this._modelPending) return;
    this._modelPending = true;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.set_model', value);
      if (res && typeof res === 'object' && res.error) {
        const reason = res.error === 'restricted'
          ? res.reason || 'Only the host can change the model'
          : res.error;
        this._emitToast(reason, 'warning');
        return;
      }
      const applied = res && typeof res === 'object' ? res.model : undefined;
      this._model = typeof applied === 'string' ? applied : '';
      this._resolved = this._resolvedFor(this._model);
      this._emitToast(`Model: ${this._modelLabel()}`, 'success');
    } catch (err) {
      this._emitToast(
        `Could not change the model: ${err?.message || err}`,
        'error',
      );
    } finally {
      this._modelPending = false;
    }
  }

  /** The display name for the alias in force, falling back to the alias. */
  _modelLabel() {
    const alias = this._model || DEFAULT_MODEL_ALIAS;
    const entry = modelEntries(this._models, alias)
      .find((e) => e.value === alias);
    return entry?.label || alias;
  }

  /**
   * A routed command asked for one part of this tab.
   *
   * Duck-typed hook the shell calls after switching tabs. This tab has no
   * segmented control, so "show a section" means bring the thing the command
   * named to where the reader is looking and mark it — one panel for `/model`,
   * one field inside a config file for `/permissions`.
   *
   * An id this tab does not have is ignored, which is what keeps a service that
   * grows a new anchor from making the tab itself unreachable.
   *
   * Returns a promise for the tests' benefit; the shell ignores it.
   *
   * @param {string} id
   * @returns {Promise<void>}
   */
  async showSection(id) {
    if (id === 'model') {
      // Re-read: the command asked about the model, and the menu may have
      // arrived since this tab last looked.
      this._loadModel();
      this._flash('model');
      await this.updateComplete;
      this._scrollTo('.model-panel', 'start');
      return;
    }
    if (id === 'permission-mode') {
      await this._showEngineField('permission_mode');
    }
  }

  /**
   * Mark one part of this tab for a moment, so a routed command visibly landed.
   *
   * One timer for both marks, and each flash clears the other: two parts of the
   * tab lit at once would say two commands arrived when only the second did.
   *
   * @param {'model'|'editor'} which
   * @private
   */
  _flash(which) {
    this._modelFlash = which === 'model';
    this._editorFlash = which === 'editor';
    if (this._flashTimer) clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._flashTimer = null;
      this._modelFlash = false;
      this._editorFlash = false;
    }, _FLASH_MS);
  }

  /**
   * Open the engine.json card and select the line that sets `field`.
   *
   * What `/permissions` lands on. The mode the *next* session starts in is a
   * field in a JSON file, not a control, so "show me the permission mode" can
   * only mean: open the file that holds it and put the reader's cursor on the
   * line. Opening the tab alone left them in front of a card grid with the field
   * they asked for one click and a scroll inside a file away.
   *
   * The selection is read off the textarea rather than `_editorContent`, because
   * the textarea is where the user's unsaved edits are — including, plausibly,
   * the line they just added after this said the field was missing.
   *
   * @param {string} field
   * @private
   */
  async _showEngineField(field) {
    await this._openCard('engine');
    this._flash('editor');
    await this.updateComplete;
    this._scrollTo('.editor-area', 'center');
    const textarea = this.shadowRoot?.querySelector('.editor-textarea');
    if (!textarea) return;
    const range = fieldLineRange(textarea.value, field);
    if (!range) {
      // Say so rather than leave a flash over a file with no such line: an
      // absent key is a real answer — the CLI's own default is in force — and
      // the reader who typed the command is the one who would set it.
      this._emitToast(
        `engine.json does not set ${field} — the engine's own default is in force`,
        'info',
      );
      return;
    }
    if (typeof textarea.setSelectionRange === 'function') {
      textarea.focus();
      textarea.setSelectionRange(range.start, range.end);
    }
  }

  /**
   * Scroll one of this tab's own elements into view, if the engine can.
   *
   * jsdom has no `scrollIntoView`, and neither does an older browser.
   *
   * @private
   */
  _scrollTo(selector, block) {
    const el = this.shadowRoot?.querySelector(selector);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block, behavior: 'smooth' });
    }
  }

  async _openCard(key) {
    if (this._activeKey === key) return;
    this._activeKey = key;
    this._editorContent = '';
    this._loading = true;
    try {
      const result = await this.rpcExtract(
        'Settings.get_config_content',
        key,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        this._activeKey = null;
        return;
      }
      const content =
        typeof result === 'object' && result !== null
          ? result.content ?? ''
          : typeof result === 'string'
            ? result
            : '';
      this._editorContent = content;
    } catch (err) {
      this._emitToast(`Load failed: ${err?.message || err}`, 'error');
      this._activeKey = null;
    } finally {
      this._loading = false;
    }
  }

  _closeEditor() {
    this._activeKey = null;
    this._editorContent = '';
  }

  async _save() {
    if (!this._activeKey || this._saving) return;
    this._saving = true;
    try {
      const textarea = this.shadowRoot?.querySelector('.editor-textarea');
      const content = textarea ? textarea.value : this._editorContent;
      const result = await this.rpcExtract(
        'Settings.save_config_content',
        this._activeKey,
        content,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        return;
      }
      // Advisory JSON warning from save.
      if (result && result.warning) {
        this._emitToast(result.warning, 'warning');
      } else {
        this._emitToast('Saved', 'success');
      }
      // Auto-reload for reloadable configs.
      const card = CONFIG_CARDS.find((c) => c.key === this._activeKey);
      if (card && card.reloadable) {
        await this._reload();
      }
    } catch (err) {
      this._emitToast(`Save failed: ${err?.message || err}`, 'error');
    } finally {
      this._saving = false;
    }
  }

  /**
   * Re-read the active config into the running process.
   *
   * Only app.json is reloadable, so there is one target. A card
   * marked `reloadable: false` — engine.json is the only one left —
   * returns early rather than calling a reload that would either
   * fail or lie: engine.json's values were consumed when the
   * subprocess started.
   */
  async _reload() {
    if (!this._activeKey) return;
    const card = CONFIG_CARDS.find((c) => c.key === this._activeKey);
    if (!card || !card.reloadable) return;
    try {
      const result = await this.rpcExtract('Settings.reload_app_config');
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(`Reload failed: ${result.error}`, 'error');
      } else {
        this._emitToast('Config reloaded', 'success');
      }
    } catch (err) {
      this._emitToast(`Reload failed: ${err?.message || err}`, 'error');
    }
  }

  _onEditorKeyDown(event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 's') {
      event.preventDefault();
      this._save();
    }
  }

  _emitToast(message, type = 'info') {
    window.dispatchEvent(
      new CustomEvent('aic-toast', {
        detail: { message, type },
        bubbles: false,
      }),
    );
  }

  /**
   * Dispatch a request to the app shell to flip the
   * active dialog tab back to the chat. Companion to
   * the equivalent method in ContextUsageTab and DocConvertTab.
   */
  _goBackToChat() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-tab', {
        detail: { tab: 'files' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  /**
   * Dispatch a request to the app shell to minimize
   * the dialog. Companion to ``_goBackToChat``: the
   * tab-strip minimize button isn't reachable when
   * Settings is the active tab (the strip sits in
   * the chat panel which is a sibling tab-panel),
   * so each overlay carries its own minimize
   * affordance.
   */
  _minimizeDialog() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  render() {
    return html`
      <div class="toolbar">
        <button
          class="back-btn"
          title="Back to chat"
          aria-label="Back to chat"
          @click=${() => this._goBackToChat()}
        >← Chat</button>
        <button
          class="back-btn minimize-right"
          title="Minimize dialog"
          aria-label="Minimize dialog"
          @click=${() => this._minimizeDialog()}
        >▾</button>
      </div>
      <!--
        The banner used to lead with the model name from
        get_config_info. That RPC no longer reports one, on
        purpose: the model in engine.json is a request, and the
        engine can answer with a different one (a fallback on
        rate-limit, a set_model mid-session). The model in
        force is reported where it is known — per turn, in the
        usage HUD.
      -->
      ${this._info
        ? html`
            <div class="info-banner">
              ${this._info.config_dir
                ? html`
                    <div class="info-row">
                      <span class="info-label">Config:</span>
                      <span>${this._info.config_dir}</span>
                    </div>
                  `
                : ''}
            </div>
          `
        : ''}

      ${this._renderModelPanel()}

      <div class="card-grid">
        ${CONFIG_CARDS.map(
          (card) => html`
            <div
              class="card ${this._activeKey === card.key ? 'active' : ''}"
              @click=${() => this._openCard(card.key)}
              title="${card.label} (${card.format})"
            >
              <span class="card-icon">${card.icon}</span>
              <span class="card-label">${card.label}</span>
            </div>
          `,
        )}
      </div>

      ${this._activeKey ? this._renderEditor() : ''}
    `;
  }

  /**
   * The model in force, what it resolves to, and a switch.
   *
   * Three lines rather than one because the alias and the model are different
   * facts and the spec's Info Banner asks for both — an alias like `opus` says
   * what was requested, `au.anthropic.claude-opus-5` says what will answer, and
   * they are allowed to disagree. The resolution is the CLI's own, from its
   * `models` list, never derived here.
   *
   * Above the card grid and outside it: every card below opens an editor on a
   * file that mostly applies next session, and this one control reaches the
   * session that is running. Putting it in the grid would file it under the
   * same "applies later" promise the rest of the tab has to make.
   */
  _renderModelPanel() {
    const entries = modelEntries(this._models, this._model);
    const alias = this._model || DEFAULT_MODEL_ALIAS;
    const entry = entries.find((e) => e.value === alias);
    const readOnly = this._canSetModel === false;
    const offline = entries.length === 0;
    const disabled =
      !this.rpcConnected || readOnly || this._modelPending || offline;
    const title = readOnly
      ? 'Only the host can change the model'
      : this._modelPending
        ? 'Waiting for the engine to confirm the switch'
        : entry?.detail || 'The model this session runs on';
    return html`
      <div
        class="model-panel ${this._modelFlash ? 'flash' : ''}"
        role="group"
        aria-label="Model"
      >
        <div class="model-head">
          <span class="model-title">🧠 Model</span>
          <select
            class="model-select"
            .value=${alias}
            ?disabled=${disabled}
            autocomplete="off"
            aria-label="Model"
            title=${title}
            @pointerdown=${() => this._noteModelGesture()}
            @keydown=${() => this._noteModelGesture()}
            @change=${(e) => this._onModelSelect(e)}
          >
            ${entries.map(
              (option) => html`<option
                value=${option.value}
                ?selected=${option.value === alias}
                title=${option.detail}
              >
                ${option.label}
              </option>`,
            )}
          </select>
          ${this._modelPending
            ? html`<span class="model-pending" aria-hidden="true">…</span>`
            : ''}
        </div>
        ${offline
          ? ''
          : html`
              <div class="model-resolved">
                ${alias} →
                ${this._resolved
                  || entry?.resolved
                  || '(the engine did not say what this resolves to)'}
              </div>
            `}
        ${this._renderModelNote(offline, readOnly)}
      </div>
    `;
  }

  /**
   * The sentences under the control, one per thing that is true.
   *
   * "From your next turn" is measured, not reasoned. A `set_model` fired 22.8s
   * into a live 34s turn answered in 252ms without interrupting it — and the
   * turn went on billing `claude-opus-5`, including a usage report 124ms
   * *after* the switch landed; the turn after it billed haiku. So the switch is
   * accepted mid-turn and applies to the next one, which is exactly the thing
   * somebody reaching for a cheaper model to rescue a runaway turn needs told.
   * Said unconditionally rather than only while streaming: it is true either
   * way, and a sentence that appears only sometimes is one nobody learns.
   */
  _renderModelNote(offline, readOnly) {
    if (offline) {
      return html`<p class="model-note">
        The engine has not connected yet, so it has not said which models it
        offers — the list arrives with the first turn.
      </p>`;
    }
    return html`
      <p class="model-note">
        ${readOnly
          ? html`Only the host can change the model. `
          : ''}
        ${this._model
          ? html`Set for this session rather than in a file: a switch here
              leaves <code>engine.json</code> alone and lasts as long as the
              session. `
          : html`<code>engine.json</code> pins no model, so the CLI uses its
              own default. `}
        A switch takes effect from your <strong>next</strong> turn — a turn
        already running finishes on the model it started with. Which model
        actually answered is reported per turn, by model, in the usage HUD.
      </p>
    `;
  }

  _renderEditor() {
    const card = CONFIG_CARDS.find((c) => c.key === this._activeKey);
    if (!card) return '';
    return html`
      <div class="editor-area ${this._editorFlash ? 'flash' : ''}">
        <div class="editor-toolbar">
          <span class="toolbar-label">
            ${card.icon} ${card.label}
          </span>
          ${card.reloadable
            ? html`
                <button
                  class="toolbar-button"
                  @click=${this._reload}
                  ?disabled=${!this.rpcConnected}
                  title="Reload config from disk"
                >
                  ↻ Reload
                </button>
              `
            : ''}
          <button
            class="toolbar-button primary"
            @click=${this._save}
            ?disabled=${this._saving || !this.rpcConnected}
            title="Save (Ctrl+S)"
          >
            💾 Save
          </button>
          <button
            class="toolbar-button"
            @click=${this._closeEditor}
            title="Close editor"
          >
            ✕
          </button>
        </div>
        ${this._loading
          ? html`<div class="loading-note">Loading…</div>`
          : html`
              <textarea
                class="editor-textarea"
                .value=${this._editorContent}
                @keydown=${this._onEditorKeyDown}
                spellcheck="false"
              ></textarea>
            `}
      </div>
    `;
  }
}

customElements.define('aic-settings-tab', SettingsTab);