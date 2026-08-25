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
  }

  onRpcReady() {
    this._loadInfo();
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

  _renderEditor() {
    const card = CONFIG_CARDS.find((c) => c.key === this._activeKey);
    if (!card) return '';
    return html`
      <div class="editor-area">
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