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
  mcpHealth,
  messageComposition,
  overLimit,
  partitionCategories,
  serverGroups,
  skillInventory,
  sourceLabel,
  thresholdPercent,
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

/**
 * The sections the spec gives this tab, and the one it does not have
 * yet.
 *
 * `viewers-hud.md` specifies three — Usage, Session, Debug — behind a
 * segmented control. Debug is a reader of `hookEvent` traffic,
 * `get_server_info` and the raw `gridRows`, none of which this panel
 * fetches, so it is absent rather than present and empty. A control with
 * a segment that opens onto nothing is the disclosure triangle problem
 * the categories table already refuses to have.
 *
 * Debug's fourth source, `get_mcp_status`, *is* fetched — but for the
 * Session section's per-server health rather than for a raw dump, since
 * "is this server answering" belongs next to what it costs.
 */
const _SECTIONS = [
  { id: 'usage', label: 'Usage' },
  { id: 'session', label: 'Session' },
];

/** localStorage key for the section the user was last reading. */
const _SECTION_KEY = 'ac-dc-context-section';

function _loadSection() {
  try {
    if (typeof localStorage === 'undefined') return 'usage';
    const saved = localStorage.getItem(_SECTION_KEY);
    return _SECTIONS.some((s) => s.id === saved) ? saved : 'usage';
  } catch {
    return 'usage';
  }
}

function _saveSection(id) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(_SECTION_KEY, id);
  } catch {
    // A private-mode quota error is not worth surfacing; the section
    // simply does not persist.
  }
}

/**
 * A tool group's tokens, split by whether the window is paying for them.
 *
 * Never summed, here or anywhere the Tools section reports a figure: a
 * deferred tool's schema is not loaded until first use, so one number
 * covering both would report a cost the session is not paying.
 */
function _splitTokens(group) {
  if (group.server) {
    return {
      loaded: group.server.loadedTokens,
      deferred: group.server.deferredTokens,
    };
  }
  let loaded = 0;
  let deferred = 0;
  for (const t of group.tools) {
    if (t.deferred) deferred += t.tokens;
    else loaded += t.tokens;
  }
  return { loaded, deferred };
}

/** The token clauses a tool heading carries, in one phrasing. */
function _tokenBits(loaded, deferred) {
  const bits = [];
  if (loaded > 0) bits.push(`${_fmtTokens(loaded)} loaded`);
  if (deferred > 0) bits.push(`${_fmtTokens(deferred)} deferred`);
  if (bits.length === 0) bits.push('no tokens');
  return bits;
}

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
    /** Which section is on screen: one of `_SECTIONS`'s ids. */
    _section: { type: String, state: true },
    /**
     * The SDK's `McpStatusResponse` from the last successful fetch, or
     * null. Separate from `_usage` because it comes from a second call
     * that is allowed to fail on its own.
     */
    _mcpStatus: { type: Object, state: true },
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

    .segmented {
      flex-shrink: 0;
      display: flex;
      gap: 0.25rem;
      padding: 0.5rem 1rem 0;
    }
    .seg {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      padding: 0.2rem 0.7rem;
      border-radius: 999px;
      cursor: pointer;
      font-size: 0.75rem;
      font-family: inherit;
      line-height: 1.4;
    }
    .seg:hover {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
    }
    .seg[aria-selected='true'] {
      background: rgba(88, 166, 255, 0.15);
      border-color: rgba(88, 166, 255, 0.5);
      color: var(--text-primary, #c9d1d9);
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
      position: relative;
      height: 12px;
      border-radius: 6px;
      background: rgba(240, 246, 252, 0.08);
      overflow: hidden;
      display: flex;
    }
    .bar.gauge {
      height: 18px;
    }
    .bar-seg {
      height: 100%;
    }
    /**
     * The autocompact mark. Inside the bar rather than a tick beneath
     * it, so the fill is read against it directly — the question is
     * "have the segments reached the mark", and an aligned tick two
     * pixels below makes that a comparison instead of a glance.
     */
    .mark {
      position: absolute;
      top: -1px;
      bottom: -1px;
      width: 2px;
      margin-left: -1px;
      background: var(--text-primary, #c9d1d9);
      box-shadow: 0 0 0 1px rgba(13, 17, 23, 0.8);
      pointer-events: none;
    }
    .mark-note {
      display: flex;
      justify-content: space-between;
      color: var(--text-secondary, #8b949e);
      font-size: 0.6875rem;
      font-variant-numeric: tabular-nums;
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
    /**
     * A table cell that opens something. A button rather than a styled
     * span so it is reachable by keyboard and announced as an action —
     * the row looks like text either way.
     */
    .link {
      background: none;
      border: none;
      padding: 0;
      margin: 0;
      font: inherit;
      color: var(--accent, #58a6ff);
      cursor: pointer;
      text-align: left;
      word-break: break-all;
    }
    .link:hover {
      text-decoration: underline;
    }

    .group {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
      padding-bottom: 0.3rem;
    }
    .group-head {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      width: 100%;
      background: none;
      border: none;
      padding: 0.3rem 0;
      color: var(--text-primary, #c9d1d9);
      font: inherit;
      cursor: pointer;
      text-align: left;
    }
    .group-head:hover {
      color: #fff;
    }
    .chev {
      color: var(--text-secondary, #8b949e);
      font-size: 0.7rem;
      width: 0.7rem;
      flex-shrink: 0;
    }
    .group-name {
      font-weight: 500;
    }
    .group-meta {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      font-variant-numeric: tabular-nums;
      flex-shrink: 0;
    }
    .pill {
      border: 1px solid currentColor;
      border-radius: 999px;
      padding: 0 0.4rem;
      font-size: 0.6875rem;
      line-height: 1.5;
      white-space: nowrap;
    }

    h3 {
      margin: 0;
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text-secondary, #8b949e);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    h3 .sub {
      text-transform: none;
      letter-spacing: 0;
      font-weight: 400;
      font-variant-numeric: tabular-nums;
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
    this._section = _loadSection();
    this._mcpStatus = null;
    /**
     * Which tool groups are expanded, by group key. A plain Set rather
     * than a reactive property: Lit compares by identity and mutating a
     * Set in place never trips that, so `_toggleGroup` asks for the
     * render itself. Collapsed is the default because the header carries
     * the counts — the summary is the thing most readers want, and 35
     * `ac-dc` rows expanded on arrival bury every other section.
     */
    this._openGroups = new Set();

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
      // Both calls go out together: they are separate control requests
      // to the same subprocess, and this one is slow enough (3-14s) that
      // sequencing them would put the health pill a full breakdown
      // behind the numbers it annotates.
      const [res, status] = await Promise.all([
        withRpcTimeout(
          this.rpcExtract('ClaudeCodeService.get_context_usage'),
          _FETCH_TIMEOUT_MS,
          'get_context_usage',
        ),
        this._fetchMcpStatus(),
      ]);
      // Written even when the breakdown failed, and written as null on
      // its own failure: a "connected" pill kept from an earlier fetch
      // is a claim about now, and this is the one field where being
      // out of date is worse than being absent.
      this._mcpStatus = status;
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

  /**
   * MCP connection health, fetched alongside the breakdown and allowed
   * to fail silently.
   *
   * The breakdown is what this tab is for; health is a decoration on it.
   * So this swallows its own errors rather than letting a status call
   * that timed out — or a backend too old to have the method — replace a
   * page of usable numbers with an error paragraph. The cost of failure
   * is that server groups carry no status pill.
   *
   * @returns {Promise<object|null>} An `McpStatusResponse`, or null.
   */
  async _fetchMcpStatus() {
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_mcp_status'),
        _FETCH_TIMEOUT_MS,
        'get_mcp_status',
      );
      if (res && !res.error && Array.isArray(res.mcpServers)) return res;
    } catch {
      // Deliberately quiet — see above.
    }
    return null;
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

  _switchSection(id) {
    if (this._section === id) return;
    this._section = id;
    _saveSection(id);
  }

  _toggleGroup(key) {
    if (this._openGroups.has(key)) this._openGroups.delete(key);
    else this._openGroups.add(key);
    this.requestUpdate();
  }

  /**
   * Open a memory file in the viewer.
   *
   * Minimizes this dialog as well as navigating, because the viewer is
   * behind it: a click that opens a file under an opaque panel is
   * indistinguishable from a click that did nothing.
   */
  _openMemoryFile(path) {
    if (!path) return;
    window.dispatchEvent(
      new CustomEvent('navigate-file', { detail: { path }, bubbles: false }),
    );
    this._minimizeDialog();
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
      ${this._usage ? this._renderSegmented() : ''}
      <div class="content">${this._renderBody()}</div>
    `;
  }

  /**
   * The section selector, in its own row rather than in the toolbar:
   * the toolbar's buttons act on the *breakdown* — go back, refetch,
   * minimize — and these choose which part of one breakdown to read.
   */
  _renderSegmented() {
    return html`
      <div class="segmented" role="tablist" aria-label="Context sections">
        ${_SECTIONS.map((s) => html`
          <button
            class="seg"
            role="tab"
            aria-selected=${this._section === s.id ? 'true' : 'false'}
            @click=${() => this._switchSection(s.id)}
          >${s.label}</button>
        `)}
      </div>
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
      ${this._section === 'session'
        ? this._renderSessionSection()
        : this._renderUsageSection()}
      ${this._renderFooter()}
    `;
  }

  /**
   * Usage — how full the window is, what is filling it, and how long
   * that leaves. Everything here is about the window's *capacity*;
   * what the session is made of is the Session section's question.
   */
  _renderUsageSection() {
    // Derived once and handed down: three of these sections read the
    // same `messageBreakdown`, and re-deriving it per section would put
    // three sorts and three filters in every render for one payload
    // that cannot change between them.
    const comp = messageComposition(this._usage);
    return html`
      ${this._renderHeadline()}
      ${this._renderCategories()}
      ${this._renderMessageBreakdown(comp)}
      ${this._renderToolTraffic(comp)}
      ${this._renderAttachments(comp)}
    `;
  }

  /**
   * Session — what this session is made of and what each part costs.
   *
   * Everything here is cost the session was *started* with, in the order
   * the spec lists it: the files the user wrote, the prompt the engine
   * prepended, the tools it can reach, and the inventory it can draw on.
   * None of it moves when a turn runs, which is what separates it from
   * Usage.
   */
  _renderSessionSection() {
    const health = mcpHealth(this._mcpStatus);
    const sections = [
      this._renderMemoryFiles(),
      this._renderSystemPrompt(),
      this._renderTools(health),
      this._renderAgents(),
      this._renderSkills(),
      this._renderSlashCommands(),
    ].filter((s) => s !== '');
    if (sections.length === 0) {
      return html`<p class="empty">
        The engine reported no memory files, prompt sections, tools or
        inventory for this session.
      </p>`;
    }
    return html`${sections}`;
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
   *
   * The mark on the bar is the third telling of the same fact, and the
   * only one that is spatial. Both numbers are answers to "how full";
   * the mark answers "how much further", which is the question a bar is
   * for.
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
    const markPct = thresholdPercent(u);
    const over = overLimit(u);
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
        <div class="bar gauge">
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
          ${markPct != null ? html`
            <div
              class="mark"
              style="left: ${markPct}%"
              title="Autocompact triggers here, at ${limit.toLocaleString()} tokens"
            ></div>
          ` : ''}
        </div>
        ${markPct != null ? html`
          <div class="mark-note">
            <span>Autocompact at ${markPct.toFixed(1)}% of the window</span>
            <span>${max.toLocaleString()}</span>
          </div>
        ` : ''}
        ${over ? html`
          <p class="error">
            ${over.kind === 'hard_limit'
              ? html`Past the model's ${over.window.toLocaleString()}-token
                  limit by ${over.over.toLocaleString()} tokens.`
              : html`${over.over.toLocaleString()} tokens past the
                  ${over.window.toLocaleString()}-token compaction
                  window.`}
            ${autoCompacts
              ? 'Autocompact should bring it back down on the next turn.'
              : 'Autocompact is off, so nothing will reduce it.'}
          </p>
        ` : ''}
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
            Autocompact is off for this session, so the bar carries no
            mark. Reaching the limit fails the turn rather than
            summarising the history.
          </p>
        ` : ''}
        ${u.model
          ? html`<p class="model-note">Measured for ${u.model}.</p>`
          : ''}
      </section>
    `;
  }

  /**
   * The category legend: every row the bar above is drawn from, plus
   * the rows it deliberately leaves out — the room left over and the
   * deferred budget — since those are the ones that explain why the bar
   * is shorter than the window.
   *
   * Zero-token rows are dropped, as the spec asks. A row whose count is
   * *unusable* is kept and shows an em dash: a category the engine named
   * but could not count is worth seeing, and a category that costs
   * nothing is not.
   */
  _renderCategories() {
    const cats = (Array.isArray(this._usage.categories)
      ? this._usage.categories
      : []
    )
      .filter((c) => {
        const n = Number(c?.tokens);
        return !Number.isFinite(n) || n > 0;
      })
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
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
   * What the conversation itself is made of.
   *
   * The one part of the window the user changes by *talking*, so it gets
   * the same treatment as the window: a proportional bar and a legend
   * under it. Everything else in this tab is fixed cost the session was
   * started with.
   *
   * Drawn against the parts' own sum rather than against the Messages
   * category, so the segments always tile the bar. The two agree to
   * within a token in the normal case; when they do not, the note says
   * which is which rather than leaving a bar that quietly does not add
   * up — see `messageComposition`.
   */
  _renderMessageBreakdown(comp) {
    if (!comp || comp.parts.length === 0) return '';
    const { parts, partsTokens, messagesTokens, reconciled } = comp;
    return html`
      <section>
        <h3>
          Messages
          <span class="sub">— ${_fmtTokens(partsTokens)} tokens</span>
        </h3>
        <div class="bar">
          ${parts.map((p) => html`
            <div
              class="bar-seg"
              style="width: ${(p.tokens / partsTokens) * 100}%;
                     background: ${p.color};"
              title="${p.label}: ${_fmtTokens(p.tokens)}"
            ></div>
          `)}
        </div>
        <table>
          <thead>
            <tr>
              <th>Part</th>
              <th class="num">Tokens</th>
              <th class="num">Share of messages</th>
            </tr>
          </thead>
          <tbody>
            ${parts.map((p) => html`
              <tr>
                <td>
                  <span
                    class="swatch"
                    style="background: ${p.color}"
                  ></span>${p.label}
                </td>
                <td class="num">${_fmtTokens(p.tokens)}</td>
                <td class="num">
                  ${((p.tokens / partsTokens) * 100).toFixed(1)}%
                </td>
              </tr>
            `)}
          </tbody>
        </table>
        ${!reconciled && messagesTokens ? html`
          <p class="note">
            These parts sum to ${partsTokens.toLocaleString()} tokens
            against the ${messagesTokens.toLocaleString()} in the
            Messages category above. The per-part figures are the
            engine's own estimate over the same history, so read the
            shares as proportions rather than as a second count.
          </p>
        ` : ''}
      </section>
    `;
  }

  /**
   * Which tools the conversation is paying for, and for what.
   *
   * This is the section that answers the spec's "names the `ac-dc` tools
   * it is paying for": the rows carry the engine's own tool names, so
   * the bridge's tools appear here under the names they were registered
   * with, alongside every built-in they compete with for the window.
   *
   * Calls and results are separate columns because they are separately
   * fixable. A heavy call column is a request passing too much in; a
   * heavy result column is a tool answering with more than was asked
   * for, and only one of those is the caller's to change.
   */
  _renderToolTraffic(comp) {
    if (!comp || comp.byTool.length === 0) return '';
    const { byTool } = comp;
    const total = byTool.reduce((sum, t) => sum + t.tokens, 0);
    return html`
      <section>
        <h3>
          Tool traffic
          <span class="sub">
            — ${byTool.length} ${byTool.length === 1 ? 'tool' : 'tools'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Tool</th>
              <th class="num">Calls</th>
              <th class="num">Results</th>
              <th class="num">Total</th>
            </tr>
          </thead>
          <tbody>
            ${byTool.map((t) => html`
              <tr>
                <td>${t.name}</td>
                <td class="num">${_fmtTokens(t.callTokens)}</td>
                <td class="num">${_fmtTokens(t.resultTokens)}</td>
                <td class="num">${_fmtTokens(t.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Attachments by kind — pasted text, images, file references.
   *
   * Its own section rather than a row in the message table because the
   * message table's Attachments row is the total, and the thing worth
   * knowing is which kind of attachment it was.
   */
  _renderAttachments(comp) {
    if (!comp || comp.byAttachment.length === 0) return '';
    const { byAttachment } = comp;
    const total = byAttachment.reduce((sum, a) => sum + a.tokens, 0);
    return html`
      <section>
        <h3>
          Attachments
          <span class="sub">— ${_fmtTokens(total)} tokens</span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${byAttachment.map((a) => html`
              <tr>
                <td>${a.name}</td>
                <td class="num">${_fmtTokens(a.tokens)}</td>
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
   * First in the section and the only clickable table in the tab, because
   * these are the one part of the fixed cost the user can *edit*. Knowing
   * `CLAUDE.md` costs 4.3K tokens is only half an answer; the other half
   * is being in the file.
   *
   * A file inside the repo is named the way every other view in this app
   * names files — relative to the root — with the engine's absolute path
   * on the row's tooltip. One outside it keeps the absolute path, because
   * that is the only name it has here.
   */
  _renderMemoryFiles() {
    const files = Array.isArray(this._usage.memoryFiles)
      ? this._usage.memoryFiles
      : [];
    if (files.length === 0) return '';
    const total = files.reduce((sum, f) => sum + (Number(f.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          Memory files
          <span class="sub">
            — ${files.length} ${files.length === 1 ? 'file' : 'files'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Type</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${files.map((f) => {
              const path = f.path || f.name || '';
              // `relPath` is the service's answer to "can the viewer
              // actually open this": present only for a file inside the
              // repo root, since every repo read rejects an absolute
              // path and `~/.claude/CLAUDE.md` is outside the repo
              // entirely. Rows without it stay text rather than
              // offering a click that would fail.
              const rel = typeof f.relPath === 'string' ? f.relPath : '';
              return html`
                <tr>
                  <td class="path" title=${path}>
                    ${rel
                      ? html`<button
                          class="link"
                          title="Open ${rel} in the viewer"
                          @click=${() => this._openMemoryFile(rel)}
                        >${rel}</button>`
                      : path || '—'}
                  </td>
                  <td>${f.type || '—'}</td>
                  <td class="num">${_fmtTokens(f.tokens)}</td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * What the engine prepended before the conversation started.
   *
   * Unlike memory files this is not editable, and that is the point of
   * showing it: it is the floor under every session, and a reader who
   * knows the floor is 12K tokens stops trying to explain it away as
   * something they did.
   */
  _renderSystemPrompt() {
    const rows = (Array.isArray(this._usage.systemPromptSections)
      ? this._usage.systemPromptSections
      : []
    )
      .filter((s) => s && typeof s === 'object')
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
    if (rows.length === 0) return '';
    const total = rows.reduce((sum, s) => sum + (Number(s.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          System prompt
          <span class="sub">
            — ${rows.length} ${rows.length === 1 ? 'section' : 'sections'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Section</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((s) => html`
              <tr>
                <td>${s.name || '—'}</td>
                <td class="num">${_fmtTokens(s.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Every tool the session can reach, grouped by where it comes from.
   *
   * One section with collapsible groups rather than three flat tables.
   * The flat `mcpTools` table this replaces listed 35 `ac-dc` rows next
   * to two from another server, which made "what does each server cost
   * me" — the question a per-server view exists to answer — a
   * subtraction the reader had to do, and buried the rest of the section
   * under it.
   *
   * Our own `ac-dc` server appears here like any other. That is a
   * deliberate spec invariant, not an accident of iteration: the bridge
   * competes for the same window as everything else and this is the one
   * place that keeps us honest about its price.
   */
  _renderTools(health) {
    const u = this._usage;
    const builtin = (Array.isArray(u.systemTools) ? u.systemTools : []).filter(
      (t) => t && typeof t === 'object',
    );
    const deferredBuiltin = (Array.isArray(u.deferredBuiltinTools)
      ? u.deferredBuiltinTools
      : []
    ).filter((t) => t && typeof t === 'object');
    const groups = [];
    if (builtin.length) {
      groups.push({
        key: 'builtin',
        name: 'Built-in tools',
        tools: builtin
          .map((t) => ({
            name: String(t.name ?? '—'),
            tokens: Number(t.tokens) || 0,
            deferred: t.isLoaded === false,
          }))
          .sort((a, b) => b.tokens - a.tokens),
      });
    }
    if (deferredBuiltin.length) {
      groups.push({
        key: 'builtin-deferred',
        name: 'Built-in tools, deferred',
        tools: deferredBuiltin
          .map((t) => ({
            name: String(t.name ?? '—'),
            tokens: Number(t.tokens) || 0,
            // The engine puts these in their own list *because* they are
            // deferred; `isLoaded` is present but redundant here.
            deferred: true,
          }))
          .sort((a, b) => b.tokens - a.tokens),
      });
    }
    for (const g of serverGroups(u, health)) {
      groups.push({ key: `mcp:${g.name}`, name: g.name, server: g, tools: g.tools });
    }
    if (groups.length === 0) return '';
    const count = groups.reduce((sum, g) => sum + g.tools.length, 0);
    const split = groups.reduce(
      (acc, g) => {
        const s = _splitTokens(g);
        return { loaded: acc.loaded + s.loaded, deferred: acc.deferred + s.deferred };
      },
      { loaded: 0, deferred: 0 },
    );
    return html`
      <section>
        <h3>
          Tools
          <span class="sub">
            — ${count} ${count === 1 ? 'tool' : 'tools'} in
            ${groups.length} ${groups.length === 1 ? 'group' : 'groups'} ·
            ${_tokenBits(split.loaded, split.deferred).join(' · ')}
          </span>
        </h3>
        ${groups.map((g) => this._renderToolGroup(g))}
      </section>
    `;
  }

  /**
   * One tool group: a header that answers the summary question on its
   * own, and a body only for the reader who asked for it.
   *
   * The header carries the counts and the health, so collapsing loses
   * nothing but the tool names — which is why collapsed is the default
   * even for the server whose 35 tools are the reason this grouping
   * exists.
   */
  _renderToolGroup(g) {
    const open = this._openGroups.has(g.key);
    const h = g.server?.health || null;
    const { loaded, deferred } = _splitTokens(g);
    const bits = [
      `${g.tools.length} ${g.tools.length === 1 ? 'tool' : 'tools'}`,
      ..._tokenBits(loaded, deferred),
    ];
    return html`
      <div class="group">
        <button
          class="group-head"
          aria-expanded=${open ? 'true' : 'false'}
          @click=${() => this._toggleGroup(g.key)}
        >
          <span class="chev">${open ? '▾' : '▸'}</span>
          <span class="group-name">${g.name}</span>
          ${h
            ? html`<span class="pill" style="color: ${h.color};
                     border-color: ${h.color}">${h.label}</span>`
            : ''}
          <span class="spacer"></span>
          <span class="group-meta">${bits.join(' · ')}</span>
        </button>
        ${open ? this._renderGroupBody(g, h) : ''}
      </div>
    `;
  }

  _renderGroupBody(g, h) {
    return html`
      ${g.server ? this._renderServerDetail(g.server, h) : ''}
      ${g.tools.length === 0
        ? html`<p class="empty">
            This server contributed no tools to the window.
          </p>`
        : html`
            <table>
              <thead>
                <tr>
                  <th>Tool</th>
                  <th class="num">Tokens</th>
                </tr>
              </thead>
              <tbody>
                ${g.tools.map((t) => html`
                  <tr class=${t.deferred ? 'deferred' : ''}>
                    <td>${t.name}</td>
                    <td class="num">${_fmtTokens(t.tokens)}</td>
                  </tr>
                `)}
              </tbody>
            </table>
          `}
    `;
  }

  /**
   * The connection facts behind a server's pill.
   *
   * From `get_mcp_status()`, not from the breakdown: `mcpTools` says what
   * a server costs and nothing about whether it is answering, and a
   * token cost for a server that failed to start is the most misleading
   * row this tab could draw. When that call did not land, the detail says
   * so rather than implying the server is fine.
   */
  _renderServerDetail(server, h) {
    if (!h) {
      return html`<p class="note">
        No connection status for this server — the engine's MCP status
        was not available, so these tokens are the whole picture.
      </p>`;
    }
    const bits = [];
    // `scope` here is the CLI's own word for where the server is
    // configured — project, user, local, managed — and not the
    // settings-key style `agents[].source` uses, so it is shown raw.
    if (h.scope) bits.push(h.scope);
    if (h.transport) bits.push(h.transport);
    if (h.version) bits.push(`v${h.version}`);
    if (h.toolCount != null) {
      bits.push(`${h.toolCount} advertised`);
    }
    return html`
      ${bits.length
        ? html`<p class="note">${bits.join(' · ')}</p>`
        : ''}
      ${h.error ? html`<p class="error">${h.error}</p>` : ''}
    `;
  }

  /**
   * The subagents this session can delegate to.
   *
   * `source` is a settings scope — which settings file defined the agent
   * — and not a path, so these rows cannot be click-to-open the way
   * memory files are. It is mapped to the CLI's own label rather than
   * shown raw, because "projectSettings" is a key and "Project" is the
   * answer.
   */
  _renderAgents() {
    const agents = (Array.isArray(this._usage.agents) ? this._usage.agents : [])
      .filter((a) => a && typeof a === 'object')
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
    if (agents.length === 0) return '';
    const total = agents.reduce((sum, a) => sum + (Number(a.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          Agent definitions
          <span class="sub">
            — ${agents.length} ${agents.length === 1 ? 'agent' : 'agents'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
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
                <td>${sourceLabel(a.source)}</td>
                <td class="num">${_fmtTokens(a.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Skills, and the gap between available and loaded.
   *
   * The heading names both counts because they answer different
   * questions: 3 of 40 loaded means 37 skills cost nothing right now,
   * and a reader who sees only "3 skills" will not go looking for the
   * other 37 when the total moves.
   */
  _renderSkills() {
    const inv = skillInventory(this._usage);
    if (!inv) return '';
    const { rows, tokens, total, included } = inv;
    return html`
      <section>
        <h3>
          Skills
          <span class="sub">
            —
            ${included != null && total != null
              ? `${included} of ${total} loaded, `
              : ''}${_fmtTokens(tokens)} tokens
          </span>
        </h3>
        ${rows.length === 0
          ? html`<p class="note">
              The engine reported a total but named no individual skills.
            </p>`
          : html`
              <table>
                <thead>
                  <tr>
                    <th>Skill</th>
                    <th>Source</th>
                    <th class="num">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map((s) => html`
                    <tr>
                      <td>${s.name}</td>
                      <td>
                        ${s.plugin
                          ? html`${s.source} <span class="note"
                              >(${s.plugin})</span
                            >`
                          : s.source}
                      </td>
                      <td class="num">${_fmtTokens(s.tokens)}</td>
                    </tr>
                  `)}
                </tbody>
              </table>
            `}
      </section>
    `;
  }

  /**
   * Slash commands, which the engine reports only as counts.
   *
   * No per-command rows exist in the payload, so this is one line rather
   * than a table with a single row in it.
   */
  _renderSlashCommands() {
    const sc = this._usage.slashCommands;
    if (!sc || typeof sc !== 'object' || Array.isArray(sc)) return '';
    const total = Number(sc.totalCommands);
    const included = Number(sc.includedCommands);
    const tokens = Number(sc.tokens) || 0;
    if (!Number.isFinite(total) && !Number.isFinite(included) && !tokens) {
      return '';
    }
    return html`
      <section>
        <h3>Slash commands</h3>
        <p class="note">
          ${Number.isFinite(included) && Number.isFinite(total)
            ? `${included} of ${total} in the window`
            : Number.isFinite(total)
              ? `${total} available`
              : 'In the window'}, costing
          ${_fmtTokens(tokens)} tokens.
        </p>
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
