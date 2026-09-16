// Pure-function tests for chat-panel helpers.
//
// `generateRequestId` and the localStorage load/save helpers are all
// exported from `chat-panel/index.js`. They have no DOM dependencies, so
// most tests here exercise them directly without mounting a panel.
//
// `parseAgentTabId` was tested here too. It mapped a tab id back to the
// LLM-chosen agent id that every tagged RPC carried, and went with the
// agent-spawn protocol in `a0cb83b`: subagent tabs are keyed by the
// spawning call's `tool_use_id` and are not writable, so there is no
// tagged call left to address.
//
// The retry-prompt builders used to be tested here. They composed a
// follow-up prompt out of the native engine's edit-application report
// — which tool a diff hunk failed to anchor in, which files were not
// in context — and phase 2 deleted them along with their caller: the
// agent applies its own edits, and a failed `Edit` is reported to the
// model directly rather than turned into prose for the user to resend.
//
// `fillComposer` does touch the DOM, and is tested here anyway because
// it is the single implementation behind every "offer some words"
// affordance in the panel — the message toolbar's paste-to-prompt and
// the re-ask button on a question that was never answered. Both are
// only safe as long as this function sends nothing, so the guarantee
// is asserted once, here, rather than in each caller's suite.

import { afterEach, describe, expect, it } from 'vitest';

import {
  generateRequestId,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _loadSearchToggle,
  _saveSearchToggle,
} from '../chat-panel/index.js';
// formatRunDuration isn't re-exported from index.js (it's an
// internal render helper), so import it from its module.
import { fillComposer, formatRunDuration } from './helpers.js';
import './test-helpers.js';

// ---------------------------------------------------------------------------
// generateRequestId
// ---------------------------------------------------------------------------

describe('generateRequestId', () => {
  it('has the epoch-ms-plus-suffix shape', () => {
    const id = generateRequestId();
    expect(id).toMatch(/^\d+-[a-z0-9]{1,6}$/);
  });

  it('produces distinct IDs across calls', () => {
    // Even same-millisecond calls must differ — the random
    // suffix breaks ties.
    const ids = new Set();
    for (let i = 0; i < 100; i += 1) ids.add(generateRequestId());
    expect(ids.size).toBe(100);
  });
});

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------

describe('search-toggle persistence', () => {
  it('search ignore-case defaults to true when no stored value', () => {
    expect(
      _loadSearchToggle(_SEARCH_IGNORE_CASE_KEY, true),
    ).toBe(true);
  });

  it('search regex defaults to false when no stored value', () => {
    expect(_loadSearchToggle(_SEARCH_REGEX_KEY, false)).toBe(false);
  });

  it('search toggle round-trips via save/load', () => {
    _saveSearchToggle(_SEARCH_REGEX_KEY, true);
    expect(_loadSearchToggle(_SEARCH_REGEX_KEY, false)).toBe(true);
  });

  it('search toggle malformed localStorage value falls back to default', () => {
    localStorage.setItem(_SEARCH_REGEX_KEY, 'maybe');
    expect(_loadSearchToggle(_SEARCH_REGEX_KEY, false)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// formatRunDuration
// ---------------------------------------------------------------------------

describe('formatRunDuration', () => {
  it('renders sub-minute durations as seconds with one decimal', () => {
    expect(formatRunDuration(0)).toBe('0.0s');
    expect(formatRunDuration(4200)).toBe('4.2s');
    expect(formatRunDuration(59900)).toBe('59.9s');
  });

  it('renders minute-scale durations as "Mm SSs" with padding', () => {
    expect(formatRunDuration(60000)).toBe('1m 00s');
    expect(formatRunDuration(64000)).toBe('1m 04s');
    expect(formatRunDuration(125000)).toBe('2m 05s');
  });

  it('renders hour-scale durations as "Hh MMm" with padding', () => {
    expect(formatRunDuration(3600000)).toBe('1h 00m');
    expect(formatRunDuration(3600000 + 5 * 60000)).toBe('1h 05m');
    expect(formatRunDuration(2 * 3600000 + 5 * 60000)).toBe('2h 05m');
  });

  it('clamps negative and non-finite inputs to zero', () => {
    expect(formatRunDuration(-1000)).toBe('0.0s');
    expect(formatRunDuration(NaN)).toBe('0.0s');
    expect(formatRunDuration(Infinity)).toBe('0.0s');
  });
});

// ---------------------------------------------------------------------------
// fillComposer
// ---------------------------------------------------------------------------

const hosts = [];

/**
 * A panel stand-in with a real composer in a real shadow root.
 *
 * Mounting the whole chat panel would work too, but the contract under test
 * is exactly "find `.input-textarea` in `shadowRoot`, edit its value, leave
 * it alone otherwise" — so the smallest thing that satisfies the query is
 * also the clearest statement of what the function is allowed to touch.
 */
function panelWithComposer(value = '', cursor = value.length) {
  const host = document.createElement('div');
  const root = host.attachShadow({ mode: 'open' });
  const ta = document.createElement('textarea');
  ta.className = 'input-textarea';
  ta.value = value;
  root.appendChild(ta);
  document.body.appendChild(host);
  hosts.push(host);
  ta.setSelectionRange(cursor, cursor);
  return { panel: { shadowRoot: root, _input: value }, root, ta };
}

afterEach(() => {
  while (hosts.length) hosts.pop().remove();
});

describe('fillComposer', () => {
  it('writes at the cursor without disturbing the rest of the draft', () => {
    const { panel, ta } = panelWithComposer('before after', 7);
    fillComposer(panel, 'X');
    expect(ta.value).toBe('before Xafter');
    expect(panel._input).toBe('before Xafter');
  });

  it('replaces a selection instead of pushing it aside', () => {
    const { panel, ta } = panelWithComposer('before after');
    ta.setSelectionRange(0, 6);
    fillComposer(panel, 'X');
    expect(ta.value).toBe('X after');
  });

  it('leaves the caret after what it wrote, in a focused box', () => {
    // So the user can keep typing on the end of the offered text — the point
    // of filling the box rather than sending it.
    const { panel, root, ta } = panelWithComposer('draft ', 6);
    fillComposer(panel, 'more words');
    expect(ta.selectionStart).toBe(16);
    expect(ta.selectionEnd).toBe(16);
    expect(root.activeElement).toBe(ta);
  });

  it('tells the panel about a value it did not see typed', () => {
    // The draft save, the mention filter and the slash palette all hang off
    // the composer's own input handler; a silent assignment would skip them.
    const { panel, ta } = panelWithComposer('');
    const seen = [];
    ta.addEventListener('input', (event) => seen.push(event.bubbles));
    fillComposer(panel, 'hello');
    expect(seen).toEqual([true]);
  });

  it('sends nothing', () => {
    // The whole reason anything is allowed to write the prompt box.
    const { panel } = panelWithComposer('');
    const sends = [];
    panel.sendMessage = () => sends.push('sendMessage');
    panel._sendPrompt = () => sends.push('_sendPrompt');
    fillComposer(panel, 'The question was never answered. Please ask it again.');
    expect(sends).toEqual([]);
  });

  it('falls back to the draft property when the composer is not rendered', () => {
    // The panel may be showing a tab that has no input — a subagent
    // transcript, say — and `_input` is what the next render reads.
    const host = document.createElement('div');
    const panel = { shadowRoot: host.attachShadow({ mode: 'open' }), _input: 'draft ' };
    fillComposer(panel, 'more');
    expect(panel._input).toBe('draft more');
  });

  it('starts a draft from nothing when there is no `_input` yet', () => {
    const panel = { shadowRoot: null, _input: undefined };
    fillComposer(panel, 'text');
    expect(panel._input).toBe('text');
  });

  it('does nothing without a panel or without text', () => {
    expect(() => fillComposer(null, 'text')).not.toThrow();
    expect(() => fillComposer(undefined, 'text')).not.toThrow();
    const { panel, ta } = panelWithComposer('kept');
    fillComposer(panel, '');
    fillComposer(panel, null);
    fillComposer(panel, { toString: () => 'nope' });
    expect(ta.value).toBe('kept');
    expect(panel._input).toBe('kept');
  });
});