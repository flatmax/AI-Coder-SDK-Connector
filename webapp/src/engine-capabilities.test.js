// Tests for engine-capabilities.js — the browser half of the descriptor.
//
// Two assertions are load-bearing.
//
// **A missing descriptor hides nothing.** The descriptor is how a panel learns
// to hide; if fetching it fails, the app must behave exactly as it does today
// on the shipped engine. The opposite default would turn one failed RPC into a
// blank UI, which is a far worse failure than a panel with no data in it.
//
// **Nothing branches on an engine name.** AG-R-4. The descriptor carries no
// engine identity at all, so there is nothing to switch on — and this file
// checks that the payload it is handed really has none, because the rule is
// only as good as the shape that enforces it.

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  SURFACE,
  descriptor,
  isLoaded,
  loadCapabilities,
  resetCapabilities,
  setCapabilities,
  supports,
  surfaceDetail,
  surfacesWithStatus,
} from './engine-capabilities.js';

/** A descriptor shaped like the server's, for the Antigravity engine. */
const ANTIGRAVITY = {
  usd_cost: {
    title: 'USD cost',
    supported: false,
    status: 'absent',
    note: 'tokens only',
  },
  context_window_usage: {
    title: 'Context window',
    supported: false,
    status: 'absent',
    note: 'write-only threshold',
  },
  transcript_history: {
    title: 'History',
    supported: false,
    status: 'unbuilt',
    note: 'phase 5',
  },
  image_generation: {
    title: 'Generated images',
    supported: true,
    status: 'supported',
    note: '',
  },
};

function host(result, { throws = false } = {}) {
  return {
    rpcExtract: vi.fn(async () => {
      if (throws) throw new Error('no proxy published');
      return result;
    }),
  };
}

beforeEach(() => {
  resetCapabilities();
});

// ---------------------------------------------------------------------
// The first one that matters: a missing descriptor hides nothing
// ---------------------------------------------------------------------

describe('the default before the answer arrives', () => {
  it('supports everything while loading', () => {
    expect(isLoaded()).toBe(false);
    expect(supports(SURFACE.USD_COST)).toBe(true);
    expect(supports(SURFACE.CONTEXT_WINDOW_USAGE)).toBe(true);
  });

  it('supports everything when the fetch fails', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    await loadCapabilities(host(null, { throws: true }));
    expect(supports(SURFACE.USD_COST)).toBe(true);
    expect(isLoaded()).toBe(false);
    debug.mockRestore();
  });

  it('supports everything when the engine answers with an error', async () => {
    await loadCapabilities(host({ error: 'no-engine' }));
    expect(supports(SURFACE.USD_COST)).toBe(true);
  });

  it('treats an unknown key as supported', async () => {
    // Most likely a webapp built against a newer server. Hiding a panel over
    // a version skew is worse than showing one whose data may be empty.
    await loadCapabilities(host(ANTIGRAVITY));
    expect(supports('a_surface_from_the_future')).toBe(true);
  });
});

// ---------------------------------------------------------------------
// The second: no engine name to branch on
// ---------------------------------------------------------------------

describe('AG-R-4 — the payload carries no engine identity', () => {
  it('has no identifying field at the top level', async () => {
    await loadCapabilities(host(ANTIGRAVITY));
    for (const key of ['engine', 'engine_name', 'name', 'id', 'adapter']) {
      expect(descriptor()[key]).toBeUndefined();
    }
  });

  it('has no identifying field on an entry', async () => {
    await loadCapabilities(host(ANTIGRAVITY));
    for (const entry of Object.values(descriptor())) {
      for (const key of ['engine', 'name', 'adapter']) {
        expect(entry[key]).toBeUndefined();
      }
    }
  });

  it('exposes surface keys as constants so a typo cannot hide a panel', () => {
    // A free string that is misspelled reads as "unknown key" and therefore
    // as supported, which is silent. The constant makes it a build error.
    expect(SURFACE.USD_COST).toBe('usd_cost');
    expect(Object.isFrozen(SURFACE)).toBe(true);
  });
});

// ---------------------------------------------------------------------
// Reading the answer
// ---------------------------------------------------------------------

describe('once loaded', () => {
  beforeEach(() => setCapabilities(ANTIGRAVITY));

  it('hides a surface the engine cannot feed', () => {
    expect(supports(SURFACE.USD_COST)).toBe(false);
    expect(supports(SURFACE.CONTEXT_WINDOW_USAGE)).toBe(false);
  });

  it('keeps a surface the engine can feed', () => {
    expect(supports(SURFACE.IMAGE_GENERATION)).toBe(true);
  });

  it('hides absent and unbuilt identically', () => {
    // Why there is no data is not the render path's business.
    expect(supports(SURFACE.USD_COST)).toBe(false);
    expect(supports(SURFACE.TRANSCRIPT_HISTORY)).toBe(false);
  });

  it('keeps the distinction for a diagnostics view', () => {
    expect(surfaceDetail(SURFACE.USD_COST).status).toBe('absent');
    expect(surfaceDetail(SURFACE.TRANSCRIPT_HISTORY).status).toBe('unbuilt');
  });

  it('reports itself loaded', () => {
    expect(isLoaded()).toBe(true);
  });
});

// ---------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------

describe('loadCapabilities', () => {
  it('fetches once and caches', async () => {
    const h = host(ANTIGRAVITY);
    await loadCapabilities(h);
    await loadCapabilities(h);
    expect(h.rpcExtract).toHaveBeenCalledTimes(1);
  });

  it('shares one round trip between concurrent callers', async () => {
    const h = host(ANTIGRAVITY);
    await Promise.all([loadCapabilities(h), loadCapabilities(h), loadCapabilities(h)]);
    expect(h.rpcExtract).toHaveBeenCalledTimes(1);
  });

  it('asks the shared namespace, not a per-engine one', async () => {
    // AG-3: there is no second namespace and no AntigravityService.*
    const h = host(ANTIGRAVITY);
    await loadCapabilities(h);
    expect(h.rpcExtract).toHaveBeenCalledWith(
      'ClaudeCodeService.get_engine_capabilities',
    );
  });

  it('refetches after a reset, because the engine may have changed', async () => {
    const h = host(ANTIGRAVITY);
    await loadCapabilities(h);
    resetCapabilities();
    await loadCapabilities(h);
    expect(h.rpcExtract).toHaveBeenCalledTimes(2);
  });

  it('does not cache a failure as an answer', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    const h = host(null, { throws: true });
    await loadCapabilities(h);
    await loadCapabilities(h);
    expect(h.rpcExtract).toHaveBeenCalledTimes(2);
    debug.mockRestore();
  });
});

// ---------------------------------------------------------------
// surfacesWithStatus — the one reader that separates absent from unbuilt
// ---------------------------------------------------------------
//
// The webapp hides both identically and must keep doing so. This exists
// for the single place the difference is the whole point: telling a user
// why the application is missing features. `unbuilt` means this project
// built a feature and has not wired it to the running engine — an
// unfinished application. `absent` means the engine has no source data
// and never will — a complete application over a different engine. Only
// the first is worth saying out loud, which is why they are asked for
// separately rather than as one "not supported" list.

describe('surfacesWithStatus', () => {
  beforeEach(() => resetCapabilities());

  it('answers empty before the descriptor has loaded', () => {
    expect(surfacesWithStatus('unbuilt')).toEqual([]);
  });

  it('separates unbuilt from absent rather than lumping them as hidden', () => {
    setCapabilities(ANTIGRAVITY);
    expect(surfacesWithStatus('unbuilt').map((s) => s.key))
      .toEqual(['transcript_history']);
    expect(surfacesWithStatus('absent').map((s) => s.key))
      .toEqual(['context_window_usage', 'usd_cost']);
  });

  it('carries the descriptor title, so the browser does not rename a surface',
    () => {
      setCapabilities(ANTIGRAVITY);
      expect(surfacesWithStatus('supported')).toEqual([
        { key: 'image_generation', title: 'Generated images', note: '' },
      ]);
    });

  it('sorts by title, so the list does not reorder between renders', () => {
    setCapabilities(ANTIGRAVITY);
    expect(surfacesWithStatus('absent').map((s) => s.title))
      .toEqual(['Context window', 'USD cost']);
  });
});
