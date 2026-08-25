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

import { describe, expect, it } from 'vitest';

import {
  generateRequestId,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _loadSearchToggle,
  _saveSearchToggle,
} from '../chat-panel/index.js';
// formatRunDuration isn't re-exported from index.js (it's an
// internal render helper), so import it from its module.
import { formatRunDuration } from './helpers.js';
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