// UsageHud — floating transient overlay showing what a turn cost and
// how full the context window is.
//
// Replaces `token-hud.js`, which is gone with the native engine. The
// difference is not cosmetic: the old HUD rendered AC⚡DC's own
// bookkeeping — L0/L1/L2/L3 context tiers, cache-warmup temperature,
// a "map block" modal — all of which described a prompt this app
// assembled itself. It assembles no prompt now. Every number here comes
// from the engine, either on the `streamComplete` payload or from
// `ClaudeCodeService.get_context_usage`, which is a straight pass-through
// of the same data the CLI's own `/context` command shows.
//
// Governing spec: specs5/5-webapp/viewers-hud.md § Usage HUD (CC-17).
//
// Three facts, in the order a user asks for them after a turn lands:
//
//   1. Context — how much room is left before a compact. `percentage`
//      and `maxTokens` come from the engine; `maxTokens` is already
//      reduced by the autocompact buffer, so the bar reaching 100% is
//      the real trigger point rather than the model's raw window.
//   2. Cost — what this turn cost. Null under subscription billing, and
//      rendered as "included" rather than "$0.00" in that case: a turn
//      on a Max plan did not cost nothing, it cost nothing *extra*.
//   3. Model — which model answered. It can change mid-session via
//      `set_model` (and a subagent may have used a different one), so
//      the turn reports its own rather than the HUD reading a config
//      default.
//
// Interaction is deliberately unchanged from the old HUD, because users
// have the muscle memory: appears on stream-complete, auto-hides after
// 8s, hover pauses the timer, the × dismisses immediately.

import { LitElement, css, html } from 'lit';
import { RpcMixin } from './rpc-mixin.js';

/** Auto-hide delay (ms). Matches the old HUD. */
const _AUTO_HIDE_MS = 8000;
/** Fade-out duration (ms). Matches the CSS transition below. */
const _FADE_MS = 800;

/**
 * Fallback colour for a context category the engine didn't colour.
 *
 * The engine sends a `color` per category and we use it verbatim, so
 * the bar segments match what `/context` draws in the terminal. A user
 * running both should not have to learn two colour languages.
 */
const _UNCOLOURED = '#6e7681';

function _fmtTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '0';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/**
 * Format a USD cost.
 *
 * Sub-cent turns are the common case, so two decimals would render
 * most turns as "$0.00" — which reads as free rather than as small.
 * Four decimals below a cent, two above.
 */
function _fmtCost(usd) {
  if (typeof usd !== 'number' || !Number.isFinite(usd)) return null;
  if (usd === 0) return '$0';
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

/** Green ≤75%, amber 75-90%, red >90%. Same bands as the dialog bar. */
function _contextColor(pct) {
  if (pct > 90) return '#f85149';
  if (pct > 75) return '#d29922';
  return '#7ee787';
}

export class UsageHud extends RpcMixin(LitElement) {
  static properties = {
    /** Whether the HUD is visible. */
    _visible: { type: Boolean, state: true },
    /** Whether the HUD is fading out. */
    _fading: { type: Boolean, state: true },
    /**
     * Context usage from `get_context_usage`, or null before the
     * first successful fetch. Shape is the SDK's
     * `ContextUsageResponse` — categories / totalTokens / maxTokens /
     * rawMaxTokens / percentage / model / isAutoCompactEnabled.
     */
    _context: { type: Object, state: true },
    /**
     * Why the context section is unavailable, when it is. Kept
     * separate from `_context` so a failed refresh shows a reason
     * instead of silently leaving the last good numbers on screen
     * looking current.
     */
    _contextError: { type: String, state: true },
    /**
     * The turn that just finished: `{ cost, models, durationMs,
     * toolCalls, cancelled }`. Derived from the `streamComplete`
     * payload, which is the only place per-turn numbers exist — the
     * engine does not keep a running session total for us to query.
     */
    _turn: { type: Object, state: true },
  };

  static styles = css`
    :host {
      position: fixed;
      top: 16px;
      right: 16px;
      z-index: 10000;
      display: none;
      pointer-events: none;
    }
    :host([visible]) {
      display: block;
      pointer-events: auto;
    }
    .hud {
      width: 300px;
      background: rgba(22, 27, 34, 0.96);
      border: 1px solid rgba(240, 246, 252, 0.15);
      border-radius: 8px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(8px);
      font-size: 0.8125rem;
      color: var(--text-primary, #c9d1d9);
      transition: opacity ${_FADE_MS}ms ease;
    }
    :host(.fading) .hud {
      opacity: 0;
    }

    .hud-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 10px;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
    }
    .model {
      font-weight: 600;
      font-size: 0.75rem;
      letter-spacing: 0.02em;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dismiss {
      background: none;
      border: none;
      color: var(--text-secondary, #8b949e);
      cursor: pointer;
      font-size: 0.9rem;
      line-height: 1;
      padding: 0 2px;
      flex: 0 0 auto;
    }
    .dismiss:hover {
      color: var(--text-primary, #c9d1d9);
    }

    .body {
      padding: 8px 10px 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
    }
    .label {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
    }
    .value {
      font-variant-numeric: tabular-nums;
    }
    .muted {
      color: var(--text-secondary, #8b949e);
    }

    .bar {
      height: 6px;
      border-radius: 3px;
      background: rgba(240, 246, 252, 0.08);
      overflow: hidden;
      display: flex;
    }
    .bar-seg {
      height: 100%;
    }

    .cats {
      display: flex;
      flex-wrap: wrap;
      gap: 3px 10px;
      font-size: 0.6875rem;
      color: var(--text-secondary, #8b949e);
    }
    .cat {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .swatch {
      width: 7px;
      height: 7px;
      border-radius: 2px;
      flex: 0 0 auto;
    }
    .deferred {
      opacity: 0.6;
      font-style: italic;
    }
    .error {
      color: #f85149;
      font-size: 0.6875rem;
    }
  `;

  constructor() {
    super();
    this._visible = false;
    this._fading = false;
    this._context = null;
    this._contextError = '';
    this._turn = null;

    this._autoHideTimer = null;
    this._fadeTimer = null;
    this._fetchInFlight = false;

    this._onStreamComplete = this._onStreamComplete.bind(this);
    this._onSessionChanged = this._onSessionChanged.bind(this);
    this._onPointerEnter = this._onPointerEnter.bind(this);
    this._onPointerLeave = this._onPointerLeave.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('stream-complete', this._onStreamComplete);
    // A session change refreshes the backing numbers without showing
    // the HUD. The HUD is per-turn feedback; popping it up because a
    // session loaded would be reporting on a turn that didn't happen.
    window.addEventListener('session-changed', this._onSessionChanged);
    this.addEventListener('pointerenter', this._onPointerEnter);
    this.addEventListener('pointerleave', this._onPointerLeave);
  }

  disconnectedCallback() {
    window.removeEventListener('stream-complete', this._onStreamComplete);
    window.removeEventListener('session-changed', this._onSessionChanged);
    this.removeEventListener('pointerenter', this._onPointerEnter);
    this.removeEventListener('pointerleave', this._onPointerLeave);
    this._clearTimers();
    super.disconnectedCallback();
  }

  // Reflect `visible` manually so the :host([visible]) selector works
  // without exposing internal state as a public attribute.
  updated(changed) {
    if (changed.has('_visible')) {
      if (this._visible) this.setAttribute('visible', '');
      else this.removeAttribute('visible');
    }
  }

  // ---------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------

  /**
   * Show the HUD for a turn that just settled.
   *
   * Cancelled turns still get a HUD. The user interrupted after some
   * work was already billed, and hiding the number would make the
   * interrupt look free.
   */
  _onStreamComplete(event) {
    const result = event.detail?.result;
    if (!result) return;
    // A stream that failed before reaching the engine has no numbers to
    // report; the chat panel already surfaces the error.
    if (result.error) return;

    this._turn = {
      // Null under subscription billing — carried through as null so
      // the renderer can say "included" instead of inventing a zero.
      cost: typeof result.total_cost_usd === 'number'
        ? result.total_cost_usd
        : null,
      // `model_usage` is keyed by model name, so a turn that delegated
      // to a subagent on a cheaper model lists both.
      models: result.model_usage
        ? Object.keys(result.model_usage)
        : [],
      durationMs: typeof result.duration_ms === 'number'
        ? result.duration_ms
        : null,
      toolCalls: typeof result.tool_calls === 'number'
        ? result.tool_calls
        : null,
      cancelled: !!result.cancelled,
    };

    this._visible = true;
    this._fading = false;
    this.classList.remove('fading');
    this._startAutoHide();
    this._fetchContext();
  }

  _onSessionChanged() {
    this._turn = null;
    this._fetchContext();
  }

  _onPointerEnter() {
    // Hovering means the user is reading. Cancel both the hide and an
    // in-progress fade, and undo the fade so the text is legible again.
    this._clearTimers();
    this._fading = false;
    this.classList.remove('fading');
  }

  _onPointerLeave() {
    if (this._visible) this._startAutoHide();
  }

  // ---------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------

  /**
   * Fetch the live context breakdown.
   *
   * Guarded against overlap: a fast succession of turns would
   * otherwise queue several control requests to the CLI for the same
   * answer, and the later reply could land before the earlier one.
   */
  async _fetchContext() {
    if (this._fetchInFlight) return;
    if (!this.rpcConnected) return;
    this._fetchInFlight = true;
    try {
      const res = await this.rpcExtract(
        'ClaudeCodeService.get_context_usage',
      );
      if (res && res.error) {
        this._contextError = String(res.error);
        return;
      }
      const usage = res && res.usage ? res.usage : null;
      if (!usage) {
        this._contextError = 'The engine returned no context usage.';
        return;
      }
      this._context = usage;
      this._contextError = '';
    } catch (err) {
      this._contextError = err?.message || 'Context usage unavailable.';
    } finally {
      this._fetchInFlight = false;
    }
  }

  // ---------------------------------------------------------------
  // Timers
  // ---------------------------------------------------------------

  _startAutoHide() {
    this._clearTimers();
    this._autoHideTimer = setTimeout(() => {
      this._fading = true;
      this.classList.add('fading');
      this._fadeTimer = setTimeout(() => {
        this._visible = false;
        this._fading = false;
        this.classList.remove('fading');
      }, _FADE_MS);
    }, _AUTO_HIDE_MS);
  }

  _clearTimers() {
    if (this._autoHideTimer) {
      clearTimeout(this._autoHideTimer);
      this._autoHideTimer = null;
    }
    if (this._fadeTimer) {
      clearTimeout(this._fadeTimer);
      this._fadeTimer = null;
    }
  }

  _dismiss() {
    this._clearTimers();
    this._visible = false;
    this._fading = false;
    this.classList.remove('fading');
  }

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------

  render() {
    if (!this._visible) return html``;
    return html`
      <div class="hud" role="status" aria-live="polite">
        <div class="hud-header">
          <span class="model" title=${this._modelTitle()}>
            ${this._modelLabel()}
          </span>
          <button
            class="dismiss"
            title="Dismiss"
            aria-label="Dismiss usage overlay"
            @click=${this._dismiss}
          >✕</button>
        </div>
        <div class="body">
          ${this._renderContext()}
          ${this._renderTurn()}
        </div>
      </div>
    `;
  }

  /**
   * Header label — the model that answered.
   *
   * Prefers the turn's own `model_usage` keys over the context
   * breakdown's `model`, because the turn is what the HUD is
   * reporting on and a `set_model` between turns would make the
   * context figure disagree with it.
   */
  _modelLabel() {
    const models = this._turn?.models || [];
    if (models.length === 1) return models[0];
    if (models.length > 1) return `${models[0]} +${models.length - 1}`;
    return this._context?.model || 'Claude Code';
  }

  _modelTitle() {
    const models = this._turn?.models || [];
    if (models.length > 1) return `Models used this turn: ${models.join(', ')}`;
    return this._modelLabel();
  }

  _renderContext() {
    if (this._contextError && !this._context) {
      return html`<div class="error">${this._contextError}</div>`;
    }
    const ctx = this._context;
    if (!ctx) return html`<div class="muted">Reading context…</div>`;

    const total = Number(ctx.totalTokens) || 0;
    const max = Number(ctx.maxTokens) || 0;
    const pct = Number.isFinite(Number(ctx.percentage))
      ? Number(ctx.percentage)
      : (max > 0 ? (total / max) * 100 : 0);
    const clamped = Math.max(0, Math.min(100, pct));
    // Segment the bar by category so the answer to "what is filling
    // this up?" is visible without opening the Context tab. Deferred
    // categories are excluded from the fill — they are tokens the
    // engine has budgeted but not yet loaded.
    const cats = Array.isArray(ctx.categories) ? ctx.categories : [];
    const live = cats.filter((c) => !c.isDeferred && Number(c.tokens) > 0);
    const segments = max > 0
      ? live.map((c) => ({
          name: c.name,
          color: c.color || _UNCOLOURED,
          width: (Number(c.tokens) / max) * 100,
          tokens: Number(c.tokens),
        }))
      : [];

    return html`
      <div>
        <div class="row">
          <span class="label">Context</span>
          <span class="value" style="color: ${_contextColor(clamped)}">
            ${clamped.toFixed(0)}% · ${_fmtTokens(total)}/${_fmtTokens(max)}
          </span>
        </div>
        <div
          class="bar"
          title=${this._contextTitle(ctx, total, max)}
          role="img"
          aria-label="Context ${clamped.toFixed(0)} percent used"
        >
          ${segments.length > 0
            ? segments.map((s) => html`
                <div
                  class="bar-seg"
                  style="width: ${s.width}%; background: ${s.color};"
                  title="${s.name}: ${_fmtTokens(s.tokens)}"
                ></div>
              `)
            : html`
                <div
                  class="bar-seg"
                  style="width: ${clamped}%; background: ${_contextColor(clamped)};"
                ></div>
              `}
        </div>
        ${live.length > 0 ? html`
          <div class="cats">
            ${cats
              .filter((c) => Number(c.tokens) > 0)
              .map((c) => html`
                <span class="cat ${c.isDeferred ? 'deferred' : ''}">
                  <span
                    class="swatch"
                    style="background: ${c.color || _UNCOLOURED}"
                  ></span>
                  ${c.name} ${_fmtTokens(Number(c.tokens))}
                </span>
              `)}
          </div>
        ` : ''}
      </div>
    `;
  }

  /**
   * Tooltip for the context bar.
   *
   * Names the autocompact buffer explicitly when there is one. Without
   * it, "155K/172K" next to a model advertised as 200K looks like a
   * bug rather than a deliberately reserved margin.
   */
  _contextTitle(ctx, total, max) {
    const raw = Number(ctx.rawMaxTokens) || 0;
    const parts = [`${total.toLocaleString()} of ${max.toLocaleString()} tokens`];
    if (raw > max) {
      parts.push(
        `${(raw - max).toLocaleString()} reserved as autocompact headroom `
        + `(model window ${raw.toLocaleString()})`,
      );
    }
    if (ctx.isAutoCompactEnabled === false) {
      parts.push('Autocompact is off — the turn will fail at the limit.');
    }
    return parts.join(' · ');
  }

  _renderTurn() {
    const turn = this._turn;
    if (!turn) return '';
    const cost = _fmtCost(turn.cost);
    const bits = [];
    if (turn.toolCalls) {
      bits.push(`${turn.toolCalls} tool ${turn.toolCalls === 1 ? 'call' : 'calls'}`);
    }
    if (turn.durationMs != null) {
      bits.push(`${(turn.durationMs / 1000).toFixed(1)}s`);
    }
    if (turn.cancelled) bits.push('interrupted');
    return html`
      <div class="row">
        <span class="label">This turn</span>
        <span class="value">
          ${cost
            // Only the engine knows the price. A null means subscription
            // billing, where the marginal cost genuinely is nothing —
            // saying "included" is accurate where "$0.00" would not be.
            ? cost
            : html`<span class="muted">included</span>`}
          ${bits.length > 0
            ? html`<span class="muted"> · ${bits.join(' · ')}</span>`
            : ''}
        </span>
      </div>
    `;
  }
}

customElements.define('ac-usage-hud', UsageHud);
