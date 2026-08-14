// Pure-function tests for chat-panel helpers.
//
// `generateRequestId`, `parseAgentTabId`, and the localStorage
// load/save helpers are all exported from `chat-panel/index.js`.
// They have no DOM dependencies, so most tests here exercise them
// directly without mounting a panel.
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
  parseAgentTabId,
  _DRAWER_STORAGE_KEY,
  _SEARCH_IGNORE_CASE_KEY,
  _SEARCH_REGEX_KEY,
  _loadDrawerOpen,
  _loadSearchToggle,
  _saveDrawerOpen,
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
// parseAgentTabId (C2b)
// ---------------------------------------------------------------------------

describe('parseAgentTabId', () => {
  // Per specs4/5-webapp/agent-browser.md and
  // specs4/7-future/parallel-agents.md § "Agent Reuse by
  // ID", agent identity is flat — the agent's LLM-chosen
  // id from its `🟧🟧🟧 AGENT` block IS the tab id IS the
  // backend registry key. parseAgentTabId returns the id
  // directly with no parsing. The literal "main" is
  // reserved for the main conversation; everything else
  // is treated as an agent id.

  it('returns null for the main tab', () => {
    // Untagged path — the caller drops the agent_tag
    // argument so the backend uses the main conversation.
    expect(parseAgentTabId('main')).toBeNull();
  });

  it('returns the id verbatim for descriptive agent ids', () => {
    // Real LLM-chosen ids look like "frontend-trivial",
    // "backend-auth-refactor", etc. The parser is the
    // identity function for any non-"main" string.
    expect(parseAgentTabId('frontend-trivial')).toBe(
      'frontend-trivial',
    );
    expect(parseAgentTabId('backend-auth-refactor')).toBe(
      'backend-auth-refactor',
    );
  });

  it('returns the id verbatim for short ids', () => {
    // The parser does not impose a minimum length or
    // require any specific shape — any non-empty non-
    // "main" string is a valid agent id.
    expect(parseAgentTabId('a')).toBe('a');
    expect(parseAgentTabId('agent-0')).toBe('agent-0');
  });

  it('preserves arbitrary characters in the id', () => {
    // The backend does not validate id shape beyond
    // non-emptiness, so the frontend parser shouldn't
    // either. Slashes, spaces, punctuation — all pass
    // through unchanged.
    expect(parseAgentTabId('a/b/c')).toBe('a/b/c');
    expect(parseAgentTabId('with spaces')).toBe('with spaces');
    expect(parseAgentTabId('punct!@#')).toBe('punct!@#');
  });

  it('returns null for empty string', () => {
    expect(parseAgentTabId('')).toBeNull();
  });

  it('returns null for non-string input', () => {
    // Defensive — tab IDs come from Map keys so should
    // always be strings, but malformed data shouldn't
    // crash the send path.
    expect(parseAgentTabId(null)).toBeNull();
    expect(parseAgentTabId(undefined)).toBeNull();
    expect(parseAgentTabId(42)).toBeNull();
    expect(parseAgentTabId({})).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------

describe('drawer / search-toggle persistence', () => {
  it('drawer defaults to closed when localStorage has no value', () => {
    expect(_loadDrawerOpen()).toBe(false);
  });

  it('drawer defaults to closed for unrecognised localStorage value', () => {
    // Defensive — a value that isn't 'true' should parse as
    // false rather than anything weird.
    localStorage.setItem(_DRAWER_STORAGE_KEY, 'maybe');
    expect(_loadDrawerOpen()).toBe(false);
  });

  it('drawer round-trips via save/load', () => {
    _saveDrawerOpen(true);
    expect(_loadDrawerOpen()).toBe(true);
    _saveDrawerOpen(false);
    expect(_loadDrawerOpen()).toBe(false);
  });

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