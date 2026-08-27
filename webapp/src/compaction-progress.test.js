// Tests for CompactionProgress overlay.
//
// Fake timers for deterministic timing, no rAF / no settle() helper
// (per D15 in IMPLEMENTATION_NOTES.md — fake timers break jsdom's rAF).
//
// The point of the component is that it outlives the toast it replaced, so
// most of what is pinned here is lifetime: it appears when the engine says a
// compaction is running, stays through an arbitrarily long pause, and leaves
// when the engine reports the boundary — or when the boundary never comes.
//
// The second thing pinned here is what it does NOT show. The PreCompact hook
// fires for the CLI's speculative background compaction too, which stalls
// nothing and often never compacts at all, so a hook on its own is held back
// for a grace period and only becomes an indicator if the engine confirms it —
// or if the grace period expires on an engine that never confirms anything.

import {
  afterEach, beforeEach, describe, expect, it, vi,
} from 'vitest';

import './compaction-progress.js';

const _mounted = [];

/** Grace period between an unconfirmed hook and showing anything. */
const GRACE_MS = 1500;

/** Ceiling on an indicator the engine never confirmed. */
const UNCONFIRMED_MS = 15000;

/** Ceiling on a confirmed one. */
const MAX_ACTIVE_MS = 180000;

function mountOverlay() {
  const el = document.createElement('aic-compaction-progress');
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

function fireCompaction(payload) {
  window.dispatchEvent(new CustomEvent('compaction-event', {
    detail: { requestId: 'req-1', event: payload },
  }));
}

/** The engine's status frame saying a compaction is running now. */
function fireStarted() {
  fireCompaction({ stage: 'compaction_started' });
}

/** Its other half: `{result, error}` when the compaction stops. */
function fireEnded(result, error) {
  fireCompaction({ stage: 'compaction_ended', result, error });
}

/** The stream's own `compact_boundary`. */
function fireBoundary(payload = {}) {
  fireCompaction({ stage: 'compact_boundary', ...payload });
}

/**
 * A real compaction as the engine reports one: the hook, then the status
 * frame confirming it milliseconds later.
 */
function startPause(trigger = 'auto') {
  firePreCompact(trigger);
  fireStarted();
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
  it('appears when the engine confirms the hook', async () => {
    const el = mountOverlay();
    startPause('auto');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.textContent).toContain('Compacting conversation');
  });

  it('appears on a status frame with no hook behind it', async () => {
    // The hook is a convenience, not a precondition — a compaction the engine
    // announces is a compaction whether or not our hook ran.
    const el = mountOverlay();
    fireStarted();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).not.toBeNull();
  });

  it('names the trigger in the words the divider uses', async () => {
    // `auto` on the wire, "automatic" on screen — the same normalisation
    // `compactionSummary` applies, so the overlay and the divider that
    // replaces it 20 seconds later do not describe one compaction two ways.
    const el = mountOverlay();
    startPause('auto');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.label').textContent)
      .toContain('(automatic)');
  });

  it('passes an unrecognised trigger through verbatim', async () => {
    const el = mountOverlay();
    startPause('microcompact');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.label').textContent)
      .toContain('(microcompact)');
  });

  it('says the plain thing when no trigger came through', async () => {
    // `hooks.py` reads the trigger with `.get()` off a CLI-owned dict, so a
    // null is a shape that reaches us. So is a status frame with no hook —
    // that one carries no trigger at all.
    const el = mountOverlay();
    startPause(null);
    await el.updateComplete;
    const label = el.shadowRoot.querySelector('.label').textContent;
    expect(label).toContain('Compacting conversation');
    expect(label).not.toContain('(');
  });

  it('shows a spinner and an indeterminate bar', async () => {
    const el = mountOverlay();
    startPause();
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
    startPause();
    await el.updateComplete;
    const bar = el.shadowRoot.querySelector('[role="progressbar"]');
    expect(bar).not.toBeNull();
    expect(bar.getAttribute('aria-valuenow')).toBeNull();
  });

  it('holds no elapsed reading for the first second', async () => {
    const el = mountOverlay();
    startPause();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed')).toBeNull();
  });

  it('ticks the elapsed counter once per second', async () => {
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(14000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('14s');
  });

  it('outlives a toast by a wide margin', async () => {
    // The regression this component exists for: at 3 seconds the old toast
    // was gone and the compaction had barely started.
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(45000);
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.classList.contains('fading')).toBe(false);
  });

  it('keeps counting from the hook, not from the confirmation', async () => {
    // The user has been waiting since the hook fired. Restarting the counter
    // when the confirmation lands would under-report the wait by however long
    // the engine's own hooks took to run.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(1000);
    fireStarted();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('3s');
  });

  it('does not restart the clock when the hook fires twice', async () => {
    // The CLI runs PreCompact again when it consumes a precomputed summary,
    // so one wait can produce two hooks. Resetting the counter mid-wait would
    // tell the user the pause just started.
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(9000);
    firePreCompact();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('11s');
  });
});

describe('CompactionProgress an unconfirmed hook', () => {
  it('shows nothing during the grace period', async () => {
    // A background precompute fires the same hook with the same trigger and
    // stalls nothing at all. Until the engine says otherwise, there is no
    // pause to report.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(GRACE_MS - 1);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('shows anyway once the grace period expires', async () => {
    // The fallback that keeps an older engine — one that emits no status
    // frames — from losing the indicator entirely.
    const el = mountOverlay();
    firePreCompact('manual');
    vi.advanceTimersByTime(GRACE_MS);
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.textContent).toContain('(manual)');
  });

  it('counts the grace period as part of the wait', async () => {
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(4000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed').textContent).toContain('4s');
  });

  it('gives up quietly, with no warning', async () => {
    // A hook the engine never confirmed is a precompute far more often than a
    // stall, and accusing a working engine of hanging is worse than saying
    // nothing.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(GRACE_MS + UNCONFIRMED_MS);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
    expect(el._state).toBe('hidden');
  });

  it('holds for the full ceiling once confirmed late', async () => {
    // Confirmation after the grace period has already shown the indicator:
    // the short ceiling has to be replaced, not merely flagged.
    const el = mountOverlay();
    firePreCompact();
    vi.advanceTimersByTime(GRACE_MS);
    fireStarted();
    vi.advanceTimersByTime(UNCONFIRMED_MS + 1000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
  });
});

describe('CompactionProgress completion', () => {
  it('reports the boundary with its token counts', async () => {
    const el = mountOverlay();
    startPause('auto');
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
    startPause();
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').textContent)
      .toContain('Context compacted');
  });

  it('settles on the status frame when it lands first', async () => {
    // `compaction_ended` carries no token counts, so the caption is the plain
    // sentence — but it retracts the spinner, which is the job.
    const el = mountOverlay();
    startPause();
    fireEnded('success');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.classList.contains('success')).toBe(true);
    expect(overlay.textContent).toContain('Context compacted');
  });

  it('upgrades that caption when the boundary follows', async () => {
    // Two reports of one compaction, in whichever order they arrive. The one
    // with the numbers wins, without restarting the fade already scheduled.
    const el = mountOverlay();
    startPause();
    fireEnded('success');
    fireBoundary({ pre_tokens: 100000, post_tokens: 20000 });
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').textContent)
      .toContain('100.0k → 20.0k tokens');
  });

  it('swaps spinner for a checkmark and settles the bar', async () => {
    const el = mountOverlay();
    startPause();
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
    startPause();
    vi.advanceTimersByTime(5000);
    fireBoundary();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.elapsed')).toBeNull();
  });

  it('stops ticking on completion', async () => {
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(3000);
    fireBoundary();
    vi.advanceTimersByTime(3000);
    expect(el._elapsed).toBe(3);
  });

  it('fades after the caption has had time to be read', async () => {
    const el = mountOverlay();
    startPause();
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
    startPause();
    fireBoundary();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('is ready to run again after hiding', async () => {
    const el = mountOverlay();
    startPause();
    fireBoundary();
    vi.advanceTimersByTime(2000);
    await el.updateComplete;
    startPause('manual');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay).not.toBeNull();
    expect(overlay.classList.contains('fading')).toBe(false);
    expect(overlay.classList.contains('success')).toBe(false);
    expect(overlay.textContent).toContain('(manual)');
  });
});

describe('CompactionProgress a compaction that failed', () => {
  it('says so, with the engine\'s reason', async () => {
    // The only report a failed compaction produces: no boundary is written,
    // so an indicator waiting for one would wait forever.
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(30000);
    fireEnded('failed', 'Conversation too long');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.classList.contains('warning')).toBe(true);
    expect(overlay.textContent).toContain('Compaction failed');
    expect(overlay.textContent).toContain('Conversation too long');
    expect(el.shadowRoot.querySelector('.spinner')).toBeNull();
  });

  it('says so without a reason, which the CLI often withholds', async () => {
    const el = mountOverlay();
    startPause();
    fireEnded('failed');
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.textContent).toContain('Compaction failed');
    expect(overlay.textContent).not.toContain(':');
  });

  it('reports a failure that never got as far as an indicator', async () => {
    // Unlike a completion, a failure is worth announcing even if the pause
    // was too short to show: the context was not reclaimed, and the next turn
    // is the one that finds out.
    const el = mountOverlay();
    fireEnded('failed', 'api error');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').classList
      .contains('warning')).toBe(true);
  });

  it('gets out of the way after the warning has been readable', async () => {
    const el = mountOverlay();
    startPause();
    fireEnded('failed', 'api error');
    vi.advanceTimersByTime(5000 + 400);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
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

  it('stays hidden for a bare success too', async () => {
    const el = mountOverlay();
    fireEnded('success');
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('does not reopen after it has already finished', async () => {
    const el = mountOverlay();
    startPause();
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
    startPause();
    vi.advanceTimersByTime(MAX_ACTIVE_MS - 1);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
  });

  it('admits it lost track at the ceiling', async () => {
    // A spinner is a claim that something is still happening, and only the
    // engine can retract it. If the engine dies mid-compaction nothing ever
    // does, and a spinner that runs forever is worse than the toast this
    // replaced. Confirmed, so this one really is a stall.
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(MAX_ACTIVE_MS);
    await el.updateComplete;
    const overlay = el.shadowRoot.querySelector('.overlay');
    expect(overlay.classList.contains('warning')).toBe(true);
    expect(overlay.textContent).toContain('has not reported finishing');
    expect(el.shadowRoot.querySelector('.spinner')).toBeNull();
    expect(el.shadowRoot.querySelector('.bar')).toBeNull();
  });

  it('gets out of the way after the warning has been readable', async () => {
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(MAX_ACTIVE_MS + 5000 + 400);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('drops the ceiling once the boundary lands', async () => {
    const el = mountOverlay();
    startPause();
    fireBoundary({ pre_tokens: 100, post_tokens: 10 });
    vi.advanceTimersByTime(MAX_ACTIVE_MS + 10000);
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
    vi.advanceTimersByTime(GRACE_MS);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('ignores doc-enrichment stages sharing the compaction channel', async () => {
    const el = mountOverlay();
    startPause();
    await el.updateComplete;
    fireCompaction({ stage: 'doc_enrichment_complete' });
    await el.updateComplete;
    // Still waiting on the real boundary.
    expect(el.shadowRoot.querySelector('.spinner')).not.toBeNull();
  });

  it('ignores a status frame that is not about compaction', async () => {
    // `requesting`, a permission-mode change: the translator keeps those on
    // the generic channel, and nothing that reaches here should move the
    // indicator either.
    const el = mountOverlay();
    fireCompaction({ stage: 'compaction_ended', result: undefined });
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
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
    fireStarted();
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
    vi.advanceTimersByTime(GRACE_MS);
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay')).toBeNull();
  });

  it('tolerates a hook payload with no nested data', async () => {
    const el = mountOverlay();
    window.dispatchEvent(new CustomEvent('system-event', {
      detail: { requestId: null, data: { subtype: 'pre_compact' } },
    }));
    fireStarted();
    await el.updateComplete;
    expect(el.shadowRoot.querySelector('.overlay').textContent)
      .toContain('Compacting conversation');
  });
});

describe('CompactionProgress cleanup', () => {
  it('stops ticking when removed from the document', async () => {
    const el = mountOverlay();
    startPause();
    vi.advanceTimersByTime(2000);
    el.parentNode.removeChild(el);
    vi.advanceTimersByTime(10000);
    expect(el._elapsed).toBe(2);
    expect(el._tickInterval).toBeNull();
  });

  it('drops the grace timer when removed mid-grace', async () => {
    const el = mountOverlay();
    firePreCompact();
    el.parentNode.removeChild(el);
    vi.advanceTimersByTime(10000);
    expect(el._pendingTimer).toBeNull();
    expect(el._state).toBe('hidden');
  });

  it('drops the exit chain when removed mid-fade', async () => {
    const el = mountOverlay();
    startPause();
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
    startPause();
    await el.updateComplete;
    expect(el._state).toBe('hidden');
  });
});
