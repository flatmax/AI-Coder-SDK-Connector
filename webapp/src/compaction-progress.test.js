// Tests for CompactionProgress overlay.
//
// Fake timers for deterministic timing, no rAF / no settle() helper
// (per D15 in IMPLEMENTATION_NOTES.md — fake timers break jsdom's rAF).
//
// The point of the component is that it outlives the toast it replaced, so
// most of what is pinned here is lifetime: it appears on the hook's event,
// stays through an arbitrarily long pause, and leaves only when the engine
// reports the boundary — or when the boundary never comes.

import {
  afterEach, beforeEach, describe, expect, it, vi,
} from 'vitest';

import './compaction-progress.js';

const _mounted = [];

function mountOverlay() {
  const el = document.createElement('ac-compaction-progress');
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

/** The PreCompact hook's broadcast, as the app shell re-dispatches it. */
function firePreCompact(trigger = 'auto') {
  window.dispatchEvent(new CustomEvent('system-event', {
    detail: { requestId: null, data: { subtype: 'pre_compact', data: { trigger } } },
  }));
}

/** The stream's own `compact_boundary`, via the compactionEvent callback. */
function fireBoundary(payload = {}) {
  window.dispatchEvent(new CustomEvent('compaction-event', {
    detail: {
      requestId: 'req-1',
      event: { stage: 'compact_boundary', ...payload },
    },
  }));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  while (_mounted.length) {
    const el = _mounted.pop();
    if (el.parentNode) el.parentNode.removeChild(el);
  }
});

describe('CompactionProgress initial state', () => {
  it('renders nothing before any event fires', async () => {
    const el = mountOverlay();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });
});

describe('CompactionProgress active state', () => {
  it('appears on the PreCompact broadcast', async () => {
    const el = mountOverlay();
    firePreCompact('auto');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.textContent).toContain('Compacting conversation');
  });

  it('names the trigger in the words the divider uses', async () => {
    // `auto` on the wire, "automatic" on screen — the same normalisation
    // `compactionSummary` applies, so the overlay and the divider that
    // replaces it 20 seconds later do not describe one compaction two ways.
    const el = mountOverlay();
    firePreCompact('auto');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.label').textContent)
      .toContain('(automatic)');
  });

  it('passes an unrecognised trigger through verbatim', async () => {
    const el = mountOverlay();
    firePreCompact('microcompact');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.label').textContent)
      .toContain('(microcompact)');
  });

  it('says the plain thing when no trigger came through', async () => {
    // `hooks.py` reads the trigger with `.get()` off a CLI-owned dict, so a
    // null is a shape that reaches us.
    const el = mountOverlay();
    firePreCompact(null);
    await el.updateComplete;
    const label = el.shadowRoot.querySelector('.label').textContent;
    expect(label).toContain('Compacting conversation');
    expect(label).not.toContain('(');
  });

  it('shows a spinner and an indeterminate bar', async () => {
    const el = mountOverlay();
    firePreCompact();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
    const fill = el.shadowRoot.querySelector('.bar-fill');
    expect(fill.classList.contains('indeterminate')).toBe(true);
  });

  it('leaves the progressbar without a value', async () => {
    // No aria-valuenow, deliberately: an indeterminate progressbar is the
    // ARIA way of saying the quantity is unknown, and the quantity IS
    // unknown — the engine reports nothing between start and finish.
    const el = mountOverlay();
    firePreCompact();
    await el.updateComplete;
    const bar = el.shadowRoot.querySelector('[role="progressbar"]');
    expect(bar).not.toBeNull();
    expect(bar.getAttribute('aria-valuenow')).toBeNull();
  });

  it('holds no elapsed reading for the first second', async () => {
    const el = mountOverlay();
    firePreCompact();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed')).toBeNull();
  });

  it('ticks the elapsed counter once per second', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(14000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('14s');
  });

  it('outlives a toast by a wide margin', async () => {
    // The regression this component exists for: at 3 seconds the old toast
    // was gone and the compaction had barely started.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(45000);
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.classList.contains('fading')).toBe(false);
  });

  it('restarts the clock for a second compaction', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(9000);
    firePreCompact();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('2s');
  });
});

describe('CompactionProgress completion', () => {
  it('reports the boundary with its token counts', async () => {
    const el = mountOverlay();
    firePreCompact('auto');
    vi.advanceTimersByTime(20000);
    fireBoundary({ pre_tokens: 168200, post_tokens: 21400, trigger: 'auto' });
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.classList.contains('success')).toBe(true);
    expect(overlay.textContent)
      .toContain('Context compacted (automatic) — 168.2k → 21.4k tokens');
  });

  it('still says compaction happened with nothing on the wire', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').textContent)
      .toContain('Context compacted');
  });

  it('swaps spinner for a checkmark and settles the bar', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary({ pre_tokens: 100, post_tokens: 10 });
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.spinner')).toBeNull();
    expect(el.shadowRoot.querySelector('.glyph').textContent.trim()).toBe('✓');
    const fill = el.shadowRoot.querySelector('.bar-fill');
    expect(fill.classList.contains('done')).toBe(true);
    expect(fill.classList.contains('indeterminate')).toBe(false);
  });

  it('drops the elapsed reading once it has stopped elapsing', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(5000);
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed')).toBeNull();
  });

  it('stops ticking on completion', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(3000);
    fireBoundary();
    vi.advanceTimersByTime(3000);
    expect(el._elapsed).toBe(3);
  });

  it('fades after the caption has had time to be read', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').classList
      .contains('fading')).toBe(false);

    vi.advanceTimersByTime(1600);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').classList
      .contains('fading')).toBe(true);
  });

  it('hides after the caption plus the fade', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('is ready to run again after hiding', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    firePreCompact('manual');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.classList.contains('fading')).toBe(false);
    expect(overlay.classList.contains('success')).toBe(false);
    expect(overlay.textContent).toContain('(manual)');
  });
});

describe('CompactionProgress a boundary it saw no start for', () => {
  it('stays hidden', async () => {
    // Microcompaction reports a boundary with no PreCompact hook behind it,
    // and there is no pause left to explain by the time it lands. The
    // transcript divider records it; this component has nothing to add.
    const el = mountOverlay();
    fireBoundary({ pre_tokens: 100, post_tokens: 10, trigger: 'microcompact' });
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('does not reopen after it has already finished', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });
});

describe('CompactionProgress a start with no end', () => {
  it('keeps waiting right up to the ceiling', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(179999);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
  });

  it('admits it lost track at the ceiling', async () => {
    // A spinner is a claim that something is still happening, and only
    // `compact_boundary` can retract it. If the engine dies mid-compaction
    // nothing ever does, and a spinner that runs forever is worse than the
    // toast this replaced.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(180000);
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.classList.contains('warning')).toBe(true);
    expect(overlay.textContent).toContain('has not reported finishing');
    expect(el.shadowRoot.querySelector('.spinner')).toBeNull();
    expect(el.shadowRoot.querySelector('.bar')).toBeNull();
  });

  it('gets out of the way after the warning has been readable', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(180000 + 5000 + 400);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('drops the ceiling once the boundary lands', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary({ pre_tokens: 100, post_tokens: 10 });
    vi.advanceTimersByTime(180000 + 10000);
    await el.updateComplete;
    // Hidden, and hidden by the success path — not warned about.
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
    expect(el._state).toBe('hidden');
  });
});

describe('CompactionProgress event filtering', () => {
  it('ignores other system-event subtypes', async () => {
    const el = mountOverlay();
    window.dispatchEvent(new CustomEvent('system-event', {
      detail: { requestId: null, data: { subtype: 'conversation_reset' } },
    }));
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('ignores doc-enrichment stages sharing the compaction channel', async () => {
    const el = mountOverlay();
    firePreCompact();
    await el.updateComplete;
    window.dispatchEvent(new CustomEvent('compaction-event', {
      detail: {
        requestId: 'req-1',
        event: { stage: 'doc_enrichment_complete' },
      },
    }));
    await el.updateComplete;
    // Still waiting on the real boundary.
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
  });

  it('shows the pause for a collaborator turn that caused it', async () => {
    // No request-id filter: compaction rewrites the session's context, and a
    // stall from someone else's turn stalls this browser identically.
    const el = mountOverlay();
    window.dispatchEvent(new CustomEvent('system-event', {
      detail: {
        requestId: 'someone-elses-turn',
        data: { subtype: 'pre_compact', data: { trigger: 'auto' } },
      },
    }));
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).not.toBeNull();
  });

  it('survives malformed payloads on both channels', async () => {
    const el = mountOverlay();
    for (const detail of [null, {}, { data: null }, { data: 'pre_compact' }]) {
      window.dispatchEvent(new CustomEvent('system-event', { detail }));
    }
    for (const detail of [null, {}, { event: null }, { event: 'boundary' }]) {
      window.dispatchEvent(new CustomEvent('compaction-event', { detail }));
    }
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('tolerates a hook payload with no nested data', async () => {
    const el = mountOverlay();
    window.dispatchEvent(new CustomEvent('system-event', {
      detail: { requestId: null, data: { subtype: 'pre_compact' } },
    }));
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').textContent)
      .toContain('Compacting conversation');
  });
});

describe('CompactionProgress cleanup', () => {
  it('stops ticking when removed from the document', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(2000);
    el.parentNode.removeChild(el);
    vi.advanceTimersByTime(10000);
    expect(el._elapsed).toBe(2);
    expect(el._tickInterval).toBeNull();
  });

  it('drops the exit chain when removed mid-fade', async () => {
    const el = mountOverlay();
    firePreCompact();
    fireBoundary();
    vi.advanceTimersByTime(1600);
    el.parentNode.removeChild(el);
    vi.advanceTimersByTime(10000);
    expect(el._exitTimer).toBeNull();
    expect(el._fadeTimer).toBeNull();
    expect(el._ceilingTimer).toBeNull();
  });

  it('deafens both channels when removed', async () => {
    const el = mountOverlay();
    el.parentNode.removeChild(el);
    firePreCompact();
    await el.updateComplete;
    expect(el._state).toBe('hidden');
  });
});
