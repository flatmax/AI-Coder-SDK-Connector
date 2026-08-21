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
// Two channels, one for each end of the pause:
//
//   - `system-event` with `subtype: "pre_compact"` — our PreCompact hook
//     (claude_code/hooks.py), the only signal that arrives *before* the stall.
//   - `compaction-event` with `stage: "compact_boundary"` — the stream's own
//     report, which arrives when compaction has finished.
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
 * Ceiling on the active state.
 *
 * A spinner is a claim that something is still happening, and the claim rests
 * entirely on a `compact_boundary` arriving to retract it. If the turn dies
 * mid-compaction, or the engine goes away, or a CLI upgrade renames the
 * subtype, nothing ever does — and a spinner that runs forever is worse than
 * the 3-second toast it replaced. At the ceiling the component says it lost
 * track and gets out of the way.
 */
const _MAX_ACTIVE_MS = 180000;

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
    // Interval for the elapsed counter; timeouts for the ceiling and for the
    // caption → fade → hidden chain. All cleared on transition and disconnect,
    // so a disconnect mid-chain cannot tick a detached element.
    this._tickInterval = null;
    this._ceilingTimer = null;
    this._exitTimer = null;
    this._fadeTimer = null;
    this._onSystemEvent = this._onSystemEvent.bind(this);
    this._onCompactionEvent = this._onCompactionEvent.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('system-event', this._onSystemEvent);
    window.addEventListener('compaction-event', this._onCompactionEvent);
  }

  disconnectedCallback() {
    window.removeEventListener('system-event', this._onSystemEvent);
    window.removeEventListener('compaction-event', this._onCompactionEvent);
    this._clearTimers();
    super.disconnectedCallback();
  }

  // ---------------------------------------------------------------
  // Event handling
  // ---------------------------------------------------------------

  /**
   * Start of the pause: `systemEvent` with `subtype: "pre_compact"`.
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
    // `hooks.py` nests the hook's own fields one level down, under `data`.
    const raw = data.data && typeof data.data === 'object'
      ? data.data.trigger
      : null;
    // Normalised through the divider's builder rather than by a local map:
    // "auto" reads as "automatic" in both places or the two disagree about
    // the same compaction 20 seconds apart.
    this._enterActive(compactionSummary({ trigger: raw }).trigger);
  }

  /**
   * End of the pause: `compactionEvent` with `stage: "compact_boundary"`.
   *
   * Ignored unless we are actually showing a pause. A boundary with no start
   * is either a microcompaction (which does not stall visibly) or a start we
   * missed, and in both cases the pause is already over — flashing a
   * completion caption for it would announce a wait that never happened. The
   * transcript divider records it either way.
   *
   * Doc-enrichment stages share this channel and fall through untouched.
   */
  _onCompactionEvent(event) {
    const payload = event.detail?.event;
    if (!payload || typeof payload !== 'object') return;
    if (payload.stage !== 'compact_boundary') return;
    if (this._state !== 'active') return;
    this._enterSuccess(compactionSummary(payload).text);
  }

  // ---------------------------------------------------------------
  // State transitions
  // ---------------------------------------------------------------

  _enterActive(trigger) {
    this._clearTimers();
    this._state = 'active';
    this._elapsed = 0;
    this._trigger = trigger || null;
    this._caption = '';
    this._fading = false;
    // First visible value is 1s, a second in — how a stopwatch reads.
    this._tickInterval = setInterval(() => {
      this._elapsed = this._elapsed + 1;
    }, 1000);
    this._ceilingTimer = setTimeout(() => {
      this._ceilingTimer = null;
      this._enterWarning(
        'Compaction has not reported finishing — the engine may have '
        + 'stopped mid-compaction',
      );
    }, _MAX_ACTIVE_MS);
  }

  _enterSuccess(caption) {
    this._clearTimers();
    this._state = 'success';
    this._caption = caption;
    this._fading = false;
    this._scheduleExit(_SUCCESS_DISPLAY_MS);
  }

  _enterWarning(caption) {
    this._clearTimers();
    this._state = 'warning';
    this._caption = caption;
    this._fading = false;
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

  _clearTimers() {
    if (this._tickInterval != null) {
      clearInterval(this._tickInterval);
      this._tickInterval = null;
    }
    for (const key of ['_ceilingTimer', '_exitTimer', '_fadeTimer']) {
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

customElements.define('ac-compaction-progress', CompactionProgress);
