// Tests for `ac-usage-hud` — the transient overlay that reports what a turn
// cost and how full the context window is.
//
// Landed in phase 3 (CC-17) with no coverage. Three things here are easy to
// break and impossible to notice: the auto-hide timers (the HUD is gone by
// the time anyone looks), the cost rendering (three different facts — a
// price, "nothing extra", "cost unknown" — that all used to render as the
// word "included"), and the session-changed path, which refreshes
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

/**
 * A `ContextUsageResponse` shaped like a real one — theme-token
 * colours, `maxTokens` equal to `rawMaxTokens`, and the structural
 * categories the engine really sends.
 *
 * The previous fixture used hex colours and a `maxTokens` reduced by
 * the autocompact buffer. Both were invented, this suite passed on
 * them, and the HUD shipped with transparent bar segments. See the
 * identity assertions in context-usage.test.js.
 */
function usageFixture(overrides = {}) {
  return {
    categories: [
      { name: 'System prompt', tokens: 3200, color: 'promptBorder' },
      { name: 'Messages', tokens: 42000, color: 'purple_FOR_SUBAGENTS_ONLY' },
      {
        name: 'Deferred tools',
        tokens: 9000,
        color: 'inactive',
        isDeferred: true,
      },
      { name: 'Empty', tokens: 0, color: 'claude' },
      { name: 'Autocompact buffer', tokens: 33000, color: 'inactive' },
      { name: 'Free space', tokens: 121800, color: 'promptBorder' },
    ],
    totalTokens: 45200,
    maxTokens: 200000,
    rawMaxTokens: 200000,
    autoCompactThreshold: 167000,
    percentage: 22.6,
    model: 'claude-opus-4-6',
    isAutoCompactEnabled: true,
    ...overrides,
  };
}

/** As `usageFixture`, at a given fill, keeping the identities intact. */
function usageAt(totalTokens, overrides = {}) {
  const max = 200000;
  const threshold = 167000;
  return usageFixture({
    categories: [
      {
        name: 'Messages',
        tokens: totalTokens,
        color: 'purple_FOR_SUBAGENTS_ONLY',
      },
      { name: 'Autocompact buffer', tokens: max - threshold, color: 'inactive' },
      {
        name: 'Free space',
        tokens: Math.max(0, threshold - totalTokens),
        color: 'promptBorder',
      },
    ],
    totalTokens,
    percentage: Math.round((totalTokens / max) * 1000) / 10,
    ...overrides,
  });
}

/**
 * A `streamComplete` result, field-for-field as the engine builds it.
 *
 * Note the two scopes, because the HUD used to read the wrong one:
 * `model_usage` / `total_cost_usd` are the *session's* running totals, and
 * `turn_model_usage` / `turn_cost_usd` / `turn_cost_basis` are this turn's,
 * differenced by `ac_dc/claude_code/cost.py`. The fixture keeps them
 * deliberately different — a session that has spent $1.20 across several
 * turns, of which this turn is 3.42 cents — so a test cannot pass by
 * reading either one in place of the other.
 */
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
    model_usage: {
      'claude-opus-4-6': { inputTokens: 4000, costUSD: 1.1 },
      'claude-haiku-4-5': { inputTokens: 900, costUSD: 0.1 },
    },
    total_cost_usd: 1.2,
    turn_model_usage: { 'claude-opus-4-6': { inputTokens: 100, costUSD: 0.0342 } },
    turn_cost_usd: 0.0342,
    turn_cost_basis: 'measured',
    tool_calls: 3,
    permission_prompts: 0,
    files_modified: [],
    cancelled: false,
    mirror_gap: false,
    user_message_id: 'msg-1',
    ...overrides,
  };
}

/** The synthetic footer AC⚡DC writes when a turn dies before the engine's. */
function crashFixture(overrides = {}) {
  return resultFixture({
    is_error: true,
    subtype: 'error_during_execution',
    terminal_reason: 'engine_error',
    response: '',
    usage: null,
    model_usage: null,
    total_cost_usd: null,
    turn_model_usage: null,
    turn_cost_usd: null,
    turn_cost_basis: 'unpriced',
    tool_calls: 0,
    duration_ms: 0,
    errors: ['kaboom'],
    ...overrides,
  });
}

function hud(el) {
  return el.shadowRoot.querySelector('.hud');
}

function turnRow(el) {
  return [...el.shadowRoot.querySelectorAll('.row')].find((r) =>
    r.querySelector('.label')?.textContent.includes('This turn'),
  );
}

/** The turn row's value, whitespace collapsed so assertions read plainly. */
function turnText(el) {
  const row = turnRow(el);
  return row ? row.querySelector('.value').textContent.replace(/\s+/g, ' ').trim() : '';
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

  it('stays hidden for a turn that failed with nothing to report', async () => {
    // Nothing ran, nothing was said, nothing was priced. The chat panel and
    // a toast already carry the error.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(crashFixture());
    await settle(el);
    expect(hud(el)).toBeNull();
  });

  it('appears for a turn that failed after doing work', async () => {
    // `error_max_turns` after twenty tool calls is the most expensive kind
    // of failure there is, and the old rule — no HUD for any errored turn —
    // hid exactly that. specs5/plan/README.md phase 6.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(
      resultFixture({
        is_error: true,
        terminal_reason: 'error_max_turns',
        turn_cost_usd: 0.87,
        total_cost_usd: 2.07,
      }),
    );
    await settle(el);
    expect(turnText(el)).toContain('$0.87');
    expect(turnText(el)).toContain('failed');
  });

  it('appears for a crash footer once the turn had already spent something', async () => {
    // The engine never wrote this footer, so it carries no usage at all —
    // the only evidence the turn cost money is that it had done work.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(crashFixture({ tool_calls: 4, response: 'partial answer' }));
    await settle(el);
    expect(turnText(el)).toContain('cost unknown');
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
  async function showCost(overrides) {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture(overrides));
    await settle(el);
    return el;
  }

  async function costFor(turn_cost_usd) {
    const el = await showCost({ turn_cost_usd });
    return turnText(el);
  }

  it('reports what this turn cost, not what the session has spent', async () => {
    // The bug this whole slice exists for: `total_cost_usd` is a running
    // total, so the row headed "This turn" showed the session's bill and
    // grew on every turn regardless of what the turn did.
    const el = await showCost({ turn_cost_usd: 0.0342, total_cost_usd: 1.2 });
    expect(turnText(el)).toContain('$0.0342');
    expect(turnText(el)).not.toContain('$1.20');
  });

  it('renders a sub-cent turn to four decimals', async () => {
    // Two decimals would print "$0.00", which reads as free.
    expect(await costFor(0.0042)).toContain('$0.0042');
  });

  it('renders a turn over fifty cents to two decimals', async () => {
    // The CLI's own cut-over, so a figure here reads like the terminal's.
    expect(await costFor(1.234_5)).toContain('$1.23');
  });

  it('keeps four decimals up to fifty cents', async () => {
    expect(await costFor(0.42)).toContain('$0.4200');
  });

  it('says a measured zero cost nothing extra rather than $0', async () => {
    // The engine's running total did not move, which is a real answer:
    // the turn was served from what had already been paid for.
    const text = await costFor(0);
    expect(text).toContain('nothing extra');
    expect(text).not.toContain('$');
  });

  it('says the cost is unknown when the turn could not be priced', async () => {
    // Distinct from "nothing extra" — the distinction phase 6 exists to
    // draw. Both used to render as the word "included".
    const el = await showCost({ turn_cost_usd: null, turn_cost_basis: 'unpriced' });
    expect(turnText(el)).toContain('cost unknown');
    expect(turnText(el)).not.toContain('nothing extra');
  });

  it('says the cost is unknown when the session total restarted', async () => {
    const el = await showCost({ turn_cost_usd: null, turn_cost_basis: 'reset' });
    expect(turnText(el)).toContain('cost unknown');
  });

  it('gives the reason for an unknown cost in the tooltip', async () => {
    // "cost unknown" on its own invites the reading that the app is broken.
    const el = await showCost({ turn_cost_usd: null, turn_cost_basis: 'reset' });
    const chip = [...turnRow(el).querySelectorAll('[title]')].pop();
    expect(chip.title).toContain('restarted');
    expect(chip.title).toContain('/clear');
  });

  it('names the session total in the tooltip of a priced turn', async () => {
    const el = await showCost({ turn_cost_usd: 0.0342, total_cost_usd: 1.2 });
    const chip = turnRow(el).querySelector('[title]');
    expect(chip.title).toContain('$1.20');
    expect(chip.title).toContain('estimate');
  });

  it('shows no cost at all for a turn that never recorded one', async () => {
    // A browsed turn: cost is not in the CLI's transcript, and "unknown" on
    // every replayed footer is noise about a thing that was never measured.
    publishUsage();
    const el = mountHud();
    await settle(el);
    const result = resultFixture();
    delete result.turn_cost_usd;
    delete result.turn_cost_basis;
    pushComplete(result);
    await settle(el);
    expect(turnText(el)).not.toContain('$');
    expect(turnText(el)).not.toContain('unknown');
    expect(turnText(el)).not.toContain('nothing extra');
  });

  it('never prints the word "included"', async () => {
    // It claimed a billing mode the payload says nothing about. The only
    // real billing-mode signal is `credential_source` on engine health.
    for (const basis of ['measured', 'reset', 'unpriced']) {
      const el = await showCost({ turn_cost_basis: basis, turn_cost_usd: 0 });
      expect(turnText(el)).not.toContain('included');
    }
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
// Token rows
// ---------------------------------------------------------------------------

describe('UsageHud token rows', () => {
  async function showTokens(turn_model_usage) {
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture({ turn_model_usage }));
    await settle(el);
    return el;
  }

  /** Each token row as `model → value`, whitespace collapsed. */
  function tokenRows(el) {
    return [...el.shadowRoot.querySelectorAll('.token-model')].map((label) => [
      label.textContent.trim(),
      label.parentElement.querySelector('.token-value')
        .textContent.replace(/\s+/g, ' ').trim(),
    ]);
  }

  it('splits the prompt from the completion', async () => {
    // The row exists to tell a cheap turn from an expensive one at the same
    // token count: 50k of cache reads and 50k of output differ by ~50x in
    // price, and one aggregate figure renders them identically.
    const el = await showTokens({
      'claude-opus-4-6': {
        inputTokens: 300,
        outputTokens: 2000,
        cacheCreationInputTokens: 1200,
        cacheReadInputTokens: 40_000,
      },
    });
    expect(tokenRows(el)).toEqual([['claude-opus-4-6', '↑ 41.5K · ↓ 2.0K']]);
  });

  it('counts the cached part of the prompt in the ↑ figure', async () => {
    // `inputTokens` alone is the *uncached remainder*; a ↑ built from it
    // would report a 40k-token prompt as 300 tokens.
    const el = await showTokens({
      'claude-opus-4-6': { inputTokens: 300, cacheReadInputTokens: 40_000 },
    });
    expect(tokenRows(el)[0][1]).toBe('↑ 40.3K · ↓ 0');
  });

  it('breaks all four counters out in the tooltip', async () => {
    // Where the cache split lives, since the row itself has no room: the
    // read and the write are priced differently (~0.1x and ~1.25x input).
    const el = await showTokens({
      'claude-opus-4-6': {
        inputTokens: 300,
        outputTokens: 2000,
        cacheCreationInputTokens: 1200,
        cacheReadInputTokens: 40_000,
      },
    });
    const title = el.shadowRoot.querySelector('.token-value').title;
    expect(title).toContain('43,500 tokens');
    expect(title).toContain('300 input at full price');
    expect(title).toContain('40,000 cache read');
    expect(title).toContain('1,200 cache write');
    expect(title).toContain('2,000 output');
    expect(title).toContain('whole prompt (41,500)');
  });

  it('omits a cache counter the engine never reported', async () => {
    // "0 cache read" in a tooltip claims a measurement that was not made.
    const el = await showTokens({
      'claude-opus-4-6': { inputTokens: 300, outputTokens: 100 },
    });
    const title = el.shadowRoot.querySelector('.token-value').title;
    expect(title).not.toContain('cache read');
    expect(title).not.toContain('cache write');
  });

  it('gives each model that answered its own row', async () => {
    // A turn that spawned Haiku subagents spent that money too, and the
    // model label only has room to say "+1".
    const el = await showTokens({
      'claude-opus-4-6': { inputTokens: 900, outputTokens: 100 },
      'claude-haiku-4-5': { inputTokens: 4000, outputTokens: 200 },
    });
    expect(tokenRows(el)).toEqual([
      ['claude-haiku-4-5', '↑ 4.0K · ↓ 200'],
      ['claude-opus-4-6', '↑ 900 · ↓ 100'],
    ]);
  });

  it('reads this turn’s usage, never the session’s running map', async () => {
    // `model_usage` grows all session; a row built from it would credit
    // this turn with every token that came before it.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(resultFixture({
      turn_model_usage: { 'claude-opus-4-6': { inputTokens: 100 } },
      model_usage: { 'claude-opus-4-6': { inputTokens: 400_000 } },
    }));
    await settle(el);
    expect(tokenRows(el)).toEqual([['claude-opus-4-6', '↑ 100 · ↓ 0']]);
  });

  it('shows no token row for a turn that reported no usage', async () => {
    // The crash footer is AC⚡DC's own, so it carries no counters at all.
    publishUsage();
    const el = mountHud();
    await settle(el);
    pushComplete(crashFixture({ tool_calls: 4, response: 'partial answer' }));
    await settle(el);
    expect(tokenRows(el)).toEqual([]);
    // The cost row still stands: the turn did work worth reporting.
    expect(turnText(el)).toContain('cost unknown');
  });

  it('reads a replayed turn’s snake_case counters', async () => {
    const el = await showTokens({
      'claude-opus-4-6': { input_tokens: 900, output_tokens: 100 },
    });
    expect(tokenRows(el)[0][1]).toBe('↑ 900 · ↓ 100');
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
      turn_model_usage: { 'claude-opus-4-6': { inputTokens: 100 } },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('names only the models that answered *this* turn', async () => {
    // The session's `model_usage` lists every model it has ever used, so
    // reading it made a plain opus turn claim a haiku subagent it never ran.
    const label = await labelFor({
      turn_model_usage: { 'claude-opus-4-6': { inputTokens: 100 } },
      model_usage: {
        'claude-opus-4-6': { inputTokens: 4000 },
        'claude-haiku-4-5': { inputTokens: 900 },
      },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('counts the extras when a subagent used another model', async () => {
    const label = await labelFor({
      turn_model_usage: {
        'claude-opus-4-6': { inputTokens: 100 },
        'claude-haiku-4-5': { inputTokens: 40 },
      },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6 +1');
    expect(label.title).toBe(
      'Models used this turn: claude-opus-4-6, claude-haiku-4-5',
    );
  });

  it('leads with the model that did the most work', async () => {
    // The `+n` should hide the subagent, not the model that answered.
    const label = await labelFor({
      turn_model_usage: {
        'claude-haiku-4-5': { inputTokens: 40 },
        'claude-opus-4-6': { inputTokens: 100 },
      },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6 +1');
  });

  it('prefers the canonical name over a provider-specific id', async () => {
    // On Bedrock the map is keyed by ids like this one; the schema carries
    // the canonical name beside it.
    const label = await labelFor({
      turn_model_usage: {
        'us.anthropic.claude-opus-4-6-v1:0': {
          inputTokens: 100,
          canonicalModel: 'claude-opus-4-6',
        },
      },
    });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('falls back to the context breakdown model', async () => {
    // `set_model` between turns would make the two disagree, so the turn
    // wins when it has an answer — this is the case where it does not.
    const label = await labelFor({ turn_model_usage: null });
    expect(label.textContent.trim()).toBe('claude-opus-4-6');
  });

  it('falls back to a generic name when neither knows', async () => {
    const usage = usageFixture();
    delete usage.model;
    const label = await labelFor({ turn_model_usage: null }, usage);
    expect(label.textContent.trim()).toBe('Claude Code');
  });

  it('titles the single-model case with the model name', async () => {
    const label = await labelFor({
      turn_model_usage: { 'claude-opus-4-6': { inputTokens: 100 } },
    });
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
    expect(value).toBe('23% · 45.2K/200.0K');
  });

  it('rounds the percentage to a whole number in the overlay', async () => {
    const el = await show(usageFixture({ percentage: 22.7 }));
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '23%',
    );
  });

  it('computes the percentage when the engine omits it', async () => {
    const usage = usageFixture();
    delete usage.percentage;
    const el = await show(usage);
    // 45200 / 200000 = 22.6%
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '23%',
    );
  });

  it('clamps a percentage above 100', async () => {
    const el = await show(usageFixture({ percentage: 130 }));
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '100%',
    );
  });

  // The HUD has no room to print two percentages, so it prints the
  // engine's and colours it by the compaction-relative one. Driving the
  // colour off the printed figure left the red band unreachable: a
  // compact fires at 83.5% of the window.

  it('colours the figure red past 90 percent of the compaction limit', async () => {
    // 160000 / 167000 = 95.8%, while the printed figure is 80%.
    const el = await show(usageAt(160000));
    expect(contextRow(el).querySelector('.value').textContent).toContain(
      '80%',
    );
    expect(contextRow(el).querySelector('.value').style.color).toBe(
      'rgb(248, 81, 73)',
    );
  });

  it('colours the figure amber in the 75-90 band of the limit', async () => {
    // 140000 / 167000 = 83.8%.
    const el = await show(usageAt(140000));
    expect(contextRow(el).querySelector('.value').style.color).toBe(
      'rgb(210, 153, 34)',
    );
  });

  it('falls back to the engine figure when no threshold is reported', async () => {
    // Without a distinct threshold the two denominators agree, so the
    // engine's own rounding wins rather than a recomputed ratio.
    const usage = usageAt(160000);
    delete usage.autoCompactThreshold;
    const el = await show(usage);
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

  it('marks where autocompact fires', async () => {
    // The bar's colour is keyed to the threshold, not the window, so
    // without the mark an amber bar at 84% looks like an arbitrary
    // choice rather than the compaction it is predicting.
    const el = await show();
    const mark = el.shadowRoot.querySelector('.mark');
    // 167000 / 200000
    expect(parseFloat(mark.style.left)).toBeCloseTo(83.5, 1);
    expect(mark.title).toContain('167,000 tokens');
  });

  it('draws the mark outside the bar that clips its own children', async () => {
    // Same structure as the Context tab's gauge, and for the same reason:
    // `.bar` has `overflow: hidden` to round the fill, which clips the
    // mark's overhang and its ring. Sibling of the bar, drawn over it.
    const el = await show();
    const mark = el.shadowRoot.querySelector('.mark');
    expect(mark.parentElement.classList.contains('bar-wrap')).toBe(true);
    expect(el.shadowRoot.querySelector('.bar .mark')).toBeNull();
  });

  it('says so in words when autocompact is off, instead of just dropping the mark', async () => {
    // An unmarked bar otherwise reads as one whose threshold is somewhere
    // off to the right, which is the opposite of the truth.
    const el = await show(usageFixture({ isAutoCompactEnabled: false }));
    expect(el.shadowRoot.querySelector('.mark')).toBeNull();
    expect(el.shadowRoot.querySelector('.no-mark').textContent)
      .toMatch(/autocompact off/i);
    expect(el.shadowRoot.querySelector('.no-mark').textContent)
      .toMatch(/fails at the limit/i);
  });

  it('carries no note while autocompact is on', async () => {
    const el = await show();
    expect(el.shadowRoot.querySelector('.no-mark')).toBeNull();
  });

  it('drops the mark when the engine reports no threshold', async () => {
    const el = await show(usageFixture({ autoCompactThreshold: 0 }));
    expect(el.shadowRoot.querySelector('.mark')).toBeNull();
    // And says nothing about it: autocompact is on, the engine just did
    // not say where. A warning here would be inventing a fact.
    expect(el.shadowRoot.querySelector('.no-mark')).toBeNull();
  });

  it('excludes deferred and empty categories from the fill', async () => {
    const el = await show();
    const titles = [...el.shadowRoot.querySelectorAll('.bar-seg')].map(
      (s) => s.title,
    );
    expect(titles.some((t) => t.includes('Deferred'))).toBe(false);
    expect(titles.some((t) => t.includes('Empty'))).toBe(false);
  });

  it('sizes segments against the window', async () => {
    const el = await show();
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    // 42000 / 200000 = 21%
    expect(segs[1].style.width.startsWith('21')).toBe(true);
  });

  it('excludes free space and the autocompact buffer from the fill', async () => {
    const el = await show();
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    const titles = segs.map((s) => s.title);
    expect(titles.some((t) => t.includes('Free space'))).toBe(false);
    expect(titles.some((t) => t.includes('Autocompact buffer'))).toBe(false);
    // 45200 / 200000 — the fill is the tokens in use, not the window.
    const width = segs.reduce((sum, s) => sum + parseFloat(s.style.width), 0);
    expect(width).toBeCloseTo(22.6, 1);
  });

  it('resolves segment colours from the engine theme tokens', async () => {
    const el = await show();
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs[0].style.background).toBe('rgb(88, 166, 255)');
    expect(segs[1].style.background).toBe('rgb(188, 140, 255)');
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
      'Context 23 percent used',
    );
  });

  it('names the compaction threshold in the tooltip', async () => {
    // This clause used to be gated on `rawMaxTokens > maxTokens`, which
    // never holds, so the tooltip's whole purpose went unrendered.
    const el = await show();
    const title = el.shadowRoot.querySelector('.bar').title;
    expect(title).toContain('45,200 of 200,000 tokens');
    // 45200 / 167000 = 27%, with 121,800 left.
    expect(title).toContain('27% of the way to an autocompact');
    expect(title).toContain('167,000 tokens');
    expect(title).toContain('121,800 left');
  });

  it('omits the compaction clause when no threshold is reported', async () => {
    const usage = usageFixture();
    delete usage.autoCompactThreshold;
    const el = await show(usage);
    expect(el.shadowRoot.querySelector('.bar').title).not.toContain(
      'autocompact',
    );
  });

  it('warns in the tooltip when autocompact is off', async () => {
    const el = await show(usageFixture({ isAutoCompactEnabled: false }));
    expect(el.shadowRoot.querySelector('.bar').title).toContain(
      'Autocompact is off',
    );
  });

  it('lists categories with their colours resolved', async () => {
    const el = await show();
    const chips = [...el.shadowRoot.querySelectorAll('.cat')];
    // Content and deferred rows; the structural rows and the
    // zero-token row are dropped.
    expect(chips).toHaveLength(3);
    expect(chips[0].textContent.trim()).toBe('System prompt 3.2K');
    expect(chips[0].querySelector('.swatch').style.background).toBe(
      'rgb(88, 166, 255)',
    );
    for (const chip of chips) {
      expect(chip.querySelector('.swatch').style.background).toMatch(/^rgb/);
    }
  });

  it('keeps free space out of the legend', async () => {
    // In 300px the legend is a glance, not a ledger. "Free space
    // 121.8K" is both the largest chip and the least useful.
    const el = await show();
    const text = el.shadowRoot.querySelector('.cats').textContent;
    expect(text).not.toContain('Free space');
    expect(text).not.toContain('Autocompact buffer');
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
