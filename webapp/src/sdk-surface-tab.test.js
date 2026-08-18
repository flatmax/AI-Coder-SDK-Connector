// Tests for `ac-sdk-surface-tab` — the panel that shows which SDK features
// this build wired up.
//
// The behaviour worth pinning is not the markup but the panel's judgement
// about *what to show first*. The report is ~80 rows, most of them settled,
// and the dozen a reader came for are the unsettled ones. So: pending is the
// default filter, untriaged names get a banner, and the static half renders
// with no engine — that last one matters because a diagnostic tab is most
// often opened when something is broken.
//
// Harness follows context-usage-tab.test.js: a flat object installed as the
// SharedRpc proxy keyed by "Service.method", each handler returning the
// single-key envelope `{fake: value}` that matches jrpc-oo's multi-remote
// shape. The panel is wrapped in the `.tab-panel` div app-shell mounts it
// in, for the same reason that file does it — jsdom reports `offsetParent`
// as null unconditionally.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import './sdk-surface-tab.js';
import { SharedRpc } from './rpc.js';
import { totalCounts, untriagedNames } from './sdk-surface-tab.js';

const _mounted = [];

function mountTab({ active = true } = {}) {
  const el = document.createElement('ac-sdk-surface-tab');
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

async function settle(el) {
  for (let i = 0; i < 6; i += 1) {
    await Promise.resolve();
    await el.updateComplete;
  }
}

/**
 * A report shaped like `surface_report()`, small enough to reason about.
 *
 * Mirrors the real shape rather than inventing one: the backend test
 * asserts the tab and the pytest gate read the same structure, so a
 * divergence here would be a fixture that lies.
 */
function makeReport(overrides = {}) {
  return {
    sdk_available: true,
    versions: {
      sdk_version: '0.2.137',
      sdk_cli_pin: '2.1.229',
      minimum_cli_version: '2.0.0',
    },
    sections: {
      options: {
        entries: [
          { name: 'cwd', status: 'handled', note: '' },
          { name: 'agents', status: 'declined', note: 'shared with the CLI instead' },
          { name: 'sandbox', status: 'pending', note: 'would confine tool execution' },
        ],
        unclassified: [],
        stale: [],
      },
      hooks: {
        entries: [
          { name: 'PostToolUse', status: 'handled', note: '' },
          { name: 'PreCompact', status: 'pending', note: 'nothing else announces it' },
        ],
        unclassified: [],
        stale: [],
        claimed_unregistered: [],
      },
      betas: {
        entries: [{ name: 'context-1m-2025-08-07', status: 'declined', note: '1M window' }],
        unclassified: [],
        stale: [],
      },
      messages: { entries: [{ name: 'ResultMessage', status: 'handled', note: '' }], unclassified: [] },
      client: { entries: [{ name: 'query', status: 'handled', note: '' }], unclassified: [] },
    },
    counts: {},
    unclassified: {},
    cli: { available: true, commands: ['commit'], tools: ['Read'], output_styles: [] },
    ...overrides,
  };
}

function publishReport(report = makeReport()) {
  const handler = vi.fn(() => report);
  publishFakeRpc({ 'ClaudeCodeService.get_sdk_surface': handler });
  return handler;
}

/**
 * `textContent` with runs of whitespace collapsed to single spaces.
 *
 * Lit templates break sentences across source lines, so a banner that
 * reads "1 surface name has no decision" in the browser comes back from
 * `textContent` with the newlines and indentation intact. HTML collapses
 * them for the reader; this collapses them for the assertion, so tests can
 * quote the sentence as it is actually seen rather than as it is authored.
 */
function text(node) {
  return node.textContent.replace(/\s+/g, ' ').trim();
}

function rowNames(el) {
  return [...el.shadowRoot.querySelectorAll('li .name')].map((n) => n.textContent.trim());
}

/** A toolbar button by the text it shows, ignoring its ↻/← glyph. */
function toolbarButton(el, label) {
  return [...el.shadowRoot.querySelectorAll('.toolbar button')].find((b) =>
    b.textContent.includes(label),
  );
}

function clickFilter(el, label) {
  const btn = [...el.shadowRoot.querySelectorAll('.filters button')].find(
    (b) => b.textContent.trim() === label,
  );
  btn.click();
  return btn;
}

beforeEach(() => {
  try {
    localStorage.clear();
  } catch {
    // Not all jsdom configurations expose localStorage; the panel copes
    // and so must the test.
  }
});

afterEach(() => {
  for (const node of _mounted) node.remove();
  _mounted.length = 0;
  SharedRpc.set(null);
  vi.restoreAllMocks();
});

describe('ac-sdk-surface-tab', () => {
  describe('the default view is the actionable one', () => {
    it('opens on pending, not on everything', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      // 'cwd' is handled and 'agents' declined; neither is work.
      expect(rowNames(el)).toEqual(['sandbox', 'PreCompact']);
    });

    it('shows every row under All', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      clickFilter(el, 'All');
      await settle(el);
      expect(rowNames(el)).toContain('cwd');
      expect(rowNames(el)).toContain('agents');
      expect(rowNames(el)).toContain('sandbox');
    });

    it('filters to a single status', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      clickFilter(el, 'declined');
      await settle(el);
      expect(rowNames(el)).toEqual(['agents', 'context-1m-2025-08-07']);
    });

    it('remembers the filter across mounts', async () => {
      publishReport();
      const first = mountTab();
      await settle(first);
      clickFilter(first, 'All');
      await settle(first);

      const second = mountTab();
      await settle(second);
      expect(rowNames(second)).toContain('cwd');
    });

    it('says so when a filter matches nothing, rather than rendering blank', async () => {
      const report = makeReport();
      report.sections.messages.entries = [{ name: 'ResultMessage', status: 'handled', note: '' }];
      publishReport(report);
      const el = mountTab();
      await settle(el);
      // Messages has no pending rows, and the default filter is pending.
      expect(el.shadowRoot.textContent).toContain('Nothing pending here.');
    });
  });

  describe('untriaged surface is the thing it exists to shout about', () => {
    it('banners unclassified names and says how to close them', async () => {
      publishReport(
        makeReport({ unclassified: { options: ['brand_new_field'], betas: ['some-beta'] } }),
      );
      const el = mountTab();
      await settle(el);
      const banner = el.shadowRoot.querySelector('.banner');
      expect(banner).toBeTruthy();
      expect(text(banner)).toContain('2 surface names have no decision');
      expect(text(banner)).toContain('options: brand_new_field');
      expect(text(banner)).toContain('betas: some-beta');
      // The reader is told which test fails and what closing it looks like.
      expect(text(banner)).toContain('test_claude_code_sdk_surface.py');
      expect(text(banner)).toContain('NEVER_SET');
    });

    it('has no banner when everything is decided', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.querySelector('.banner')).toBeNull();
    });

    it('uses the singular for one name', async () => {
      publishReport(makeReport({ unclassified: { options: ['just_one'] } }));
      const el = mountTab();
      await settle(el);
      expect(text(el.shadowRoot.querySelector('.banner'))).toContain(
        '1 surface name has',
      );
    });

    it('flags stale entries that outlived the SDK field they explain', async () => {
      const report = makeReport();
      report.sections.options.stale = ['removed_field'];
      publishReport(report);
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.textContent).toContain('removed_field');
      expect(el.shadowRoot.textContent).toContain('Delete the entries');
    });
  });

  describe('the static half does not need an engine', () => {
    it('renders the reflected sections with no live CLI', async () => {
      publishReport(
        makeReport({ cli: { available: false, commands: [], tools: [], output_styles: [] } }),
      );
      const el = mountTab();
      await settle(el);
      expect(rowNames(el)).toContain('sandbox');
      expect(el.shadowRoot.textContent).toContain('No engine to ask');
      expect(el.shadowRoot.textContent).toContain('not connected');
    });

    it('lists what the CLI advertises when there is one', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.textContent).toContain('commit');
      expect(el.shadowRoot.textContent).toContain('Read');
      expect(el.shadowRoot.textContent).toContain('none advertised');
    });
  });

  describe('fetching', () => {
    it('shows the versions it read', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      const versions = el.shadowRoot.querySelector('.versions').textContent;
      expect(versions).toContain('0.2.137');
      expect(versions).toContain('2.1.229');
    });

    it('surfaces an error rather than an empty panel', async () => {
      publishFakeRpc({
        'ClaudeCodeService.get_sdk_surface': () => ({ error: 'transport died' }),
      });
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.querySelector('.error').textContent).toContain('transport died');
    });

    it('treats a reply with no sections as an error', async () => {
      publishFakeRpc({ 'ClaudeCodeService.get_sdk_surface': () => ({}) });
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.querySelector('.error')).toBeTruthy();
    });

    it('refetches on becoming visible when the CLI half was missing', async () => {
      const handler = publishReport(
        makeReport({ cli: { available: false, commands: [], tools: [], output_styles: [] } }),
      );
      const el = mountTab();
      await settle(el);
      const before = handler.mock.calls.length;
      el.onTabVisible();
      await settle(el);
      expect(handler.mock.calls.length).toBeGreaterThan(before);
    });

    it('does not refetch a complete report, which cannot have changed', async () => {
      const handler = publishReport();
      const el = mountTab();
      await settle(el);
      const before = handler.mock.calls.length;
      el.onTabVisible();
      await settle(el);
      expect(handler.mock.calls.length).toBe(before);
    });

    it('refreshes on the button', async () => {
      const handler = publishReport();
      const el = mountTab();
      await settle(el);
      const before = handler.mock.calls.length;
      toolbarButton(el, 'Refresh').click();
      await settle(el);
      expect(handler.mock.calls.length).toBeGreaterThan(before);
    });
  });

  describe('getting back out', () => {
    // The dialog has no rendered tab strip, so a tab without a Back button
    // is a dead end you escape with Alt+1 — which is how this was found.
    // The toolbar matches context-usage-tab.js so the control is where a
    // reader who came from that tab already expects it.

    it('asks the shell for the chat tab', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      const seen = [];
      el.addEventListener('request-dialog-tab', (e) => seen.push(e.detail?.tab));
      toolbarButton(el, 'Chat').click();
      expect(seen).toEqual(['files']);
    });

    it('escapes the shadow root so the shell can hear it', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      const seen = [];
      const onTab = (e) => seen.push(e.detail?.tab);
      document.addEventListener('request-dialog-tab', onTab);
      toolbarButton(el, 'Chat').click();
      document.removeEventListener('request-dialog-tab', onTab);
      expect(seen).toEqual(['files']);
    });

    it('offers Back even when the fetch failed', async () => {
      // The state most likely to make someone want out is the one the
      // first version of this panel had no toolbar in.
      publishFakeRpc({
        'ClaudeCodeService.get_sdk_surface': () => ({ error: 'transport died' }),
      });
      const el = mountTab();
      await settle(el);
      expect(el.shadowRoot.querySelector('.error')).toBeTruthy();
      expect(toolbarButton(el, 'Chat')).toBeTruthy();
    });

    it('offers Back before any report has arrived', async () => {
      // No RPC published at all: `onRpcReady` never fires and the panel
      // sits in its empty state.
      const el = mountTab();
      await settle(el);
      expect(toolbarButton(el, 'Chat')).toBeTruthy();
    });

    it('minimizes the dialog', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      const seen = [];
      document.addEventListener('request-dialog-minimize', () => seen.push(true));
      toolbarButton(el, '▾').click();
      expect(seen).toEqual([true]);
    });
  });

  describe('sections collapse', () => {
    it('hides rows without losing the counts', async () => {
      publishReport();
      const el = mountTab();
      await settle(el);
      const head = el.shadowRoot.querySelector('.section-head');
      head.click();
      await settle(el);
      expect(rowNames(el)).not.toContain('sandbox');
      expect(el.shadowRoot.querySelector('.section-head').textContent).toContain('pending');
    });
  });
});

describe('totalCounts', () => {
  it('counts every status across sections', () => {
    expect(totalCounts(makeReport())).toEqual({ handled: 4, declined: 2, pending: 2 });
  });

  it('is zero rather than undefined for a missing report', () => {
    expect(totalCounts(null)).toEqual({ handled: 0, declined: 0, pending: 0 });
  });
});

describe('untriagedNames', () => {
  it('qualifies each name with its section', () => {
    expect(untriagedNames({ unclassified: { options: ['a'], hooks: ['b'] } })).toEqual([
      'options: a',
      'hooks: b',
    ]);
  });

  it('is empty for a healthy report', () => {
    expect(untriagedNames(makeReport())).toEqual([]);
  });
});
