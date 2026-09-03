// UsageHud — floating transient overlay showing what a turn cost and
// how full the context window is.
//
// Replaces `token-hud.js`, which is gone with the native engine. The
// difference is not cosmetic: the old HUD rendered AIC⚡DC's own
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
// Two more the spec always asked for and nothing rendered until now:
//
//   4. Rate limits — how much of the window is spent, which window, and
//      when it resets. This is the *cost* row for a reader whose billing
//      makes the dollar figures meaningless: R-6 names `RateLimitEvent`
//      as the subscription-mode equivalent of a cost signal, so it shows
//      at any status rather than only when something is wrong. The
//      arithmetic and the wording are in rate-limit.js, shared with the
//      chat panel's toast — which is the alarm this deliberately is not.
//   5. Files modified — what the turn did to the repo, one click from
//      the diff. The chips follow the house rule every file on screen
//      follows: repo-relative label, engine's absolute path on the
//      tooltip, and the unconverted path in the navigation event.
//
// Sections with a body under them collapse, and the set of collapsed ones
// is remembered across sessions. The head keeps the section's headline
// figure, so closing one costs no height and hides no answer.
//
// Interaction is otherwise unchanged from the old HUD, because users
// have the muscle memory: appears on stream-complete, auto-hides after
// 8s, hover pauses the timer, the × dismisses immediately.

import { LitElement, css, html, nothing } from 'lit';
import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';
import {
  SURFACE,
  loadCapabilities,
  resetCapabilities,
  supports,
} from './engine-capabilities.js';
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
import { costLabel, modelUsageLines, reportsUsage } from './turn-cost.js';
import {
  formatResetTime,
  hasSomethingToSay,
  limitTypeLabel,
  utilizationPercent,
} from './rate-limit.js';
import { toRepoPath } from './repo-path.js';

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

/**
 * Where the collapse set is kept.
 *
 * A serialised array of section *names*, not a per-section boolean, so a
 * section this build has never heard of round-trips instead of being reset —
 * two browsers on one profile, or a downgrade, would otherwise each clear the
 * other's preferences for the sections it does not render.
 */
const _COLLAPSE_KEY = 'aic-dc-hud-collapsed';

/**
 * The collapse set, or an empty one.
 *
 * Every failure mode lands on "nothing collapsed", which is the pre-existing
 * behaviour: a Safari private window that throws on `localStorage`, a value
 * some other tool wrote, a half-written string. A HUD that fails to open is a
 * worse outcome than a HUD that forgets, and this is a preference rather than
 * a record.
 */
function _loadCollapsed() {
  try {
    const raw = JSON.parse(window.localStorage.getItem(_COLLAPSE_KEY) || '[]');
    if (!Array.isArray(raw)) return new Set();
    return new Set(raw.filter((name) => typeof name === 'string'));
  } catch {
    return new Set();
  }
}

function _saveCollapsed(names) {
  try {
    window.localStorage.setItem(_COLLAPSE_KEY, JSON.stringify([...names]));
  } catch {
    // Storage full or unavailable. The session keeps the preference in
    // memory; only its persistence is lost, and there is nothing to tell
    // the user that they could act on.
  }
}

function _fmtTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '0';
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/**
 * Whether an `engineHealth` payload says the engine is *gone*, as
 * opposed to not started yet.
 *
 * `connected` alone cannot tell those apart: it is false before the
 * first prompt as well, which is the ordinary state of a freshly loaded
 * page and not something to report. The discriminator is `last_error` —
 * a session that loses its engine sets it on the way out — and it is the
 * same one `health-banner.js` uses to decide it has something to say.
 * One rule, two readers; a second definition of "gone" could only come
 * to disagree with the banner the HUD defers to for the reason.
 */
function _engineIsGone(health) {
  if (!health || typeof health !== 'object') return false;
  if (health.connected !== false) return false;
  return typeof health.last_error === 'string' && health.last_error !== '';
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
     * Whether the engine is gone, in which case there is nothing to
     * poll and a state to sit in instead.
     *
     * Its own flag rather than a third reading of `_contextError`,
     * because it gates the fetch as well as the render: this is the
     * one context failure that is a standing condition rather than a
     * request that went wrong, so the answer is to stop asking.
     */
    _engineGone: { type: Boolean, state: true },
    /**
     * The turn that just finished: `{ cost, usage, models, durationMs,
     * toolCalls, cancelled }`. Derived from the `streamComplete`
     * payload, which is the only place per-turn numbers exist — the
     * engine does not keep a running session total for us to query.
     *
     * `usage` is the per-model lines, split into prompt and completion;
     * `models` is their names, for the header. The HUD used to carry the
     * names alone, so the one panel titled "usage" reported a price and
     * no tokens.
     */
    _turn: { type: Object, state: true },
    /**
     * The last `rateLimit` record, or null before one arrives.
     *
     * Not per-turn, unlike `_turn`: the CLI emits this when the status
     * *transitions*, so one record stands until the next transition or the
     * window's reset — which is why the engine holds it too, and why the
     * shell's first-paint snapshot carries it (`rate_limit`).
     */
    _rateLimit: { type: Object, state: true },
    /**
     * Which sections the user has collapsed, by name. A `Set`, replaced
     * rather than mutated on every toggle — Lit compares by identity and a
     * `Set` that grows in place is the same object it was.
     */
    _collapsed: { type: Object, state: true },
  };

  static styles = css`
    :host {
      position: fixed;
      top: 16px;
      right: 16px;
      /* 500, the rung specs-reference/5-webapp/shell.md § Viewport-scoped
       * overlay z-index ladder assigns: above the dialog panel, below the
       * toast layer, and far below the permission dialog's 9000.
       *
       * It was 10000, which is above everything including the one surface in
       * the app that blocks a turn. § Placement has said "below the
       * permission dialog, which is modal over everything" since phase 3 and
       * the number never matched it — a request arriving within the HUD's
       * eight seconds had this overlay floating over its top-right corner,
       * with pointer-events auto taking the clicks there too. Found while
       * building the sections below, by reading the ladder for where a fifth
       * section could go. */
      z-index: 500;
      display: none;
      pointer-events: none;
    }
    :host([visible]) {
      display: block;
      pointer-events: auto;
    }
    .hud {
      width: 300px;
      /* Specified since phase 3 (§ Behaviour, "fixed width, max height with
       * internal scroll") and never written, which cost nothing while the
       * HUD was four short rows. The Files modified section is what makes it
       * matter: a turn that touches forty files is an ordinary refactor, and
       * without a ceiling the overlay runs off the bottom of the screen —
       * taking the dismiss button, which is at the top, out of reach of the
       * part that is off-screen. */
      max-height: 80vh;
      overflow-y: auto;
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
    /* A token row in 300px: the model name gives way before the
     * numbers do. A clipped name is still recognisable and the
     * tooltip has it in full; a clipped count is a different
     * number. */
    .token-model {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    .token-value {
      flex: none;
      font-size: 0.75rem;
    }
    .muted {
      color: var(--text-secondary, #8b949e);
    }

    /* A collapsible section. The head is the row that would have been there
     * anyway — a label on the left and the section's headline figure on the
     * right — with a caret in front of it, so collapsing costs no height and
     * a collapsed section still reports its number. Only sections with a
     * body under them get one; see the _section helper below. */
    .sec-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      width: 100%;
      padding: 0;
      background: none;
      border: none;
      font: inherit;
      color: inherit;
      text-align: left;
      cursor: pointer;
    }
    .sec-head:hover .sec-name {
      color: var(--text-primary, #c9d1d9);
    }
    .sec-label {
      display: flex;
      align-items: baseline;
      gap: 4px;
      min-width: 0;
    }
    .sec-caret {
      color: var(--text-secondary, #8b949e);
      font-size: 0.625rem;
      flex: 0 0 auto;
    }
    .sec-name {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      white-space: nowrap;
    }
    .sec-body {
      margin-top: 4px;
      display: flex;
      flex-direction: column;
      gap: 4px;
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
    /* Amber, not the error red: the engine being gone is a condition to
     * sit in rather than a request that failed, and it is already being
     * alarmed about by the health banner — which owns the reason, so
     * this says only that there is nothing to read. */
    .gone {
      color: #d29922;
      font-size: 0.6875rem;
    }

    /* Rate limits. The reset line is the half that makes the figure
     * actionable — "72% used" is a fact, "72% used, resets at 14:30" is a
     * decision — so it is body text rather than a tooltip. */
    .rl-note {
      font-size: 0.6875rem;
      color: var(--text-secondary, #8b949e);
    }
    .rl-rejected {
      color: #f85149;
    }

    /* File chips. The same shape as the chat panel's tool-card footer chip,
     * in this shadow root's own stylesheet because a shadow root does not
     * inherit one — the *rule* the two share is toRepoPath, which is the
     * part that could drift, and it does not live here either. */
    .files {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .file-chip {
      display: inline-block;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      padding: 1px 6px;
      border-radius: 10px;
      background: rgba(240, 246, 252, 0.08);
      color: var(--text-secondary, #8b949e);
      font-size: 0.6875rem;
      font-family: var(--font-mono, ui-monospace, monospace);
      cursor: pointer;
    }
    .file-chip:hover {
      background: rgba(240, 246, 252, 0.16);
      color: var(--text-primary, #c9d1d9);
    }
  `;

  constructor() {
    super();
    this._visible = false;
    this._fading = false;
    this._context = null;
    this._contextError = '';
    this._engineGone = false;
    this._turn = null;
    this._rateLimit = null;
    this._collapsed = _loadCollapsed();

    this._autoHideTimer = null;
    this._fadeTimer = null;
    this._fetchInFlight = false;

    this._onStreamComplete = this._onStreamComplete.bind(this);
    this._onSessionChanged = this._onSessionChanged.bind(this);
    this._onEngineHealth = this._onEngineHealth.bind(this);
    this._onRateLimit = this._onRateLimit.bind(this);
    this._onStateLoaded = this._onStateLoaded.bind(this);
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
    // Pushed on connect and again whenever it changes, which includes the
    // way out: a lost session broadcasts one. This is the only signal that
    // can stop a poll *before* it is sent — the other one the HUD reads is
    // the reply to a poll, which by then has already gone out. See
    // `_fetchContext` for why both are needed.
    window.addEventListener('engine-health', this._onEngineHealth);
    // Two arrivals for one figure, because a status change and a page load
    // are different moments and neither covers the other. The push is the
    // only thing that reports a *transition*; the snapshot is the only thing
    // a browser that reloaded between transitions will ever get.
    window.addEventListener('rate-limit', this._onRateLimit);
    window.addEventListener('state-loaded', this._onStateLoaded);
    this.addEventListener('pointerenter', this._onPointerEnter);
    this.addEventListener('pointerleave', this._onPointerLeave);
  }

  disconnectedCallback() {
    window.removeEventListener('stream-complete', this._onStreamComplete);
    window.removeEventListener('session-changed', this._onSessionChanged);
    window.removeEventListener('engine-health', this._onEngineHealth);
    window.removeEventListener('rate-limit', this._onRateLimit);
    window.removeEventListener('state-loaded', this._onStateLoaded);
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
    if (changed.has('rpcConnected')) {
      if (this.rpcConnected) {
        // Read the descriptor once the socket is up. The re-render is
        // needed because `supports()` is synchronous and answers "yes"
        // until the real answer lands: a panel that should be hidden is
        // briefly drawn, and nothing tells Lit that changed. Deliberate —
        // see engine-capabilities.js for why the loading default is
        // "supported" rather than "hidden".
        loadCapabilities(this).then(() => this.requestUpdate());
      } else {
        // The engine behind a reconnect need not be the one that went
        // away, and a stale descriptor hides the wrong panels.
        resetCapabilities();
      }
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

    const usage = modelUsageLines(result);
    this._turn = {
      // What this turn cost, with the reason attached when there is no
      // figure. Emphatically not `total_cost_usd`, which is the whole
      // session's running total — see turn-cost.js.
      cost: costLabel(result),
      // From `turn_model_usage`, so a turn that delegated to a subagent
      // on a cheaper model lists both and a turn that did not is not
      // credited with models the session used earlier. Busiest model
      // first, which is the order the header's `+n` truncates from.
      usage,
      models: usage.map((line) => line.model),
      durationMs: typeof result.duration_ms === 'number'
        ? result.duration_ms
        : null,
      toolCalls: typeof result.tool_calls === 'number'
        ? result.tool_calls
        : null,
      cancelled: !!result.cancelled,
      failed,
      // What the turn did to the repo. The single most useful thing to know
      // in the moment after an agentic turn lands, and the one thing here
      // that is not a number — see `_renderFiles`. Absolute, as every path
      // the engine reports is; the chip converts for display and leaves the
      // navigation contract alone.
      filesModified: Array.isArray(result.files_modified)
        ? result.files_modified.filter((path) => typeof path === 'string' && path)
        : [],
    };

    this._visible = true;
    this._fading = false;
    this.classList.remove('fading');
    this._startAutoHide();
    this._fetchContext();
  }

  _onSessionChanged() {
    this._turn = null;
    // A session change is new information, so the gate is dropped and
    // this engine gets asked once. Starting or resuming a session is
    // exactly what the gone state tells the user to do, and a flag that
    // survived them doing it would leave the HUD reporting a dead engine
    // at a live one until the next health push happened to arrive.
    this._engineGone = false;
    this._fetchContext();
  }

  /**
   * Track whether there is an engine worth polling.
   *
   * Clears as readily as it sets: a reconnect broadcasts `connected`
   * true, and this is a claim about now.
   */
  _onEngineHealth(event) {
    const health = event?.detail;
    if (!health || typeof health !== 'object') return;
    this._engineGone = _engineIsGone(health);
  }

  /**
   * A pushed rate-limit record.
   *
   * Kept whatever the status, including the `allowed` the chat panel's toast
   * deliberately says nothing about. The toast reports a transition and this
   * reports a standing figure: under subscription billing it is the only
   * number on the HUD that maps to anything the user can act on
   * (`specs5/plan/risks.md` § R-6), so it is not gated on being alarming.
   *
   * A record never *replaces* a good one with nothing — the CLI only sends
   * this when it has something to say, so there is no empty push to guard
   * against, and the window's own reset is what ends a record's life.
   */
  _onRateLimit(event) {
    const data = event?.detail?.data;
    if (!data || typeof data !== 'object') return;
    this._rateLimit = data;
  }

  /**
   * The shell's first-paint snapshot.
   *
   * Read for `rate_limit` alone. Everything else the HUD shows is per-turn
   * and there is no turn to report at first paint — the HUD does not even
   * appear until one lands, which is exactly why adopting the record here
   * costs nothing: it is waiting by the time there is a HUD to put it in.
   *
   * A snapshot that carries no record leaves the current one alone rather
   * than clearing it. A reconnect re-delivers the snapshot mid-session, and
   * a backend older than the field sends nothing at all; neither is evidence
   * that a limit the browser has been told about has gone away.
   */
  _onStateLoaded(event) {
    const record = event?.detail?.rate_limit;
    if (!record || typeof record !== 'object') return;
    this._rateLimit = record;
  }

  /**
   * Collapse or expand a section, and remember it.
   *
   * A new `Set` each time: Lit's default `hasChanged` is an identity
   * comparison, so a `Set` mutated in place is the same object it was and
   * nothing would re-render.
   */
  _toggleSection(name) {
    const next = new Set(this._collapsed);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    this._collapsed = next;
    _saveCollapsed(next);
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
   *
   * And guarded against there being no engine to ask. `get_context_usage`
   * is a control request to the CLI subprocess, so once the engine is gone
   * every call is a round trip whose best outcome is an error the health
   * banner has already given in better words — and whose worst is a
   * 60-second `Control request timeout`, logged server-side with a
   * traceback, because a pump that died without the session being marked
   * lost leaves a client that still looks usable and a subprocess whose
   * reply nobody is reading. Four of those in one log is what put the
   * gate here; the tracebacks were noise about a thing already reported.
   */
  async _fetchContext() {
    if (this._fetchInFlight) return;
    if (!this.rpcConnected) return;
    if (this._engineGone) return;
    // AG-9: an engine that cannot report a context window gets no bar at
    // all, rather than a 0% one. The difference is that a bar is a
    // measurement and its absence is an absence, and a measurement is
    // believed. Checked by capability, never by engine name (AG-R-4).
    //
    // This also stops a poll the server would refuse: the router raises
    // `UnsupportedOnThisEngine` for a method whose surface is hidden, so
    // without this the HUD would retry a guaranteed error every tick.
    if (!supports(SURFACE.CONTEXT_WINDOW_USAGE)) return;
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
        // `reason` tells the two failures apart — there being no engine
        // versus a request to a live one that failed — and this is the
        // half that is a state rather than a mishap. Reading it here is
        // what closes the gate on the turn that *causes* it: the engine
        // emits `streamComplete` before the `engineHealth` that follows
        // it, so the HUD is called from the dying turn a moment before
        // the push arrives, and without this the first fetch after every
        // loss would go out anyway. The push covers every later one.
        if (res.reason === 'no-engine') {
          this._engineGone = true;
          return;
        }
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
      // An engine that answered with a breakdown is an engine that is
      // there, whatever a health push said earlier.
      this._engineGone = false;
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
          ${this._renderTokens()}
          ${this._renderRateLimit()}
          ${this._renderFiles()}
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

  /**
   * One collapsible section: a head that is always drawn, and a body that is
   * drawn when the section is open.
   *
   * **Only sections with a body take one.** The spec's § Sections calls the
   * HUD's sections collapsible without qualification, and for four of them
   * that is what this implements. "This turn" is the exception and stays a
   * plain row: its entire content *is* its headline, so a disclosure control
   * there would hide the figure the HUD exists to show and cost a caret to
   * do it. A collapsed section that says nothing is not a smaller section,
   * it is an absent one.
   *
   * `body` is a thunk so a collapsed section costs no render work — the
   * categories legend and the file chips are the two biggest things here and
   * both are behind one.
   *
   * The head is a real `<button>`, so it is reachable by keyboard and
   * announces its state; `aria-expanded` is on the control rather than the
   * region because the control is what the reader lands on.
   *
   * `key` is stored, `name` is displayed, and they are two arguments because
   * one section's name moves: "5-hour limit" becomes "7-day limit" when the
   * window the account is against changes. Keying the preference on the label
   * would silently re-open a section the user closed, on the day the label
   * changed — which is the day they least want to look at it.
   */
  _section(key, name, headline, body) {
    const collapsed = this._collapsed.has(key);
    return html`
      <div class="sec">
        <button
          class="sec-head"
          aria-expanded=${collapsed ? 'false' : 'true'}
          title=${collapsed ? `Show ${name}` : `Hide ${name}`}
          @click=${() => this._toggleSection(key)}
        >
          <span class="sec-label">
            <span class="sec-caret">${collapsed ? '▸' : '▾'}</span>
            <span class="sec-name">${name}</span>
          </span>
          ${headline ?? nothing}
        </button>
        ${collapsed ? nothing : html`<div class="sec-body">${body()}</div>`}
      </div>
    `;
  }

  _renderContext() {
    // Ahead of everything, because "this engine has no such measurement"
    // outranks any state of a measurement it does not take. AG-9: hidden,
    // not drawn empty — `nothing` rather than a 0% bar or a "no data"
    // note, because a placeholder where a bar used to be still reads as a
    // reading. The section *header* goes with it too: `_section` is called
    // at the end of this method, so returning early removes the collapsible
    // head as well as its body, leaving no empty "Context" row behind.
    if (!supports(SURFACE.CONTEXT_WINDOW_USAGE)) return nothing;
    // Ahead of both the error and the numbers, because it outranks them.
    // The last good breakdown would otherwise sit here looking current
    // while describing a window no engine holds any more — the same
    // reason `_contextError` was split from `_context` in the first
    // place — and a stale error string from the fetch that discovered
    // the loss says less than naming the loss does.
    if (this._engineGone) {
      return html`<div class="gone">
        The engine is gone — no context to read until a session is
        started or resumed. The health banner has the reason.
      </div>`;
    }
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

    // The headline stays on the head when the section is closed, which is
    // what makes closing it cheap: the percentage and the totals are the
    // answer, and the bar and the legend are the working.
    const headline = html`
      <span class="value" style="color: ${_contextColor(warnPct)}">
        ${clamped.toFixed(0)}% · ${_fmtTokens(total)}/${_fmtTokens(max)}
      </span>
    `;
    return this._section('Context', 'Context', headline, () => html`
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
    `);
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
    // The row stays; the price leaves. AG-6: this engine reports usage in
    // tokens and no USD is invented for it, so the tool-call count and the
    // duration beside the figure are as true as ever — hiding the whole
    // row would take three measurements away to hide the one that was
    // never taken. `turn-cost.js`'s "cost unknown" rendering is the wrong
    // instrument here too: unknown is a failure to establish a price, and
    // this engine quotes none by design.
    const cost = supports(SURFACE.USD_COST) ? turn.cost : null;
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

  /**
   * Token rows — one per model that answered, prompt and completion apart.
   *
   * The split is the point. A price alone cannot be checked against
   * anything, and one aggregate token count cannot either: 50k tokens is
   * a cheap turn if they were cache reads and an expensive one if they
   * were output, and those are the two turns this row tells apart.
   *
   * `↑` is the whole prompt — the uncached part and the cached part
   * together — because in 300px there is no room for three numbers and a
   * model name, and the sum is the honest one to show. The tooltip
   * carries the breakdown, and the chat panel's turn footer shows the
   * cache column in full.
   *
   * **The headline is a count, never a total.** Summing the rows is the one
   * thing this section may not do — "the expensive model did a little and
   * the cheap model did a lot" is the shape of a well-delegated turn, and a
   * single number erases it (§ Per-Model Rows Are Not Summed). A count says
   * how many rows are hidden without claiming anything about them, and a
   * one-model turn gets no headline at all because "1 model" is the model
   * name already on the HUD's own header.
   */
  _renderTokens() {
    const lines = this._turn?.usage;
    if (!Array.isArray(lines) || lines.length === 0) return '';
    const headline = lines.length > 1
      ? html`<span class="label">${lines.length} models</span>`
      : null;
    return this._section('Per-model usage', 'Per-model usage', headline, () => lines.map((line) => html`
      <div class="row">
        <span class="label token-model" title=${line.model}>${line.model}</span>
        <span class="value token-value" title=${this._tokenTitle(line)}>
          ↑ ${_fmtTokens(line.prompt)} · ↓ ${_fmtTokens(line.output)}
        </span>
      </div>
    `));
  }

  /**
   * Rate limits — the subscription-mode cost signal.
   *
   * Rendered from the standing record whatever its status, which is the
   * decision `rate-limit.js` argues: under a subscription the dollar figures
   * above stop meaning anything and this is what replaces them, so gating it
   * on `allowed_warning` would leave the HUD with nothing to say in exactly
   * the billing mode R-6 is about. The chat panel's toast is the alarm; this
   * is the gauge.
   *
   * Absent rather than empty when there is nothing true to show — before the
   * CLI has ever sent a record, and after the window it describes has reset.
   * A stale utilisation is worse than no utilisation, because nothing else on
   * screen contradicts it.
   */
  _renderRateLimit() {
    const rl = this._rateLimit;
    if (!hasSomethingToSay(rl)) return '';
    const pct = utilizationPercent(rl);
    const type = limitTypeLabel(rl.rate_limit_type);
    const rejected = rl.status === 'rejected';
    // `rejected` is red whatever the figure says. A limit can be refused at a
    // utilisation the bands would call healthy — an overage cut-off is the
    // case — and the colour reports the outcome, not the arithmetic.
    const color = rejected ? '#f85149' : _contextColor(pct ?? 0);
    const resets = formatResetTime(rl.resets_at);
    const headline = html`
      <span class="value" style="color: ${color}">
        ${pct != null ? `${pct.toFixed(0)}%` : (rejected ? 'reached' : '—')}
      </span>
    `;
    const name = type ? `${type} limit` : 'Rate limit';
    return this._section('Rate limits', name, headline, () => html`
      ${pct != null ? html`
        <div class="bar-wrap">
          <div
            class="bar"
            role="img"
            aria-label="${name} ${pct.toFixed(0)} percent used"
          >
            <div class="bar-seg" style="width: ${pct}%; background: ${color};"></div>
          </div>
        </div>
      ` : nothing}
      ${rejected ? html`
        <div class="rl-note rl-rejected">
          Limit reached${resets ? ` — resets ${resets}` : ''}.
        </div>
      ` : resets ? html`
        <div class="rl-note">Resets ${resets}</div>
      ` : nothing}
      ${this._renderOverage(rl)}
    `);
  }

  /**
   * Pay-as-you-go, when the record says anything about it.
   *
   * One line, not a second gauge. Overage is a fallback rather than a budget
   * — what the reader needs is whether it is there to fall back on — and the
   * CLI reports it as a status and a reason rather than a figure.
   *
   * `overage_disabled_reason` is printed in the CLI's own words. It is the
   * answer to the only question this line raises, and paraphrasing a reason
   * we have never enumerated would be inventing one.
   *
   * Its underscores are dropped, which is not a paraphrase — the words are
   * still the CLI's, and `org_level_disabled` in running prose is a machine
   * token that reads as a leaked identifier rather than an explanation. The
   * Context tab has always spelled the same field this way; this line was
   * the one showing the raw form, so the two surfaces disagreed about a
   * value they both read from `rate_limit`.
   */
  _renderOverage(rl) {
    const status = rl.overage_status;
    if (typeof status !== 'string' || !status) return nothing;
    const resets = formatResetTime(rl.overage_resets_at);
    if (status === 'rejected') {
      const why = typeof rl.overage_disabled_reason === 'string'
        && rl.overage_disabled_reason
        ? ` — ${rl.overage_disabled_reason.replace(/_/g, ' ')}`
        : '';
      return html`<div class="rl-note rl-rejected">Overage unavailable${why}</div>`;
    }
    const warning = status === 'allowed_warning' ? ' (near its own limit)' : '';
    return html`
      <div class="rl-note">
        Overage available${warning}${resets ? `, resets ${resets}` : ''}
      </div>
    `;
  }

  /**
   * What the turn changed, one click from the diff.
   *
   * The most useful thing to know in the moment after an agentic turn lands
   * is which files moved, and the answer is only useful if it is one click
   * from the diff — so these are the same chips the tool-card footer draws,
   * following the same house rule: repo-relative label, engine's absolute
   * path on the tooltip, and `detail.path` left exactly as the engine sent
   * it. `onNavigateFile` normalises, and it is the one place that should; a
   * second conversion here would make a display concern into the navigation
   * contract (specs5/next.md § C4).
   *
   * Deduplicated, because a turn that edits one file three times reports it
   * three times, and on the raw path rather than the label — two spellings
   * of one file would otherwise survive as two chips right up until they
   * rendered identically.
   *
   * The HUD is not dismissed by a click. Unlike the Context tab's memory
   * rows, which minimise the dialog because it is opaque over the viewer,
   * this is a small corner overlay: hovering it has already stopped the
   * auto-hide, so a reader opening three files in turn keeps the list, and
   * it fades on its own once they move away.
   */
  _renderFiles() {
    const files = this._turn?.filesModified;
    if (!Array.isArray(files) || files.length === 0) return '';
    const unique = [...new Set(files)];
    const headline = html`<span class="label">${unique.length}</span>`;
    return this._section('Files modified', 'Files modified', headline, () => html`
      <div class="files">
        ${unique.map((path) => html`
          <span
            class="file-chip"
            title="Open ${path}"
            @click=${() => {
              window.dispatchEvent(new CustomEvent('navigate-file', {
                detail: { path },
                bubbles: false,
              }));
            }}
          >${toRepoPath(path)}</span>
        `)}
      </div>
    `);
  }

  /** Every counter behind one token row, unrounded. */
  _tokenTitle(line) {
    const n = (value) => Math.round(value).toLocaleString();
    const parts = [`${n(line.input)} input at full price`];
    if (line.cacheRead > 0) {
      parts.push(`${n(line.cacheRead)} cache read`);
    }
    if (line.cacheCreation > 0) {
      parts.push(`${n(line.cacheCreation)} cache write`);
    }
    parts.push(`${n(line.output)} output`);
    return `${n(line.tokens)} tokens: ${parts.join(', ')}. `
      + `↑ is the whole prompt (${n(line.prompt)}), cached part included.`;
  }
}

customElements.define('aic-usage-hud', UsageHud);
