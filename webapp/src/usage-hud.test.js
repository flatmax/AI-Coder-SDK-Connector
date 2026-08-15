// Tests for `ac-usage-hud` — the transient overlay that reports what a turn
// cost and how full the context window is.
//
// Landed in phase 3 (CC-17) with no coverage. Three things here are easy to
// break and impossible to notice: the auto-hide timers (the HUD is gone by
// the time anyone looks), the cost rendering (a null cost means
// "subscription", not "free"), and the session-changed path, which refreshes
// the numbers *without* showing the HUD — phase 5 is what starts firing it,
// and a HUD that pops up on session load would be reporting a turn that
// never happened.
//
// Harness matches context-usage-tab.test.js: a flat SharedRpc proxy keyed by
// "Service.method", each handler wrapped in a single-key envelope.
//
// `settle` deliberately uses only microtasks (no setTimeout), so the same
// helper works under `vi.useFakeTimers()` in the timer tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './usage-hud.js';
import { SharedRpc } from './rpc.js';

const _mounted = [];

function mountHud() {
  const el = document.createElement('ac-usage-hud');
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

function publishFakeRpc(methods) {
  const proxy = {};
  for (const [name, impl] of Object.entries(methods)) {
    proxy[name] = async (...args) => {
      const value = await impl(...args);
      return { fake: value };
    };
  }
  SharedRpc.set(proxy);
}

function publishUsage(usage = usageFixture()) {
  const handler = vi.fn(() => ({ usage, fetched_at: '2026-08-15T10:30:00Z' }));
  publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
  return handler;
}

async function settle(el) {
  for (let i = 0; i < 6; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function pushComplete(result, requestId = 'req-1') {
  window.dispatchEvent(
    new CustomEvent('stream-complete', { detail: { requestId, result } }),
  );
}

afterEach(() => {
  while (_mounted.length) {
    const el = _mounted.pop();
    el.remove();
  }
  SharedRpc.reset();
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function usageFixture(overrides = {}) {
  return {
    categories: [
      { name: 'System prompt', tokens: 3200, color: '#4a9eff' },
      { name: 'Messages', tokens: 42000, color: '#f59e0b' },
      {
        name: 'Deferred tools',
        tokens: 9000,
        color: '#6b7280',
        isDeferred: true,
      },
      { name: 'Empty', tokens: 0, color: '#000000' },
    ],
    totalTokens: 45200,
    maxTokens: 172000,
    rawMaxTokens: 200000,
    percentage: 26.3,
    model: 'claude-opus-4-6',
    isAutoCompactEnabled: true,
    ...overrides,
  };
}

/** A `streamComplete` result, field-for-field as `messages.py` builds it. */
function resultFixture(overrides = {}) {
  return {
    session_id: 'sess-1',
    response: 'done',
    subtype: 'success',
    terminal_reason: null,
    is_error: false,
    num_turns: 1,
    duration_ms: 4200,
    duration_api_ms: 3900,
    usage: { input_tokens: 100, output_tokens: 50 },
    model_usage: { 'claude-opus-4-6': { inputTokens: 100 } },
    total_cost_usd: 0.0342,
    tool_calls: 3,
    permission_prompts: 0,
    files_modified: [],
    cancelled: false,
    mirror_gap: false,
    user_message_id: 'msg-1',
    ...overrides,
  };
}

function hud(el) {
  return el.shadowRoot.querySelector('.hud');
}

function turnRow(el) {
  return [...el.shadowRoot.querySelectorAll('.row')].find((r) =>
    r.querySelector('.label')?.textContent.includes('This turn'),
  );
}

function contextRow(el) {
  return [...el.shadowRoot.querySelectorAll('.row')].find((r) =>
    r.querySelector('.label')?.textContent.includes('Context'),
  );
}

// ---------------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------------

describe('UsageHud visibility', () => {
  it('renders nothing before a turn completes', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    expect(hud(el)).toBeNull();
    expect(el.hasAttribute('visible')).toBe(false);
  });

  it('appears when a turn completes', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(hud(el)).toBeTruthy();
    expect(el.hasAttribute('visible')).toBe(true);
  });

  it('announces itself politely rather than interrupting', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(hud(el).getAttribute('role')).toBe('status');
    expect(hud(el).getAttribute('aria-live')).toBe('polite');
  });

  it('ignores a stream-complete with no result', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(undefined);
    await settle(el);
    expect(hud(el)).toBeNull();
  });

  it('stays hidden for a turn that failed', async () => {
    // The chat panel already surfaces the error, and an errored result has
    // no cost to report — `total_cost_usd` is null because the price is
    // unknown, not because the turn was free.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(
      resultFixture({
        is_error: true,
        subtype: 'error_during_execution',
        terminal_reason: 'engine_error',
        total_cost_usd: null,
        tool_calls: 0,
        duration_ms: 0,
        model_usage: null,
      }),
    );
    await settle(el);
    expect(hud(el)).toBeNull();
  });

  it('dismisses immediately on the close button', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    el.shadowRoot.querySelector('.dismiss').click();
    await settle(el);
    expect(hud(el)).toBeNull();
    expect(el.hasAttribute('visible')).toBe(false);
  });

  it('labels the close button for screen readers', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.dismiss').getAttribute('aria-label'),
    ).toBe('Dismiss usage overlay');
  });

  it('shows a HUD for an interrupted turn', async () => {
    // Work was already billed before the interrupt; hiding the number
    // would make cancelling look free.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture({ cancelled: true, total_cost_usd: 0.0071 }));
    await settle(el);
    expect(hud(el)).toBeTruthy();
    expect(turnRow(el).textContent).toContain('interrupted');
  });
});

// ---------------------------------------------------------------------------
// Auto-hide timers
// ---------------------------------------------------------------------------

describe('UsageHud auto-hide', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function showHud() {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    return el;
  }

  it('stays up for eight seconds', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(7999);
    await settle(el);
    expect(el.classList.contains('fading')).toBe(false);
    expect(hud(el)).toBeTruthy();
  });

  it('starts fading at eight seconds', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(8000);
    await settle(el);
    expect(el.classList.contains('fading')).toBe(true);
    // Still rendered — the fade is a CSS transition, not a teardown.
    expect(hud(el)).toBeTruthy();
  });

  it('is gone once the fade completes', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(8000 + 800);
    await settle(el);
    expect(hud(el)).toBeNull();
    expect(el.classList.contains('fading')).toBe(false);
    expect(el.hasAttribute('visible')).toBe(false);
  });

  it('pauses the countdown while the pointer is over it', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(7000);
    el.dispatchEvent(new CustomEvent('pointerenter'));
    vi.advanceTimersByTime(60_000);
    await settle(el);
    expect(hud(el)).toBeTruthy();
  });

  it('undoes an in-progress fade on hover so the text is legible', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(8000);
    await settle(el);
    expect(el.classList.contains('fading')).toBe(true);
    el.dispatchEvent(new CustomEvent('pointerenter'));
    await settle(el);
    expect(el.classList.contains('fading')).toBe(false);
    // The pending fade timer was cancelled with it.
    vi.advanceTimersByTime(5000);
    await settle(el);
    expect(hud(el)).toBeTruthy();
  });

  it('restarts the countdown when the pointer leaves', async () => {
    const el = await showHud();
    el.dispatchEvent(new CustomEvent('pointerenter'));
    vi.advanceTimersByTime(60_000);
    el.dispatchEvent(new CustomEvent('pointerleave'));
    vi.advanceTimersByTime(8000 + 800);
    await settle(el);
    expect(hud(el)).toBeNull();
  });

  it('does not schedule a hide for a HUD that is already gone', async () => {
    const el = await showHud();
    el.shadowRoot.querySelector('.dismiss').click();
    el.dispatchEvent(new CustomEvent('pointerleave'));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('restarts the countdown when a second turn lands', async () => {
    const el = await showHud();
    vi.advanceTimersByTime(7000);
    pushComplete(resultFixture({ total_cost_usd: 0.02 }));
    await settle(el);
    vi.advanceTimersByTime(7000);
    await settle(el);
    // Would have hidden at 8s from the first turn; the second reset it.
    expect(hud(el)).toBeTruthy();
  });

  it('drops its timers when removed from the DOM', async () => {
    const el = await showHud();
    el.remove();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops listening for turns once removed', async () => {
    const handler = publishUsage();
    const el = mountHud();
    await settle(el);
    el.remove();
    handler.mockClear();
    pushComplete(resultFixture());
    await Promise.resolve();
    expect(handler).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Cost
// ---------------------------------------------------------------------------

describe('UsageHud cost', () => {
  async function costFor(total_cost_usd) {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture({ total_cost_usd }));
    await settle(el);
    return turnRow(el).querySelector('.value').textContent.trim();
  }

  it('renders a sub-cent turn to four decimals', async () => {
    // Two decimals would print "$0.00", which reads as free.
    expect(await costFor(0.0042)).toContain('$0.0042');
  });

  it('renders a turn over a cent to two decimals', async () => {
    expect(await costFor(1.234_5)).toContain('$1.23');
  });

  it('renders an exact zero without decimal noise', async () => {
    expect(await costFor(0)).toContain('$0');
  });

  it('says "included" under subscription billing rather than $0.00', async () => {
    // A turn on a Max plan did not cost nothing — it cost nothing extra.
    const text = await costFor(null);
    expect(text).toContain('included');
    expect(text).not.toContain('$');
  });

  it('says "included" when the engine omits a cost entirely', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    const result = resultFixture();
    delete result.total_cost_usd;
    pushComplete(result);
    await settle(el);
    expect(turnRow(el).textContent).toContain('included');
  });
});

// ---------------------------------------------------------------------------
// Turn detail
// ---------------------------------------------------------------------------

describe('UsageHud turn detail', () => {
  async function showTurn(overrides) {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture(overrides));
    await settle(el);
    return el;
  }

  it('reports tool calls and elapsed time', async () => {
    const el = await showTurn({ tool_calls: 3, duration_ms: 4200 });
    expect(turnRow(el).textContent).toContain('3 tool calls');
    expect(turnRow(el).textContent).toContain('4.2s');
  });

  it('says "call" for a single tool call', async () => {
    const el = await showTurn({ tool_calls: 1 });
    expect(turnRow(el).textContent).toContain('1 tool call');
    expect(turnRow(el).textContent).not.toContain('1 tool calls');
  });

  it('omits the tool count for a turn that used none', async () => {
    const el = await showTurn({ tool_calls: 0 });
    expect(turnRow(el).textContent).not.toContain('tool call');
  });

  it('shows a zero duration rather than hiding it', async () => {
    const el = await showTurn({ duration_ms: 0 });
    expect(turnRow(el).textContent).toContain('0.0s');
  });

  it('omits the duration when the engine sent none', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    const result = resultFixture();
    delete result.duration_ms;
    pushComplete(result);
    await settle(el);
    expect(turnRow(el).textContent).not.toContain('s ·');
  });
});

// ---------------------------------------------------------------------------
// Model label
// ---------------------------------------------------------------------------

describe('UsageHud model label', () => {
  async function labelFor(overrides, usage = usageFixture()) {
    publishUsage(usage);
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture(overrides));
    await settle(el);
    return el.shadowRoot.querySelector('.model');
  }

  it('names the model that answered', async () => {
    const label = await labelFor({
      model_usage: { 'claude-opus-4-6': {} },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('counts the extras when a subagent used another model', async () => {
    const label = await labelFor({
      model_usage: { 'claude-opus-4-6': {}, 'claude-haiku-4-5': {} },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6 +1');
    expect(label.title).toBe(
      'Models used this turn: claude-opus-4-6, claude-haiku-4-5',
    );
  });

  it('falls back to the context breakdown model', async () => {
    // `set_model` between turns would make the two disagree, so the turn
    // wins when it has an answer — this is the case where it does not.
    const label = await labelFor({ model_usage: null });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('falls back to a generic name when neither knows', async () => {
    const usage = usageFixture();
    delete usage.model;
    const label = await labelFor({ model_usage: null }, usage);
    expect(label.textContent.trim()).toBe('Claude Code');
  });

  it('titles the single-model case with the model name', async () => {
    const label = await labelFor({ model_usage: { 'claude-opus-4-6': {} } });
    expect(label.title).toBe('claude-opus-4-6');
  });
});

// ---------------------------------------------------------------------------
// Context section
// ---------------------------------------------------------------------------

describe('UsageHud context section', () => {
  async function show(usage = usageFixture()) {
    publishUsage(usage);
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    return el;
  }

  it('shows the percentage and abbreviated totals', async () => {
    const el = await show();
    const value = contextRow(el).querySelector('.value').textContent.trim();
    expect(value).toBe('26% · 45.2K/172.0K');
  });

  it('rounds the percentage to a whole number in the overlay', async () => {
    const el = await show(usageFixture({ percentage: 26.7 }));
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '27%',
    );
  });

  it('computes the percentage when the engine omits it', async () => {
    const usage = usageFixture();
    delete usage.percentage;
    const el = await show(usage);
    // 45200 / 172000 = 26.3%
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '26%',
    );
  });

  it('clamps a percentage above 100', async () => {
    const el = await show(usageFixture({ percentage: 130 }));
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '100%',
    );
  });

  it('colours the figure red past 90 percent', async () => {
    const el = await show(usageFixture({ percentage: 94 }));
    expect(contextRow(el).querySelector('.value').style.color).toBe(
      'rgb(248, 81, 73)',
    );
  });

  it('colours the figure amber in the 75-90 band', async () => {
    const el = await show(usageFixture({ percentage: 82 }));
    expect(contextRow(el).querySelector('.value').style.color).toBe(
      'rgb(210, 153, 34)',
    );
  });

  it('says it is still reading before the first answer lands', async () => {
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el.shadowRoot.querySelector('.muted').textContent).toContain(
      'Reading context',
    );
  });

  it('shows why the breakdown is missing when the engine refuses', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => ({
        error: 'engine is not ready',
      }),
    });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'engine is not ready',
    );
  });

  it('reports a missing usage payload', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => ({ fetched_at: 'now' }),
    });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'returned no context usage',
    );
  });

  it('reports a thrown fetch', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => {
        throw new Error('websocket closed');
      },
    });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'websocket closed',
    );
  });
});

// ---------------------------------------------------------------------------
// Context bar
// ---------------------------------------------------------------------------

describe('UsageHud context bar', () => {
  async function show(usage = usageFixture()) {
    publishUsage(usage);
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    return el;
  }

  it('segments the fill by category', async () => {
    const el = await show();
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs).toHaveLength(2);
    expect(segs[0].title).toBe('System prompt: 3.2K');
    expect(segs[1].title).toBe('Messages: 42.0K');
  });

  it('excludes deferred and empty categories from the fill', async () => {
    const el = await show();
    const titles = [...el.shadowRoot.querySelectorAll('.bar-seg')].map(
      (s) => s.title,
    );
    expect(titles.some((t) => t.includes('Deferred'))).toBe(false);
    expect(titles.some((t) => t.includes('Empty'))).toBe(false);
  });

  it('sizes segments against the effective maximum', async () => {
    const el = await show();
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    // 42000 / 172000 = 24.4%
    expect(segs[1].style.width.startsWith('24.4')).toBe(true);
  });

  it('falls back to one solid segment without categories', async () => {
    const el = await show(usageFixture({ categories: [], percentage: 61 }));
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs).toHaveLength(1);
    expect(segs[0].style.width).toBe('61%');
  });

  it('describes the fill for screen readers', async () => {
    const el = await show();
    expect(el.shadowRoot.querySelector('.bar').getAttribute('aria-label')).toBe(
      'Context 26 percent used',
    );
  });

  it('names the autocompact headroom in the tooltip', async () => {
    const el = await show();
    const title = el.shadowRoot.querySelector('.bar').title;
    expect(title).toContain('45,200 of 172,000 tokens');
    expect(title).toContain('28,000 reserved as autocompact headroom');
    expect(title).toContain('model window 200,000');
  });

  it('omits the headroom clause when there is none', async () => {
    const el = await show(usageFixture({ rawMaxTokens: 172000 }));
    expect(el.shadowRoot.querySelector('.bar').title).not.toContain(
      'headroom',
    );
  });

  it('warns in the tooltip when autocompact is off', async () => {
    const el = await show(usageFixture({ isAutoCompactEnabled: false }));
    expect(el.shadowRoot.querySelector('.bar').title).toContain(
      'Autocompact is off',
    );
  });

  it('lists categories with their own colours', async () => {
    const el = await show();
    const chips = [...el.shadowRoot.querySelectorAll('.cat')];
    expect(chips).toHaveLength(3);
    expect(chips[0].textContent.trim()).toBe('System prompt 3.2K');
    expect(chips[0].querySelector('.swatch').style.background).toBe(
      'rgb(74, 158, 255)',
    );
  });

  it('keeps deferred categories in the legend but marks them', async () => {
    const el = await show();
    const deferred = [...el.shadowRoot.querySelectorAll('.cat.deferred')];
    expect(deferred).toHaveLength(1);
    expect(deferred[0].textContent).toContain('Deferred tools');
  });

  it('omits the legend when nothing is loaded yet', async () => {
    const el = await show(
      usageFixture({
        categories: [{ name: 'Deferred tools', tokens: 10, isDeferred: true }],
      }),
    );
    expect(el.shadowRoot.querySelector('.cats')).toBeNull();
  });

  it('falls back to a neutral swatch for an uncoloured category', async () => {
    const el = await show(
      usageFixture({ categories: [{ name: 'Mystery', tokens: 500 }] }),
    );
    expect(el.shadowRoot.querySelector('.swatch').style.background).toBe(
      'rgb(110, 118, 129)',
    );
  });
});

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------

describe('UsageHud fetching', () => {
  it('fetches the breakdown when a turn lands', async () => {
    const handler = publishUsage();
    const el = mountHud();
    await settle(el);
    expect(handler).not.toHaveBeenCalled();
    pushComplete(resultFixture());
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('makes no call without a proxy', async () => {
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    // Visible, reporting the turn it does know about.
    expect(hud(el)).toBeTruthy();
    expect(el._context).toBeNull();
  });

  it('collapses overlapping fetches into one control request', async () => {
    // Two turns in quick succession would otherwise queue two control
    // requests for the same answer, and the later reply could land first.
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const handler = vi.fn(async () => {
      await gate;
      return { usage: usageFixture(), fetched_at: 'now' };
    });
    publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    pushComplete(resultFixture());
    release();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('clears the in-flight guard after a failure', async () => {
    const handler = vi
      .fn()
      .mockImplementationOnce(() => {
        throw new Error('first failed');
      })
      .mockImplementationOnce(() => ({
        usage: usageFixture(),
        fetched_at: 'now',
      }));
    publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(2);
    expect(el._contextError).toBe('');
  });

  it('falls back to a generic message when the throw has no message', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => {
        throw {};
      },
    });
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el._contextError).toBe('Context usage unavailable.');
  });
});

// ---------------------------------------------------------------------------
// Session changes — phase 5's trigger
// ---------------------------------------------------------------------------

describe('UsageHud session changes', () => {
  function pushSessionChanged() {
    window.dispatchEvent(
      new CustomEvent('session-changed', { detail: { messages: [] } }),
    );
  }

  it('refreshes the numbers without showing the HUD', async () => {
    // The HUD is per-turn feedback. Popping it up because a session loaded
    // would be reporting on a turn that did not happen.
    const handler = publishUsage();
    const el = mountHud();
    await settle(el);
    pushSessionChanged();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(el._context.totalTokens).toBe(45200);
    expect(hud(el)).toBeNull();
  });

  it('forgets the previous turn, which belonged to another session', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    expect(el._turn).not.toBeNull();
    pushSessionChanged();
    await settle(el);
    expect(el._turn).toBeNull();
  });

  it('leaves an already-visible HUD without a turn row', async () => {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture());
    await settle(el);
    pushSessionChanged();
    await settle(el);
    expect(hud(el)).toBeTruthy();
    expect(turnRow(el)).toBeUndefined();
    expect(contextRow(el)).toBeTruthy();
  });

  it('stops listening for session changes once removed', async () => {
    const handler = publishUsage();
    const el = mountHud();
    await settle(el);
    el.remove();
    handler.mockClear();
    pushSessionChanged();
    await Promise.resolve();
    expect(handler).not.toHaveBeenCalled();
  });
});
