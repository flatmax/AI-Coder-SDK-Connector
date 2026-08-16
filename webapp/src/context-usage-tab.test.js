// Tests for `ac-context-usage-tab` — the Context tab's breakdown of what is
// filling the engine's context window.
//
// The panel landed in phase 3 (CC-17) with no coverage at all, and phase 5
// adds `session-changed` as a second refresh trigger. These tests pin the
// behaviour that a second trigger would otherwise silently change: when the
// panel asks the engine for numbers, when it refuses to (hidden tab, no
// proxy, fetch already in flight), and what it renders from a real
// `ContextUsageResponse`.
//
// Harness follows settings-tab.test.js: a flat object installed as the
// SharedRpc proxy, keyed by "Service.method", each handler returning a
// single-key envelope `{fake: value}` matching jrpc-oo's multi-remote shape.
//
// One structural detail matters. `_isTabActive()` reads
// `parentElement.classList` for `tab-panel` / `active`, which is exactly how
// app-shell/render.js mounts this element. So tests wrap the panel in that
// div to control visibility, rather than relying on layout — jsdom reports
// `offsetParent` as null unconditionally, so the fallback branch cannot be
// driven by attaching to the document.

import { afterEach, describe, expect, it, vi } from 'vitest';

import './context-usage-tab.js';
import { SharedRpc } from './rpc.js';

const _mounted = [];

/**
 * Mount a panel wrapped in the `.tab-panel` div app-shell gives it.
 *
 * @param {{active?: boolean, bare?: boolean}} opts — `active` sets the
 *   class that marks the tab visible; `bare` skips the wrapper entirely to
 *   exercise the `offsetParent` fallback.
 */
function mountTab({ active = true, bare = false } = {}) {
  const el = document.createElement('ac-context-usage-tab');
  if (bare) {
    document.body.appendChild(el);
    _mounted.push(el);
    return el;
  }
  const panel = document.createElement('div');
  panel.className = active ? 'tab-panel active' : 'tab-panel';
  panel.appendChild(el);
  document.body.appendChild(panel);
  _mounted.push(panel);
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

/** Install a `get_context_usage` that answers with `usage`. */
function publishUsage(usage, fetchedAt = '2026-08-15T10:30:00Z') {
  const handler = vi.fn(() => ({ usage, fetched_at: fetchedAt }));
  publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
  return handler;
}

async function settle(el) {
  // The fetch chain is rpcExtract → proxy fn → envelope unwrap → state
  // write → Lit re-render. Each hop is a microtask; loop enough times for
  // the whole chain plus the render it triggers.
  for (let i = 0; i < 6; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

function pushEvent(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

/**
 * Click through to one of the tab's sections.
 *
 * Phase 6 put the breakdown behind a segmented control, so the Session
 * tables are no longer on screen at mount — the panel opens on Usage.
 * Matching the segment by its visible label rather than by index keeps
 * these tests indifferent to a third segment being added later.
 */
async function showSection(el, label) {
  const seg = [...el.shadowRoot.querySelectorAll('.seg')].find(
    (b) => b.textContent.trim() === label,
  );
  if (!seg) throw new Error(`no "${label}" segment on screen`);
  seg.click();
  await settle(el);
  return el;
}

afterEach(() => {
  while (_mounted.length) {
    const el = _mounted.pop();
    el.remove();
  }
  SharedRpc.reset();
  // The selected section persists, so a test that switches would
  // otherwise decide which section the next test opens on.
  localStorage.removeItem('ac-dc-context-section');
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/**
 * A `ContextUsageResponse` shaped like a real one.
 *
 * This fixture used to assume hex `color` values and a `maxTokens`
 * already reduced by the autocompact buffer. Neither is true, both
 * suites passed anyway, and the tab shipped drawing transparent bar
 * segments and shares like "Free space — 692.0%". The shape below is
 * the live payload's, verified by the three identities asserted in
 * context-usage.test.js:
 *
 *   content categories  sum to `totalTokens`
 *   Free space        = `autoCompactThreshold` - `totalTokens`
 *   Autocompact buffer = `maxTokens` - `autoCompactThreshold`
 *
 * Keep them holding when editing token counts, or `partitionCategories`
 * stops verifying and the bar silently falls back to one segment.
 */
function usageFixture(overrides = {}) {
  return {
    categories: [
      { name: 'System prompt', tokens: 3200, color: 'promptBorder' },
      { name: 'System tools', tokens: 11500, color: 'inactive' },
      { name: 'MCP tools', tokens: 4800, color: 'claude' },
      {
        name: 'Messages',
        tokens: 42000,
        color: 'purple_FOR_SUBAGENTS_ONLY',
      },
      {
        name: 'Deferred tools',
        tokens: 9000,
        color: 'inactive',
        isDeferred: true,
      },
      { name: 'Autocompact buffer', tokens: 33000, color: 'inactive' },
      { name: 'Free space', tokens: 105500, color: 'promptBorder' },
    ],
    totalTokens: 61500,
    maxTokens: 200000,
    rawMaxTokens: 200000,
    autoCompactThreshold: 167000,
    percentage: 30.8,
    model: 'claude-opus-4-6',
    isAutoCompactEnabled: true,
    ...overrides,
  };
}

/**
 * A consistent payload at a given fill level.
 *
 * Rebuilds the structural categories so the identities above keep
 * holding — overriding `totalTokens` on `usageFixture` alone would
 * leave "Free space" describing a different window and quietly switch
 * the bar to its unsegmented fallback.
 */
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

function rows(el, section) {
  const sections = [...el.shadowRoot.querySelectorAll('section')];
  const match = sections.find((s) =>
    s.querySelector('h3')?.textContent.includes(section),
  );
  if (!match) return null;
  return [...match.querySelectorAll('tbody tr')].map((tr) =>
    [...tr.querySelectorAll('td')].map((td) => td.textContent.trim()),
  );
}

function sectionFor(el, heading) {
  return [...el.shadowRoot.querySelectorAll('section')].find((s) =>
    s.querySelector('h3')?.textContent.includes(heading),
  );
}

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------

describe('ContextUsageTab fetching', () => {
  it('reads the breakdown once the proxy is published', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(el._usage.totalTokens).toBe(61500);
  });

  it('renders a waiting message and makes no call without a proxy', async () => {
    const el = mountTab();
    await settle(el);
    expect(el.rpcConnected).toBe(false);
    expect(el.shadowRoot.querySelector('.empty').textContent).toContain(
      'No breakdown yet',
    );
  });

  it('collapses concurrent refreshes into one control request', async () => {
    // A refresh costs a control request to the CLI subprocess, and two
    // replies racing could land out of order.
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    const handler = vi.fn(async () => {
      await gate;
      return { usage: usageFixture(), fetched_at: '2026-08-15T10:30:00Z' };
    });
    publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
    const el = mountTab();
    await Promise.resolve();
    el._refresh();
    el._refresh();
    release();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('clears the loading flag after a failed fetch', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => {
        throw new Error('control request timed out');
      },
    });
    const el = mountTab();
    await settle(el);
    expect(el._loading).toBe(false);
    expect(el._error).toBe('control request timed out');
  });

  it('surfaces the engine error envelope', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => ({
        error: 'engine is not ready',
      }),
    });
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'engine is not ready',
    );
    expect(el.shadowRoot.querySelector('.note').textContent).toContain(
      'unavailable until a session is connected',
    );
  });

  it('reports a missing usage payload rather than rendering blank', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => ({ fetched_at: 'now' }),
    });
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'returned no context usage',
    );
  });

  it('falls back to a generic message when the throw has no message', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => {
        throw {};
      },
    });
    const el = mountTab();
    await settle(el);
    expect(el._error).toBe('Could not read context usage.');
  });

  it('keeps the previous numbers when a later refresh fails', async () => {
    const handler = vi
      .fn()
      .mockImplementationOnce(() => ({
        usage: usageFixture(),
        fetched_at: '2026-08-15T10:30:00Z',
      }))
      .mockImplementationOnce(() => ({ error: 'session lost' }));
    publishFakeRpc({ 'ClaudeCodeService.get_context_usage': handler });
    const el = mountTab();
    await settle(el);
    await el._refresh();
    await settle(el);
    // Prior numbers stay on screen, but the footer says they are stale.
    expect(el._usage.totalTokens).toBe(61500);
    const footer = [...el.shadowRoot.querySelectorAll('.error')].pop();
    expect(footer.textContent).toContain('Last refresh failed: session lost');
  });
});

// ---------------------------------------------------------------------------
// Refresh triggers
// ---------------------------------------------------------------------------

describe('ContextUsageTab refresh triggers', () => {
  it('refreshes on stream-complete while the tab is visible', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: true });
    await settle(el);
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(2);
    expect(el._stale).toBe(false);
  });

  it('marks itself stale instead of fetching while hidden', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: false });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(el._stale).toBe(true);
  });

  it('shows a stale badge once the numbers predate a turn', async () => {
    publishUsage(usageFixture());
    const el = mountTab({ active: false });
    await settle(el);
    expect(el.shadowRoot.querySelector('.stale-badge')).toBeNull();
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    expect(el.shadowRoot.querySelector('.stale-badge').textContent).toContain(
      'stale',
    );
  });

  it('refreshes on session-changed while visible', async () => {
    // Phase 5's trigger. Nothing dispatches this event yet; the panel is
    // already wired for it, so the wiring is what gets pinned here.
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: true });
    await settle(el);
    pushEvent('session-changed', { messages: [] });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('marks stale on session-changed while hidden', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: false });
    await settle(el);
    pushEvent('session-changed', { messages: [] });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(el._stale).toBe(true);
  });

  it('refreshes and clears the badge when the tab becomes visible', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: false });
    await settle(el);
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    el.onTabVisible();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(2);
    expect(el._stale).toBe(false);
    expect(el.shadowRoot.querySelector('.stale-badge')).toBeNull();
  });

  it('does not re-fetch on becoming visible when nothing changed', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: true });
    await settle(el);
    el.onTabVisible();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('fetches on becoming visible when the first fetch never landed', async () => {
    const el = mountTab({ active: false });
    await settle(el);
    const handler = publishUsage(usageFixture());
    // Publishing re-fires rpc-ready, so wait that out before asserting.
    await settle(el);
    handler.mockClear();
    el._usage = null;
    el.onTabVisible();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('treats an unwrapped element as hidden in jsdom', async () => {
    // The `offsetParent` fallback: no `.tab-panel` parent and no layout
    // engine, so the panel considers itself hidden and defers the fetch.
    const handler = publishUsage(usageFixture());
    const el = mountTab({ bare: true });
    await settle(el);
    handler.mockClear();
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    expect(handler).not.toHaveBeenCalled();
    expect(el._stale).toBe(true);
  });

  it('uses offsetParent when there is no tab-panel wrapper', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ bare: true });
    await settle(el);
    Object.defineProperty(el, 'offsetParent', {
      configurable: true,
      value: document.body,
    });
    handler.mockClear();
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('stops listening once removed from the DOM', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab({ active: true });
    await settle(el);
    const wrapper = el.parentElement;
    wrapper.remove();
    handler.mockClear();
    pushEvent('stream-complete', { requestId: 'r1', result: {} });
    pushEvent('session-changed', {});
    await Promise.resolve();
    expect(handler).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Headline
// ---------------------------------------------------------------------------

describe('ContextUsageTab headline', () => {
  it('shows the engine percentage and the token totals', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').textContent.trim()).toBe(
      '30.8%',
    );
    expect(el.shadowRoot.querySelector('.of').textContent).toContain(
      '61,500 / 200,000 tokens',
    );
  });

  it('computes the percentage when the engine omits it', async () => {
    const usage = usageFixture();
    delete usage.percentage;
    publishUsage(usage);
    const el = mountTab();
    await settle(el);
    // 61500 / 200000 = 30.75
    expect(el.shadowRoot.querySelector('.pct').textContent.trim()).toBe(
      '30.8%',
    );
  });

  it('clamps a percentage above 100', async () => {
    publishUsage(usageFixture({ percentage: 142 }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').textContent.trim()).toBe(
      '100.0%',
    );
  });

  // The bands are measured against the autocompact threshold, not the
  // raw window. Driving them off the engine's `percentage` is what made
  // the red band unreachable: a compact fires at 83.5% of the window,
  // so a payload never reported 90% before the pause it was warning
  // about had already happened.

  it('colours the headline green below the amber band', async () => {
    publishUsage(usageAt(61500));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').style.color).toContain('126');
  });

  it('colours the headline amber between 75 and 90 percent of the limit', async () => {
    // 140000 / 167000 = 83.8% — amber, while the engine's own figure
    // for the same payload is a green-looking 70% of the window.
    publishUsage(usageAt(140000));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').textContent.trim()).toBe(
      '70.0%',
    );
    expect(el.shadowRoot.querySelector('.pct').style.color).toBe(
      'rgb(210, 153, 34)',
    );
  });

  it('colours the headline red above 90 percent of the limit', async () => {
    // 160000 / 167000 = 95.8%. A compact is one turn away and the
    // headline says 80% of the window.
    publishUsage(usageAt(160000));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').style.color).toBe(
      'rgb(248, 81, 73)',
    );
  });

  it('names the compaction threshold and the room left', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const note = el.shadowRoot.querySelector('.note');
    // 61500 / 167000 = 36.8%
    expect(note.textContent).toContain('36.8%');
    expect(note.textContent).toContain('167,000 tokens');
    expect(note.textContent).toContain('105,500 tokens of');
    expect(note.textContent).toContain('33,000');
  });

  it('colours the compaction note by its own figure', async () => {
    publishUsage(usageAt(160000));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.note').style.color).toBe(
      'rgb(248, 81, 73)',
    );
  });

  it('omits the compaction note when the engine reports no threshold', async () => {
    const usage = usageFixture();
    delete usage.autoCompactThreshold;
    publishUsage(usage);
    const el = mountTab();
    await settle(el);
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes('autocompact'))).toBe(false);
  });

  it('omits the compaction note when autocompact is off', async () => {
    // The warning takes its place — there is no threshold to count
    // down to when nothing intervenes.
    publishUsage(usageFixture({ isAutoCompactEnabled: false }));
    const el = mountTab();
    await settle(el);
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes('way to an autocompact'))).toBe(false);
  });

  it('warns when autocompact is disabled', async () => {
    publishUsage(usageFixture({ isAutoCompactEnabled: false }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.warn').textContent).toContain(
      'Autocompact is off',
    );
  });

  it('does not warn when autocompact is enabled', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.warn')).toBeNull();
  });

  it('names the model the numbers were measured for', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.model-note').textContent).toContain(
      'claude-opus-4-6',
    );
  });

  it('omits the model note when the engine sends no model', async () => {
    const usage = usageFixture();
    delete usage.model;
    publishUsage(usage);
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.model-note')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Segmented bar
// ---------------------------------------------------------------------------

describe('ContextUsageTab bar', () => {
  it('draws one segment per live category, sized against maxTokens', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const segs = [...el.shadowRoot.querySelectorAll('.bar .bar-seg')];
    expect(segs).toHaveLength(4);
    expect(segs[0].title).toBe('System prompt: 3.2K');
    // 42000 / 200000 = 21%
    expect(segs[3].style.width.startsWith('21')).toBe(true);
  });

  it('excludes deferred categories from the fill', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const titles = [...el.shadowRoot.querySelectorAll('.bar-seg')].map(
      (s) => s.title,
    );
    expect(titles.some((t) => t.includes('Deferred tools'))).toBe(false);
  });

  it('resolves the engine theme token to a CSS colour', async () => {
    // `color` carries a token name — 'promptBorder' here — not CSS.
    // Pushing it into the style attribute verbatim, as this did,
    // yielded a transparent segment.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const seg = el.shadowRoot.querySelector('.bar-seg');
    expect(seg.style.background).toBe('rgb(88, 166, 255)');
  });

  it('excludes free space and the autocompact buffer from the fill', async () => {
    // The engine's categories tile the whole window. Segmenting by all
    // of them drew a permanently full bar, 73% of it "Free space".
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    const titles = segs.map((s) => s.title);
    expect(titles.some((t) => t.includes('Free space'))).toBe(false);
    expect(titles.some((t) => t.includes('Autocompact buffer'))).toBe(false);
    const width = segs.reduce(
      (sum, s) => sum + parseFloat(s.style.width),
      0,
    );
    // 61500 / 200000 — the fill matches the tokens in use.
    expect(width).toBeCloseTo(30.75, 2);
  });

  it('falls back to one segment when the categories do not add up', async () => {
    // The engine renaming "Free space" would leave the split unable to
    // tell room from content. A plain bar is wrong-looking; a segmented
    // one would be wrong.
    publishUsage(
      usageFixture({
        categories: [
          { name: 'Messages', tokens: 42000, color: 'warning' },
          { name: 'Headroom', tokens: 105500, color: 'promptBorder' },
        ],
        percentage: 50,
      }),
    );
    const el = mountTab();
    await settle(el);
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs).toHaveLength(1);
    expect(segs[0].style.width).toBe('50%');
  });

  it('falls back to a neutral colour for an uncoloured category', async () => {
    publishUsage(
      usageFixture({
        categories: [{ name: 'Mystery', tokens: 1000 }],
        totalTokens: 1000,
      }),
    );
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.bar-seg').style.background).toBe(
      'rgb(110, 118, 129)',
    );
  });

  it('draws a single percentage segment when there are no categories', async () => {
    publishUsage(usageFixture({ categories: [], percentage: 50 }));
    const el = mountTab();
    await settle(el);
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs).toHaveLength(1);
    expect(segs[0].style.width).toBe('50%');
  });

  it('draws a single segment when maxTokens is unknown', async () => {
    publishUsage(usageFixture({ maxTokens: 0, percentage: 12 }));
    const el = mountTab();
    await settle(el);
    const segs = [...el.shadowRoot.querySelectorAll('.bar-seg')];
    expect(segs).toHaveLength(1);
    expect(segs[0].style.width).toBe('12%');
  });
});

// ---------------------------------------------------------------------------
// Categories table
// ---------------------------------------------------------------------------

describe('ContextUsageTab categories', () => {
  it('sorts categories by token count, largest first', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const names = rows(el, 'Categories').map((r) => r[0]);
    // Free space and the autocompact buffer are listed — the table is
    // the whole window's breakdown, unlike the bar, which is the fill.
    expect(names[0]).toContain('Free space');
    expect(names[1]).toContain('Messages');
    expect(names[2]).toContain('Autocompact buffer');
    expect(names[3]).toContain('System tools');
    expect(names[4]).toContain('Deferred tools');
    expect(names[5]).toContain('MCP tools');
    expect(names[6]).toContain('System prompt');
  });

  it('shows each category share of the window', async () => {
    // Against `maxTokens`, not `totalTokens`. The engine's rows include
    // the room left, so the tokens-in-use denominator produced shares
    // over 100% — "Free space — 692.0%" on a live payload.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const messages = rows(el, 'Categories').find((r) =>
      r[0].includes('Messages'),
    );
    expect(messages[1]).toBe('42.0K');
    // 42000 / 200000 = 21.0%
    expect(messages[2]).toBe('21.0%');
  });

  it('keeps every share at or below 100 percent', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    for (const row of rows(el, 'Categories')) {
      expect(parseFloat(row[2])).toBeLessThanOrEqual(100);
    }
  });

  it('does not repeat a marker the engine already put in the name', async () => {
    // Live rows are named "System tools (deferred)" AND flagged
    // `isDeferred`, which rendered as "... (deferred) (deferred)".
    publishUsage(
      usageFixture({
        categories: [
          { name: 'Messages', tokens: 61500, color: 'warning' },
          {
            name: 'MCP tools (deferred)',
            tokens: 9182,
            color: 'inactive',
            isDeferred: true,
          },
        ],
      }),
    );
    const el = mountTab();
    await settle(el);
    const row = [
      ...sectionFor(el, 'Categories').querySelectorAll('tr.deferred'),
    ][0];
    expect(row.textContent).toContain('MCP tools (deferred)');
    expect(row.textContent).not.toContain('(deferred) (deferred)');
    expect(row.querySelector('.note')).toBeNull();
  });

  it('marks a deferred category and says why', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const deferred = [
      ...sectionFor(el, 'Categories').querySelectorAll('tr.deferred'),
    ];
    expect(deferred).toHaveLength(1);
    expect(deferred[0].textContent).toContain('(deferred)');
    expect(deferred[0].querySelector('.note').title).toContain(
      'not loaded into the window yet',
    );
  });

  it('shows an em dash for a share the window cannot support', async () => {
    publishUsage(
      usageFixture({
        maxTokens: 0,
        categories: [{ name: 'Messages', tokens: 10, color: 'warning' }],
      }),
    );
    const el = mountTab();
    await settle(el);
    expect(rows(el, 'Categories')[0][2]).toBe('—');
  });

  it('says so when the engine reports no categories', async () => {
    publishUsage(usageFixture({ categories: [] }));
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Categories').textContent).toContain(
      'no categories',
    );
  });

  it('renders a swatch resolved from the engine theme token', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const messages = [
      ...sectionFor(el, 'Categories').querySelectorAll('tbody tr'),
    ].find((tr) => tr.textContent.includes('Messages'));
    // 'purple_FOR_SUBAGENTS_ONLY' — a token name, unusable as CSS.
    expect(messages.querySelector('.swatch').style.background).toBe(
      'rgb(188, 140, 255)',
    );
  });

  it('leaves no swatch without a background', async () => {
    // The shipped defect: every swatch and every bar segment was
    // transparent, because a theme token is not a CSS colour.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const swatches = [
      ...sectionFor(el, 'Categories').querySelectorAll('.swatch'),
    ];
    expect(swatches.length).toBeGreaterThan(0);
    for (const s of swatches) {
      expect(s.style.background).toMatch(/^rgb/);
    }
  });

  it('drops a category the engine counted at zero', async () => {
    publishUsage(
      usageFixture({
        categories: [
          { name: 'Messages', tokens: 61500, color: 'warning' },
          { name: 'Skills', tokens: 0, color: 'warning' },
        ],
      }),
    );
    const el = mountTab();
    await settle(el);
    expect(rows(el, 'Categories').map((r) => r[0])).toHaveLength(1);
  });

  it('keeps a category whose count is unusable', async () => {
    // A row the engine named but could not count is worth seeing; a row
    // that costs nothing is not. Only the second is dropped.
    publishUsage(
      usageFixture({
        categories: [
          { name: 'Messages', tokens: 61500, color: 'warning' },
          { name: 'Mystery', color: 'warning' },
        ],
      }),
    );
    const el = mountTab();
    await settle(el);
    const mystery = rows(el, 'Categories').find((r) =>
      r[0].includes('Mystery'),
    );
    expect(mystery[1]).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// The autocompact mark on the gauge
// ---------------------------------------------------------------------------

describe('ContextUsageTab autocompact mark', () => {
  it('marks the threshold short of the bar end', async () => {
    // The point of the gauge. 167000/200000 — the fill reads 30.8% and
    // the session gives out at 83.5%, not at the bar's end.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const mark = el.shadowRoot.querySelector('.bar .mark');
    expect(mark).not.toBeNull();
    expect(parseFloat(mark.style.left)).toBeCloseTo(83.5, 6);
  });

  it('names the token count it marks', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.mark').title).toContain('167,000');
  });

  it('labels the mark and the window end beneath the bar', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const note = el.shadowRoot
      .querySelector('.mark-note')
      .textContent.replace(/\s+/g, ' ');
    expect(note).toContain('Autocompact at 83.5%');
    expect(note).toContain('200,000');
  });

  it('draws no mark when autocompact is off, and says why', async () => {
    // A mark would promise an intervention that is not coming.
    publishUsage(usageFixture({ isAutoCompactEnabled: false }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.mark')).toBeNull();
    expect(el.shadowRoot.querySelector('.mark-note')).toBeNull();
    expect(
      el.shadowRoot.querySelector('.warn').textContent.replace(/\s+/g, ' '),
    ).toContain('the bar carries no mark');
  });

  it('draws no mark when the threshold is the window itself', async () => {
    publishUsage(usageFixture({ autoCompactThreshold: 200000 }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.mark')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Past the window
// ---------------------------------------------------------------------------

describe('ContextUsageTab over the limit', () => {
  it('says nothing while the context fits', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.error')).toBeNull();
  });

  it('reports the overshoot a clamped gauge cannot', async () => {
    // The bar pins at 100% here, so without this line the panel shows a
    // full bar and no explanation.
    publishUsage(usageAt(210000));
    const el = mountTab();
    await settle(el);
    const text = el.shadowRoot
      .querySelector('.error')
      .textContent.replace(/\s+/g, ' ');
    expect(text).toContain('10,000 tokens past the 200,000-token');
    expect(text).toContain('Autocompact should bring it back down');
  });

  it('does not promise a recovery when autocompact is off', async () => {
    publishUsage(usageAt(210000, { isAutoCompactEnabled: false }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.error').textContent).toContain(
      'nothing will reduce it',
    );
  });

  it('calls an auto-sized window a hard limit', async () => {
    publishUsage(usageAt(210000, { autocompactSource: 'auto' }));
    const el = mountTab();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.error').textContent.replace(/\s+/g, ' '),
    ).toContain("Past the model's 200,000-token limit by 10,000 tokens.");
  });
});

// ---------------------------------------------------------------------------
// Message breakdown, tool traffic, attachments
// ---------------------------------------------------------------------------

describe('ContextUsageTab message breakdown', () => {
  /** Parts summing to the fixture's 42000-token Messages category. */
  function breakdown(overrides = {}) {
    return {
      userMessageTokens: 8000,
      assistantMessageTokens: 12000,
      toolCallTokens: 4000,
      toolResultTokens: 16000,
      attachmentTokens: 1500,
      redirectedContextTokens: 0,
      unattributedTokens: 500,
      toolCallsByType: [
        { name: 'Read', callTokens: 1500, resultTokens: 9000 },
        { name: 'Bash', callTokens: 2500, resultTokens: 7000 },
      ],
      attachmentsByType: [{ name: 'pasted_text', tokens: 1500 }],
      ...overrides,
    };
  }

  const withBreakdown = (mb = breakdown()) =>
    usageFixture({ messageBreakdown: mb });

  it('totals the conversation in the heading', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Messages').querySelector('h3').textContent).toContain(
      '42.0K tokens',
    );
  });

  it('draws one segment per non-zero part, tiling the bar', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    const segs = [
      ...sectionFor(el, 'Messages').querySelectorAll('.bar-seg'),
    ];
    expect(segs).toHaveLength(6);
    expect(segs[0].title).toBe('User messages: 8.0K');
    const width = segs.reduce((sum, s) => sum + parseFloat(s.style.width), 0);
    expect(width).toBeCloseTo(100, 6);
  });

  it('lists the parts in conversation order with their share', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    expect(rows(el, 'Messages')).toEqual([
      ['User messages', '8.0K', '19.0%'],
      ['Assistant messages', '12.0K', '28.6%'],
      ['Tool calls', '4.0K', '9.5%'],
      ['Tool results', '16.0K', '38.1%'],
      ['Attachments', '1.5K', '3.6%'],
      ['Unattributed', '500', '1.2%'],
    ]);
  });

  it('gives every part a visible swatch', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    const swatches = [
      ...sectionFor(el, 'Messages').querySelectorAll('.swatch'),
    ];
    expect(swatches).toHaveLength(6);
    for (const s of swatches) expect(s.style.background).toMatch(/^rgb/);
  });

  it('says so when the parts overshoot the Messages category', async () => {
    // The engine floors `unattributedTokens` at zero, so its per-part
    // estimate can exceed what the category is charged. The bar still
    // tiles; the note explains the difference.
    publishUsage(withBreakdown(breakdown({
      assistantMessageTokens: 20000,
      unattributedTokens: 0,
    })));
    const el = mountTab();
    await settle(el);
    const note = sectionFor(el, 'Messages')
      .querySelector('.note')
      .textContent.replace(/\s+/g, ' ');
    expect(note).toContain('49,500 tokens against the 42,000');
  });

  it('adds no note when the parts reconcile', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Messages').querySelector('.note')).toBeNull();
  });

  it('omits the section when the engine sent no breakdown', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Messages')).toBeUndefined();
  });

  it('omits the section for a session with no turns yet', async () => {
    publishUsage(withBreakdown({
      userMessageTokens: 0,
      toolCallsByType: [],
      attachmentsByType: [],
    }));
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Messages')).toBeUndefined();
  });

  it('names the tools the context is paying for, heaviest first', async () => {
    // The exit criterion's "names the ac-dc tools it is paying for":
    // these are the engine's own tool names, so a bridge tool appears
    // under the name it was registered with.
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    expect(rows(el, 'Tool traffic')).toEqual([
      ['Read', '1.5K', '9.0K', '10.5K'],
      ['Bash', '2.5K', '7.0K', '9.5K'],
    ]);
  });

  it('splits calls from results, which are separately fixable', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    const heads = [
      ...sectionFor(el, 'Tool traffic').querySelectorAll('th'),
    ].map((th) => th.textContent.trim());
    expect(heads).toEqual(['Tool', 'Calls', 'Results', 'Total']);
  });

  it('counts the tools and their total in the heading', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    const heading = sectionFor(el, 'Tool traffic')
      .querySelector('h3')
      .textContent.replace(/\s+/g, ' ');
    expect(heading).toContain('2 tools');
    expect(heading).toContain('20.0K tokens');
  });

  it('omits tool traffic when no tool has been called', async () => {
    publishUsage(withBreakdown(breakdown({ toolCallsByType: [] })));
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Tool traffic')).toBeUndefined();
  });

  it('breaks attachments down by kind', async () => {
    publishUsage(withBreakdown(breakdown({
      attachmentsByType: [
        { name: 'image', tokens: 400 },
        { name: 'pasted_text', tokens: 1100 },
      ],
    })));
    const el = mountTab();
    await settle(el);
    expect(rows(el, 'Attachments')).toEqual([
      ['pasted_text', '1.1K'],
      ['image', '400'],
    ]);
  });

  it('omits attachments when nothing was attached', async () => {
    publishUsage(withBreakdown(breakdown({ attachmentsByType: [] })));
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Attachments')).toBeUndefined();
  });

  it('keeps the conversation sections out of the Session view', async () => {
    publishUsage(withBreakdown());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(sectionFor(el, 'Messages')).toBeUndefined();
    expect(sectionFor(el, 'Tool traffic')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// The segmented control
// ---------------------------------------------------------------------------

describe('ContextUsageTab sections', () => {
  function segments(el) {
    return [...el.shadowRoot.querySelectorAll('.seg')].map((b) =>
      b.textContent.trim(),
    );
  }

  it('offers only the sections that have something in them', async () => {
    // The spec lists a third, Debug, which reads hook traffic and server
    // info this panel does not fetch. A segment opening onto nothing
    // would be worse than its absence.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(segments(el)).toEqual(['Usage', 'Session']);
  });

  it('shows no control before the first breakdown arrives', async () => {
    // Nothing to switch between, and the empty state is one line.
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': () => ({ error: 'no session' }),
    });
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.segmented')).toBeNull();
  });

  it('opens on Usage', async () => {
    publishUsage(usageFixture({
      memoryFiles: [{ path: '/repo/CLAUDE.md', type: 'Project', tokens: 1800 }],
    }));
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Categories')).not.toBeUndefined();
    expect(sectionFor(el, 'Memory files')).toBeUndefined();
  });

  it('swaps the body when a section is selected', async () => {
    publishUsage(usageFixture({
      memoryFiles: [{ path: '/repo/CLAUDE.md', type: 'Project', tokens: 1800 }],
    }));
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(sectionFor(el, 'Memory files')).not.toBeUndefined();
    expect(sectionFor(el, 'Categories')).toBeUndefined();
  });

  it('marks the active segment for assistive tech', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const selected = () =>
      [...el.shadowRoot.querySelectorAll('.seg')]
        .filter((b) => b.getAttribute('aria-selected') === 'true')
        .map((b) => b.textContent.trim());
    expect(selected()).toEqual(['Usage']);
    await showSection(el, 'Session');
    expect(selected()).toEqual(['Session']);
  });

  it('keeps the footer under every section', async () => {
    // It carries when the numbers were read, which qualifies both views.
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(el.shadowRoot.textContent).toContain('Read from the engine at');
  });

  it('reopens on the section last read', async () => {
    publishUsage(usageFixture());
    const first = mountTab();
    await settle(first);
    await showSection(first, 'Session');
    expect(localStorage.getItem('ac-dc-context-section')).toBe('session');

    const second = mountTab();
    await settle(second);
    expect(
      [...second.shadowRoot.querySelectorAll('.seg')]
        .filter((b) => b.getAttribute('aria-selected') === 'true')
        .map((b) => b.textContent.trim()),
    ).toEqual(['Session']);
  });

  it('ignores a stored section it no longer has', async () => {
    localStorage.setItem('ac-dc-context-section', 'debug');
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    expect(sectionFor(el, 'Categories')).not.toBeUndefined();
  });

  it('survives localStorage throwing', async () => {
    // Private-mode quota errors are not worth a broken panel; the
    // section simply does not persist.
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    try {
      publishUsage(usageFixture());
      const el = mountTab();
      await settle(el);
      await showSection(el, 'Session');
      expect(sectionFor(el, 'Memory files')).toBeUndefined();
      expect(el.shadowRoot.textContent).toContain('no memory files');
    } finally {
      spy.mockRestore();
    }
  });

  it('says so when the session has nothing to show', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(el.shadowRoot.querySelector('.empty').textContent).toContain(
      'no memory files, MCP tools or agent',
    );
  });
});

// ---------------------------------------------------------------------------
// Token formatting
// ---------------------------------------------------------------------------

describe('ContextUsageTab token formatting', () => {
  async function tokensFor(tokens) {
    publishUsage(
      usageFixture({
        totalTokens: 1,
        categories: [{ name: 'Only', tokens, color: '#fff' }],
      }),
    );
    const el = mountTab();
    await settle(el);
    return rows(el, 'Categories')[0][1];
  }

  it('renders sub-thousand counts exactly', async () => {
    expect(await tokensFor(742)).toBe('742');
  });

  it('rounds a fractional sub-thousand count', async () => {
    expect(await tokensFor(742.6)).toBe('743');
  });

  it('renders thousands with one decimal', async () => {
    expect(await tokensFor(11500)).toBe('11.5K');
  });

  it('renders millions with two decimals', async () => {
    expect(await tokensFor(1_250_000)).toBe('1.25M');
  });

  it('renders an em dash for a non-numeric count', async () => {
    expect(await tokensFor(undefined)).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// Memory files, MCP tools, agents — the Session section
// ---------------------------------------------------------------------------

describe('ContextUsageTab memory files', () => {
  it('lists memory files with path, type and cost', async () => {
    publishUsage(
      usageFixture({
        memoryFiles: [
          { path: '/repo/CLAUDE.md', type: 'Project', tokens: 1800 },
          { path: '/home/u/.claude/CLAUDE.md', type: 'User', tokens: 320 },
        ],
      }),
    );
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(rows(el, 'Memory files')).toEqual([
      ['/repo/CLAUDE.md', 'Project', '1.8K'],
      ['/home/u/.claude/CLAUDE.md', 'User', '320'],
    ]);
  });

  it('omits the section when no memory files are loaded', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(sectionFor(el, 'Memory files')).toBeUndefined();
  });

  it('falls back to name, then an em dash, for a path-less entry', async () => {
    publishUsage(
      usageFixture({
        memoryFiles: [{ name: 'AGENTS.md', tokens: 90 }, { tokens: 5 }],
      }),
    );
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(rows(el, 'Memory files')).toEqual([
      ['AGENTS.md', '—', '90'],
      ['—', '—', '5'],
    ]);
  });
});

describe('ContextUsageTab MCP tools', () => {
  const tools = [
    { name: 'symbol_map', serverName: 'ac-dc', tokens: 900 },
    { name: 'doc_outline', serverName: 'ac-dc', tokens: 700 },
    {
      name: 'find_references',
      serverName: 'ac-dc',
      tokens: 600,
      isLoaded: false,
    },
  ];

  it('reports the tool count, loaded tokens, and deferred tokens', async () => {
    // The heading read "MCP tools — 0 loaded" on a live payload, where
    // every tool is deferred until first use. Directly above a table of
    // 35 tools, that parses as "no tools" rather than "no tokens".
    publishUsage(usageFixture({ mcpTools: tools }));
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    const heading = sectionFor(el, 'MCP tools')
      .querySelector('h3')
      .textContent.replace(/\s+/g, ' ')
      .trim();
    // 900 + 700 in the window, the 600 that is not counted separately.
    expect(heading).toContain('3 tools');
    expect(heading).toContain('1.6K tokens loaded');
    expect(heading).toContain('600 deferred');
  });

  it('omits the deferred clause when every tool is loaded', async () => {
    publishUsage(usageFixture({
      mcpTools: [{ name: 'symbol_map', serverName: 'ac-dc', tokens: 900 }],
    }));
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    const heading = sectionFor(el, 'MCP tools')
      .querySelector('h3')
      .textContent.replace(/\s+/g, ' ')
      .trim();
    expect(heading).toContain('1 tool,');
    expect(heading).not.toContain('deferred');
  });

  it('lists every tool, loaded or not', async () => {
    publishUsage(usageFixture({ mcpTools: tools }));
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(rows(el, 'MCP tools')).toEqual([
      ['symbol_map', 'ac-dc', '900'],
      ['doc_outline', 'ac-dc', '700'],
      ['find_references', 'ac-dc', '600'],
    ]);
  });

  it('dims a tool whose schema is not in the window', async () => {
    publishUsage(usageFixture({ mcpTools: tools }));
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    const dimmed = [
      ...sectionFor(el, 'MCP tools').querySelectorAll('tr.deferred'),
    ];
    expect(dimmed).toHaveLength(1);
    expect(dimmed[0].textContent).toContain('find_references');
  });

  it('omits the section when the engine has no MCP tools', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(sectionFor(el, 'MCP tools')).toBeUndefined();
  });
});

describe('ContextUsageTab agent definitions', () => {
  // `agents[]`'s element shape was the last guess left in this payload
  // — a live capture came back with the list empty. It is the CLI's own
  // wire schema now: `{agentType, source, tokens}`. The `name` fallback
  // below is kept for a payload that predates it.
  it('lists agents by type and source', async () => {
    publishUsage(
      usageFixture({
        agents: [
          { agentType: 'Explore', source: 'built-in', tokens: 450 },
          { name: 'reviewer', source: 'project', tokens: 300 },
        ],
      }),
    );
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(rows(el, 'Agent definitions')).toEqual([
      ['Explore', 'built-in', '450'],
      ['reviewer', 'project', '300'],
    ]);
  });

  it('omits the section when no agents are loaded', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    await showSection(el, 'Session');
    expect(sectionFor(el, 'Agent definitions')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Toolbar and footer
// ---------------------------------------------------------------------------

describe('ContextUsageTab toolbar', () => {
  function buttons(el) {
    return [...el.shadowRoot.querySelectorAll('.toolbar button')];
  }

  it('refreshes on demand', async () => {
    const handler = publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    buttons(el)[1].click();
    await settle(el);
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('disables refresh while a fetch is outstanding', async () => {
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    publishFakeRpc({
      'ClaudeCodeService.get_context_usage': async () => {
        await gate;
        return { usage: usageFixture(), fetched_at: 'now' };
      },
    });
    const el = mountTab();
    await Promise.resolve();
    await Promise.resolve();
    await el.updateComplete;
    const refresh = buttons(el)[1];
    expect(refresh.disabled).toBe(true);
    expect(refresh.textContent.trim()).toBe('Reading…');
    release();
    await settle(el);
    expect(buttons(el)[1].disabled).toBe(false);
  });

  it('disables refresh when there is no proxy', async () => {
    const el = mountTab();
    await settle(el);
    expect(buttons(el)[1].disabled).toBe(true);
  });

  it('asks the dialog for the chat tab', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const seen = [];
    el.addEventListener('request-dialog-tab', (e) => seen.push(e.detail));
    buttons(el)[0].click();
    expect(seen).toEqual([{ tab: 'files' }]);
  });

  it('asks the dialog to minimize', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const seen = vi.fn();
    el.addEventListener('request-dialog-minimize', seen);
    buttons(el).at(-1).click();
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it('bubbles both dialog requests out of the shadow root', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const seen = [];
    document.body.addEventListener('request-dialog-tab', (e) =>
      seen.push(e.type),
    );
    document.body.addEventListener('request-dialog-minimize', (e) =>
      seen.push(e.type),
    );
    buttons(el)[0].click();
    buttons(el).at(-1).click();
    expect(seen).toEqual([
      'request-dialog-tab',
      'request-dialog-minimize',
    ]);
  });
});

describe('ContextUsageTab footer', () => {
  it('says when the numbers were read from the engine', async () => {
    publishUsage(usageFixture(), '2026-08-15T10:30:00Z');
    const el = mountTab();
    await settle(el);
    const expected = new Date('2026-08-15T10:30:00Z').toLocaleTimeString();
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes(expected))).toBe(true);
  });

  it('shows an unparseable timestamp verbatim', async () => {
    publishUsage(usageFixture(), 'just now');
    const el = mountTab();
    await settle(el);
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes('just now'))).toBe(true);
  });

  it('omits the footer when the engine sent no timestamp', async () => {
    publishUsage(usageFixture(), '');
    const el = mountTab();
    await settle(el);
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes('Read from the engine'))).toBe(false);
  });
});
