// CompactionProgress — the pause while the engine compacts, made visible.
//
// A component by this name existed before and was deleted with the native
// engine, on the reasoning that a progress bar over someone else's compaction
// would be an animation rather than a measurement. That reasoning still holds
// and this component honours it: there is no percentage here and never can be.
// The CLI runs its summarisation call internally and reports nothing between
// starting and finishing, so anything claiming to be "62% compacted" would be
// invented.
//
// What was wrong was the conclusion. The pause is real — tens of seconds on a
// long session, during which the engine emits nothing at all — and the only
// thing announcing it was a toast that auto-dismisses after 3 seconds (see
// app-shell/toasts.js). A notice that expires long before the condition it
// describes leaves the user looking at a hung UI, which is the exact failure
// the notice existed to prevent. So: an indeterminate indicator, held for the
// real duration, with an elapsed-seconds counter — the honest half of a
// progress bar. Same shape the Claude Code CLI shows for its own compaction.
//
// Four signals, and which one starts the indicator is the whole subtlety:
//
//   - `compaction-event` with `stage: "compaction_started"` — the engine's own
//     status frame, and the authoritative start. Emitted only when the session
//     is actually waiting on a compaction.
//   - `system-event` with `subtype: "pre_compact"` — our PreCompact hook
//     (claude_code/hooks.py). Earlier than the status frame, and *ambiguous*:
//     the CLI runs the same hook for its speculative background compaction,
//     precomputed well ahead of the threshold and frequently discarded without
//     compacting anything. A hook firing is therefore a maybe, not a start.
//   - `compaction-event` with `stage: "compaction_ended"` — the status frame's
//     other half, carrying `result` and, when the CLI is willing to say, an
//     error. The only report a *failed* compaction produces at all.
//   - `compaction-event` with `stage: "compact_boundary"` — the boundary
//     record, which is emitted on success and carries the token counts.
//
// So the hook opens a grace period rather than the indicator: hold it back
// briefly, and if the engine confirms with a status frame in that window —
// which a real compaction does, the two being milliseconds apart — show the
// pause with the hook's own start time, so the elapsed counter is honest about
// when the wait began. A hook that is never confirmed showed nothing.
//
// The fallback matters as much as the fast path: an engine that emits no
// status frames at all (an older CLI) still gets an indicator when the grace
// period expires, on the hook alone. That one is marked unconfirmed and gives
// up quietly after `_UNCONFIRMED_MAX_MS` rather than accusing the engine of
// hanging, because on this engine an unconfirmed start is far more likely to
// be a background precompute than a stall.
//
// The caption on completion comes from `compactionSummary`, the same builder
// the transcript divider uses, so the overlay and the divider cannot end up
// describing one compaction two different ways.
//
// Scope boundaries — what this component does NOT do:
//   - Replace the divider. The divider is the durable record in scrollback;
//     this is the live indicator, and it fades.
//   - Announce a `compact_boundary` it saw no start for. Microcompaction can
//     report a boundary without a PreCompact hook ever firing, and there is no
//     pause to explain after the fact — the divider covers it.
//   - Offer cancel. Compaction is the engine's and is not interruptible from
//     here.

import { LitElement, css, html } from 'lit';

import { compactionSummary } from './chat-panel/block-render.js';

/** How long the completion caption stays before fade-out begins. */
const _SUCCESS_DISPLAY_MS = 1600;

/** CSS transition duration for fade-out. Matches the style rule. */
const _FADE_DURATION_MS = 400;

/** How long a warning caption stays — longer, because it has to be read. */
const _WARNING_DISPLAY_MS = 5000;

/**
 * Ceiling on a confirmed active state.
 *
 * A spinner is a claim that something is still happening, and the claim rests
 * entirely on a `compact_boundary` arriving to retract it. If the turn dies
 * mid-compaction, or the engine goes away, or a CLI upgrade renames the
 * subtype, nothing ever does — and a spinner that runs forever is worse than
 * the 3-second toast it replaced. At the ceiling the component says it lost
 * track and gets out of the way.
 */
const _MAX_ACTIVE_MS = 180000;

/**
 * How long a PreCompact hook waits for the engine to confirm it.
 *
 * Short, because on a real compaction the confirming status frame follows the
 * hook by milliseconds — the CLI runs the hooks and then starts summarising.
 * Long enough that a background precompute, which is never confirmed, never
 * reaches the screen at all.
 */
const _HOOK_GRACE_MS = 1500;

/**
 * Ceiling on an *unconfirmed* active state — one showing on the hook alone.
 *
 * Not the same failure as `_MAX_ACTIVE_MS` and so not the same duration or the
 * same exit. This one is most likely a background precompute the engine never
 * announced, which is not a stall and not an error, so it leaves without a
 * caption rather than warning about an engine that is working normally.
 */
const _UNCONFIRMED_MAX_MS = 15000;

export class CompactionProgress extends LitElement {
  static properties = {
    /**
     * `hidden` renders nothing at all; `active` is the pause itself;
     * `success` and `warning` are captions on the way out.
     */
    _state: { type: String, state: true },
    /** Elapsed seconds since `pre_compact` fired. */
    _elapsed: { type: Number, state: true },
    /** Normalised compaction trigger — `automatic`, `manual`, or null. */
    _trigger: { type: String, state: true },
    /** Completion caption. Only read in `success` / `warning`. */
    _caption: { type: String, state: true },
    /** True while the fade-out CSS transition is running. */
    _fading: { type: Boolean, state: true },
  };

  static styles = css`
    :host {
      /* Inline in the dialog, directly above the context-capacity bar, so
       * the two compaction-related strips read as one region: how close we
       * are to compacting, and — when it happens — that it is happening.
       * No fixed positioning; the row flows with the dialog layout.
       *
       * Deliberately NOT hidden by the .dialog.minimized rules that hide
       * the body and the capacity bar: a user who collapsed the dialog and
       * is waiting on a turn is precisely the one with no other way to tell
       * a compaction from a hang. */
      display: block;
      flex-shrink: 0;
    }

    .overlay {
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      background: rgba(22, 27, 34, 0.96);
      border-top: 1px solid rgba(240, 246, 252, 0.1);
      border-left: 3px solid var(--accent-primary, #58a6ff);
      padding: 0.5rem 1rem;
      font-size: 0.8125rem;
      color: var(--text-primary, #c9d1d9);
      opacity: 1;
      transition: opacity 400ms ease-out;
    }

    .overlay.fading {
      opacity: 0;
    }

    .overlay.success {
      border-left-color: #7ee787;
    }

    .overlay.warning {
      border-left-color: #d29922;
    }

    .row {
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }

    .spinner {
      width: 14px;
      height: 14px;
      border: 2px solid rgba(240, 246, 252, 0.2);
      border-top-color: var(--accent-primary, #58a6ff);
      border-radius: 50%;
      animation: spin 800ms linear infinite;
      flex-shrink: 0;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .glyph {
      font-size: 1rem;
      line-height: 1;
      flex-shrink: 0;
    }

    /* Active: one line, clipped. The elapsed counter shares the row and has
     * to keep its place at the right edge, so the label yields. */
    .label {
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Caption: wraps instead. Nothing shares the row once the counter has
     * stopped, and a narrow dialog was ellipsising the warning at exactly
     * the clause that says what went wrong. */
    .label.caption {
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
    }

    .elapsed {
      opacity: 0.6;
      font-variant-numeric: tabular-nums;
      flex-shrink: 0;
    }

    .bar {
      height: 3px;
      background: rgba(240, 246, 252, 0.08);
      border-radius: 2px;
      overflow: hidden;
    }

    .bar-fill {
      height: 100%;
      background: var(--accent-primary, #58a6ff);
    }

    /* Indeterminate: a short fill sweeping the track. It carries no
     * quantity — its whole job is to say "still working" without implying
     * how far along, which is the one thing we do not know. */
    .bar-fill.indeterminate {
      width: 35%;
      animation: sweep 1400ms ease-in-out infinite;
    }

    @keyframes sweep {
      from { transform: translateX(-100%); }
      to { transform: translateX(285%); }
    }

    /* Success collapses the sweep into a settled full bar. */
    .bar-fill.done {
      width: 100%;
      background: #7ee787;
    }

    /* A sweeping bar plus a spinning ring is two moving things for one
     * fact. Under reduced motion the bar holds still and breathes. */
    @media (prefers-reduced-motion: reduce) {
      .spinner {
        animation: none;
        border-top-color: var(--accent-primary, #58a6ff);
      }
      .bar-fill.indeterminate {
        width: 100%;
        animation: breathe 2400ms ease-in-out infinite;
      }
      @keyframes breathe {
        0%, 100% { opacity: 0.25; }
        50% { opacity: 0.75; }
      }
    }
  `;

  constructor() {
    super();
    this._state = 'hidden';
    this._elapsed = 0;
    this._trigger = null;
    this._caption = '';
    this._fading = false;
    // True once the engine's own status frame has said a compaction is
    // running. Governs which ceiling applies and how the state exits at it.
    this._confirmed = false;
    // Interval for the elapsed counter; timeouts for the hook's grace period,
    // the ceiling, and the caption → fade → hidden chain. All cleared on
    // transition and disconnect, so a disconnect mid-chain cannot tick a
    // detached element.
    this._tickInterval = null;
    this._pendingTimer = null;
    this._ceilingTimer = null;
    this._exitTimer = null;
    this._fadeTimer = null;
    this._onSystemEvent = this._onSystemEvent.bind(this);
    this._onCompactionEvent = this._onCompactionEvent.bind(this);
    this._onStateLoaded = this._onStateLoaded.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('system-event', this._onSystemEvent);
    window.addEventListener('compaction-event', this._onCompactionEvent);
    window.addEventListener('state-loaded', this._onStateLoaded);
  }

  disconnectedCallback() {
    window.removeEventListener('system-event', this._onSystemEvent);
    window.removeEventListener('compaction-event', this._onCompactionEvent);
    window.removeEventListener('state-loaded', this._onStateLoaded);
    this._clearTimers();
    super.disconnectedCallback();
  }

  // ---------------------------------------------------------------
  // Event handling
  // ---------------------------------------------------------------

  /**
   * A maybe-start: `systemEvent` with `subtype: "pre_compact"`.
   *
   * Opens the grace period rather than the indicator — see the header note on
   * why a PreCompact hook is not evidence that anything is stalling. An
   * already-running indicator is left alone: the CLI runs the hooks a second
   * time when it consumes a precomputed summary, and restarting the clock
   * there would reset an elapsed counter mid-wait.
   *
   * Every other subtype on this channel belongs to someone else — a
   * conversation reset, an unrecognised message type — and is ignored here.
   *
   * No request-id filter. Compaction is the session's context being rewritten,
   * not a property of one turn, and a collaborator's turn triggering it stalls
   * this browser exactly the same way.
   */
  _onSystemEvent(event) {
    const data = event.detail?.data;
    if (!data || typeof data !== 'object') return;
    if (data.subtype !== 'pre_compact') return;
    if (this._state === 'active' || this._pendingTimer != null) return;
    // `hooks.py` nests the hook's own fields one level down, under `data`.
    const raw = data.data && typeof data.data === 'object'
      ? data.data.trigger
      : null;
    // Normalised through the divider's builder rather than by a local map:
    // "auto" reads as "automatic" in both places or the two disagree about
    // the same compaction 20 seconds apart.
    this._enterPending(compactionSummary({ trigger: raw }).trigger);
  }

  /**
   * The engine's own compaction reports, all on the `compaction-event`
   * channel and distinguished by `stage`:
   *
   *   - `compaction_started` — a compaction is running *now*. Confirms a
   *     pending hook (keeping its earlier start time) or, when the hook never
   *     fired, starts the indicator on its own.
   *   - `compaction_ended` with `result: "failed"` — the one failure report
   *     there is. Shown even if the indicator never became visible: a
   *     compaction that failed means the context was not reclaimed, which the
   *     user finds out about the hard way on the next turn otherwise.
   *   - `compaction_ended` with `result: "success"` — retracts the indicator
   *     if the boundary has not already done it, with a plainer caption
   *     because this frame carries no token counts.
   *   - `compact_boundary` — the end, with counts. Preferred for the caption
   *     wherever both arrive, in either order.
   *
   * A boundary or a success with no start showing is left alone. It is either
   * a microcompaction (which does not stall visibly) or a start we missed, and
   * in both cases the pause is over — flashing a completion caption for it
   * would announce a wait that never happened. The transcript divider records
   * it either way.
   *
   * Doc-enrichment stages share this channel and fall through untouched.
   */
  /**
   * A compaction that started before this browser was listening.
   *
   * The four signals above are all *live* — they say what the engine is
   * doing now, to whoever happens to be connected. Refresh the page during
   * the pause and every one of them has already been and gone, so the
   * indicator that existed to explain a long silence was itself erased by
   * the one action a user watching a silent UI is most likely to take.
   *
   * `get_current_state` now carries the pause as state rather than only as
   * a broadcast, so the shell's `state-loaded` restores it. Treated as
   * confirmed, because the server only sets it from the engine's own status
   * frame — never from the ambiguous `PreCompact` hook — so this can never
   * be a speculative background precompute.
   *
   * The elapsed seconds come from the server already computed. Sending a
   * start timestamp would have made the browser difference two clocks, and
   * a collaborating client can be on another machine.
   *
   * Ignored while something is already on screen: a live signal is at least
   * as fresh as a snapshot, and re-entering would re-phase the counter.
   */
  _onStateLoaded(event) {
    if (this._state !== 'hidden' || this._pendingTimer != null) return;
    const compaction = event.detail?.compaction;
    if (!compaction || typeof compaction !== 'object') return;
    const elapsed = Number(compaction.elapsed_seconds);
    if (!Number.isFinite(elapsed) || elapsed < 0) return;
    // No trigger: the server sets this from the status frame, which does not
    // carry one. The caption says how long, not why.
    this._enterActive(null, { elapsed: Math.round(elapsed) });
    this._confirm();
  }

  _onCompactionEvent(event) {
    const payload = event.detail?.event;
    if (!payload || typeof payload !== 'object') return;
    switch (payload.stage) {
      case 'compaction_started':
        if (this._state === 'active') {
          // Already showing on the hook — promote it rather than restart it,
          // so the counter keeps counting from when the wait began.
          this._confirm();
          return;
        }
        this._enterActive(this._trigger, { keepClock: this._pendingTimer != null });
        this._confirm();
        return;
      case 'compaction_ended':
        if (payload.result === 'failed') {
          const detail = typeof payload.error === 'string' && payload.error.trim()
            ? `: ${payload.error.trim()}`
            : '';
          this._enterWarning(`Compaction failed${detail}`);
          return;
        }
        if (payload.result === 'success' && this._state === 'active') {
          this._enterSuccess(compactionSummary({}).text);
        }
        return;
      case 'compact_boundary': {
        const { text } = compactionSummary(payload);
        if (this._state === 'active') {
          this._enterSuccess(text);
        } else if (this._state === 'success') {
          // `compaction_ended` got here first with the caption that has no
          // counts. Same compaction, better sentence — swap it without
          // restarting the fade, which is already scheduled.
          this._caption = text;
        }
        return;
      }
      default:
        return;
    }
  }

  // ---------------------------------------------------------------
  // State transitions
  // ---------------------------------------------------------------

  /**
   * A hook has fired and nothing is on screen yet.
   *
   * The clock starts here even though the indicator does not, so that a
   * confirmation arriving a second later shows the elapsed time the user has
   * actually been waiting rather than restarting from zero.
   */
  _enterPending(trigger) {
    this._clearTimers();
    this._state = 'hidden';
    this._elapsed = 0;
    this._trigger = trigger || null;
    this._caption = '';
    this._fading = false;
    this._confirmed = false;
    this._startClock();
    this._pendingTimer = setTimeout(() => {
      this._pendingTimer = null;
      // Unconfirmed at the end of the grace period. Shown anyway — an engine
      // that emits no status frames would otherwise never show one — but on
      // the shorter, quieter ceiling.
      this._enterActive(this._trigger, { keepClock: true });
    }, _HOOK_GRACE_MS);
  }

  /**
   * `keepClock` continues a wait that began at the hook — the interval itself
   * is left running rather than restarted, because restarting it re-phases the
   * ticks and silently drops the fraction of a second already counted.
   */
  _enterActive(trigger, { keepClock = false, elapsed = 0 } = {}) {
    this._clearTimers({ keepClock });
    this._state = 'active';
    // `elapsed` seeds a wait that began before this component was watching —
    // a page refreshed mid-compaction. Zero for every live path, which is
    // every path that saw the start itself.
    if (!keepClock) this._elapsed = elapsed;
    this._trigger = trigger || null;
    this._caption = '';
    this._fading = false;
    this._confirmed = false;
    if (!keepClock) this._startClock();
    this._armCeiling();
  }

  /**
   * The engine says a compaction really is running.
   *
   * Re-arms the ceiling rather than merely setting a flag: the two ceilings
   * differ in length and in how they exit, and a confirmation that arrived
   * after the grace period would otherwise keep the unconfirmed one.
   */
  _confirm() {
    if (this._confirmed) return;
    this._confirmed = true;
    if (this._ceilingTimer != null) {
      clearTimeout(this._ceilingTimer);
      this._ceilingTimer = null;
    }
    this._armCeiling();
  }

  /**
   * The ceiling is a budget for the *whole* compaction, not for this
   * component's view of it. A restored indicator arrives with seconds
   * already on the clock, and granting it a fresh 180 would let a
   * compaction that died before the refresh sit there for three more
   * minutes claiming to be working. A floor of one second keeps a restore
   * that arrives past the budget from firing synchronously inside the
   * handler that set the state.
   */
  _armCeiling() {
    const confirmed = this._confirmed;
    const budget = confirmed ? _MAX_ACTIVE_MS : _UNCONFIRMED_MAX_MS;
    const remaining = Math.max(1000, budget - this._elapsed * 1000);
    this._ceilingTimer = setTimeout(() => {
      this._ceilingTimer = null;
      if (confirmed) {
        this._enterWarning(
          'Compaction has not reported finishing — the engine may have '
          + 'stopped mid-compaction',
        );
      } else {
        this._hide();
      }
    }, remaining);
  }

  /** First visible value is 1s, a second in — how a stopwatch reads. */
  _startClock() {
    this._tickInterval = setInterval(() => {
      this._elapsed = this._elapsed + 1;
    }, 1000);
  }

  /** Straight to nothing, no caption and no fade. */
  _hide() {
    this._clearTimers();
    this._state = 'hidden';
    this._elapsed = 0;
    this._trigger = null;
    this._caption = '';
    this._fading = false;
    this._confirmed = false;
  }

  _enterSuccess(caption) {
    this._clearTimers();
    this._state = 'success';
    this._trigger = null;
    this._caption = caption;
    this._fading = false;
    this._confirmed = false;
    this._scheduleExit(_SUCCESS_DISPLAY_MS);
  }

  _enterWarning(caption) {
    this._clearTimers();
    this._state = 'warning';
    this._trigger = null;
    this._caption = caption;
    this._fading = false;
    this._confirmed = false;
    this._scheduleExit(_WARNING_DISPLAY_MS);
  }

  /**
   * Start the caption → fade → hidden chain.
   *
   * displayMs after entry → `_fading = true` (CSS takes opacity 1 → 0).
   * displayMs + _FADE_DURATION_MS → `_state = 'hidden'`, so the element
   *   stops rendering and `_fading` is clean for the next cycle.
   */
  _scheduleExit(displayMs) {
    this._exitTimer = setTimeout(() => {
      this._exitTimer = null;
      this._fading = true;
      this._fadeTimer = setTimeout(() => {
        this._fadeTimer = null;
        this._state = 'hidden';
        this._fading = false;
      }, _FADE_DURATION_MS);
    }, displayMs);
  }

  _clearTimers({ keepClock = false } = {}) {
    if (this._tickInterval != null && !keepClock) {
      clearInterval(this._tickInterval);
      this._tickInterval = null;
    }
    for (const key of ['_pendingTimer', '_ceilingTimer', '_exitTimer', '_fadeTimer']) {
      if (this[key] != null) {
        clearTimeout(this[key]);
        this[key] = null;
      }
    }
  }

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------

  render() {
    if (this._state === 'hidden') return html``;
    const classes = [
      'overlay',
      this._state === 'success' ? 'success' : '',
      this._state === 'warning' ? 'warning' : '',
      this._fading ? 'fading' : '',
    ].filter(Boolean).join(' ');
    return html`
      <div class=${classes} role="status" aria-live="polite">
        <div class="row">
          ${this._renderIcon()}
          ${this._renderLabel()}
          ${this._renderElapsed()}
        </div>
        ${this._renderBar()}
      </div>
    `;
  }

  _renderIcon() {
    if (this._state === 'active') {
      return html`<div class="spinner" aria-hidden="true"></div>`;
    }
    if (this._state === 'success') {
      return html`<span class="glyph" aria-hidden="true">✓</span>`;
    }
    return html`<span class="glyph" aria-hidden="true">⚠</span>`;
  }

  _renderLabel() {
    if (this._state !== 'active') {
      return html`<span class="label caption">${this._caption}</span>`;
    }
    // The trigger is worth a word: an automatic compaction is the context
    // window filling up, which the capacity bar below has been warning
    // about, while a manual one is something the user just asked for.
    const trigger = this._trigger ? ` (${this._trigger})` : '';
    return html`<span class="label">Compacting conversation…${trigger}</span>`;
  }

  _renderElapsed() {
    if (this._state !== 'active') return '';
    if (this._elapsed <= 0) return '';
    return html`<span class="elapsed">${this._elapsed}s</span>`;
  }

  _renderBar() {
    if (this._state === 'active') {
      return html`
        <div
          class="bar"
          role="progressbar"
          aria-label="Compacting conversation"
        >
          <div class="bar-fill indeterminate"></div>
        </div>
      `;
    }
    if (this._state === 'success') {
      return html`
        <div class="bar"><div class="bar-fill done"></div></div>
      `;
    }
    // Warning: the bar would be claiming progress on something we have just
    // said we lost track of.
    return '';
  }
}

customElements.define('aic-compaction-progress', CompactionProgress);
