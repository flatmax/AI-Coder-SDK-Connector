// SlashPalette — pick a `/command` while typing it.
//
// Opens over the composer as soon as `/` is the first
// non-whitespace character the user has typed, and narrows as
// they keep typing. The list is whatever the live CLI
// advertises (`ClaudeCodeService.list_commands`), so skills,
// plugin commands and `.claude/commands/` entries appear
// without this component knowing they exist.
//
// Host/guest contract, deliberately the same shape as
// `aic-input-history` so the chat panel's keydown handler can
// treat both the same way:
//   - `show(commands, query, {partial})` — called on every
//     input event while the cursor is inside a command token;
//     opens if closed, re-filters if already open. `partial`
//     says the list is short because the engine has not
//     connected yet; it travels with the list rather than as a
//     bound property, because the list only ever changes here
//   - `hide()` — called when the token is gone
//   - `handleKey(e)` — called from the host's keydown handler
//     while open; returns true if consumed
//   - `command-select` event — carries the chosen command
//     object; the host edits the textarea and decides whether
//     to act on it or hand it to the engine
//
// No `command-cancel` counterpart to `history-cancel`: this
// overlay never touches the composer, so dismissing it has
// nothing to restore.
//
// Each entry carries an `action` from the service — `route`
// means selecting it opens an AIC⚡DC surface instead of
// sending text, and the row says so, because a command that
// silently does something other than what its CLI description
// promises is worse than no palette at all.
//
// It also carries `during_turn`: whether it is still reachable
// while a turn streams. Rows it withholds are rendered disabled
// with when they come back, not filtered out — a list that
// silently shrinks while streaming teaches the user that
// commands come and go, where a list with some rows greyed out
// teaches them the actual rule (specs5/3-engine/session.md
// § Mid-turn availability).

import { LitElement, css, html } from 'lit';

import { filterCommands } from './slash-commands.js';

export class SlashPalette extends LitElement {
  static properties = {
    /**
     * Whether a turn is streaming right now.
     *
     * A public property the host binds to its own streaming
     * flag, rather than a `show()` argument: a turn can start or
     * end while the overlay is already up, and `show()` is only
     * called on composer input. Bound this way, Lit re-renders
     * the rows the moment the turn does either.
     */
    streaming: { type: Boolean },
    /**
     * Whether the overlay is showing. Driven by `show()` /
     * `hide()` from the host, never by this component's own
     * reading of the composer — it can't see the textarea.
     */
    _open: { type: Boolean, state: true },
    /**
     * The full command list as handed in by the host. Not
     * fetched here: the host owns the RPC connection and
     * caches the reply across opens.
     */
    _commands: { type: Array, state: true },
    /**
     * Whether `_commands` is the short list — the routes AIC⚡DC
     * answers itself, with none of the CLI's own commands,
     * because the engine had not connected when it was asked.
     *
     * Rendered as a line of explanation rather than used to
     * suppress anything. Every count in this overlay is a count
     * of a list the user has no other way to size, so "4 of 4"
     * with nothing else said reads as "four commands exist".
     */
    _partial: { type: Boolean, state: true },
    /**
     * The token after `/`, left of the cursor. Filtering
     * input, not a display string.
     */
    _query: { type: String, state: true },
    /**
     * Index into the *filtered* list. A valid index whenever
     * the filtered list holds a row that can be acted on —
     * unlike aic-input-history's -1-means-nothing-yet, because a
     * palette that opens with nothing selected makes Enter
     * ambiguous.
     *
     * -1 is the one case that ambiguity is the honest answer:
     * every matching row is waiting on the turn, so there is
     * nothing for Enter to mean and highlighting a row would
     * promise otherwise.
     */
    _focusedIndex: { type: Number, state: true },
  };

  static styles = css`
    :host {
      display: block;
      position: relative;
    }
    .overlay {
      position: absolute;
      bottom: 100%;
      left: 0;
      right: 0;
      margin-bottom: 2px;
      background: rgba(22, 27, 34, 0.98);
      border: 1px solid rgba(240, 246, 252, 0.2);
      border-radius: 6px;
      box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.4);
      display: flex;
      flex-direction: column;
      max-height: 50vh;
      overflow: hidden;
      z-index: 50;
    }
    .entries {
      overflow-y: auto;
      /* No horizontal scrollbar under a list that is navigated
       * with the arrow keys. Rows ellipsize instead; see
       * .hint-args below. */
      overflow-x: hidden;
      display: flex;
      flex-direction: column;
      max-height: 40vh;
    }
    .entry {
      flex: 0 0 auto;
      box-sizing: border-box;
      display: flex;
      align-items: baseline;
      gap: 0.4rem;
      padding: 0.35rem 0.6rem;
      cursor: pointer;
      overflow: hidden;
      font-size: 0.8125rem;
      line-height: 1.25;
      color: var(--text-primary, #c9d1d9);
      border-bottom: 1px solid rgba(240, 246, 252, 0.12);
    }
    .entry:last-child {
      border-bottom: none;
    }
    .entry:hover {
      background: rgba(240, 246, 252, 0.04);
    }
    .entry.focused {
      background: rgba(88, 166, 255, 0.12);
      border-left: 3px solid var(--accent-primary, #58a6ff);
      padding-left: calc(0.6rem - 3px);
    }
    /* Dimmed but still legible, and it keeps its place in the
     * list. The row is here to be read, not to be picked: the
     * user needs to see that the command exists and that the
     * turn is what is holding it. */
    .entry.blocked {
      opacity: 0.55;
      cursor: default;
    }
    .entry.blocked:hover {
      background: none;
    }
    .name {
      flex: 0 0 auto;
      font-family: var(--font-mono, ui-monospace, monospace);
      color: var(--accent-primary, #58a6ff);
    }
    /* Shrinks and ellipsizes. The CLI's hints run long — the
     * one for /code-review is 70-odd characters — and at this
     * width no amount of them helps. Capped rather than merely
     * shrinkable so a long hint cannot eat the description,
     * which is what tells the user which command they want; the
     * hint only matters once they have chosen. */
    .hint-args {
      flex: 0 1 auto;
      min-width: 0;
      max-width: 40%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--text-secondary, #8b949e);
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 0.75rem;
    }
    /* The description is the row's flexible column and the
     * first thing to lose space — the name is what the user
     * is aiming at, so it never truncates. */
    .description {
      flex: 1 1 auto;
      min-width: 0;
      color: var(--text-secondary, #8b949e);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .badge {
      flex: 0 0 auto;
      padding: 0.05rem 0.3rem;
      background: rgba(88, 166, 255, 0.15);
      border-radius: 3px;
      font-size: 0.6875rem;
      color: var(--accent-primary, #58a6ff);
    }
    /* Not the accent colour: the accent means "this row does
     * something", and this badge means the opposite. Amber
     * rather than red because nothing has gone wrong. */
    .badge.waiting {
      background: rgba(210, 153, 34, 0.15);
      color: #d29922;
      white-space: nowrap;
    }
    .empty {
      padding: 0.75rem;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
      text-align: center;
      font-size: 0.8125rem;
    }
    .hint {
      flex-shrink: 0;
      padding: 0.25rem 0.6rem;
      background: rgba(13, 17, 23, 0.4);
      border-top: 1px solid rgba(240, 246, 252, 0.05);
      color: var(--text-secondary, #8b949e);
      font-size: 0.6875rem;
      text-align: center;
    }
    /* Its own row above the counts, and allowed to wrap: it is a
     * sentence, where the hint below it is a legend. Muted and
     * italic like .empty rather than amber like .badge.waiting —
     * amber already means "the turn is holding this" in here, and
     * nothing has gone wrong with a list that is merely early. */
    .hint-partial {
      flex-shrink: 0;
      padding: 0.3rem 0.6rem;
      background: rgba(13, 17, 23, 0.4);
      border-top: 1px solid rgba(240, 246, 252, 0.05);
      color: var(--text-secondary, #8b949e);
      font-size: 0.6875rem;
      font-style: italic;
      line-height: 1.35;
      text-align: center;
    }
  `;

  constructor() {
    super();
    this.streaming = false;
    this._open = false;
    this._commands = [];
    this._partial = false;
    this._query = '';
    this._focusedIndex = 0;
  }

  /**
   * Keep the highlight on a row that can actually be picked.
   *
   * A turn starting while the overlay is up disables rows
   * underneath the highlight, and a turn ending re-enables them
   * — either way `streaming` changing can leave `_focusedIndex`
   * pointing at a row Enter would refuse, which is precisely the
   * state this whole feature exists to avoid.
   */
  willUpdate(changed) {
    if (!changed.has('streaming') || !this._open) return;
    const filtered = this.filtered;
    if (this._actionable(filtered[this._focusedIndex])) return;
    this._focusedIndex = this._firstActionableIndex(filtered);
  }

  // ---------------------------------------------------------------
  // Host API
  // ---------------------------------------------------------------

  /**
   * Open (or re-filter) the overlay.
   *
   * Focus resets to the top whenever the query changes, since
   * the ranking has changed underneath it and keeping the old
   * index would leave the highlight on an unrelated row. A
   * repeat call with the same query — the host fires on every
   * input event, including cursor moves — leaves focus alone
   * so arrow-key navigation survives.
   *
   * @param {Array<object>} commands
   * @param {string} query
   * @param {{partial?: boolean}} [options] `partial` marks the
   *   list as the short pre-handshake one. Defaults to false on
   *   every call rather than sticking, so a full list replacing
   *   a partial one clears the notice without the host saying so.
   */
  show(commands, query = '', { partial = false } = {}) {
    const next = Array.isArray(commands) ? commands : [];
    const queryChanged = query !== this._query;
    this._commands = next;
    this._partial = partial === true;
    this._query = query;
    if (!this._open || queryChanged) {
      this._focusedIndex = this._firstActionableIndex(this.filtered);
    }
    this._open = true;
  }

  /** Close without selecting. Safe to call when already closed. */
  hide() {
    if (!this._open) return;
    this._open = false;
    this._query = '';
    this._focusedIndex = 0;
  }

  /**
   * Route a keydown from the host. Returns true when consumed,
   * in which case the host must not also act on it.
   *
   * Enter and Tab both select — Tab because completing a
   * partially-typed command is the reflex a terminal trains,
   * Enter because the palette is where the user's attention
   * is. Neither is consumed when nothing matches: the overlay
   * stays open to explain itself, but a stray `/typo` must
   * still be sendable, and the engine gives a better answer
   * about an unknown command than this list can.
   *
   * They *are* consumed when the match is a row the turn is
   * holding, and do nothing. Letting the key through instead
   * would reach `send`, which refuses mid-turn anyway — so the
   * outcome is the same silence, minus the overlay that was
   * explaining why.
   */
  handleKey(event) {
    if (!this._open) return false;
    const filtered = this.filtered;
    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        this.hide();
        return true;
      case 'Enter':
      case 'Tab':
        if (filtered.length === 0) return false;
        event.preventDefault();
        this._selectFocused(filtered);
        return true;
      case 'ArrowUp':
        if (filtered.length === 0) return false;
        event.preventDefault();
        this._moveFocus(-1, filtered);
        return true;
      case 'ArrowDown':
        if (filtered.length === 0) return false;
        event.preventDefault();
        this._moveFocus(1, filtered);
        return true;
      default:
        return false;
    }
  }

  /** Whether the host should be delegating keys here. */
  get isOpen() {
    return this._open;
  }

  /** The ranked, filtered list currently on show. */
  get filtered() {
    return filterCommands(this._commands, this._query);
  }

  // ---------------------------------------------------------------
  // Internals
  // ---------------------------------------------------------------

  /**
   * Whether this row can be picked right now.
   *
   * `during_turn` is read strictly: an entry without it — a list
   * cached from an older service — counts as blocked while
   * streaming. Withholding a command that would have worked is
   * recoverable by waiting; offering one that the guard then
   * refuses is the confusing failure.
   */
  _actionable(command) {
    if (!command) return false;
    return !this.streaming || command.during_turn === true;
  }

  /**
   * The first row Enter could act on, or -1 when there is none.
   *
   * -1 rather than 0, so that a list entirely held by the turn
   * opens with nothing highlighted. A highlight is a promise
   * that Enter does something.
   */
  _firstActionableIndex(filtered) {
    return filtered.findIndex((command) => this._actionable(command));
  }

  /**
   * Move the highlight, wrapping at both ends, skipping rows the
   * turn is holding.
   *
   * Wraps where `aic-input-history` clamps, and the difference
   * is the list itself: history is a long chronology the user
   * reads through, where wrapping loses their place, while
   * this is a short list ordered by relevance that they are
   * scanning for one known item.
   *
   * Skipping rather than stopping at a blocked row: the arrows
   * are how the user reaches the routed few among thirty-odd
   * passthrough commands mid-turn, and stopping would make that
   * a tour of everything they cannot have. A full lap finding
   * nothing leaves the highlight alone.
   */
  _moveFocus(delta, filtered) {
    const count = filtered.length;
    if (count === 0) return;
    const from = this._focusedIndex >= 0 ? this._focusedIndex : -delta;
    for (let step = 1; step <= count; step += 1) {
      const index = (((from + delta * step) % count) + count) % count;
      if (!this._actionable(filtered[index])) continue;
      this._focusedIndex = index;
      this.updateComplete.then(() => {
        const el = this.shadowRoot?.querySelector('.entry.focused');
        if (el && typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ block: 'nearest' });
        }
      });
      return;
    }
  }

  _selectFocused(filtered) {
    const index =
      this._focusedIndex >= 0 && this._focusedIndex < filtered.length
        ? this._focusedIndex
        : 0;
    const command = filtered[index];
    if (!command) return;
    // The row said it was waiting on the turn. Refusing here is
    // what makes that true for a click as well as for Enter, and
    // the overlay stays up still saying it.
    if (!this._actionable(command)) return;
    this.hide();
    this.dispatchEvent(
      new CustomEvent('command-select', {
        detail: { command },
        bubbles: true,
        composed: true,
      }),
    );
  }

  _onEntryClick(index, filtered) {
    if (!this._actionable(filtered[index])) return;
    this._focusedIndex = index;
    this._selectFocused(filtered);
  }

  render() {
    if (!this._open) return html``;
    const filtered = this.filtered;
    const total = this._commands.length;
    const waiting = filtered.filter(
      (command) => !this._actionable(command),
    ).length;
    return html`
      <div class="overlay" role="listbox" aria-label="Slash commands">
        ${filtered.length === 0
          ? html`<div class="empty">
              No command matches /${this._query}
            </div>`
          : html`<div class="entries">
              ${filtered.map((command, index) =>
                this._renderEntry(command, index, filtered),
              )}
            </div>`}
        ${this._partial
          ? html`<div class="hint-partial" role="note">
              Only the commands AIC⚡DC routes itself — the CLI's own list
              arrives once the engine connects, on your first turn.
            </div>`
          : ''}
        <div class="hint">
          ${filtered.length} of ${total}${waiting
            ? html` · ${waiting} wait for the turn`
            : ''}
          · ↑↓ navigate · Enter select · Esc dismiss
        </div>
      </div>
    `;
  }

  _renderEntry(command, index, filtered) {
    const blocked = !this._actionable(command);
    return html`
      <div
        class="entry ${index === this._focusedIndex ? 'focused' : ''} ${blocked
          ? 'blocked'
          : ''}"
        role="option"
        aria-selected=${index === this._focusedIndex}
        aria-disabled=${blocked}
        title=${command.description || ''}
        @click=${() => this._onEntryClick(index, filtered)}
      >
        <span class="name">/${command.name}</span>
        ${command.argument_hint
          ? html`<span class="hint-args">${command.argument_hint}</span>`
          : ''}
        <span class="description">${command.description || ''}</span>
        ${blocked
          ? html`<span
              class="badge waiting"
              title=${command.action === 'route'
                ? 'It would swap the session out from under the turn you are watching'
                : 'It needs a turn of its own, and one is already running'}
              >when the turn ends</span
            >`
          : command.action === 'route'
            ? html`<span class="badge" title="Opens an AIC⚡DC surface"
                >opens UI</span
              >`
            : ''}
      </div>
    `;
  }
}

customElements.define('aic-slash-palette', SlashPalette);
