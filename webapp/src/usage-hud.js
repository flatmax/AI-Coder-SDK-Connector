// UsageHud — floating transient overlay showing what a turn cost and
// how full the context window is.
//
// Replaces `token-hud.js`, which is gone with the native engine. The
// difference is not cosmetic: the old HUD rendered AC⚡DC's own
// bookkeeping — L0/L1/L2/L3 context tiers, a "map block" modal — all
// of which described a prompt this app assembled itself. It assembles no prompt now. Every number here comes
// from the engine, either on the `streamComplete` payload or from
// `ClaudeCodeService.get_context_usage`, which is a straight pass-through
// of the same data the CLI's own `/context` command shows.
//
// Governing spec: specs5/5-webapp/viewers-hud.md § Usage HUD (CC-17).
//
// Three facts, in the order a user asks for them after a turn lands:
//
//   1. Context — how much room is left before a compact. `percentage`
//      and `maxTokens` come from the engine, but `maxTokens` is the
//      model's raw window: the compaction point is `autoCompactThreshold`,
//      a separate field some 16% below it. See context-usage.js, which
//      owns that arithmetic for all three views that render this payload.
//      That threshold is marked on the bar, because it is what the bar's
//      colour is keyed to: an amber bar at 84% of the window is not a
//      near-miss, it is the compaction, and a bar with nothing on it to
//      compare against made the colour look arbitrary.
//   2. Cost — what this turn cost. Which is *not* `total_cost_usd`: that
//      field is the session's running total, so this row reported the
//      whole session's spend under the label "This turn". The engine
//      differences it for us now; turn-cost.js reads the result and owns
//      the wording, including the difference between a turn that cost
//      nothing extra and one whose cost is unknown.
//   3. Model — which model answered. It can change mid-session via
//      `set_model` (and a subagent may have used a different one), so
//      the turn reports its own rather than the HUD reading a config
//      default. Also not `model_usage`, for the same reason: cumulative,
//      so it named every model the session had ever used.
//
// Interaction is deliberately unchanged from the old HUD, because users
// have the muscle memory: appears on stream-complete, auto-hides after
// 8s, hover pauses the timer, the × dismisses immediately.

import { LitElement, css, html, nothing } from 'lit';
import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';
import {
  bandColor as _contextColor,
  categoryColor,
  compactionLimit,
  compactionPercent,
  partitionCategories,
  thresholdPercent,
  warningPercent,
  windowPercent,
} from './context-usage.js';
import { costLabel, modelNames, reportsUsage } from './turn-cost.js';

/** Auto-hide delay (ms). Matches the old HUD. */
const _AUTO_HIDE_MS = 8000;
/** Fade-out duration (ms). Matches the CSS transition below. */
const _FADE_MS = 800;
/**
 * Deadline for the context fetch. Set above the SDK's own 60s
 * control-request deadline on purpose — see context-usage-tab.js for
 * the reasoning and the measured latencies. This is not a fast call:
 * the guard it protects is held for seconds on every turn, which is
 * why the guard needed a release path at all.
 */
const _FETCH_TIMEOUT_MS = 90000;

function _fmtTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '0';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(2)}M`;
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
      position: relative;
      height: 6px;
      border-radius: 3px;
      background: rgba(240, 246, 252, 0.08);
      overflow: hidden;
      display: flex;
    }
    .bar-seg {
      height: 100%;
    }
    /* The mark's positioning parent, not the bar: .bar clips its
     * children to round the ends of the fill, which would eat the
     * overhang and the ring the mark is drawn with. Same as the
     * Context tab's gauge. */
    .bar-wrap {
      position: relative;
    }
    /* The autocompact mark, same treatment as the Context tab's gauge:
     * over the bar rather than a tick beneath it, so the fill is read
     * against it directly. Without it the bar's colour turns amber for
     * a reason nothing on screen accounts for — the threshold sits some
     * 16% below the window, and the tooltip was the only place that
     * said so. */
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
    /* Said out loud, not just in the tooltip: with autocompact off the
     * bar has no mark, and an unmarked bar looks like a bar whose
     * threshold happens to be off-screen. */
    .no-mark {
      color: #d29922;
      font-size: 0.6875rem;
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
   *
   * So do failed turns that got far enough to spend something. The old
   * rule dropped every errored turn, on the grounds that it "has no
   * numbers to report" — true of a turn that died at the first message,
   * false of one that hit `error_max_turns` after twenty tool calls,
   * which is the most expensive kind there is. `reportsUsage` draws that
   * line; the chat panel and a toast still carry the error itself.
   *
   * (The engine's flag is `is_error` — a `streamComplete` payload has no
   * `error` key. `error` is checked too, for any caller that sends one.)
   */
  _onStreamComplete(event) {
    const result = event.detail?.result;
    if (!result) return;
    const failed = !!(result.error || result.is_error);
    if (failed && !reportsUsage(result)) return;

    this._turn = {
      // What this turn cost, with the reason attached when there is no
      // figure. Emphatically not `total_cost_usd`, which is the whole
      // session's running total — see turn-cost.js.
      cost: costLabel(result),
      // From `turn_model_usage`, so a turn that delegated to a subagent
      // on a cheaper model lists both and a turn that did not is not
      // credited with models the session used earlier.
      models: modelNames(result),
      durationMs: typeof result.duration_ms === 'number'
        ? result.duration_ms
        : null,
      toolCalls: typeof result.tool_calls === 'number'
        ? result.tool_calls
        : null,
      cancelled: !!result.cancelled,
      failed,
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
      // Bounded: a reply dropped by a reconnecting socket would
      // otherwise leave `_fetchInFlight` set forever and blank this
      // section for the rest of the session. See withRpcTimeout.
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_context_usage'),
        _FETCH_TIMEOUT_MS,
        'get_context_usage',
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
   * Prefers the turn's own models over the context breakdown's `model`,
   * because the turn is what the HUD is reporting on and a `set_model`
   * between turns would make the context figure disagree with it. The
   * busiest model leads, so the `+n` hides subagents rather than the
   * model that did the work.
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
    const clamped = windowPercent(ctx);
    // The warning colour goes on the compaction-relative figure, not
    // the engine's headline percentage — see context-usage.js. In 300px
    // there is no room to show both numbers, and the one worth
    // colouring is the one that predicts the pause.
    const warnPct = warningPercent(ctx);
    // Segment the bar by category so the answer to "what is filling
    // this up?" is visible without opening the Context tab. Deferred
    // categories are excluded from the fill — they are tokens the
    // engine has budgeted but not yet loaded — and so are the
    // structural rows, which are the room left rather than content.
    // Where the compaction fires, as a share of the window the bar is
    // drawn against. Null when autocompact is off, which the note below
    // says in words rather than leaving an unmarked bar to imply.
    const markPct = thresholdPercent(ctx);
    const limit = compactionLimit(ctx);
    const { content, deferred, verified } = partitionCategories(ctx);
    const segments = verified && max > 0
      ? content.map((c) => ({
          name: c.name,
          color: categoryColor(c.color),
          width: (Number(c.tokens) / max) * 100,
          tokens: Number(c.tokens),
        }))
      : [];

    return html`
      <div>
        <div class="row">
          <span class="label">Context</span>
          <span class="value" style="color: ${_contextColor(warnPct)}">
            ${clamped.toFixed(0)}% · ${_fmtTokens(total)}/${_fmtTokens(max)}
          </span>
        </div>
        <div class="bar-wrap">
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
                    style="width: ${clamped}%; background: ${_contextColor(warnPct)};"
                  ></div>
                `}
          </div>
          ${markPct != null ? html`
            <div
              class="mark"
              style="left: ${markPct}%"
              title="Autocompact triggers here, at ${limit.toLocaleString()} tokens"
            ></div>
          ` : nothing}
        </div>
        ${ctx.isAutoCompactEnabled === false ? html`
          <div class="no-mark">
            Autocompact off — no mark, and the turn fails at the limit.
          </div>
        ` : nothing}
        ${content.length > 0 ? html`
          <div class="cats">
            ${[...content, ...deferred].map((c) => html`
              <span class="cat ${c.isDeferred ? 'deferred' : ''}">
                <span
                  class="swatch"
                  style="background: ${categoryColor(c.color)}"
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
   * Carries the compaction figure the 300px row has no space for, so
   * the number driving the bar's colour is readable somewhere.
   *
   * This used to name the autocompact reserve only when
   * `rawMaxTokens > maxTokens`, which is never: the engine reports both
   * as the model's full window and keeps the reserve in
   * `autoCompactThreshold`. The branch was dead, so the one thing the
   * tooltip existed to explain was the one thing it never said.
   */
  _contextTitle(ctx, total, max) {
    const parts = [`${total.toLocaleString()} of ${max.toLocaleString()} tokens`];
    const limit = compactionLimit(ctx);
    const toLimit = compactionPercent(ctx);
    if (ctx.isAutoCompactEnabled === false) {
      parts.push('Autocompact is off — the turn will fail at the limit.');
    } else if (toLimit != null && limit > 0 && limit < max) {
      parts.push(
        `${toLimit.toFixed(0)}% of the way to an autocompact at `
        + `${limit.toLocaleString()} tokens `
        + `(${Math.max(0, limit - total).toLocaleString()} left)`,
      );
    }
    return parts.join(' · ');
  }

  _renderTurn() {
    const turn = this._turn;
    if (!turn) return '';
    const cost = turn.cost;
    const bits = [];
    if (turn.toolCalls) {
      bits.push(`${turn.toolCalls} tool ${turn.toolCalls === 1 ? 'call' : 'calls'}`);
    }
    if (turn.durationMs != null) {
      bits.push(`${(turn.durationMs / 1000).toFixed(1)}s`);
    }
    if (turn.cancelled) bits.push('interrupted');
    // A turn only reaches the HUD after failing if it spent something, so
    // say which it was: without this the row reads as a normal turn's
    // receipt for a turn that never finished.
    if (turn.failed) bits.push('failed');
    return html`
      <div class="row">
        <span class="label">This turn</span>
        <span class="value">
          ${cost
            // Three renderings, because they are three different facts: a
            // price, "nothing extra", and "cost unknown". The tooltip
            // carries the why — turn-cost.js owns both.
            ? html`<span
                class=${cost.known ? '' : 'muted'}
                title=${cost.title}
              >${cost.text}</span>`
            : nothing}
          ${bits.length > 0
            ? html`<span class="muted"> · ${bits.join(' · ')}</span>`
            : ''}
        </span>
      </div>
    `;
  }
}

customElements.define('ac-usage-hud', UsageHud);
