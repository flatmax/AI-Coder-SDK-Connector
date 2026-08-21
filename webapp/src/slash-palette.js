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
//   - `show(commands, query)` — called on every input event
//     while the cursor is inside a command token; opens if
//     closed, re-filters if already open
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

import { LitElement, css, html } from 'lit';

import { filterCommands } from './slash-commands.js';

export class SlashPalette extends LitElement {
  static properties = {
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
     * The token after `/`, left of the cursor. Filtering
     * input, not a display string.
     */
    _query: { type: String, state: true },
    /**
     * Index into the *filtered* list. Always a valid index
     * when the filtered list is non-empty — unlike
     * aic-input-history's -1-means-nothing-yet, because a
     * palette that opens with nothing selected makes Enter
     * ambiguous.
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
  `;

  constructor() {
    super();
    this._open = false;
    this._commands = [];
    this._query = '';
    this._focusedIndex = 0;
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
   */
  show(commands, query = '') {
    const next = Array.isArray(commands) ? commands : [];
    const queryChanged = query !== this._query;
    this._commands = next;
    this._query = query;
    if (!this._open || queryChanged) this._focusedIndex = 0;
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
   * Move the highlight, wrapping at both ends.
   *
   * Wraps where `aic-input-history` clamps, and the difference
   * is the list itself: history is a long chronology the user
   * reads through, where wrapping loses their place, while
   * this is a short list ordered by relevance that they are
   * scanning for one known item.
   */
  _moveFocus(delta, filtered) {
    const count = filtered.length;
    this._focusedIndex = (this._focusedIndex + delta + count) % count;
    this.updateComplete.then(() => {
      const el = this.shadowRoot?.querySelector('.entry.focused');
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  _selectFocused(filtered) {
    const index =
      this._focusedIndex >= 0 && this._focusedIndex < filtered.length
        ? this._focusedIndex
        : 0;
    const command = filtered[index];
    if (!command) return;
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
    this._focusedIndex = index;
    this._selectFocused(filtered);
  }

  render() {
    if (!this._open) return html``;
    const filtered = this.filtered;
    const total = this._commands.length;
    return html`
      <div class="overlay" role="listbox" aria-label="Slash commands">
        ${filtered.length === 0
          ? html`<div class="empty">
              No command matches /${this._query}
            </div>`
          : html`<div class="entries">
              ${filtered.map(
                (command, index) => html`
                  <div
                    class="entry ${index === this._focusedIndex
                      ? 'focused'
                      : ''}"
                    role="option"
                    aria-selected=${index === this._focusedIndex}
                    title=${command.description || ''}
                    @click=${() => this._onEntryClick(index, filtered)}
                  >
                    <span class="name">/${command.name}</span>
                    ${command.argument_hint
                      ? html`<span class="hint-args"
                          >${command.argument_hint}</span
                        >`
                      : ''}
                    <span class="description"
                      >${command.description || ''}</span
                    >
                    ${command.action === 'route'
                      ? html`<span class="badge" title="Opens an AIC⚡DC surface"
                          >opens UI</span
                        >`
                      : ''}
                  </div>
                `,
              )}
            </div>`}
        <div class="hint">
          ${filtered.length} of ${total} · ↑↓ navigate · Enter select · Esc
          dismiss
        </div>
      </div>
    `;
  }
}

customElements.define('aic-slash-palette', SlashPalette);
