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

const COMMANDS = [
  {
    name: 'clear',
    aliases: ['reset'],
    argument_hint: '[name]',
    description: 'Start a new session',
    action: 'route',
    target: 'new-session',
  },
  {
    name: 'compact',
    aliases: [],
    argument_hint: '<instructions>',
    description: 'Free up context',
    action: 'send',
    target: '',
  },
  {
    name: 'context',
    aliases: [],
    argument_hint: '',
    description: 'Show context usage',
    action: 'route',
    target: 'tab:context',
  },
];

const _mounted = [];

function mountPalette() {
  const el = document.createElement('ac-slash-palette');
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
