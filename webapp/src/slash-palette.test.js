// Tests for webapp/src/slash-palette.js — the `/` command
// overlay.
//
// Unit-level for the host contract (show / hide / handleKey /
// isOpen), integration-level through the shadow DOM for the
// rows and click-to-select.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { SlashPalette } from './slash-palette.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// `during_turn` mirrors the service's real answers: the two
// session-swapping routes are withheld mid-turn, the passthrough
// command is a turn of its own, and `/context` is a control request
// the streaming turn does not block.
const COMMANDS = [
  {
    name: 'clear',
    aliases: ['reset'],
    argument_hint: '[name]',
    description: 'Start a new session',
    action: 'route',
    target: 'new-session',
    during_turn: false,
  },
  {
    name: 'compact',
    aliases: [],
    argument_hint: '<instructions>',
    description: 'Free up context',
    action: 'send',
    target: '',
    during_turn: false,
  },
  {
    name: 'context',
    aliases: [],
    argument_hint: '',
    description: 'Show context usage',
    action: 'route',
    target: 'tab:context',
    during_turn: true,
  },
];

const _mounted = [];

function mountPalette() {
  const el = document.createElement('aic-slash-palette');
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

async function settle(el) {
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
}

function keyEvent(key) {
  const event = new KeyboardEvent('keydown', { key, cancelable: true });
  vi.spyOn(event, 'preventDefault');
  return event;
}

afterEach(() => {
  while (_mounted.length) {
    const el = _mounted.pop();
    if (el.isConnected) el.remove();
  }
});

// ---------------------------------------------------------------------------
// show / hide
// ---------------------------------------------------------------------------

describe('SlashPalette.show', () => {
  it('opens with the first entry focused', () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    expect(el.isOpen).toBe(true);
    expect(el._focusedIndex).toBe(0);
  });

  it('resets focus when the query changes', () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    el._focusedIndex = 2;
    el.show(COMMANDS, 'co');
    expect(el._focusedIndex).toBe(0);
  });

  it('leaves focus alone when re-shown with the same query', () => {
    // The host fires on every input event, cursor moves
    // included. Resetting there would fight the arrow keys.
    const el = mountPalette();
    el.show(COMMANDS, 'co');
    el._focusedIndex = 1;
    el.show(COMMANDS, 'co');
    expect(el._focusedIndex).toBe(1);
  });

  it('filters through filterCommands', () => {
    const el = mountPalette();
    el.show(COMMANDS, 'con');
    expect(el.filtered.map((c) => c.name)).toEqual(['context']);
  });
});

describe('SlashPalette.hide', () => {
  it('closes and clears the query', () => {
    const el = mountPalette();
    el.show(COMMANDS, 'con');
    el.hide();
    expect(el.isOpen).toBe(false);
    expect(el._query).toBe('');
  });

  it('is safe to call when already closed', () => {
    const el = mountPalette();
    expect(() => el.hide()).not.toThrow();
    expect(el.isOpen).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// handleKey
// ---------------------------------------------------------------------------

describe('SlashPalette.handleKey', () => {
  it('consumes nothing while closed', () => {
    const el = mountPalette();
    expect(el.handleKey(keyEvent('Enter'))).toBe(false);
  });

  it('moves focus down and up', () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(1);
    el.handleKey(keyEvent('ArrowUp'));
    expect(el._focusedIndex).toBe(0);
  });

  it('wraps at both ends', () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    el.handleKey(keyEvent('ArrowUp'));
    expect(el._focusedIndex).toBe(COMMANDS.length - 1);
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(0);
  });

  it('selects the focused entry on Enter', () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.show(COMMANDS, '');
    el.handleKey(keyEvent('ArrowDown'));
    const event = keyEvent('Enter');
    expect(el.handleKey(event)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(seen.map((c) => c.name)).toEqual(['compact']);
    expect(el.isOpen).toBe(false);
  });

  it('selects on Tab as well', () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.show(COMMANDS, 'con');
    expect(el.handleKey(keyEvent('Tab'))).toBe(true);
    expect(seen.map((c) => c.name)).toEqual(['context']);
  });

  it('closes on Escape without selecting', () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.show(COMMANDS, '');
    expect(el.handleKey(keyEvent('Escape'))).toBe(true);
    expect(el.isOpen).toBe(false);
    expect(seen).toEqual([]);
  });

  it('does not consume Enter when nothing matches', () => {
    // The overlay stays up to explain itself, but a stray
    // `/typo` must still reach the engine, which gives a
    // better answer about an unknown command than this list.
    const el = mountPalette();
    el.show(COMMANDS, 'zzz');
    expect(el.filtered).toEqual([]);
    expect(el.handleKey(keyEvent('Enter'))).toBe(false);
    expect(el.isOpen).toBe(true);
  });

  it('does not consume arrow keys when nothing matches', () => {
    const el = mountPalette();
    el.show(COMMANDS, 'zzz');
    expect(el.handleKey(keyEvent('ArrowDown'))).toBe(false);
  });

  it('leaves unrelated keys to the host', () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    expect(el.handleKey(keyEvent('a'))).toBe(false);
    expect(el.handleKey(keyEvent('Backspace'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

describe('SlashPalette rendering', () => {
  it('renders nothing while closed', async () => {
    const el = mountPalette();
    await settle(el);
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('renders one row per filtered command', async () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    expect(rows.length).toBe(3);
    expect(rows[0].querySelector('.name').textContent).toBe('/clear');
    expect(rows[0].querySelector('.hint-args').textContent).toBe('[name]');
  });

  it('badges a routed command and leaves a sent one plain', async () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    // clear routes, compact sends.
    expect(rows[0].querySelector('.badge')).not.toBeNull();
    expect(rows[1].querySelector('.badge')).toBeNull();
  });

  it('shows a count of matches against the total', async () => {
    const el = mountPalette();
    el.show(COMMANDS, 'con');
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint').textContent).toContain(
      '1 of 3',
    );
  });

  it('explains itself when nothing matches', async () => {
    const el = mountPalette();
    el.show(COMMANDS, 'zzz');
    await settle(el);
    const empty = el.shadowRoot.querySelector('.empty');
    expect(empty.textContent).toContain('/zzz');
    expect(el.shadowRoot.querySelector('.entry')).toBeNull();
  });

  it('selects on click', async () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.show(COMMANDS, '');
    await settle(el);
    el.shadowRoot.querySelectorAll('.entry')[2].click();
    expect(seen.map((c) => c.name)).toEqual(['context']);
    expect(el.isOpen).toBe(false);
  });

  it('keeps the rules that stop a long argument hint widening a row', () => {
    // A regression guard, not a layout test: jsdom does no
    // layout and does not resolve styles from Lit's adopted
    // stylesheet, so the rules are read from the source. What
    // it caught: /code-review advertises 70-odd characters of
    // argument hint, and an unshrinkable `.hint-args` pushed
    // the row to twice the overlay's width, putting a
    // horizontal scrollbar under a list navigated by arrow key.
    // Verified visually in a browser; only the rules' presence
    // is checked here.
    const cssText = SlashPalette.styles.cssText;
    // Anchored on the selector's opening brace: these class
    // names are also named in the comments explaining them.
    const ruleFor = (selector) => {
      const at = cssText.indexOf(`${selector} {`);
      expect(at, `${selector} rule is gone`).toBeGreaterThan(-1);
      return cssText.slice(at, cssText.indexOf('}', at));
    };
    const hintRule = ruleFor('.hint-args');
    expect(hintRule).toContain('text-overflow: ellipsis');
    expect(hintRule).toContain('max-width: 40%');
    expect(ruleFor('.entries')).toContain('overflow-x: hidden');
  });

  it('marks the focused row for assistive tech', async () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    expect(rows[0].getAttribute('aria-selected')).toBe('true');
    expect(rows[1].getAttribute('aria-selected')).toBe('false');
  });
});

// ---------------------------------------------------------------------------
// Mid-turn availability
// ---------------------------------------------------------------------------
//
// Only `query()` starts a turn, so a routed command is answerable while
// one streams and a passthrough one is not. The palette shows the whole
// list either way and marks the difference on the rows, because a list
// that silently shrinks teaches the user that commands come and go.

describe('SlashPalette while a turn streams', () => {
  /** Two rows the turn allows, so navigation has somewhere to go. */
  const TWO_LIVE = [
    { name: 'clear', description: 'Start a new session', action: 'route', during_turn: false },
    { name: 'context', description: 'Show context usage', action: 'route', during_turn: true },
    { name: 'cost', description: 'Session cost', action: 'route', during_turn: true },
  ];

  it('blocks the rows the turn holds and leaves the rest alone', async () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    expect(rows[0].classList.contains('blocked')).toBe(true);
    expect(rows[1].classList.contains('blocked')).toBe(true);
    expect(rows[2].classList.contains('blocked')).toBe(false);
  });

  it('says when a blocked row comes back, on the row', async () => {
    // Visible text rather than a tooltip: this list is navigated by
    // keyboard, and a tooltip is unreachable from there.
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    expect(rows[0].querySelector('.badge.waiting').textContent).toContain(
      'when the turn ends',
    );
    expect(rows[2].querySelector('.badge.waiting')).toBeNull();
  });

  it('drops the opens-UI badge from a blocked routed row', async () => {
    // That badge warns "this does something other than send text". A
    // row that does nothing right now has no such warning to give.
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    const clear = el.shadowRoot.querySelectorAll('.entry')[0];
    expect(clear.textContent).not.toContain('opens UI');
    expect(clear.getAttribute('aria-disabled')).toBe('true');
  });

  it('opens with focus on the first row that can be picked', () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    expect(el._focusedIndex).toBe(2);
  });

  it('highlights nothing when the whole list is waiting', async () => {
    // A highlight promises Enter does something. With nothing
    // actionable, -1 is the honest answer.
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS.slice(0, 2), '');
    await settle(el);
    expect(el._focusedIndex).toBe(-1);
    expect(el.shadowRoot.querySelector('.entry.focused')).toBeNull();
  });

  it('skips blocked rows when navigating', () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(TWO_LIVE, '');
    expect(el._focusedIndex).toBe(1);
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(2);
    // Wrapping past the blocked row rather than stopping on it.
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(1);
    el.handleKey(keyEvent('ArrowUp'));
    expect(el._focusedIndex).toBe(2);
  });

  it('leaves the highlight alone when a full lap finds nothing', () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(2);
  });

  it('consumes Enter on a blocked row and selects nothing', () => {
    // Consumed rather than passed through: the host's send refuses
    // mid-turn anyway, so letting it go would give the same silence
    // minus the overlay that was explaining it.
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.streaming = true;
    el.show(COMMANDS, 'clear');
    const event = keyEvent('Enter');
    expect(el.handleKey(event)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(seen).toEqual([]);
    expect(el.isOpen).toBe(true);
  });

  it('refuses a click on a blocked row', async () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    el.shadowRoot.querySelectorAll('.entry')[0].click();
    expect(seen).toEqual([]);
    expect(el.isOpen).toBe(true);
  });

  it('still selects a row the turn allows', async () => {
    const el = mountPalette();
    const seen = [];
    el.addEventListener('command-select', (e) => seen.push(e.detail.command));
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    el.shadowRoot.querySelectorAll('.entry')[2].click();
    expect(seen.map((c) => c.name)).toEqual(['context']);
  });

  it('moves the highlight off a row a starting turn just blocked', async () => {
    // The overlay can be up when the turn starts. Leaving focus where
    // it was would point Enter at a row it now refuses.
    const el = mountPalette();
    el.show(COMMANDS, '');
    await settle(el);
    expect(el._focusedIndex).toBe(0);
    el.streaming = true;
    await settle(el);
    expect(el._focusedIndex).toBe(2);
  });

  it('re-enables every row when the turn ends', async () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    el.streaming = false;
    await settle(el);
    const rows = el.shadowRoot.querySelectorAll('.entry');
    expect([...rows].some((row) => row.classList.contains('blocked'))).toBe(
      false,
    );
  });

  it('counts what is waiting in the hint line', async () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, '');
    await settle(el);
    const hint = el.shadowRoot.querySelector('.hint').textContent;
    expect(hint).toContain('3 of 3');
    expect(hint).toContain('2 wait for the turn');
  });

  it('says nothing about waiting when no row is', async () => {
    const el = mountPalette();
    el.show(COMMANDS, '');
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint').textContent).not.toContain(
      'wait for the turn',
    );
  });

  it('counts the waiting rows among the matches, not the whole list', async () => {
    const el = mountPalette();
    el.streaming = true;
    el.show(COMMANDS, 'c');
    await settle(el);
    // /clear and /compact match and both wait; /context matches and
    // does not. The count describes the rows on screen.
    const hint = el.shadowRoot.querySelector('.hint').textContent;
    expect(hint).toContain('3 of 3');
    expect(hint).toContain('2 wait for the turn');
    el.show(COMMANDS, 'cont');
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint').textContent).not.toContain(
      'wait for the turn',
    );
  });

  it('treats an entry with no during_turn as waiting', async () => {
    // A list cached from a service that predates the field. Withholding
    // a command that would have worked is recoverable by waiting;
    // offering one the guard then refuses is the confusing failure.
    const el = mountPalette();
    el.streaming = true;
    el.show([{ name: 'context', description: 'Show context usage', action: 'route' }], '');
    await settle(el);
    expect(el.shadowRoot.querySelector('.entry').classList).toContain(
      'blocked',
    );
  });
});

// ---------------------------------------------------------------------------
// The pre-handshake list
// ---------------------------------------------------------------------------
//
// Before the engine connects, `list_commands` answers with the routed
// commands and `partial: true` — the CLI's own list comes out of a
// handshake that has not happened. The counts alone cannot say that:
// "2 of 2" is what a complete list of two commands looks like too.

describe('SlashPalette with a partial list', () => {
  const ROUTES_ONLY = [
    { name: 'clear', description: 'Start a new session', action: 'route', during_turn: false },
    { name: 'context', description: 'Show context usage', action: 'route', during_turn: true },
  ];

  it('says why the list is short', async () => {
    const el = mountPalette();
    el.show(ROUTES_ONLY, '', { partial: true });
    await settle(el);
    const note = el.shadowRoot.querySelector('.hint-partial');
    expect(note).not.toBeNull();
    expect(note.textContent).toContain('on your first turn');
  });

  it('says nothing when the list is whole', async () => {
    const el = mountPalette();
    el.show(ROUTES_ONLY, '');
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint-partial')).toBeNull();
  });

  it('keeps saying it while the query narrows', async () => {
    // The explanation matters most on a miss, and a miss is reached by
    // typing — so it cannot be tied to the unfiltered view.
    const el = mountPalette();
    el.show(ROUTES_ONLY, '', { partial: true });
    await settle(el);
    el.show(ROUTES_ONLY, 'zzz', { partial: true });
    await settle(el);
    expect(el.shadowRoot.querySelector('.empty')).not.toBeNull();
    expect(el.shadowRoot.querySelector('.hint-partial')).not.toBeNull();
  });

  it('drops the notice when the full list replaces it', async () => {
    // Defaulting to false on every show is what does this: the host
    // stops passing the flag rather than having to unset it.
    const el = mountPalette();
    el.show(ROUTES_ONLY, '', { partial: true });
    await settle(el);
    el.show(COMMANDS, '');
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint-partial')).toBeNull();
  });

  it('still counts and still navigates', async () => {
    const el = mountPalette();
    el.show(ROUTES_ONLY, '', { partial: true });
    await settle(el);
    expect(el.shadowRoot.querySelector('.hint').textContent).toContain(
      '2 of 2',
    );
    el.handleKey(keyEvent('ArrowDown'));
    expect(el._focusedIndex).toBe(1);
  });
});
