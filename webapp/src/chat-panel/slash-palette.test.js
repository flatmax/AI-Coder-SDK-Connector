// Tests for the `/` palette's wiring into the chat panel —
// the slash-commands section of input.js.
//
// The palette component's own behaviour is tested in
// ../slash-palette.test.js and the detection rules in
// ../slash-commands.test.js. What is under test here is the
// part that needs a panel: when the list is fetched, when it
// is cached, and what selecting an entry does to the composer.

import { describe, expect, it, vi } from 'vitest';

import { openSlashPalette } from './input.js';
import { mountPanel, publishFakeRpc, settle } from './test-helpers.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FULL = [
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

const ROUTES_ONLY = [
  {
    name: 'clear',
    aliases: [],
    argument_hint: '',
    description: 'Start a new session',
    action: 'route',
    target: 'new-session',
    during_turn: false,
  },
  {
    name: 'resume',
    aliases: [],
    argument_hint: '',
    description: 'Browse and resume an earlier session',
    action: 'route',
    target: 'history',
    during_turn: false,
  },
];

/** Type into the composer, cursor at the end. */
function type(panel, text) {
  const ta = panel.shadowRoot.querySelector('.input-textarea');
  ta.value = text;
  ta.selectionStart = ta.selectionEnd = text.length;
  ta.dispatchEvent(new Event('input'));
  return ta;
}

const palette = (panel) => panel.shadowRoot.querySelector('aic-slash-palette');

/** Let the list_commands round trip land and the palette re-render. */
async function landFetch(panel) {
  await panel._slashCommandsPending;
  await new Promise((r) => setTimeout(r, 0));
  await settle(panel);
}

// ---------------------------------------------------------------------------
// Fetching and caching
// ---------------------------------------------------------------------------

describe('ChatPanel slash palette fetching', () => {
  it('opens on a leading slash with the fetched list', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).isOpen).toBe(true);
    expect(palette(p).filtered.map((c) => c.name)).toEqual([
      'compact',
      'context',
    ]);
  });

  it('does not fetch until a slash is typed', async () => {
    const list = vi.fn(async () => ({ commands: FULL }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, 'hello');
    await settle(p);
    expect(list).not.toHaveBeenCalled();
  });

  it('fetches once across a burst of keystrokes', async () => {
    const list = vi.fn(async () => ({ commands: FULL }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    type(p, '/c');
    type(p, '/co');
    await landFetch(p);
    type(p, '/con');
    await settle(p);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('closes once whitespace settles the command', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    type(p, '/compact ');
    await settle(p);
    expect(palette(p).isOpen).toBe(false);
  });

  it('never opens for a slash that is not leading', async () => {
    const list = vi.fn(async () => ({ commands: FULL }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, 'what does /compact do');
    await settle(p);
    expect(palette(p).isOpen).toBe(false);
    expect(list).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The disconnected engine
// ---------------------------------------------------------------------------

describe('ChatPanel slash palette before the engine connects', () => {
  it('opens with the routed commands a disconnected engine offers', async () => {
    // The engine connects on the first turn, so this is the
    // state the palette is in for every fresh start — it must
    // still open, or it is useless exactly when it is wanted.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({
        commands: ROUTES_ONLY,
        partial: true,
      }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).isOpen).toBe(true);
    expect(palette(p).filtered.map((c) => c.name)).toEqual(['clear', 'resume']);
  });

  it('caches the partial list rather than re-asking per keystroke', async () => {
    const list = vi.fn(async () => ({ commands: ROUTES_ONLY, partial: true }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    type(p, '/c');
    type(p, '/cl');
    await settle(p);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('re-asks once the engine reports itself connected', async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce({ commands: ROUTES_ONLY, partial: true })
      .mockResolvedValueOnce({ commands: FULL });
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(p._slashCommandsPartial).toBe(true);

    p._engineHealth = { connected: true };
    type(p, '/');
    await landFetch(p);
    expect(list).toHaveBeenCalledTimes(2);
    expect(p._slashCommandsPartial).toBe(false);
    expect(palette(p).filtered.map((c) => c.name)).toEqual([
      'compact',
      'context',
    ]);
  });

  it('stops re-asking once the full list is in hand', async () => {
    const list = vi.fn(async () => ({ commands: FULL }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    p._engineHealth = { connected: true };
    await settle(p);
    type(p, '/');
    await landFetch(p);
    type(p, '/c');
    await settle(p);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('shows the stale list while the refresh is in flight', async () => {
    // An overlay that grows beats one that appears late.
    let release;
    const list = vi
      .fn()
      .mockResolvedValueOnce({ commands: ROUTES_ONLY, partial: true })
      .mockImplementationOnce(
        () => new Promise((r) => { release = () => r({ commands: FULL }); }),
      );
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);

    p._engineHealth = { connected: true };
    type(p, '/');
    await settle(p);
    expect(palette(p).isOpen).toBe(true);
    expect(palette(p).filtered.map((c) => c.name)).toEqual(['clear', 'resume']);
    release();
    await landFetch(p);
    expect(palette(p).filtered.map((c) => c.name)).toEqual([
      'compact',
      'context',
    ]);
  });
});

// ---------------------------------------------------------------------------
// Failure
// ---------------------------------------------------------------------------

describe('ChatPanel slash palette failure', () => {
  it('does not cache an error as an empty list', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ error: 'session lost' }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).isOpen).toBe(false);
    expect(p._slashCommands).toBe(null);
    expect(p._slashCommandsFailedAt).toBeGreaterThan(0);
  });

  it('holds off for the retry window rather than asking per keystroke', async () => {
    const list = vi.fn(async () => ({ error: 'session lost' }));
    publishFakeRpc({ 'ClaudeCodeService.list_commands': list });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    type(p, '/c');
    type(p, '/co');
    await settle(p);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it('survives a thrown RPC', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => {
        throw new Error('transport down');
      },
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).isOpen).toBe(false);
  });

  it('shows no overlay when the engine advertises nothing', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: [] }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).isOpen).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Selecting
// ---------------------------------------------------------------------------

describe('ChatPanel slash palette selection', () => {
  it('completes a sent command in the composer', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    const ta = type(p, '/comp');
    await landFetch(p);
    palette(p).handleKey(new KeyboardEvent('keydown', { key: 'Enter' }));
    await settle(p);
    // Trailing space because /compact takes an argument.
    expect(ta.value).toBe('/compact ');
    expect(p._input).toBe('/compact ');
    expect(palette(p).isOpen).toBe(false);
  });

  it('replaces the whole token, not just what was left of the cursor', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    const ta = type(p, '/context');
    await landFetch(p);
    // Cursor back inside the token: `/con|text`.
    ta.selectionStart = ta.selectionEnd = 4;
    ta.dispatchEvent(new Event('input'));
    await landFetch(p);
    palette(p).handleKey(new KeyboardEvent('keydown', { key: 'Tab' }));
    await settle(p);
    expect(ta.value).not.toContain('texttext');
  });

  it('clears the token and opens the surface for a routed command', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    const asked = [];
    p.addEventListener('request-dialog-tab', (e) => asked.push(e.detail));
    const ta = type(p, '/context');
    await landFetch(p);
    palette(p).handleKey(new KeyboardEvent('keydown', { key: 'Enter' }));
    await settle(p);
    expect(ta.value).toBe('');
    expect(p._input).toBe('');
    expect(asked.length).toBe(1);
  });

  it('leaves an unmatched command alone and sendable', async () => {
    const started = vi.fn(async () => ({ status: 'started' }));
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
      'ClaudeCodeService.chat_streaming': started,
    });
    const p = mountPanel();
    await settle(p);
    const ta = type(p, '/zzz');
    await landFetch(p);
    // The palette is up saying nothing matches, but Enter is
    // not its to consume.
    const event = new KeyboardEvent('keydown', { key: 'Enter' });
    expect(palette(p).handleKey(event)).toBe(false);
    expect(ta.value).toBe('/zzz');
    expect(started).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The composer's palette button
// ---------------------------------------------------------------------------
//
// The button is the drawer's replacement (CC-22), and it opens the palette by
// typing `/` rather than by setting a flag — so what these tests check is that
// the composer really holds a `/` afterwards, not just that an overlay is up.
// If the button ever short-circuits into `palette.show()`, the filter, Escape
// and send-anyway paths all stop agreeing with the engine, and only the
// composer assertions here would notice.

const button = (panel) =>
  panel.shadowRoot.querySelector('.slash-palette-button');

describe('ChatPanel slash palette button', () => {
  it('types a slash and opens the palette', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    button(p).click();
    await landFetch(p);
    expect(p.shadowRoot.querySelector('.input-textarea').value).toBe('/');
    expect(p._input).toBe('/');
    expect(palette(p).isOpen).toBe(true);
    // Nothing typed after the slash, so the whole list is on show.
    expect(palette(p).filtered).toHaveLength(FULL.length);
  });

  it('leaves the cursor after the slash so typing filters', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    button(p).click();
    await landFetch(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    expect(ta.selectionStart).toBe(1);
    // Continue as the user would, from where the button left them.
    ta.value = '/con';
    ta.selectionStart = ta.selectionEnd = 4;
    ta.dispatchEvent(new Event('input'));
    await settle(p);
    expect(palette(p).filtered.map((c) => c.name)).toEqual(['context']);
  });

  it('is disabled once the composer has text', async () => {
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    expect(button(p).disabled).toBe(false);
    type(p, 'half a thought');
    await settle(p);
    expect(button(p).disabled).toBe(true);
  });

  it('refuses to overwrite a draft even when invoked directly', async () => {
    // The disabled attribute is a render behind a fast keystroke, so
    // the guard has to be in the handler too — losing a half-written
    // prompt is not recoverable.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    const ta = type(p, 'half a thought');
    openSlashPalette(p);
    await settle(p);
    expect(ta.value).toBe('half a thought');
    expect(palette(p).isOpen).toBe(false);
  });

  it('stays live while a turn streams', async () => {
    // Part of the list is reachable mid-turn — the routed commands are
    // answered from the route table and never start a turn — so the
    // entry point stays open and the rows say which ones. Disabling it
    // would withhold the reachable rows to avoid explaining the rest.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    p._streaming = true;
    await settle(p);
    expect(button(p).disabled).toBe(false);
    button(p).click();
    await landFetch(p);
    expect(palette(p).isOpen).toBe(true);
  });

  it('tells the palette a turn is streaming', async () => {
    // Bound as a property rather than passed to show(), so a turn
    // starting or ending while the overlay is up re-renders the rows.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    type(p, '/');
    await landFetch(p);
    expect(palette(p).streaming).toBe(false);
    p._streaming = true;
    await settle(p);
    expect(palette(p).streaming).toBe(true);
    expect(palette(p).isOpen).toBe(true);
  });

  it('routes a mid-turn command without sending anything', async () => {
    // The whole point of the exercise: /context opens the tab while the
    // turn it is reporting on is still running.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    const tabs = [];
    p.addEventListener('request-dialog-tab', (e) => tabs.push(e.detail.tab));
    p._streaming = true;
    await settle(p);
    button(p).click();
    await landFetch(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = '/context';
    ta.selectionStart = ta.selectionEnd = 8;
    ta.dispatchEvent(new Event('input'));
    await settle(p);
    palette(p).handleKey(
      new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }),
    );
    await settle(p);
    expect(tabs).toEqual(['context']);
    expect(p._input).toBe('');
  });

  it('Escape closes the overlay and leaves the slash behind', async () => {
    // Dismissing never edits the composer — including when the button
    // is what put the `/` there. A second Escape is the input-clear
    // rung, which is the composer's job and not the palette's.
    publishFakeRpc({
      'ClaudeCodeService.list_commands': async () => ({ commands: FULL }),
    });
    const p = mountPanel();
    await settle(p);
    button(p).click();
    await landFetch(p);
    palette(p).handleKey(new KeyboardEvent('keydown', { key: 'Escape' }));
    await settle(p);
    expect(palette(p).isOpen).toBe(false);
    expect(p.shadowRoot.querySelector('.input-textarea').value).toBe('/');
  });
});
