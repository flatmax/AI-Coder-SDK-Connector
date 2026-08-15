// <ac-permission-dialog> — the browser surface for `can_use_tool`.
//
// This is the component that justifies a browser frontend over a
// terminal: what the user sees is the consequence of the call, not its
// name. Governing spec: specs5/5-webapp/permission-dialog.md. The
// engine-side contract is specs5/3-engine/permissions.md, implemented in
// src/ac_dc/claude_code/permissions.py.
//
// The invariants that shape the code, in the order they cost most to get
// wrong:
//
//   1. **A request resolves exactly once, and never silently.** Every
//      exit from the queue is a `resolve_permission` call, a broadcast
//      `permissionResolved`, or expiry. There is no path that closes a
//      dialog without one of those, because a dismissed-but-unresolved
//      dialog leaves the turn stalled behind something the user believes
//      they closed.
//   2. **Escape denies; the scrim does nothing.** A stray Escape is
//      recoverable — the agent gets a reason and can ask again. A stray
//      click on a modal over a UI the user was mid-gesture in is not.
//   3. **Nothing holds focus during the settling interval**, and
//      Enter/Space are swallowed for its duration, on first appearance
//      and on reconnect alike. A keystroke already in flight when the
//      dialog opened must not be able to approve anything.
//   4. **Only localhost gets decision controls**, but every client gets
//      the full body. The restriction is on authority, not information.

import { LitElement, html } from 'lit';

import { RpcMixin } from '../rpc-mixin.js';
import { PERMISSION_DIALOG_STYLES } from './styles.js';
import {
  ALLOW_ACTIONS,
  ANNOUNCE_AT_SECONDS,
  CHIME_SETTING_KEY,
  CLASS_GLYPHS,
  COUNTDOWN_TICK_MS,
  ESCAPE_DENY_REASON,
  SETTLING_MS,
  TITLE_MARKER,
} from './constants.js';
import {
  arrivalAnnouncement,
  countdownUrgency,
  defaultDenyReason,
  defaultFocusTarget,
  formatCountdown,
  headerTarget,
  interactQuestions,
  orderQueue,
  secondsRemaining,
} from './queue.js';
import { renderBody, formatJson } from './bodies.js';
import { renderDecisions } from './decisions.js';
import {
  currentProposedContent,
  disposeDiffEditor,
  refreshEditMode,
  syncDiffEditor,
} from './diff-editor.js';

/**
 * Tools whose input carries the file's full content, and which can
 * therefore accept an edited proposal without guesswork.
 *
 * `Edit` and `MultiEdit` are deliberately absent: their input is a set
 * of replacements, and turning an edited full-file result back into
 * replacements means guessing at which one the user meant. A call that
 * ran something other than what the dialog showed would be worse than no
 * edit affordance at all. For those, deny with a reason.
 */
const FULL_CONTENT_TOOLS = { Write: 'content', NotebookEdit: 'new_source' };

export class PermissionDialog extends RpcMixin(LitElement) {
  static properties = {
    /** Pending requests as `{payload, arrivedAt}`, unordered. */
    _entries: { type: Array, state: true },
    /** Epoch ms, ticked once a second to drive the countdown. */
    _now: { type: Number, state: true },
    /** Epoch ms at which the settling interval ends. */
    _settleUntil: { type: Number, state: true },
    /** Whether this client may answer. False for participants. */
    _canDecide: { type: Boolean, state: true },
    /** Deny reason field open, and whether it stops the turn. */
    _denyOpen: { type: Boolean, state: true },
    _denyInterrupts: { type: Boolean, state: true },
    _denyReason: { type: String, state: true },
    /** Open ▾ menus. */
    _ruleMenuOpen: { type: Boolean, state: true },
    _denyMenuOpen: { type: Boolean, state: true },
    /** Which suggested rule the always-allow button carries. */
    _ruleIndex: { type: Number, state: true },
    /** Edit state for the two editable classes. */
    _editingDiff: { type: Boolean, state: true },
    _diffDirty: { type: Boolean, state: true },
    _editingCommand: { type: Boolean, state: true },
    _commandDraft: { type: String, state: true },
    /** `interact` answers: question index → chosen option indices. */
    _answers: { type: Object, state: true },
    /** `interact` freeform replies: question index → typed answer. */
    _answerTexts: { type: Object, state: true },
    /** Live-region text: arrival announcement or countdown milestone. */
    _announcement: { type: String, state: true },
  };

  static styles = PERMISSION_DIALOG_STYLES;

  constructor() {
    super();
    this._entries = [];
    this._now = Date.now();
    this._settleUntil = 0;
    this._canDecide = true;
    this._denyOpen = false;
    this._denyInterrupts = false;
    this._denyReason = '';
    this._ruleMenuOpen = false;
    this._denyMenuOpen = false;
    this._ruleIndex = 0;
    this._editingDiff = false;
    this._diffDirty = false;
    this._editingCommand = false;
    this._commandDraft = '';
    this._answers = new Map();
    this._answerTexts = new Map();
    this._announcement = '';

    /** Monotonic arrival counter — two requests can share a millisecond. */
    this._arrivalCounter = 0;
    /** permission_ids already seen, so a broadcast and a reconnect
        snapshot describing the same request cannot queue it twice. */
    this._seen = new Set();
    /** permission_ids resolved, so a late broadcast is a no-op rather
        than a re-open. */
    this._resolved = new Set();
    /** The permission_id the settling interval and edit state belong to. */
    this._currentId = null;
    /** Countdown milestones already announced, keyed by permission_id. */
    this._announced = new Map();

    this._diffEditor = null;
    this._diffKey = null;
    this._diffSubscriptions = [];

    this._tickTimer = null;
    this._settleTimer = null;
    /** What held focus before the first request, to give it back. */
    this._focusBeforeDialog = null;
    /** The document title before we prefixed it. */
    this._titleBeforeDialog = null;

    this._onPermissionRequest = this._onPermissionRequest.bind(this);
    this._onPermissionResolved = this._onPermissionResolved.bind(this);
    this._onRoleChanged = this._onRoleChanged.bind(this);
    this._onKeydownCapture = this._onKeydownCapture.bind(this);
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('permission-request', this._onPermissionRequest);
    window.addEventListener('permission-resolved', this._onPermissionResolved);
    window.addEventListener('role-changed', this._onRoleChanged);
    // Capture phase on `window`, which is the outermost target in the
    // bubble/capture path — so this handler runs before the chat panel's
    // @-filter, the snippet drawer, the input-clear chain, and the
    // lightbox, all of which bind on document or on their own roots.
    // That ordering is the spec's "Escape takes priority over every
    // other Escape binding in the application".
    window.addEventListener('keydown', this._onKeydownCapture, true);
  }

  disconnectedCallback() {
    window.removeEventListener('permission-request', this._onPermissionRequest);
    window.removeEventListener('permission-resolved', this._onPermissionResolved);
    window.removeEventListener('role-changed', this._onRoleChanged);
    window.removeEventListener('keydown', this._onKeydownCapture, true);
    this._stopTicking();
    if (this._settleTimer) clearTimeout(this._settleTimer);
    this._restoreTitle();
    disposeDiffEditor(this);
    super.disconnectedCallback();
  }

  /**
   * On connect and on every reconnect, rebuild from the server.
   *
   * A refresh mid-request must re-open the dialog with the time it
   * actually has left, not a fresh countdown — which is why the payload
   * carries `expires_at` rather than a duration
   * (permission-dialog.md § Reconnect).
   */
  async onRpcReady() {
    await this._probeAuthority();
    try {
      const state = await this.rpcExtract('ClaudeCodeService.get_current_state');
      const pending = state?.pending_permissions;
      if (Array.isArray(pending)) {
        for (const payload of pending) this._enqueue(payload);
      }
    } catch (err) {
      // A dialog that cannot rebuild its queue is bad; a component that
      // throws on connect and takes the shell's wiring with it is worse.
      console.warn('[permission-dialog] could not restore pending requests', err);
    }
  }

  /**
   * Ask the backend whether this client may answer.
   *
   * Defaults to "may" when there is no collab service, which is the
   * single-user case. It only ever *narrows* authority — the server
   * enforces the real gate in `resolve_permission`, so being wrong here
   * costs a rejected call, not an unauthorised one.
   */
  async _probeAuthority() {
    try {
      const role = await this.rpcExtract('Collab.get_collab_role');
      if (role && typeof role === 'object' && !role.error) {
        this._canDecide = role.is_localhost !== false;
      }
    } catch (_) {
      // No collab service registered — single-user, and we are the host.
      this._canDecide = true;
    }
  }

  _onRoleChanged(event) {
    const detail = event?.detail;
    if (detail && typeof detail.is_localhost === 'boolean') {
      this._canDecide = detail.is_localhost;
    }
  }

  // ------------------------------------------------------------------
  // Queue
  // ------------------------------------------------------------------

  /** Ordered queue: soonest expiry first, arrival breaking ties. */
  get queue() {
    return orderQueue(this._entries);
  }

  /** The one request on screen. Exactly one dialog is ever visible. */
  get current() {
    return this.queue[0]?.payload ?? null;
  }

  get _settling() {
    return this._now < this._settleUntil;
  }

  _onPermissionRequest(event) {
    const payload = event?.detail;
    if (payload) this._enqueue(payload);
  }

  _enqueue(payload) {
    const id = payload?.permission_id;
    if (!id || this._seen.has(id) || this._resolved.has(id)) return;
    this._seen.add(id);
    const wasEmpty = this._entries.length === 0;
    this._entries = [
      ...this._entries,
      { payload, arrivedAt: (this._arrivalCounter += 1) },
    ];
    if (wasEmpty) {
      this._captureFocus();
      this._startTicking();
      // One chime per empty→non-empty transition, not one per request:
      // a fan-out of nine gated calls must not produce nine chimes
      // (permission-dialog.md § Attention).
      this._alert();
    }
    this._updateTitle();
    this._now = Date.now();
    this._syncCurrent();
  }

  /**
   * Reset the per-request state when the visible request changes.
   *
   * Every field here belongs to one request. Carrying a half-typed deny
   * reason across to the next one is the mistake the spec calls out for
   * racing clients — "the losing client's in-progress reason text is
   * discarded, not resubmitted against the next request in the queue".
   */
  _syncCurrent() {
    const payload = this.current;
    const id = payload?.permission_id ?? null;
    if (id === this._currentId) return;
    this._currentId = id;
    this._denyOpen = false;
    this._denyInterrupts = false;
    this._denyReason = '';
    this._ruleMenuOpen = false;
    this._denyMenuOpen = false;
    this._ruleIndex = 0;
    this._editingDiff = false;
    this._diffDirty = false;
    this._editingCommand = false;
    this._commandDraft = payload?.command?.command ?? '';
    this._answers = new Map();
    this._answerTexts = new Map();
    if (!payload) {
      this._settleUntil = 0;
      return;
    }
    // The settling interval applies to every request, including one
    // reconstructed from a reconnect snapshot: a page load should not be
    // able to approve anything either.
    this._settleUntil = Date.now() + SETTLING_MS;
    this._announcement = arrivalAnnouncement(payload);
    if (this._settleTimer) clearTimeout(this._settleTimer);
    this._settleTimer = setTimeout(() => {
      this._settleTimer = null;
      this._now = Date.now();
      this.requestUpdate();
      this.updateComplete.then(() => this._focusDefault());
    }, SETTLING_MS + 20);
  }

  /**
   * Drop a request from the queue. Callers must have resolved it first —
   * this is the bookkeeping half, never the whole exit.
   */
  _dequeue(permissionId) {
    if (!permissionId) return;
    this._resolved.add(permissionId);
    this._announced.delete(permissionId);
    const before = this._entries.length;
    this._entries = this._entries.filter(
      (entry) => entry.payload.permission_id !== permissionId,
    );
    if (this._entries.length === before) return;
    this._updateTitle();
    this._syncCurrent();
    if (this._entries.length === 0) {
      this._stopTicking();
      disposeDiffEditor(this);
      this._restoreTitle();
      this._releaseFocus();
    }
  }

  /**
   * A resolution from anywhere: this client, another window, the
   * timeout, or shutdown. Idempotent — the client that sent the decision
   * also receives the broadcast.
   */
  _onPermissionResolved(event) {
    const detail = event?.detail;
    const id = detail?.permission_id;
    if (!id) return;
    const known = this._entries.some(
      (entry) => entry.payload.permission_id === id,
    );
    this._dequeue(id);
    if (!known || detail.resolved_by === 'self') return;
    this._notifyResolution(detail);
  }

  /**
   * Say who answered, when it was not this window.
   *
   * A dialog that vanished with no explanation reads as a bug; the user
   * needs to know a decision was taken and by whom
   * (permission-dialog.md § Multiple Clients).
   */
  _notifyResolution(detail) {
    const action = detail.action;
    let message;
    if (action === 'timeout') {
      message = `Permission for ${detail.tool_use_id ? 'a tool call' : 'a call'} `
        + 'expired and was denied for want of an answer.';
    } else if (action === 'shutdown') {
      message = 'A pending permission request was denied — the session shut down.';
    } else if (detail.resolved_by && detail.resolved_by !== 'localhost') {
      message = `${ALLOW_ACTIONS.includes(action) ? 'Allowed' : 'Denied'}`
        + ` by another window (${detail.resolved_by}).`;
    } else {
      return;
    }
    this._toast(message, action === 'timeout' ? 'warning' : 'info');
    window.dispatchEvent(new CustomEvent('permission-notice', {
      detail: { message, ...detail },
    }));
  }

  // ------------------------------------------------------------------
  // Countdown
  // ------------------------------------------------------------------

  _startTicking() {
    if (this._tickTimer) return;
    this._tickTimer = setInterval(() => {
      this._now = Date.now();
      this._checkExpiry();
      this._announceMilestone();
    }, COUNTDOWN_TICK_MS);
  }

  _stopTicking() {
    if (!this._tickTimer) return;
    clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  /**
   * Close a request whose clock has run out.
   *
   * The server is the authority — it denies on its own timeout and
   * broadcasts. This is the client catching up when that broadcast is
   * slow or lost, so the user is not left staring at `0:00` on a dialog
   * that is already dead.
   */
  _checkExpiry() {
    for (const entry of [...this._entries]) {
      const remaining = secondsRemaining(entry.payload, this._now);
      if (remaining === 0) {
        const name = entry.payload.tool_name;
        this._dequeue(entry.payload.permission_id);
        this._toast(
          `Permission for ${name} expired — denied for want of an answer.`,
          'warning',
        );
      }
    }
  }

  /**
   * Announce the countdown at coarse intervals only. A per-second live
   * region is unusable with a screen reader.
   */
  _announceMilestone() {
    const payload = this.current;
    if (!payload) return;
    const id = payload.permission_id;
    const remaining = secondsRemaining(payload, this._now);
    if (remaining == null) return;
    const done = this._announced.get(id) || new Set();
    for (const threshold of ANNOUNCE_AT_SECONDS) {
      if (remaining <= threshold && !done.has(threshold)) {
        done.add(threshold);
        this._announced.set(id, done);
        this._announcement = threshold >= 60
          ? `${threshold / 60} minute${threshold === 60 ? '' : 's'} left to answer`
          : `${threshold} seconds left to answer`;
        return;
      }
    }
  }

  // ------------------------------------------------------------------
  // Attention
  // ------------------------------------------------------------------

  _updateTitle() {
    if (typeof document === 'undefined') return;
    const count = this._entries.length;
    if (count === 0) return;
    if (this._titleBeforeDialog == null) {
      this._titleBeforeDialog = document.title;
    }
    document.title = `${TITLE_MARKER} ${count} — ${this._titleBeforeDialog}`;
  }

  _restoreTitle() {
    if (typeof document === 'undefined') return;
    if (this._titleBeforeDialog == null) return;
    document.title = this._titleBeforeDialog;
    this._titleBeforeDialog = null;
  }

  /** A short chime, once per empty→non-empty transition. Default on. */
  _alert() {
    let enabled = true;
    try {
      enabled = window.localStorage?.getItem(CHIME_SETTING_KEY) !== 'off';
    } catch (_) { /* storage blocked; default on */ }
    if (!enabled) return;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return;
    try {
      const context = new Ctor();
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = 880;
      oscillator.type = 'sine';
      gain.gain.setValueAtTime(0.0001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.24);
      oscillator.onended = () => { try { context.close(); } catch (_) {} };
    } catch (err) {
      console.debug('[permission-dialog] chime unavailable', err);
    }
  }

  // ------------------------------------------------------------------
  // Focus
  // ------------------------------------------------------------------

  _captureFocus() {
    if (typeof document === 'undefined') return;
    let node = document.activeElement;
    // Pierce shadow roots so we give focus back to the chat input the
    // user was mid-sentence in, not to its host element.
    while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement;
    this._focusBeforeDialog = node ?? null;
  }

  _releaseFocus() {
    const node = this._focusBeforeDialog;
    this._focusBeforeDialog = null;
    if (node && typeof node.focus === 'function' && node.isConnected) {
      try { node.focus(); } catch (_) { /* gone from the DOM */ }
    }
  }

  /** After settling, focus the class-appropriate control. */
  _focusDefault() {
    if (!this._canDecide || this._settling) return;
    const payload = this.current;
    if (!payload) return;
    const target = defaultFocusTarget(payload);
    const button = this.shadowRoot?.querySelector(
      `button.decision[data-decision="${target}"]`,
    );
    if (button && typeof button.focus === 'function') {
      try { button.focus(); } catch (_) { /* not focusable yet */ }
    }
  }

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------

  _onKeydownCapture(event) {
    if (!this.current) return;
    if (event.key === 'Escape') {
      // Escape is a deny, and it takes priority over every other Escape
      // binding in the application — hence stopImmediatePropagation on
      // the capture phase.
      event.preventDefault();
      event.stopImmediatePropagation();
      if (this._ruleMenuOpen || this._denyMenuOpen) {
        this._ruleMenuOpen = false;
        this._denyMenuOpen = false;
        return;
      }
      if (!this._canDecide) return;
      this._decide('deny', { reason: ESCAPE_DENY_REASON });
      return;
    }
    if (this._settling && (event.key === 'Enter' || event.key === ' ')) {
      // A keystroke already in flight when the dialog opened must not be
      // able to approve anything.
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (event.key === 'Tab') this._trapFocus(event);
  }

  /**
   * Keep Tab inside the dialog for its lifetime.
   *
   * Focus escaping to the inert background would let a keyboard user
   * interact with a UI the scrim says is unavailable.
   */
  _trapFocus(event) {
    const root = this.shadowRoot?.querySelector('.dialog');
    if (!root) return;
    const focusable = [...root.querySelectorAll(
      'button:not([disabled]), input, textarea, [tabindex]:not([tabindex="-1"])',
    )].filter((node) => node.offsetParent !== null || node.getClientRects?.().length);
    if (focusable.length === 0) return;
    const active = this.shadowRoot.activeElement;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && (active === first || !root.contains(active))) {
      event.preventDefault();
      last.focus();
    }
  }

  // ------------------------------------------------------------------
  // Editing
  // ------------------------------------------------------------------

  /** Whether the user has changed what will run. */
  _hasEdits() {
    const payload = this.current;
    if (!payload) return false;
    if (payload.tool_class === 'exec') {
      return this._editingCommand
        && this._commandDraft !== (payload.command?.command ?? '');
    }
    return this._editingDiff && this._diffDirty;
  }

  /** Whether an edit affordance is offered at all. See FULL_CONTENT_TOOLS. */
  _canEditInput() {
    const payload = this.current;
    if (!payload || !this._canDecide) return false;
    if (payload.tool_class === 'exec') return true;
    if (payload.tool_class !== 'write') return false;
    if (!(payload.tool_name in FULL_CONTENT_TOOLS)) return false;
    const diff = payload.diff;
    return !!diff && diff.proposed != null && !diff.is_binary && !diff.too_large;
  }

  _toggleEdit() {
    const payload = this.current;
    if (!payload) return;
    if (payload.tool_class === 'exec') {
      this._editingCommand = !this._editingCommand;
      if (this._editingCommand) this._commandDraft = payload.command?.command ?? '';
      return;
    }
    this._editingDiff = !this._editingDiff;
    if (!this._editingDiff) this._diffDirty = false;
    this.updateComplete.then(() => refreshEditMode(this));
  }

  _onCommandInput(event) {
    this._commandDraft = event.target.value;
  }

  /**
   * Select or clear one option of one question.
   *
   * Single-select replaces the question's whole set, so the radio group
   * cannot end up with two answers; multi-select adds to it. Other
   * questions are untouched — each carries its own answer.
   */
  _onOptionToggle(questionIndex, index, event, multi) {
    const current = this._answers.get(questionIndex);
    const next = multi ? new Set(current) : new Set();
    if (event.target.checked) next.add(index); else next.delete(index);
    const answers = new Map(this._answers);
    answers.set(questionIndex, next);
    this._answers = answers;
    // Picking an option on a single-select question clears a reply typed
    // into its Other field. The two are alternatives there, and leaving
    // stale text behind would send the text and drop the click.
    if (!multi && event.target.checked && this._answerTexts.get(questionIndex)) {
      const texts = new Map(this._answerTexts);
      texts.set(questionIndex, '');
      this._answerTexts = texts;
    }
  }

  /**
   * The freeform reply to one question.
   *
   * The mirror of `_onOptionToggle`: on a single-select question typing
   * clears the radio selection, because the engine sends the typed reply
   * *instead of* the labels there and a checked radio would show the user
   * an answer that is not the one being sent.
   */
  _onAnswerTextInput(questionIndex, event, multi) {
    const value = event.target.value ?? '';
    const texts = new Map(this._answerTexts);
    texts.set(questionIndex, value);
    this._answerTexts = texts;
    if (!multi && value.trim() && this._answers.get(questionIndex)?.size) {
      const answers = new Map(this._answers);
      answers.set(questionIndex, new Set());
      this._answers = answers;
    }
  }

  /**
   * The `updated_input` to send, or null when nothing was edited.
   *
   * Only ever built from an input shape we can express faithfully: a
   * command string, or a tool whose input carries the whole file. See
   * FULL_CONTENT_TOOLS for why `Edit` is not in that set.
   */
  _updatedInput() {
    const payload = this.current;
    if (!payload || !this._hasEdits()) return null;
    if (payload.tool_class === 'exec') {
      return { ...payload.input, command: this._commandDraft };
    }
    const key = FULL_CONTENT_TOOLS[payload.tool_name];
    if (!key) return null;
    const content = currentProposedContent(this);
    if (content == null) return null;
    return { ...payload.input, [key]: content };
  }

  /**
   * The `interact` answers, one entry per question.
   *
   * Indices and the typed reply, not labels, and a field of their own
   * rather than `updated_input`: the engine owns the mapping into the
   * `answers` shape `AskUserQuestion` reads, and `updated_input` being
   * present is what marks a call as user-modified in the transcript.
   * Answering a question the agent asked is not modifying the call it made.
   *
   * @returns {Array<{options: Array<number>, text: string}>|null} null when
   *   the user chose and typed nothing at all
   */
  _answerSelections() {
    const questions = interactQuestions(this.current);
    if (!questions.length) return null;
    const answers = questions.map((_question, index) => ({
      options: [...(this._answers.get(index) || [])].sort((a, b) => a - b),
      text: (this._answerTexts.get(index) || '').trim(),
    }));
    return answers.some((answer) => answer.options.length || answer.text)
      ? answers
      : null;
  }

  // ------------------------------------------------------------------
  // Menus and deny
  // ------------------------------------------------------------------

  _toggleRuleMenu() {
    this._ruleMenuOpen = !this._ruleMenuOpen;
    this._denyMenuOpen = false;
  }

  _toggleDenyMenu() {
    this._denyMenuOpen = !this._denyMenuOpen;
    this._ruleMenuOpen = false;
  }

  _chooseRule(index) {
    this._ruleIndex = index;
    this._ruleMenuOpen = false;
    this._decide('allow_always');
  }

  _openDeny(interrupts) {
    this._denyMenuOpen = false;
    this._denyInterrupts = interrupts;
    this._denyOpen = true;
    if (!this._denyReason) this._denyReason = defaultDenyReason(this.current);
    this.updateComplete.then(() => {
      const field = this.shadowRoot?.querySelector('input.deny-reason');
      if (field?.focus) {
        field.focus();
        field.select?.();
      }
    });
  }

  _onDenyReasonInput(event) {
    this._denyReason = event.target.value;
  }

  _onDenyReasonKeydown(event) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    event.stopPropagation();
    this._decide(this._denyInterrupts ? 'deny_interrupt' : 'deny');
  }

  // ------------------------------------------------------------------
  // Resolution
  // ------------------------------------------------------------------

  /**
   * Send a decision and dequeue.
   *
   * Dequeues optimistically so a second click cannot send a second
   * decision, then reports whatever the server says. `already_resolved`
   * is not an error here — another window won the race — so it surfaces
   * as attribution rather than as a failure.
   */
  async _decide(action, overrides = {}) {
    const payload = this.current;
    if (!payload || !this._canDecide) return;
    if (this._settling) return;

    const decision = { action, ...overrides };
    if (action === 'deny' || action === 'deny_interrupt') {
      const reason = (overrides.reason ?? this._denyReason ?? '').trim();
      // A deny always carries a non-empty reason: a blank denial
      // produces an agent that retries the same call.
      decision.reason = reason || defaultDenyReason(payload);
    } else {
      if (action === 'allow_always') decision.rule_index = this._ruleIndex;
      const updated = this._updatedInput();
      if (updated) decision.updated_input = updated;
      if (payload.tool_class === 'interact') {
        const answers = this._answerSelections();
        if (answers) decision.answers = answers;
      }
    }

    const permissionId = payload.permission_id;
    const wasEdited = !!decision.updated_input;
    this._dequeue(permissionId);

    try {
      const answer = await this.rpcExtract(
        'ClaudeCodeService.resolve_permission',
        permissionId,
        decision,
      );
      if (answer?.error === 'restricted') {
        this._canDecide = false;
        this._toast('Only the host can answer permission requests.', 'error');
      } else if (answer?.error === 'already_resolved') {
        this._toast(`Already answered by ${answer.resolved_by}.`, 'info');
      } else if (answer?.error) {
        this._toast(`Permission request could not be answered: ${answer.error}`, 'error');
      } else if (wasEdited) {
        // The transcript must record what actually ran, marked as
        // user-modified — a transcript showing the agent's original
        // proposal while a different command ran would lie about the
        // repository's history.
        window.dispatchEvent(new CustomEvent('permission-input-edited', {
          detail: {
            permission_id: permissionId,
            tool_use_id: payload.tool_use_id,
            request_id: payload.request_id,
            tool_name: payload.tool_name,
            updated_input: decision.updated_input,
          },
        }));
      }
    } catch (err) {
      // The request is already out of our queue and the server will
      // time it out. Say so rather than silently re-opening a dialog
      // whose decision may or may not have landed.
      console.error('[permission-dialog] resolve_permission failed', err);
      this._toast(
        'Could not send that decision. The request will time out and be denied.',
        'error',
      );
    }
  }

  /** AppShell listens for `ac-toast` window events; see toasts.js. */
  _toast(message, type = 'info') {
    window.dispatchEvent(new CustomEvent('ac-toast', {
      detail: { message, type },
    }));
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  updated(changed) {
    super.updated?.(changed);
    const payload = this.current;
    if (payload?.tool_class === 'write') {
      syncDiffEditor(this, payload);
    } else if (this._diffEditor) {
      disposeDiffEditor(this);
    }
  }

  render() {
    const payload = this.current;
    if (!payload) return null;

    const queue = this.queue;
    const remaining = secondsRemaining(payload, this._now);
    const urgency = countdownUrgency(remaining);
    const glyph = CLASS_GLYPHS[payload.tool_class] || '•';
    const diff = payload.diff;
    const risky = defaultFocusTarget(payload) === 'deny';

    return html`
      <div class="scrim" @click=${(event) => event.stopPropagation()}></div>
      <div
        class="dialog ${risky ? 'risky' : ''}"
        role="dialog"
        aria-modal="true"
        aria-labelledby="permission-header"
      >
        <header id="permission-header">
          <span class="glyph" aria-hidden="true">${glyph}</span>
          <span class="tool-name">${payload.display_name || payload.tool_name}</span>
          <span class="target">${headerTarget(payload)}</span>
          ${queue.length > 1
            ? html`<span class="queue-position">1 of ${queue.length}</span>`
            : null}
          <span class="countdown ${urgency}" aria-hidden="true">
            ${formatCountdown(remaining)} ⏱
          </span>
        </header>

        ${payload.agent_id
          ? html`
              <div class="attribution">
                requested by subagent
                <span class="agent-id">${payload.agent_id}</span>
              </div>
            `
          : null}

        ${payload.localhost_available === false
          ? html`
              <div class="no-localhost">
                No host client was connected when this was asked, so it has a
                shorter deadline than usual.
              </div>
            `
          : null}

        ${this._renderWhy(payload)}

        <div class="body ${payload.tool_class === 'write' && diff?.proposed != null
          && !diff.is_new_file ? 'no-padding' : ''}">
          ${renderBody(this, payload)}
        </div>

        <div class="detail-strip">
          ${diff && diff.proposed != null && !diff.is_new_file
            ? html`
                <span class="stats">
                  <span class="added">+${diff.additions}</span>
                  <span class="removed">−${diff.deletions}</span>
                </span>
              `
            : null}
          <details class="full-input">
            <summary>full input</summary>
            <pre class="json">${formatJson(payload.input)}</pre>
          </details>
          ${this._canEditInput()
            ? html`
                <button class="edit-toggle" @click=${() => this._toggleEdit()}>
                  ${this._editingDiff || this._editingCommand
                    ? 'stop editing'
                    : payload.tool_class === 'exec'
                      ? 'edit command'
                      : 'edit proposed content'}
                </button>
              `
            : null}
        </div>

        ${renderDecisions(this, payload)}

        <div class="sr-only" role="status" aria-live="polite">
          ${this._announcement}
        </div>
      </div>
    `;
  }

  /**
   * Why the dialog appeared, when the SDK told us.
   *
   * Part of what the user is deciding about: "a hook asked for
   * confirmation" and "no rule matched" warrant different answers.
   */
  _renderWhy(payload) {
    const reason = payload.decision_reason;
    const text = typeof reason === 'string'
      ? reason
      : reason?.reason || reason?.message || null;
    if (!text && !payload.blocked_path) return null;
    return html`
      <div class="why">
        ${text || ''}
        ${payload.blocked_path
          ? html`
              ${text ? ' — ' : ''}
              <span class="cwd">${payload.blocked_path}</span>
              is outside the directories this session may touch.
            `
          : null}
      </div>
    `;
  }
}

customElements.define('ac-permission-dialog', PermissionDialog);
