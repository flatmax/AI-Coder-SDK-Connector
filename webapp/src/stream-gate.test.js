import { describe, expect, it } from 'vitest';

import { noteStreamChunk, noteStreamComplete } from './stream-gate.js';

function event(requestId) {
  return { detail: requestId ? { requestId } : {} };
}

describe('stream gate', () => {
  it('locks on a chunk and opens on its completion', () => {
    const host = { _streaming: false };
    noteStreamChunk(host, event('req-1'));
    expect(host._streaming).toBe(true);
    noteStreamComplete(host, event('req-1'));
    expect(host._streaming).toBe(false);
  });

  it('an older turn’s late result does not open it under a newer turn', () => {
    // A turn whose background subagents outlive its result keeps streaming
    // their work after the next turn has started.
    const host = { _streaming: false };
    noteStreamChunk(host, event('req-1'));
    noteStreamChunk(host, event('req-2'));
    noteStreamComplete(host, event('req-1'));
    expect(host._streaming).toBe(true);
    noteStreamComplete(host, event('req-2'));
    expect(host._streaming).toBe(false);
  });

  it('a completion naming no request releases everything', () => {
    const host = { _streaming: false };
    noteStreamChunk(host, event('req-1'));
    noteStreamChunk(host, event('req-2'));
    noteStreamComplete(host, event(null));
    expect(host._streaming).toBe(false);
  });

  it('a completion before any chunk leaves it open', () => {
    const host = { _streaming: false };
    noteStreamComplete(host, event('req-1'));
    expect(host._streaming).toBe(false);
  });
});
