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
 * A `ContextUsageResponse` shaped like the CLI's own `/context` output:
 * live categories summing to `totalTokens`, one deferred category, and a
 * `maxTokens` already reduced by the autocompact buffer.
 */
function usageFixture(overrides = {}) {
  return {
    categories: [
      { name: 'System prompt', tokens: 3200, color: '#4a9eff' },
      { name: 'System tools', tokens: 11500, color: '#8b5cf6' },
      { name: 'MCP tools', tokens: 4800, color: '#22c55e' },
      { name: 'Messages', tokens: 42000, color: '#f59e0b' },
      {
        name: 'Deferred tools',
        tokens: 9000,
        color: '#6b7280',
        isDeferred: true,
      },
    ],
    totalTokens: 61500,
    maxTokens: 172000,
    rawMaxTokens: 200000,
    percentage: 35.8,
    model: 'claude-opus-4-6',
    isAutoCompactEnabled: true,
    ...overrides,
  };
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
      '35.8%',
    );
    expect(el.shadowRoot.querySelector('.of').textContent).toContain(
      '61,500 / 172,000 tokens',
    );
  });

  it('computes the percentage when the engine omits it', async () => {
    const usage = usageFixture();
    delete usage.percentage;
    publishUsage(usage);
    const el = mountTab();
    await settle(el);
    // 61500 / 172000 = 35.755…
    expect(el.shadowRoot.querySelector('.pct').textContent.trim()).toBe(
      '35.8%',
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

  it('colours the headline green below the amber band', async () => {
    publishUsage(usageFixture({ percentage: 40 }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').style.color).toContain('126');
  });

  it('colours the headline amber between 75 and 90 percent', async () => {
    publishUsage(usageFixture({ percentage: 80 }));
    const el = mountTab();
    await settle(el);
    const rgb = el.shadowRoot.querySelector('.pct').style.color;
    expect(rgb).toBe('rgb(210, 153, 34)');
  });

  it('colours the headline red above 90 percent', async () => {
    publishUsage(usageFixture({ percentage: 96 }));
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.pct').style.color).toBe(
      'rgb(248, 81, 73)',
    );
  });

  it('names the autocompact headroom when max is below the raw window', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const note = el.shadowRoot.querySelector('.note');
    expect(note.textContent).toContain('28,000 tokens');
    expect(note.textContent).toContain('200,000-token window');
  });

  it('omits the headroom note when the window is not reduced', async () => {
    publishUsage(usageFixture({ rawMaxTokens: 172000 }));
    const el = mountTab();
    await settle(el);
    const notes = [...el.shadowRoot.querySelectorAll('.note')].map(
      (n) => n.textContent,
    );
    expect(notes.some((t) => t.includes('headroom'))).toBe(false);
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
    // 42000 / 172000 = 24.4%
    expect(segs[3].style.width.startsWith('24.4')).toBe(true);
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

  it('uses the engine colour verbatim so it matches the terminal', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const seg = el.shadowRoot.querySelector('.bar-seg');
    expect(seg.style.background).toBe('rgb(74, 158, 255)');
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
    expect(names[0]).toContain('Messages');
    expect(names[1]).toContain('System tools');
    expect(names[2]).toContain('Deferred tools');
    expect(names[3]).toContain('MCP tools');
    expect(names[4]).toContain('System prompt');
  });

  it('shows each category share of the total', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const messages = rows(el, 'Categories')[0];
    expect(messages[1]).toBe('42.0K');
    // 42000 / 61500 = 68.3%
    expect(messages[2]).toBe('68.3%');
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

  it('shows an em dash for a share the total cannot support', async () => {
    publishUsage(
      usageFixture({
        totalTokens: 0,
        categories: [{ name: 'Messages', tokens: 10, color: '#fff' }],
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

  it('renders a swatch coloured by the engine', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
    const swatch = sectionFor(el, 'Categories').querySelector('.swatch');
    expect(swatch.style.background).toBe('rgb(245, 158, 11)');
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
// Memory files, MCP tools, agents
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
    expect(rows(el, 'Memory files')).toEqual([
      ['/repo/CLAUDE.md', 'Project', '1.8K'],
      ['/home/u/.claude/CLAUDE.md', 'User', '320'],
    ]);
  });

  it('omits the section when no memory files are loaded', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
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

  it('totals only the tools whose schemas are loaded', async () => {
    publishUsage(usageFixture({ mcpTools: tools }));
    const el = mountTab();
    await settle(el);
    // 900 + 700, excluding the 600 that is not in the window.
    expect(sectionFor(el, 'MCP tools').querySelector('h3').textContent).toContain(
      '1.6K loaded',
    );
  });

  it('lists every tool, loaded or not', async () => {
    publishUsage(usageFixture({ mcpTools: tools }));
    const el = mountTab();
    await settle(el);
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
    expect(sectionFor(el, 'MCP tools')).toBeUndefined();
  });
});

describe('ContextUsageTab agent definitions', () => {
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
    expect(rows(el, 'Agent definitions')).toEqual([
      ['Explore', 'built-in', '450'],
      ['reviewer', 'project', '300'],
    ]);
  });

  it('omits the section when no agents are loaded', async () => {
    publishUsage(usageFixture());
    const el = mountTab();
    await settle(el);
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
