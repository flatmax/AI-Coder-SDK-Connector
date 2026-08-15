// ContextUsageTab — what is in the engine's context window right now.
//
// Replaces `context-tab.js`, which had two sub-views (Budget and Cache)
// built entirely on AC⚡DC's own prompt assembly: category allocations it
// chose, L0-L3 cache tiers it maintained, a stability tracker that
// decided when a tier could graduate, and a warmer that pre-heated them.
// None of that survives the conversion, and none of it had an analogue
// to port — the CLI assembles its own prompt and manages its own cache.
//
// So this tab answers the one question the old tab answered that still
// has an answer: what is filling the context window, and how much room
// is left. The data is `ClaudeCodeService.get_context_usage`, a
// pass-through of the same breakdown the CLI's `/context` command
// prints, so this view and that command cannot disagree.
//
// Governing spec: specs5/5-webapp/viewers-hud.md § Context Usage (CC-17).
//
// What it deliberately does NOT do:
//   - No refresh loop. The breakdown only changes when a turn runs or a
//     session loads, so it refreshes on exactly those events plus an
//     explicit button. A poll would spend control requests to watch a
//     number that cannot move on its own.
//   - No "rebuild cache" affordance. The cache is the CLI's, and it has
//     no request to rebuild it. A button that quietly did nothing would
//     be worse than no button.

import { LitElement, css, html } from 'lit';
import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';
import {
  bandColor as _pctColor,
  categoryColor,
  compactionLimit,
  compactionPercent,
  partitionCategories,
  warningPercent,
  windowPercent,
} from './context-usage.js';

/**
 * Deadline for a breakdown fetch. Without one, a reply dropped by a
 * reconnecting socket leaves `_loading` set — which both blocks every
 * later refresh and disables the Refresh button that would retry.
 *
 * Deliberately *above* the SDK's own 60s control-request deadline, not
 * below it. `ClaudeCodeService.get_context_usage` catches that timeout
 * and answers `{error}`, so the backend always replies; a shorter
 * deadline here would pre-empt a reply that is on its way and stack a
 * retry onto a subprocess already struggling to answer the first. This
 * call is slow — measured live at 3-5s warm, 14s on the first fetch
 * after an idle session, and past 60s often enough to log eight
 * `Control request timeout: get_context_usage` failures in one
 * half-hour run. So the only case left for this deadline is the one it
 * was written for: no reply is coming at all.
 */
const _FETCH_TIMEOUT_MS = 90000;

function _fmtTokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (v < 1000) return String(Math.round(v));
  if (v < 1_000_000) return `${(v / 1000).toFixed(1)}K`;
  return `${(v / 1_000_000).toFixed(2)}M`;
}

export class ContextUsageTab extends RpcMixin(LitElement) {
  static properties = {
    /**
     * The SDK's `ContextUsageResponse`, or null before the first
     * successful fetch.
     */
    _usage: { type: Object, state: true },
    /** When the engine's answer was taken, as an ISO string. */
    _fetchedAt: { type: String, state: true },
    /** Error text from the last failed fetch, or ''. */
    _error: { type: String, state: true },
    /** True while a fetch is outstanding. */
    _loading: { type: Boolean, state: true },
    /**
     * True when a turn completed while this tab was hidden, so the
     * numbers on screen predate it. Shown as a badge rather than
     * silently refreshed, because the user may be mid-read.
     */
    _stale: { type: Boolean, state: true },
  };

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 0;
      background: var(--bg-primary, #0d1117);
      color: var(--text-primary, #c9d1d9);
      font-size: 0.875rem;
      overflow-y: auto;
    }

    .toolbar {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
      background: rgba(22, 27, 34, 0.4);
    }
    .back-btn,
    .tool-btn {
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
    .back-btn:hover,
    .tool-btn:hover:not(:disabled) {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
      border-color: rgba(240, 246, 252, 0.3);
    }
    .tool-btn:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .spacer {
      flex: 1 1 auto;
    }
    .stale-badge {
      color: #d29922;
      font-size: 0.75rem;
    }

    .content {
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }

    .headline {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
    }
    .headline .pct {
      font-size: 1.5rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }
    .headline .of {
      color: var(--text-secondary, #8b949e);
      font-variant-numeric: tabular-nums;
    }
    .model-note {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
    }

    .bar {
      height: 12px;
      border-radius: 6px;
      background: rgba(240, 246, 252, 0.08);
      overflow: hidden;
      display: flex;
    }
    .bar-seg {
      height: 100%;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }
    th {
      text-align: left;
      font-weight: 500;
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      padding: 0.25rem 0.5rem 0.25rem 0;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
    }
    th.num,
    td.num {
      text-align: right;
      padding-right: 0;
    }
    td {
      padding: 0.25rem 0.5rem 0.25rem 0;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
    }
    tr.deferred td {
      opacity: 0.6;
      font-style: italic;
    }
    .swatch {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 2px;
      margin-right: 0.4rem;
      vertical-align: middle;
    }
    .path {
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 0.75rem;
      word-break: break-all;
    }

    h3 {
      margin: 0;
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text-secondary, #8b949e);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
    }
    .empty,
    .note {
      color: var(--text-secondary, #8b949e);
      font-size: 0.8125rem;
    }
    .error {
      color: #f85149;
    }
    .warn {
      color: #d29922;
    }
  `;

  constructor() {
    super();
    this._usage = null;
    this._fetchedAt = '';
    this._error = '';
    this._loading = false;
    this._stale = false;

    this._onStreamComplete = this._onStreamComplete.bind(this);
    this._onSessionChanged = this._onSessionChanged.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('stream-complete', this._onStreamComplete);
    window.addEventListener('session-changed', this._onSessionChanged);
  }

  disconnectedCallback() {
    window.removeEventListener('stream-complete', this._onStreamComplete);
    window.removeEventListener('session-changed', this._onSessionChanged);
    super.disconnectedCallback();
  }

  async onRpcReady() {
    await this._refresh();
  }

  /** Called by the dialog when this tab becomes visible. */
  onTabVisible() {
    if (this._stale || !this._usage) {
      this._stale = false;
      this._refresh();
    }
  }

  // ---------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------

  _isTabActive() {
    const panel = this.parentElement;
    if (panel?.classList?.contains('tab-panel')) {
      return panel.classList.contains('active');
    }
    return this.offsetParent !== null;
  }

  _onStreamComplete() {
    // Only fetch when the user can see the result. Unlike the old tab,
    // which fetched eagerly on every completion, this costs a control
    // request to the CLI subprocess rather than a local computation —
    // so a hidden tab marks itself stale and refreshes on the way in.
    if (this._isTabActive()) this._refresh();
    else this._stale = true;
  }

  _onSessionChanged() {
    if (this._isTabActive()) this._refresh();
    else this._stale = true;
  }

  // ---------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------

  async _refresh() {
    if (this._loading) return;
    if (!this.rpcConnected) return;
    this._loading = true;
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_context_usage'),
        _FETCH_TIMEOUT_MS,
        'get_context_usage',
      );
      if (res && res.error) {
        this._error = String(res.error);
        return;
      }
      const usage = res && res.usage ? res.usage : null;
      if (!usage) {
        this._error = 'The engine returned no context usage.';
        return;
      }
      this._usage = usage;
      this._fetchedAt = res.fetched_at || '';
      this._error = '';
      this._stale = false;
    } catch (err) {
      this._error = err?.message || 'Could not read context usage.';
    } finally {
      this._loading = false;
    }
  }

  _goBackToChat() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-tab', {
        detail: { tab: 'files' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  _minimizeDialog() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------

  render() {
    return html`
      <div class="toolbar">
        <button
          class="back-btn"
          title="Back to chat"
          @click=${this._goBackToChat}
        >← Chat</button>
        <button
          class="tool-btn"
          ?disabled=${this._loading || !this.rpcConnected}
          title="Ask the engine for a fresh breakdown"
          @click=${this._refresh}
        >${this._loading ? 'Reading…' : '↻ Refresh'}</button>
        ${this._stale
          ? html`<span class="stale-badge">● stale</span>`
          : ''}
        <span class="spacer"></span>
        <button
          class="tool-btn"
          title="Minimize"
          @click=${this._minimizeDialog}
        >▾</button>
      </div>
      <div class="content">${this._renderBody()}</div>
    `;
  }

  _renderBody() {
    if (this._error && !this._usage) {
      return html`
        <p class="error">${this._error}</p>
        <p class="note">
          The breakdown comes from the running engine, so it is
          unavailable until a session is connected.
        </p>
      `;
    }
    if (!this._usage) {
      return html`<p class="empty">
        ${this._loading ? 'Reading context…' : 'No breakdown yet.'}
      </p>`;
    }
    return html`
      ${this._renderHeadline()}
      ${this._renderCategories()}
      ${this._renderMemoryFiles()}
      ${this._renderMcpTools()}
      ${this._renderAgents()}
      ${this._renderFooter()}
    `;
  }

  /**
   * The headline reports two percentages against two denominators,
   * both labelled, because there is no single honest one.
   *
   * The big number is the engine's own, against the raw window, so
   * this view and `/context` cannot disagree. On its own it is
   * reassuring to the point of useless — it reads 78% when a compact
   * is one turn away — so the line beneath it measures the same tokens
   * against `autoCompactThreshold`, which is where the session
   * actually gives out.
   *
   * The big number is deliberately *coloured* by that second figure
   * rather than by itself: the warning has to land on the thing being
   * looked at. A green 70% above an amber "83.8% of the way to an
   * autocompact" is a mixed signal, and the green wins. So the digits
   * stay in parity with `/context` while the colour tracks the event
   * the user cares about, and the note spells out which is which.
   */
  _renderHeadline() {
    const u = this._usage;
    const total = Number(u.totalTokens) || 0;
    const max = Number(u.maxTokens) || 0;
    const clamped = windowPercent(u);
    const warnPct = warningPercent(u);
    const limit = compactionLimit(u);
    const toLimit = compactionPercent(u);
    const autoCompacts = u.isAutoCompactEnabled !== false;
    // Segment the fill by what is actually in the window. The engine's
    // `categories` also contains "Free space" and "Autocompact
    // buffer", which together make up the rest of the window — segment
    // by all of them and the bar is permanently 100% full.
    const { content, verified } = partitionCategories(u);
    const segmented = verified && content.length > 0 && max > 0;

    return html`
      <section>
        <div class="headline">
          <span class="pct" style="color: ${_pctColor(warnPct)}">
            ${clamped.toFixed(1)}%
          </span>
          <span class="of">
            ${total.toLocaleString()} / ${max.toLocaleString()} tokens
          </span>
        </div>
        <div class="bar">
          ${segmented
            ? content.map((c) => html`
                <div
                  class="bar-seg"
                  style="width: ${(Number(c.tokens) / max) * 100}%;
                         background: ${categoryColor(c.color)};"
                  title="${c.name}: ${_fmtTokens(c.tokens)}"
                ></div>
              `)
            : html`<div
                class="bar-seg"
                style="width: ${clamped}%; background: ${_pctColor(clamped)};"
              ></div>`}
        </div>
        ${toLimit != null && autoCompacts && limit < max ? html`
          <p class="note" style="color: ${_pctColor(toLimit)}">
            ${toLimit.toFixed(1)}% of the way to an autocompact, which
            triggers at ${limit.toLocaleString()} tokens —
            ${Math.max(0, limit - total).toLocaleString()} tokens of
            room left. The remaining
            ${(max - limit).toLocaleString()} are reserved for the
            summary.
          </p>
        ` : ''}
        ${!autoCompacts ? html`
          <p class="warn">
            Autocompact is off for this session. Reaching the limit
            fails the turn rather than summarising the history.
          </p>
        ` : ''}
        ${u.model
          ? html`<p class="model-note">Measured for ${u.model}.</p>`
          : ''}
      </section>
    `;
  }

  _renderCategories() {
    const cats = Array.isArray(this._usage.categories)
      ? [...this._usage.categories].sort(
          (a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0),
        )
      : [];
    if (cats.length === 0) {
      return html`<section>
        <h3>Categories</h3>
        <p class="empty">The engine reported no categories.</p>
      </section>`;
    }
    // Share is against the window, not against `totalTokens`. The
    // engine's rows include the room left over, so dividing by the
    // tokens in use rendered "Free space — 692.0%".
    const max = Number(this._usage.maxTokens) || 0;
    return html`
      <section>
        <h3>Categories</h3>
        <table>
          <thead>
            <tr>
              <th>Category</th>
              <th class="num">Tokens</th>
              <th class="num">Share of window</th>
            </tr>
          </thead>
          <tbody>
            ${cats.map((c) => html`
              <tr class=${c.isDeferred ? 'deferred' : ''}>
                <td>
                  <span
                    class="swatch"
                    style="background: ${categoryColor(c.color)}"
                  ></span>${c.name}${c.isDeferred
                    && !/\(deferred\)/i.test(String(c.name ?? ''))
                    // The engine names some rows "System tools
                    // (deferred)" and also flags them, which rendered
                    // as "System tools (deferred) (deferred)".
                    ? html` <span
                        class="note"
                        title="Budgeted by the engine but not loaded into the window yet"
                      >(deferred)</span>`
                    : ''}
                </td>
                <td class="num">${_fmtTokens(c.tokens)}</td>
                <td class="num">
                  ${max > 0
                    ? `${((Number(c.tokens) / max) * 100).toFixed(1)}%`
                    : '—'}
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * CLAUDE.md and other memory files the CLI loaded.
   *
   * Worth its own section: these are files the user controls and can
   * shrink, which makes this the most actionable part of the
   * breakdown. The `path` / `type` / token keys are read defensively
   * because the SDK types them as plain dicts.
   */
  _renderMemoryFiles() {
    const files = Array.isArray(this._usage.memoryFiles)
      ? this._usage.memoryFiles
      : [];
    if (files.length === 0) return '';
    return html`
      <section>
        <h3>Memory files</h3>
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Type</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${files.map((f) => html`
              <tr>
                <td class="path">${f.path || f.name || '—'}</td>
                <td>${f.type || '—'}</td>
                <td class="num">${_fmtTokens(f.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Per-tool MCP cost, including AC⚡DC's own bridge once phase 4
   * lands. `isLoaded` false means the tool's schema is not in the
   * window yet, so its tokens are prospective.
   */
  _renderMcpTools() {
    const tools = Array.isArray(this._usage.mcpTools)
      ? this._usage.mcpTools
      : [];
    if (tools.length === 0) return '';
    const loaded = tools.filter((t) => t.isLoaded !== false);
    const total = loaded.reduce((sum, t) => sum + (Number(t.tokens) || 0), 0);
    // Deferred tools are the normal case, not the exception — the
    // engine loads a tool's schema on first use. The old heading said
    // "MCP tools — 0 loaded", which reads as "no tools" when it meant
    // "no tokens", directly above a table of 35 of them. Both figures
    // are named, with units.
    const deferredTokens = tools
      .filter((t) => t.isLoaded === false)
      .reduce((sum, t) => sum + (Number(t.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          MCP tools — ${tools.length}
          ${tools.length === 1 ? 'tool' : 'tools'},
          ${_fmtTokens(total)} tokens loaded${deferredTokens > 0
            ? html`, ${_fmtTokens(deferredTokens)} deferred`
            : ''}
        </h3>
        <table>
          <thead>
            <tr>
              <th>Tool</th>
              <th>Server</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${tools.map((t) => html`
              <tr class=${t.isLoaded === false ? 'deferred' : ''}>
                <td>${t.name || '—'}</td>
                <td>${t.serverName || '—'}</td>
                <td class="num">${_fmtTokens(t.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  _renderAgents() {
    const agents = Array.isArray(this._usage.agents)
      ? this._usage.agents
      : [];
    if (agents.length === 0) return '';
    return html`
      <section>
        <h3>Agent definitions</h3>
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Source</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${agents.map((a) => html`
              <tr>
                <td>${a.agentType || a.name || '—'}</td>
                <td>${a.source || '—'}</td>
                <td class="num">${_fmtTokens(a.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  _renderFooter() {
    if (this._error) {
      // A failed refresh with usable prior numbers: say so rather than
      // leaving stale figures looking current.
      return html`<p class="error">
        Last refresh failed: ${this._error}
      </p>`;
    }
    if (!this._fetchedAt) return '';
    return html`<p class="note">
      Read from the engine at ${this._formatFetchedAt()}.
    </p>`;
  }

  _formatFetchedAt() {
    const d = new Date(this._fetchedAt);
    if (Number.isNaN(d.getTime())) return this._fetchedAt;
    return d.toLocaleTimeString();
  }
}

customElements.define('ac-context-usage-tab', ContextUsageTab);
