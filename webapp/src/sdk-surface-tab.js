// SdkSurfaceTab — which SDK features this build wired up, and which it did not.
//
// The other tabs answer questions about the repo or the session. This one
// answers a question about AC⚡DC itself: the `claude-agent-sdk` wheel and
// the `claude` CLI both ship on their own cadence, adding options, hook
// events, message types and beta gates, and none of that arrives as a
// build break. It arrives as a feature we silently do not offer. So the
// backend reflects over the installed wheel and diffs it against what
// `ac_dc.claude_code` actually reaches for, and this renders the diff.
//
// Data is `ClaudeCodeService.get_sdk_surface` — see
// `src/ac_dc/claude_code/sdk_surface.py` for how coverage is derived (from
// the package's own syntax trees, not from a hand-kept list).
//
// Three statuses, and the third is the whole point:
//   handled  — we set it, register it, or dispatch on it
//   declined — we deliberately do not, and the note says why
//   pending  — real surface with no decision recorded yet
//
// **Pending is the default view.** Handled and declined together are ~60
// rows of "nothing to do here", and a tab that opens on them buries the
// dozen rows a reader came for. The filter is one click away and the
// counts are always visible, so nothing is hidden — it is ordered.
//
// What this tab deliberately does NOT do:
//   - No polling. The static half of the report cannot change while the
//     process is running: it is read from the installed wheel and from
//     source files on disk. A refresh loop would re-derive a constant.
//   - No "enable this option" controls. Every pending row is a code
//     change with a design decision behind it (see the notes), not a
//     switch. A toggle here would imply the decision was already made.
//   - No duplicate of the pytest gate's verdict. The gate in
//     `tests/test_claude_code_sdk_surface.py` is what fails CI on
//     untriaged surface; this shows the same data to a human who wants to
//     look before CI does.

import { LitElement, css, html } from 'lit';
import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';

/**
 * Deadline for the report fetch.
 *
 * Far below the Context tab's 90s because this call is cheap and mostly
 * local: reflection over an already-imported module plus three AST parses.
 * The one part that can be slow is the live-CLI half, and the backend
 * degrades that on its own rather than letting it hold the reply — so a
 * fetch that has not returned in 15s is a dropped reply, not a slow one.
 */
const _FETCH_TIMEOUT_MS = 15000;

/** The report's sections, in the order they are worth reading. */
const _SECTIONS = [
  {
    id: 'options',
    label: 'Options',
    blurb: 'Fields on ClaudeAgentOptions. "Handled" means options.py assigns it.',
  },
  {
    id: 'hooks',
    label: 'Hooks',
    blurb: 'Hook events the SDK accepts. "Handled" means hooks.py registers a matcher.',
  },
  {
    id: 'betas',
    label: 'Betas',
    blurb: 'Opt-in feature gates. The likeliest place a new capability appears.',
  },
  {
    id: 'messages',
    label: 'Messages',
    blurb: "Members of the SDK's Message union, versus the pump's dispatch chain.",
  },
  {
    id: 'client',
    label: 'Client',
    blurb: 'Methods on ClaudeSDKClient, versus the ones this package calls.',
  },
];

const _STATUSES = ['pending', 'handled', 'declined'];

const _STATUS_COLOR = {
  handled: '#7ee787',
  declined: '#8b949e',
  pending: '#d29922',
};

/** localStorage key for the filter the reader last used. */
const _FILTER_KEY = 'ac-dc-sdk-surface-filter';

function _loadFilter() {
  try {
    if (typeof localStorage === 'undefined') return 'pending';
    const saved = localStorage.getItem(_FILTER_KEY);
    return saved === 'all' || _STATUSES.includes(saved) ? saved : 'pending';
  } catch {
    return 'pending';
  }
}

function _saveFilter(value) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(_FILTER_KEY, value);
  } catch {
    // Private-mode quota errors are not worth surfacing; the filter
    // simply does not persist.
  }
}

/**
 * Total per status across every section.
 *
 * Computed here rather than read from the report's own `counts` so the
 * header cannot disagree with the rows below it: both come from
 * `entries`.
 */
export function totalCounts(report) {
  const totals = { handled: 0, declined: 0, pending: 0 };
  const sections = report?.sections;
  if (!sections) return totals;
  for (const section of Object.values(sections)) {
    for (const entry of section?.entries || []) {
      if (entry?.status in totals) totals[entry.status] += 1;
    }
  }
  return totals;
}

/**
 * Every name the backend could not classify, flattened for the banner.
 *
 * These are the rows that fail the pytest gate, and the only ones that
 * represent work nobody has looked at — a `pending` row with a note is a
 * decision to defer, which is different.
 */
export function untriagedNames(report) {
  const out = [];
  for (const [section, names] of Object.entries(report?.unclassified || {})) {
    for (const name of names || []) out.push(`${section}: ${name}`);
  }
  return out;
}

export class SdkSurfaceTab extends RpcMixin(LitElement) {
  static properties = {
    /** The `get_sdk_surface` report, or null before the first fetch. */
    _report: { type: Object, state: true },
    /** Error text from the last failed fetch, or ''. */
    _error: { type: String, state: true },
    /** True while a fetch is outstanding. */
    _loading: { type: Boolean, state: true },
    /** 'all' or one of `_STATUSES`. */
    _filter: { type: String, state: true },
    /** Section ids the reader has collapsed. */
    _collapsed: { type: Object, state: true },
  };

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 0;
      background: var(--bg-primary, #0d1117);
      color: var(--text-primary, #c9d1d9);
      font-size: 13px;
    }

    /*
     * The toolbar is copied from context-usage-tab.js rather than styled
     * afresh: it carries the same three controls in the same order (back,
     * refresh, minimize), and a diagnostic tab that put its Back button
     * somewhere else would be the reason someone reaches for Alt+1.
     */
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

    header {
      display: flex;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border, #30363d);
    }

    h2 {
      font-size: 13px;
      font-weight: 600;
      margin: 0;
    }

    .versions {
      color: var(--text-secondary, #8b949e);
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 11px;
    }

    .spacer {
      flex: 1;
    }

    button {
      background: transparent;
      color: var(--text-secondary, #8b949e);
      border: 1px solid var(--border, #30363d);
      border-radius: 4px;
      padding: 3px 8px;
      font: inherit;
      font-size: 11px;
      cursor: pointer;
    }

    button:hover:not(:disabled) {
      color: var(--text-primary, #c9d1d9);
      border-color: var(--accent, #58a6ff);
    }

    button:disabled {
      opacity: 0.5;
      cursor: default;
    }

    button.on {
      color: var(--bg-primary, #0d1117);
      background: var(--accent, #58a6ff);
      border-color: var(--accent, #58a6ff);
    }

    .filters {
      display: flex;
      gap: 4px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--border, #30363d);
      flex-wrap: wrap;
      align-items: center;
    }

    .banner {
      margin: 10px 12px 0;
      padding: 8px 10px;
      border-radius: 4px;
      border: 1px solid #d29922;
      background: rgba(210, 153, 34, 0.12);
      line-height: 1.5;
    }

    .banner code {
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 11px;
    }

    .body {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding: 10px 12px 16px;
    }

    section {
      margin-bottom: 18px;
    }

    .section-head {
      display: flex;
      align-items: baseline;
      gap: 8px;
      cursor: pointer;
      user-select: none;
    }

    .section-head h3 {
      font-size: 12px;
      font-weight: 600;
      margin: 0;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .blurb {
      color: var(--text-secondary, #8b949e);
      font-size: 11px;
      margin: 2px 0 8px;
      line-height: 1.5;
    }

    .pill {
      font-size: 10px;
      font-family: var(--font-mono, ui-monospace, monospace);
      padding: 1px 5px;
      border-radius: 8px;
      border: 1px solid currentColor;
      white-space: nowrap;
    }

    ul {
      list-style: none;
      margin: 0;
      padding: 0;
    }

    li {
      display: grid;
      grid-template-columns: minmax(140px, max-content) max-content 1fr;
      gap: 8px;
      align-items: baseline;
      padding: 5px 0;
      border-top: 1px solid var(--border, #21262d);
    }

    .name {
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 11px;
      word-break: break-all;
    }

    .note {
      color: var(--text-secondary, #8b949e);
      font-size: 11px;
      line-height: 1.5;
    }

    .empty,
    .status {
      color: var(--text-secondary, #8b949e);
      font-size: 11px;
      padding: 4px 0;
    }

    .error {
      color: #f85149;
      padding: 10px 12px;
    }

    .cli-lists {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }

    .cli-lists div {
      min-width: 140px;
    }

    .cli-lists h4 {
      font-size: 11px;
      margin: 0 0 4px;
      color: var(--text-secondary, #8b949e);
      font-weight: 600;
    }

    .cli-lists span {
      display: block;
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 11px;
    }
  `;

  constructor() {
    super();
    this._report = null;
    this._error = '';
    this._loading = false;
    this._filter = _loadFilter();
    this._collapsed = {};
  }

  async onRpcReady() {
    await this._refresh();
  }

  /**
   * Called by the dialog when this tab becomes visible.
   *
   * Refetches only when there is nothing yet, or when the last report had
   * no live CLI to probe. The static half is a constant, so re-reading it
   * on every visit would spend a round trip to learn nothing; the live
   * half genuinely changes once the engine comes up, and opening this tab
   * before connecting is the ordinary case.
   */
  onTabVisible() {
    if (!this._report || this._report?.cli?.available === false) this._refresh();
  }

  async _refresh() {
    if (this._loading) return;
    this._loading = true;
    this._error = '';
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_sdk_surface'),
        _FETCH_TIMEOUT_MS,
        'get_sdk_surface',
      );
      // The backend does not answer `{error}` for this call — the static
      // half works with the engine down, which is documented on
      // `get_sdk_surface`. Handled anyway: this reads a JSON-RPC reply,
      // and a transport that decides otherwise should not render blank.
      if (res && res.error) {
        this._error = String(res.error);
        return;
      }
      if (!res || !res.sections) {
        this._error = 'The engine returned no SDK surface report.';
        return;
      }
      this._report = res;
    } catch (err) {
      this._error = err?.message ? String(err.message) : String(err);
    } finally {
      this._loading = false;
    }
  }

  /**
   * Back to the chat tab.
   *
   * The dialog has no rendered tab strip, so leaving a tab means either
   * this control or knowing Alt+1. Every other tab in the dialog offers
   * the button; this one shipped without it and the omission read exactly
   * as it was — a dead end.
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

  _minimizeDialog() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  _setFilter(value) {
    this._filter = value;
    _saveFilter(value);
  }

  /**
   * The toolbar, rendered in every state.
   *
   * Deliberately outside the error and empty branches' own markup: the
   * first version of this panel put Refresh in the header and the header
   * only in the loaded branch, so a failed fetch left no way out but a
   * keyboard shortcut. Back must survive the states that make someone
   * want it.
   */
  _renderToolbar() {
    return html`
      <div class="toolbar">
        <button class="back-btn" title="Back to chat" @click=${this._goBackToChat}>
          ← Chat
        </button>
        <button
          class="tool-btn"
          ?disabled=${this._loading || !this.rpcConnected}
          title="Re-read the installed SDK"
          @click=${this._refresh}
        >
          ${this._loading ? 'Reading…' : '↻ Refresh'}
        </button>
        <span class="spacer"></span>
        <button class="tool-btn" title="Minimize" @click=${this._minimizeDialog}>▾</button>
      </div>
    `;
  }

  _toggleSection(id) {
    this._collapsed = { ...this._collapsed, [id]: !this._collapsed[id] };
  }

  _visibleEntries(section) {
    const entries = section?.entries || [];
    if (this._filter === 'all') return entries;
    return entries.filter((e) => e.status === this._filter);
  }

  render() {
    if (this._error) {
      return html`
        ${this._renderToolbar()}
        <header><h2>SDK Surface</h2></header>
        <div class="error">${this._error}</div>
      `;
    }
    if (!this._report) {
      return html`
        ${this._renderToolbar()}
        <header><h2>SDK Surface</h2></header>
        <div class="status">${this._loading ? 'Reading the installed SDK…' : 'No report yet.'}</div>
      `;
    }

    const report = this._report;
    const totals = totalCounts(report);
    const untriaged = untriagedNames(report);
    const v = report.versions || {};

    return html`
      ${this._renderToolbar()}
      <header>
        <h2>SDK Surface</h2>
        <span class="versions">
          sdk ${v.sdk_version || '?'} · cli pin ${v.sdk_cli_pin || '?'} · floor
          ${v.minimum_cli_version || '?'}
        </span>
        <span class="spacer"></span>
        ${_STATUSES.map(
          (s) => html`
            <span class="pill" style="color:${_STATUS_COLOR[s]}">${totals[s]} ${s}</span>
          `,
        )}
      </header>

      ${untriaged.length
        ? html`
            <div class="banner">
              <strong>${untriaged.length} surface
              ${untriaged.length === 1 ? 'name has' : 'names have'} no decision
              recorded.</strong>
              This is what the SDK added since anyone last looked, and it fails
              <code>tests/test_claude_code_sdk_surface.py</code>. Implement it,
              refuse it in <code>NEVER_SET</code>/<code>HOOK_EVENTS</code> with the
              reason, or defer it in <code>PENDING_OPTIONS</code>/<code>KNOWN_BETAS</code>
              with what it would buy.
              <div class="note">${untriaged.join(' · ')}</div>
            </div>
          `
        : null}

      <div class="filters">
        <button class=${this._filter === 'all' ? 'on' : ''} @click=${() => this._setFilter('all')}>
          All
        </button>
        ${_STATUSES.map(
          (s) => html`
            <button
              class=${this._filter === s ? 'on' : ''}
              @click=${() => this._setFilter(s)}
            >
              ${s}
            </button>
          `,
        )}
      </div>

      <div class="body">
        ${_SECTIONS.map((meta) => this._renderSection(meta, report.sections[meta.id]))}
        ${this._renderCli(report.cli)}
      </div>
    `;
  }

  _renderSection(meta, section) {
    if (!section) return null;
    const entries = this._visibleEntries(section);
    const collapsed = !!this._collapsed[meta.id];
    const counts = _STATUSES.map((s) => ({
      status: s,
      n: (section.entries || []).filter((e) => e.status === s).length,
    })).filter((c) => c.n > 0);

    return html`
      <section>
        <div class="section-head" @click=${() => this._toggleSection(meta.id)}>
          <h3>${collapsed ? '▸' : '▾'} ${meta.label}</h3>
          ${counts.map(
            (c) => html`
              <span class="pill" style="color:${_STATUS_COLOR[c.status]}">
                ${c.n} ${c.status}
              </span>
            `,
          )}
        </div>
        ${collapsed
          ? null
          : html`
              <p class="blurb">${meta.blurb}</p>
              ${section.stale?.length
                ? html`
                    <p class="note">
                      Stale — classified here but absent from the installed SDK:
                      ${section.stale.join(', ')}. Delete the entries.
                    </p>
                  `
                : null}
              ${section.resolved?.length
                ? html`
                    <p class="note">
                      Resolved — now set, but still carrying a note arguing
                      against setting it: ${section.resolved.join(', ')}. Delete
                      the entries.
                    </p>
                  `
                : null}
              ${entries.length
                ? html`
                    <ul>
                      ${entries.map(
                        (e) => html`
                          <li>
                            <span class="name">${e.name}</span>
                            <span class="pill" style="color:${_STATUS_COLOR[e.status]}">
                              ${e.status}
                            </span>
                            <span class="note">${e.note || ''}</span>
                          </li>
                        `,
                      )}
                    </ul>
                  `
                : html`<p class="empty">Nothing ${this._filter} here.</p>`}
            `}
      </section>
    `;
  }

  /**
   * What the live CLI advertises.
   *
   * Kept visually alongside the reflected sections but described as a
   * different kind of fact, because it is: the lists above come from the
   * Python wheel, and these come from the Node binary at initialize.
   * A slash command added there appears in no dataclass, which is the
   * whole reason this half exists.
   */
  _renderCli(cli) {
    if (!cli) return null;
    return html`
      <section>
        <div class="section-head">
          <h3>Live CLI</h3>
          ${cli.available
            ? null
            : html`<span class="pill" style="color:#8b949e">not connected</span>`}
        </div>
        <p class="blurb">
          Advertised by the running <code>claude</code> binary at initialize. The CLI
          ships independently of the wheel, so features can land here that
          reflection above cannot see.
        </p>
        ${cli.available
          ? html`
              <div class="cli-lists">
                ${[
                  ['Commands', cli.commands],
                  ['Tools', cli.tools],
                  ['Output styles', cli.output_styles],
                ].map(
                  ([label, items]) => html`
                    <div>
                      <h4>${label} (${(items || []).length})</h4>
                      ${(items || []).length
                        ? (items || []).map((i) => html`<span>${i}</span>`)
                        : html`<span class="note">none advertised</span>`}
                    </div>
                  `,
                )}
              </div>
            `
          : html`
              <p class="empty">
                No engine to ask. Connect a session and refresh; the reflected
                sections above do not need one.
              </p>
            `}
      </section>
    `;
  }
}

customElements.define('ac-sdk-surface-tab', SdkSurfaceTab);
