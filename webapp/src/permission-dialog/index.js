// <aic-permission-dialog> — the browser surface for `can_use_tool`.
//
// This is the component that justifies a browser frontend over a
// terminal: what the user sees is the consequence of the call, not its
// name. Governing spec: specs5/5-webapp/permission-dialog.md. The
// engine-side contract is specs5/3-engine/permissions.md, implemented in
// src/aic_dc/claude_code/permissions.py.
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

import { LitElement, html, nothing } from 'lit';

import { RpcMixin } from '../rpc-mixin.js';
import { PERMISSION_DIALOG_STYLES } from './styles.js';
import {
  ALLOW_ACTIONS,
  ANNOUNCE_AT_SECONDS,
  CHIME_SETTING_KEY,
  CLASS_GLYPHS,
  COLLAPSE_GLYPH,
  COLLAPSE_QUESTION_LABEL,
  COUNTDOWN_TICK_MS,
  ESCAPE_DENY_REASON,
  EXPAND_GLYPH,
  EXPAND_QUESTION_LABEL,
  QUESTION_DOCK_GUTTER,
  QUESTION_DOCK_MIN_PLAIN,
  QUESTION_DOCK_MIN_WIDTH,
  SETTLING_MS,
  TITLE_MARKER,
} from './constants.js';
import {
  arrivalAnnouncement,
  countdownUrgency,
  defaultDenyReason,
  defaultFocusTarget,
  formatCountdown,
  hasPreviews,
  headerTarget,
  interactQuestions,
  orderQueue,
  secondsRemaining,
  spokenSeconds,
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

/**
 * Why the session denied a request, keyed by the `cause` the engine sends
 * on `permissionResolved` (`CAUSE_*` in
 * src/aic_dc/claude_code/permissions.py).
 *
 * The `action` cannot carry this: Stop, the end of a turn and the end of a
 * subagent all resolve as `cancelled`, so a dialog that read the action
 * announced whichever one was hardcoded — and told a user who had just
 * pressed Stop that "the turn it belonged to ended" instead.
 *
 * `agent_ended` is the one that most needs saying out loud. A background
 * subagent's dialog now outlives the turn that spawned it, so when one does
 * get denied it is because that subagent stopped, and pointing at the turn
 * would send the user looking in the wrong place entirely.
 */
const CANCEL_CAUSES = {
  stopped: 'the turn was stopped.',
  turn_ended: 'the turn it belonged to ended.',
  agent_ended: 'the subagent that asked for it ended.',
  shutdown: 'the session shut down.',
};

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
    /** `interact` notes: question index → the note on that answer. Not an
        answer itself — it annotates one. */
    _answerNotes: { type: Object, state: true },
    /** Which option's preview the compare pane shows: question index →
        option index. Absent means "whichever the renderer picks". */
    _previewFocus: { type: Object, state: true },
    /** Live-region text: arrival announcement or countdown milestone. */
    _announcement: { type: String, state: true },
    /**
     * Where the shell's docked panel ends, in viewport px, or null for
     * "there is nowhere to dock — use the centred modal".
     *
     * Set as a property by the shell, which owns the panel's geometry and
     * is the only thing that knows whether the chat is on screen at all
     * (app-shell/dialog.js § questionDockLeft). A property rather than an
     * attribute because null is one of its values and an absent attribute
     * and an attribute reading "null" are the same string.
     */
    dockLeft: { attribute: false },
    /**
     * Whether the user has collapsed a question that could not be docked.
     *
     * Never read directly — `_collapsed` gates it on the request still being
     * one that offers the control. Reset per request in `_syncCurrent`.
     */
    _collapsedByUser: { type: Boolean, state: true },
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
    this._answerNotes = new Map();
    this._previewFocus = new Map();
    this._announcement = '';
    this.dockLeft = null;
    this._collapsedByUser = false;
    /** Last `dockMinWidth` announced to the shell. Not reactive — it exists
        to keep the announcement from firing on every render. */
    this._publishedDockMinWidth = null;

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
    /** Watches document.head so Monaco's styles reach this shadow root. */
    this._styleObserver = null;

    this._tickTimer = null;
    this._settleTimer = null;
    /** What held focus before the first request, to give it back. */
    this._focusBeforeDialog = null;
    /** The document title before we prefixed it. */
    this._titleBeforeDialog = null;

    this._onPermissionRequest = this._onPermissionRequest.bind(this);
    this._onPermissionDeadline = this._onPermissionDeadline.bind(this);
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
    window.addEventListener('permission-deadline', this._onPermissionDeadline);
    window.addEventListener('permission-resolved', this._onPermissionResolved);
    window.addEventListener('role-changed', this._onRoleChanged);
    // Capture phase on `window`, which is the outermost target in the
    // bubble/capture path — so this handler runs before the chat panel's
    // @-filter, the `/` palette, the input-clear chain, and the
    // lightbox, all of which bind on document or on their own roots.
    // That ordering is the spec's "Escape takes priority over every
    // other Escape binding in the application".
    window.addEventListener('keydown', this._onKeydownCapture, true);
  }

  disconnectedCallback() {
    window.removeEventListener('permission-request', this._onPermissionRequest);
    window.removeEventListener('permission-deadline', this._onPermissionDeadline);
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
   * (permission-dialog.md § Reconnect). Usually there is nothing to count:
   * a refresh *is* a host client, and the snapshot the server serves has
   * had its clock cancelled by the time it arrives.
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

  /**
   * Whether the current request is docked beside the chat rather than
   * modal over it.
   *
   * `interact` only, and only where the shell says there is room. The
   * modality of every other class is a security argument — a destructive
   * call must not be answered while the user is reading something else —
   * and a question is not a security decision: allow *is* the answer, deny
   * means "ask me in prose", and § interact already refuses to offer a rule
   * that could pre-answer one. What a question needs instead is the
   * transcript it was written about, which is what the modal was covering
   * (permission-dialog.md § Placement).
   *
   * Live rather than frozen at arrival: every input to it is something the
   * user just did with their own hands — resized the panel, minimized it,
   * switched tabs — so a placement that follows them is not the app moving
   * controls under the pointer that § Anti-Click-Through is about.
   */
  get _docked() {
    return this.current?.tool_class === 'interact' && this.dockLeft != null;
  }

  /**
   * The narrowest region this request could be docked into, in px.
   *
   * Public, because the shell decides whether there is room and this is the
   * half of that decision the shell cannot know: which floor applies is a
   * fact about the pending request's own layout. Asked rather than
   * re-derived, on the same argument as `modal` — a copy of the rule in the
   * shell is a copy that can disagree with what actually rendered.
   *
   * The wide floor buys the `.question-compare` grid, so the test has to be
   * the one that grid is rendered on: `!multi_select && hasPreviews`, exactly
   * as in bodies.js § renderInteractBody. `hasPreviews` alone would be
   * subtly wrong in the direction that matters — the compare pane is
   * single-select only, so a *multi*-select question carrying previews
   * renders as a plain list and would be handed a 720px floor to protect a
   * layout it never draws, which is how the whole dock came to be
   * unreachable in the first place.
   *
   * Any question in the set forces the wide floor: they render stacked in one
   * panel, so the widest layout among them sets the width.
   *
   * With nothing pending — or a class that never docks — the answer is the
   * wide floor rather than the narrow one. The shell measures the dock on its
   * own schedule, including before any request exists, and the two ways of
   * being wrong are not symmetrical: too wide costs a centred modal the user
   * can collapse, too narrow docks a comparison into a region that stacks it.
   * The narrow floor is a claim about a request, so it takes one to make.
   */
  get dockMinWidth() {
    const questions = interactQuestions(this.current);
    if (!questions.length) return QUESTION_DOCK_MIN_WIDTH;
    const compare = questions.some(
      (question) => !question.multi_select && hasPreviews(question),
    );
    return compare ? QUESTION_DOCK_MIN_WIDTH : QUESTION_DOCK_MIN_PLAIN;
  }

  /**
   * Whether this request offers the collapse control.
   *
   * The question that could not be docked, and only that. `_docked` is the
   * good outcome and has nothing to collapse away from — it is already beside
   * the transcript rather than over it. Every other class keeps the modal it
   * has: their modality is the security argument § Placement makes, and while
   * collapsing cannot *answer* anything (the decision row is not rendered, so
   * there is nothing to click through to), dropping the scrim off a pending
   * destructive call is a wider change than the one this control is for.
   */
  get _collapsible() {
    return this.current?.tool_class === 'interact' && !this._docked;
  }

  /**
   * Whether the question is parked as its header.
   *
   * Gated on `_collapsible` rather than trusted on its own, because
   * `dockLeft` is live: collapse the modal, widen the panel, and the question
   * docks — at which point the collapse the user asked for has been granted
   * by better means, and honouring the flag as well would hide the docked
   * panel they can now see. The flag survives (narrow it again and it is
   * still collapsed); what it does not do is outlive the condition it was
   * a workaround for.
   */
  get _collapsed() {
    return this._collapsible && this._collapsedByUser;
  }

  /**
   * Whether the request is on screen without a scrim, with the UI behind it
   * genuinely live.
   *
   * Two ways to get there and they need identical treatment — no scrim, no
   * `aria-modal`, no Tab trap, and an Escape that belongs to whatever the
   * user is actually typing in rather than to this dialog. Named once because
   * the first cut spelled `_docked` out at each of those four sites, and a
   * fifth state that also drops the scrim would have had to find all four.
   */
  get _nonModal() {
    return this._docked || this._collapsed;
  }

  /**
   * Whether a request is on screen *as a modal* — a scrim over an inert UI
   * with focus trapped inside it. Public, because the shell asks: it is what
   * makes its global shortcuts inert (shell.md § Keyboard Shortcuts).
   *
   * Asked rather than re-derived. "Is this modal" has two inputs — the tool
   * class and whether the shell reported room beside the chat — and a second
   * copy of that rule in the shell would be a copy that can disagree with
   * the scrim actually on screen. There is one owner, and it is the element
   * that draws it.
   *
   * A collapsed question answers false for the same reason a docked one does,
   * and it matters more here: collapsing exists so the user can read the chat,
   * and suppressed shortcuts would be the app agreeing to that in appearance
   * only.
   */
  get modal() {
    return !!this.current && !this._nonModal;
  }

  _onPermissionRequest(event) {
    const payload = event?.detail;
    if (payload) this._enqueue(payload);
  }

  /**
   * A deadline armed or cancelled on a request already on screen.
   *
   * Most requests have no deadline at all: a request waits as long as
   * somebody who could answer it is connected, because nothing is consumed
   * while it waits (permissions.py § Deadline). A clock appears only when
   * the last host client leaves, and disappears again when one returns —
   * which is why this is an update to the entry rather than a new one. The
   * settling interval and any half-typed deny reason belong to the request,
   * not to its clock, and re-enqueuing would throw both away.
   *
   * Milestone announcements are cleared on a disarm so a clock that arms a
   * second time announces itself again rather than staying silent.
   */
  _onPermissionDeadline(event) {
    const detail = event?.detail;
    const id = detail?.permission_id;
    if (!id) return;
    let changed = false;
    this._entries = this._entries.map((entry) => {
      if (entry.payload.permission_id !== id) return entry;
      changed = true;
      return {
        ...entry,
        payload: {
          ...entry.payload,
          expires_at: detail.expires_at ?? null,
          localhost_available: detail.localhost_available !== false,
        },
      };
    });
    if (!changed) return;
    if (detail.expires_at == null) this._announced.delete(id);
    const wasCurrent = this._currentId === id;
    this._now = Date.now();
    // A clock can reorder the queue — a counting-down request is answered
    // before an open-ended one — and that can put a different request on
    // screen. `_syncCurrent` is a no-op when it does not, and when it does
    // the newly visible one gets its own settling interval.
    this._syncCurrent();
    // A clock starting is the news here, and it is the one thing a screen
    // reader cannot pick up from a numeral that just appeared. Said with
    // the time the request actually has: the window is 30 s, so the first
    // milestone below it is the 10-second one — far too late to be the only
    // notice. Skipped when the arm promoted a *different* request onto the
    // screen, because `_syncCurrent` has already announced that one and
    // which request the user is now looking at matters more.
    if (detail.expires_at != null && wasCurrent && this._currentId === id) {
      const remaining = secondsRemaining(this.current, this._now);
      if (remaining != null) {
        this._announcement =
          `no host client is connected — ${spokenSeconds(remaining)} to answer`;
      }
    }
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
    this._answerNotes = new Map();
    this._previewFocus = new Map();
    // The load-bearing half of the collapse control. A request that arrived
    // while the last one was parked must arrive *open*: a queue that inherits
    // the collapse would show the next question — or the next `write` — as a
    // one-line bar the user has no reason to look at, which is invariant 1
    // ("never silently") reached by a different route.
    this._collapsedByUser = false;
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
      // Expiry now means one thing only: no host client was connected to
      // answer. Saying "for want of an answer" would blame the user for a
      // dialog that was never on their screen.
      message = 'A pending permission request was denied — no host client was '
        + 'connected to answer it.';
    } else if (action === 'cancelled' || action === 'shutdown') {
      // `cause`, not `action`: Stop, a turn ending and a subagent ending all
      // resolve as `cancelled`, so reading the action alone told the user
      // "the turn it belonged to ended" when they had pressed Stop. The
      // fallback covers a server too old to send a cause, and reads as the
      // vaguer thing rather than as a specific wrong thing.
      message = 'A pending permission request was denied — '
        + (CANCEL_CAUSES[detail.cause] ?? 'it was no longer waiting on anyone.');
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
   * Most requests have no clock, and `secondsRemaining` returns null for
   * those — they are never swept from here. The ones that do have one got
   * it because no host client was connected, which makes this a rare path
   * on a client that is, by definition, connected: the host came back
   * inside the window and the disarm has not arrived yet.
   *
   * The server is the authority — it denies on its own deadline and
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
          `Permission for ${name} expired — no host client was connected `
          + 'to answer it.',
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
    let done = this._announced.get(id);
    if (!done) {
      // First sight of this request's clock. Thresholds already above the
      // time it actually has are retired unannounced, because the loop
      // below stops at the first match: the only deadline that exists is
      // the 30 s no-host one, and "5 minutes left to answer" said over a
      // thirty-second window is worse than saying nothing.
      done = new Set(ANNOUNCE_AT_SECONDS.filter((t) => t > remaining));
      this._announced.set(id, done);
    }
    for (const threshold of ANNOUNCE_AT_SECONDS) {
      if (remaining <= threshold && !done.has(threshold)) {
        done.add(threshold);
        this._announcement = `${spokenSeconds(threshold)} to answer`;
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

  /**
   * Collapse or expand a question that could not be docked.
   *
   * Presentation only. Nothing is sent, the countdown is untouched, and the
   * half-typed answers live on `this` rather than in the DOM, so they are
   * still there on expand.
   */
  _toggleCollapsed() {
    this._collapsedByUser = !this._collapsedByUser;
  }

  /**
   * After settling, focus the class-appropriate control.
   *
   * Nothing to focus while collapsed — the decision row is not rendered — and
   * stealing focus back from the chat the user collapsed the question to read
   * would undo the gesture 700ms after they made it.
   */
  _focusDefault() {
    if (!this._canDecide || this._settling || this._collapsed) return;
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
      // A docked question is not modal, so the chat input behind it is
      // live — and Escape there clears the input. Denying the agent's
      // question because the user cleared a half-typed message would
      // resolve a request they never touched, so while docked Escape only
      // denies from inside the panel. The modal case keeps the priority
      // the spec gives it, and can: nothing else on screen is reachable.
      //
      // A collapsed question is in the same position, and reaches it more
      // often: collapsing is *for* going back to the chat input, so the very
      // next Escape is overwhelmingly likely to be that input's.
      if (this._nonModal && !event.composedPath?.().includes(this)) return;
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
    // Alt+M — collapse or expand the parked question, taking the shortcut
    // over from the shell's own dialog minimize while one is on screen.
    //
    // Handled here rather than added to the shell's global table, and that is
    // forced rather than stylistic: the shell makes every Alt shortcut inert
    // while a modal permission dialog is open (shell.md § Global Keyboard
    // Shortcuts), which is exactly the state this needs to act in — an
    // expanded modal question is modal, so a shortcut living there could
    // never be the one that collapses it. This handler is on `window` in the
    // capture phase and the shell's is on `document` bubbling, so consuming
    // the event here is also what keeps Alt+M from collapsing the shell's
    // panel *as well* once the question has gone non-modal.
    //
    // Both cases, so Caps Lock does not break it — matching the shell's own
    // Alt+M. Modifier combinations are left alone for the same reason the
    // shell leaves them alone: Alt+Shift+M is not this shortcut.
    if (
      event.altKey
      && !event.ctrlKey
      && !event.metaKey
      && !event.shiftKey
      && (event.key === 'm' || event.key === 'M')
    ) {
      if (!this._collapsible) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      this._toggleCollapsed();
      return;
    }
    if (this._settling && (event.key === 'Enter' || event.key === ' ')) {
      // A keystroke already in flight when the dialog opened must not be
      // able to approve anything.
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    // A docked or collapsed question does not trap Tab. The trap exists so a
    // keyboard user cannot reach a UI the scrim calls unavailable; with no
    // scrim there is nothing to be inconsistent with, and reaching the
    // transcript is how a keyboard user reads what the question is about.
    // Trapping Tab inside a collapsed bar would be worse than pointless — the
    // only thing in it is the button that expands it again.
    if (event.key === 'Tab' && !this._nonModal) this._trapFocus(event);
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
    // Deciding is comparing, so the pane follows the choice: picking an
    // option shows what was picked rather than leaving the example of the
    // one before it beside a filled radio.
    if (event.target.checked) this._onPreviewFocus(questionIndex, index);
  }

  /**
   * Which option's example the compare pane shows.
   *
   * Bound to focus and hover on the options, so comparing two mockups is
   * arrow keys or a mouse move rather than a click each way — the point of
   * the pane is the comparison, and a click commits an answer. It is
   * display state only: nothing here reaches the decision.
   */
  _onPreviewFocus(questionIndex, index) {
    if (this._previewFocus.get(questionIndex) === index) return;
    const next = new Map(this._previewFocus);
    next.set(questionIndex, index);
    this._previewFocus = next;
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
   * The note on one question's answer.
   *
   * Unlike `_onAnswerTextInput` this clears nothing and is cleared by
   * nothing: a note is about the question, so switching from one option to
   * another keeps it. It is also never an answer on its own — see
   * `_answerSelections` — so typing here cannot enable the Answer button.
   */
  _onAnswerNotesInput(questionIndex, event) {
    const notes = new Map(this._answerNotes);
    notes.set(questionIndex, event.target.value ?? '');
    this._answerNotes = notes;
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
   * The note travels here too, as a third field rather than folded into
   * `text`: the engine sends a reply as the *answer* and a note as an
   * annotation of one, and merging them would put a remark where the CLI
   * checks for an option label.
   *
   * @returns {Array<{options: Array<number>, text: string, notes: string}>|null}
   *   null when the user chose and typed nothing at all
   */
  _answerSelections() {
    const questions = interactQuestions(this.current);
    if (!questions.length) return null;
    const answers = questions.map((_question, index) => ({
      options: [...(this._answers.get(index) || [])].sort((a, b) => a - b),
      text: (this._answerTexts.get(index) || '').trim(),
      notes: (this._answerNotes.get(index) || '').trim(),
    }));
    // A note is deliberately not in this test. It annotates an answer, so
    // a dialog holding nothing but notes has still not been answered.
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

  /** AppShell listens for `aic-toast` window events; see toasts.js. */
  _toast(message, type = 'info') {
    window.dispatchEvent(new CustomEvent('aic-toast', {
      detail: { message, type },
    }));
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  updated(changed) {
    super.updated?.(changed);
    this._publishDockRequirement();
    const payload = this.current;
    if (payload?.tool_class === 'write') {
      syncDiffEditor(this, payload);
    } else if (this._diffEditor) {
      disposeDiffEditor(this);
    }
  }

  /**
   * Tell the shell when the width this request needs to dock has changed.
   *
   * Necessary because the floor stopped being a constant. The shell measures
   * the dock on panel resize, minimize, undock, tab switch and window resize —
   * every way its *own* geometry moves — and none of those fire when a request
   * arrives. That was fine while every question wanted 720px: the cached
   * answer could not be stale. Now a plain question following a compare one
   * would inherit the compare one's verdict and stay modal in a region it
   * fits.
   *
   * Announced from `updated` rather than by the shell listening for
   * `permission-request` directly, because the shell's answer depends on this
   * element's queue having already absorbed the payload. Two window listeners
   * racing to be second is not an ordering to rely on; a notification that
   * fires *after* the state settled is.
   */
  _publishDockRequirement() {
    // The tracked value is the published quantity itself, not a proxy for it.
    // An earlier cut tracked null while nothing was pending, which fired on
    // the arrival of a compare question — from null to 720, when 720 is what
    // the shell had already measured with. The event has to mean "the number
    // you read has changed" or the shell relayouts under a question for no
    // reason.
    const width = this.dockMinWidth;
    if (width === this._publishedDockMinWidth) return;
    this._publishedDockMinWidth = width;
    this.dispatchEvent(new CustomEvent('dock-requirement-changed', {
      bubbles: true,
      composed: true,
    }));
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
    const docked = this._docked;
    const collapsed = this._collapsed;

    return html`
      ${this._nonModal
        // No scrim at all, rather than a transparent one: the picker, the
        // chat and the viewer are genuinely live behind a docked question,
        // and a user who wants to open the file it is about should be able
        // to. Nothing the agent tries meanwhile can slip past — its next
        // gated call queues behind this one. A collapsed question is the
        // same bargain reached by hand: it was collapsed *to* uncover them.
        ? null
        : html`<div class="scrim" @click=${(event) => event.stopPropagation()}></div>`}
      <div
        class="dialog ${risky ? 'risky' : ''} ${docked ? 'docked' : ''} ${collapsed ? 'collapsed' : ''}"
        style=${docked
          ? `--question-dock-gutter: ${QUESTION_DOCK_GUTTER}px;`
            + ` left: ${this.dockLeft + QUESTION_DOCK_GUTTER}px;`
          : collapsed
            ? `--question-dock-gutter: ${QUESTION_DOCK_GUTTER}px;`
            : nothing}
        role="dialog"
        aria-modal=${this._nonModal ? nothing : 'true'}
        aria-labelledby="permission-header"
      >
        <header id="permission-header">
          <span class="glyph" aria-hidden="true">${glyph}</span>
          <span class="tool-name">${payload.display_name || payload.tool_name}</span>
          <span class="target">${headerTarget(payload)}</span>
          ${queue.length > 1
            ? html`<span class="queue-position">1 of ${queue.length}</span>`
            : null}
          ${remaining == null
            ? null
            : html`
                <span class="countdown ${urgency}" aria-hidden="true">
                  ${formatCountdown(remaining)} ⏱
                </span>
              `}
          ${this._collapsible
            ? html`
                <button
                  class="collapse-toggle"
                  aria-expanded=${collapsed ? 'false' : 'true'}
                  aria-label=${collapsed
                    ? EXPAND_QUESTION_LABEL
                    : COLLAPSE_QUESTION_LABEL}
                  title=${collapsed
                    ? EXPAND_QUESTION_LABEL
                    : COLLAPSE_QUESTION_LABEL}
                  @click=${() => this._toggleCollapsed()}
                >${collapsed ? EXPAND_GLYPH : COLLAPSE_GLYPH}</button>
              `
            : null}
        </header>

        ${collapsed ? null : html`
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
                No host client is connected, so this request is counting down and
                will be denied when it reaches zero. Requests wait indefinitely
                while a host is here.
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
        `}

        <!--
          Outside the collapse, deliberately. The countdown milestones are the
          one thing a collapsed question still has to say — a parked request
          that expires silently is the failure § Accessibility's live region
          exists to prevent, and collapsing must not be a way to opt out of it.
        -->
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

customElements.define('aic-permission-dialog', PermissionDialog);
