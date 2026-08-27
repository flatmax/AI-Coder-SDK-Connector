// Tests for webapp/src/slash-commands.js — detection and
// ranking for the `/` palette.
//
// No DOM mount. The boundary rules are the whole point of
// the module, and they read better as a table of
// (value, cursor) → result than as keystrokes against a
// mounted chat panel.

import { describe, expect, it } from 'vitest';

import {
  completionFor,
  detectActiveSlash,
  filterCommands,
} from './slash-commands.js';

// ---------------------------------------------------------------------------
// detectActiveSlash
// ---------------------------------------------------------------------------

describe('detectActiveSlash', () => {
  it('detects a bare slash with the cursor after it', () => {
    expect(detectActiveSlash('/', 1)).toEqual({
      start: 0,
      end: 1,
      query: '',
    });
  });

  it('reports the query as the text left of the cursor', () => {
    expect(detectActiveSlash('/context', 4)).toEqual({
      start: 0,
      end: 8,
      query: 'con',
    });
  });

  it('ends the token at the whole word, not the cursor', () => {
    // The asymmetry that keeps mid-token completion from
    // leaving `/contexttext` behind: query stops at the
    // cursor, end runs to the end of the token.
    const token = detectActiveSlash('/context', 4);
    expect(token.query).toBe('con');
    expect(token.end).toBe(8);
  });

  it('tolerates leading whitespace, as the engine does', () => {
    // service.py strips before testing for the slash, so
    // this must agree or the palette and the engine would
    // disagree about what got sent.
    expect(detectActiveSlash('  /ctx', 6)).toEqual({
      start: 2,
      end: 6,
      query: 'ctx',
    });
  });

  it('stops once the command is settled by whitespace', () => {
    // `/context |` — the user is typing arguments now.
    expect(detectActiveSlash('/context ', 9)).toBeNull();
  });

  it('keeps the token while the cursor is still inside it', () => {
    // Same value, cursor back inside the command word.
    expect(detectActiveSlash('/context arg', 5)).toEqual({
      start: 0,
      end: 8,
      query: 'cont',
    });
  });

  it('ignores a slash that is not the first non-whitespace char', () => {
    expect(detectActiveSlash('see src/foo.py', 11)).toBeNull();
    expect(detectActiveSlash('what does /compact do?', 18)).toBeNull();
    expect(detectActiveSlash('a\n/context', 10)).toBeNull();
  });

  it('ignores a cursor at or before the slash', () => {
    expect(detectActiveSlash('/context', 0)).toBeNull();
    expect(detectActiveSlash('  /context', 2)).toBeNull();
  });

  it('returns null for empty and whitespace-only input', () => {
    expect(detectActiveSlash('', 0)).toBeNull();
    expect(detectActiveSlash('   ', 3)).toBeNull();
  });

  it('tolerates non-string and non-number arguments', () => {
    expect(detectActiveSlash(undefined, undefined)).toBeNull();
    expect(detectActiveSlash(null, 3)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// filterCommands
// ---------------------------------------------------------------------------

const COMMANDS = [
  { name: 'clear', aliases: ['reset', 'new'], description: 'Start over' },
  { name: 'code-review', aliases: ['review'], description: 'Review a diff' },
  { name: 'compact', aliases: [], description: 'Summarize' },
  { name: 'context', aliases: [], description: 'Show context usage' },
  { name: 'run', aliases: [], description: 'Launch the app' },
  { name: 'run-skill-generator', aliases: [], description: 'Author a skill' },
  { name: 'usage', aliases: ['cost', 'stats'], description: 'Session cost' },
];

const names = (list) => list.map((command) => command.name);

describe('filterCommands', () => {
  it('returns everything for an empty query, in supplied order', () => {
    expect(names(filterCommands(COMMANDS, ''))).toEqual([
      'clear',
      'code-review',
      'compact',
      'context',
      'run',
      'run-skill-generator',
      'usage',
    ]);
  });

  it('ranks an exact name above a longer prefix match', () => {
    expect(names(filterCommands(COMMANDS, 'run'))).toEqual([
      'run',
      'run-skill-generator',
    ]);
  });

  it('ranks name prefixes above alias matches', () => {
    // `co` prefixes three names; it also prefixes `cost`,
    // which is an alias of usage. The names come first.
    expect(names(filterCommands(COMMANDS, 'co'))).toEqual([
      'code-review',
      'compact',
      'context',
      'usage',
    ]);
  });

  it('finds a command by alias', () => {
    expect(names(filterCommands(COMMANDS, 'reset'))).toEqual(['clear']);
    expect(names(filterCommands(COMMANDS, 'stat'))).toEqual(['usage']);
  });

  it('falls back to a substring match', () => {
    // `review` is both an alias of code-review and a
    // substring of its name; either way it is the only hit.
    expect(names(filterCommands(COMMANDS, 'review'))).toEqual(['code-review']);
    expect(names(filterCommands(COMMANDS, 'text'))).toEqual(['context']);
  });

  it('is case-insensitive', () => {
    expect(names(filterCommands(COMMANDS, 'CoNt'))).toEqual(['context']);
  });

  it('does not match descriptions', () => {
    // "Summarize" is compact's description. Matching it would
    // put a row on screen whose reason for being there is
    // invisible in the row itself.
    expect(filterCommands(COMMANDS, 'summarize')).toEqual([]);
  });

  it('returns nothing for a query that matches nothing', () => {
    expect(filterCommands(COMMANDS, 'zzz')).toEqual([]);
  });

  it('tolerates a missing or malformed list', () => {
    expect(filterCommands(undefined, 'co')).toEqual([]);
    expect(filterCommands([{}, { name: '' }], 'co')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// completionFor
// ---------------------------------------------------------------------------

describe('completionFor', () => {
  it('adds a trailing space when the command takes arguments', () => {
    expect(completionFor({ name: 'config', argument_hint: 'key=value' })).toBe(
      '/config ',
    );
  });

  it('adds no trailing space when it does not', () => {
    expect(completionFor({ name: 'context', argument_hint: '' })).toBe(
      '/context',
    );
  });

  it('returns empty for a nameless command', () => {
    expect(completionFor({})).toBe('');
    expect(completionFor(null)).toBe('');
  });
});
