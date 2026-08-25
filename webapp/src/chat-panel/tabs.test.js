// Tests for tab strip rendering, click, overflow menu, close
// button, close→RPC wiring, agent_tag routing, streaming
// indicator (D1), Alt+` cycling (D2), and per-tab URL chip
// snapshots.

import { describe, expect, it, vi } from 'vitest';

import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  seedLabeledTab,
  seedTab,
  settle,
} from './test-helpers.js';

// ---------------------------------------------------------------------------
// Tab strip rendering (D21 Phase B1)
// ---------------------------------------------------------------------------

describe('ChatPanel tab strip rendering', () => {
  it('renders even when only the main tab exists', async () => {
    // The strip is always rendered because the per-tab
    // 📊 Context icon is the only path to the Context
    // overlay — see specs4/5-webapp/shell.md § Tab Bar
    // Layout. Single-tab mode shows just the Main tab.
    const p = mountPanel();
    await settle(p);
    const strip = p.shadowRoot.querySelector('.tab-strip');
    expect(strip).toBeTruthy();
    const buttons = p.shadowRoot.querySelectorAll(
      '.tab-strip-tab',
    );
    expect(buttons.length).toBe(1);
    expect(buttons[0].getAttribute('data-tab-id')).toBe('main');
  });

  it('grows as agent tabs are added', async () => {
    const p = mountPanel();
    await settle(p);
    expect(
      p.shadowRoot.querySelectorAll('.tab-strip-tab').length,
    ).toBe(1);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const strip = p.shadowRoot.querySelector('.tab-strip');
    expect(strip).toBeTruthy();
    expect(
      p.shadowRoot.querySelectorAll('.tab-strip-tab').length,
    ).toBe(2);
  });

  it('renders one button per tab in insertion order', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    seedLabeledTab(p, 'agent-1', 'Agent 1');
    seedLabeledTab(p, 'agent-2', 'Agent 2');
    p.requestUpdate();
    await settle(p);
    const buttons = p.shadowRoot.querySelectorAll(
      '.tab-strip-tab',
    );
    expect(buttons.length).toBe(4);
    expect(buttons[0].getAttribute('data-tab-id')).toBe('main');
    expect(buttons[1].getAttribute('data-tab-id')).toBe('agent-0');
    expect(buttons[2].getAttribute('data-tab-id')).toBe('agent-1');
    expect(buttons[3].getAttribute('data-tab-id')).toBe('agent-2');
  });

  it('renders the main tab label as "Main"', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    // Tabs render the 📊 (Context) icon BEFORE the
    // label and the ✕ (close) icon AFTER it (agent
    // tabs only). Strip both edges to recover the
    // visible label text. The `u` flag is required
    // because 📊 is outside the BMP — without it,
    // the character class treats the emoji as two
    // surrogate code units and leaves a stray
    // \uD83D behind.
    const labelText = mainBtn.textContent
      .replace(/^\s*[📊✕]\s*/gu, '')
      .replace(/\s*[📊✕]\s*$/gu, '')
      .trim();
    expect(labelText).toBe('Main');
  });

  it('renders custom labels for agent tabs', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0: refactor auth');
    p.requestUpdate();
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    // Tabs render the 📊 (Context) icon BEFORE the
    // label and the ✕ (close) icon AFTER it on agent
    // tabs. Strip both edges to isolate the visible
    // label text. The `u` flag is required so 📊
    // (outside the BMP) is treated as one codepoint
    // inside the character class.
    const labelText = agentBtn.textContent
      .replace(/^\s*[📊✕]\s*/gu, '')
      .replace(/\s*[📊✕]\s*$/gu, '')
      .trim();
    expect(labelText).toBe('Agent 0: refactor auth');
  });

  it('falls back to tab ID when label is missing', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('orphan-tab', p._makeTabState());
    p.requestUpdate();
    await settle(p);
    const orphanBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="orphan-tab"]',
    );
    // Strip leading 📊 and trailing ✕ icons before
    // comparing — see "renders custom labels" for
    // rationale (the `u` flag is required for 📊 to
    // match as one codepoint).
    const labelText = orphanBtn.textContent
      .replace(/^\s*[📊✕]\s*/gu, '')
      .replace(/\s*[📊✕]\s*$/gu, '')
      .trim();
    expect(labelText).toBe('orphan-tab');
  });

  it('active tab gets the .active class', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(mainBtn.classList.contains('active')).toBe(true);
    expect(agentBtn.classList.contains('active')).toBe(false);
  });

  it('active class follows _activeTabId changes', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p._activeTabId = 'agent-0';
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(mainBtn.classList.contains('active')).toBe(false);
    expect(agentBtn.classList.contains('active')).toBe(true);
  });

  it('active button carries aria-selected="true"', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(mainBtn.getAttribute('aria-selected')).toBe('true');
    expect(agentBtn.getAttribute('aria-selected')).toBe('false');
  });

  it('button title attribute mirrors the label', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0: refactor auth module');
    p.requestUpdate();
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(agentBtn.getAttribute('title')).toBe(
      'Agent 0: refactor auth module',
    );
  });

  it('tablist role on the inner scroll container', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const scroll = p.shadowRoot.querySelector('.tab-strip-scroll');
    expect(scroll.getAttribute('role')).toBe('tablist');
  });

  it('buttons carry role="tab"', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const buttons = p.shadowRoot.querySelectorAll(
      '.tab-strip-tab',
    );
    for (const btn of buttons) {
      expect(btn.getAttribute('role')).toBe('tab');
    }
  });

  it('tab strip carries data-drag-handle="true"', async () => {
    // The dialog's drag-detection logic walks the
    // composedPath() looking for an element with
    // this attribute — without it, pointerdowns on
    // the strip's empty background fall through and
    // the dialog can't be dragged. Spec contract:
    // specs4/5-webapp/shell.md § Layout / Drag
    // detection.
    const p = mountPanel();
    await settle(p);
    const strip = p.shadowRoot.querySelector('.tab-strip');
    expect(strip).toBeTruthy();
    expect(strip.getAttribute('data-drag-handle')).toBe('true');
  });
});

describe('ChatPanel tab strip interaction', () => {
  it('clicking a tab flips _activeTabId', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    expect(p._activeTabId).toBe('main');
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    agentBtn.click();
    await settle(p);
    expect(p._activeTabId).toBe('agent-0');
  });

  it('click dispatches active-tab-changed event', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('active-tab-changed', listener);
    try {
      const agentBtn = p.shadowRoot.querySelector(
        '.tab-strip-tab[data-tab-id="agent-0"]',
      );
      agentBtn.click();
      await settle(p);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        tabId: 'agent-0',
        previousTabId: 'main',
      });
    } finally {
      p.removeEventListener('active-tab-changed', listener);
    }
  });

  it('clicking the already-active tab is a no-op', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('active-tab-changed', listener);
    try {
      const mainBtn = p.shadowRoot.querySelector(
        '.tab-strip-tab[data-tab-id="main"]',
      );
      mainBtn.click();
      mainBtn.click();
      await settle(p);
      expect(listener).not.toHaveBeenCalled();
    } finally {
      p.removeEventListener('active-tab-changed', listener);
    }
  });

  it('clicking updates the active class on the strip', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    agentBtn.click();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    const agentBtn2 = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(mainBtn.classList.contains('active')).toBe(false);
    expect(agentBtn2.classList.contains('active')).toBe(true);
  });

  it('switching tabs swaps the visible message list', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p._tabs.get('main').messages = [
      { role: 'user', content: 'main tab message' },
    ];
    p._tabs.get('agent-0').messages = [
      { role: 'user', content: 'agent tab message' },
    ];
    p.requestUpdate();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.messages').textContent,
    ).toContain('main tab message');
    expect(
      p.shadowRoot.querySelector('.messages').textContent,
    ).not.toContain('agent tab message');
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    agentBtn.click();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.messages').textContent,
    ).toContain('agent tab message');
    expect(
      p.shadowRoot.querySelector('.messages').textContent,
    ).not.toContain('main tab message');
  });
});

// ---------------------------------------------------------------------------
// Tab strip overflow (D21 Phase B2)
// ---------------------------------------------------------------------------

describe('ChatPanel tab strip overflow — structure', () => {
  it('has a scroll container inside the strip', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const scroll = p.shadowRoot.querySelector('.tab-strip-scroll');
    expect(scroll).toBeTruthy();
    const tabsInScroll = scroll.querySelectorAll(
      '.tab-strip-tab',
    );
    expect(tabsInScroll.length).toBeGreaterThan(0);
  });

  it('overflow button is visible when strip is visible', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.tab-strip-overflow');
    expect(btn).toBeTruthy();
  });

  it('overflow button is present in single-tab mode', async () => {
    // Strip is always rendered, so the overflow button
    // is too. Clicking it in single-tab mode opens a
    // menu containing just the Main entry — harmless
    // and consistent with the multi-tab behaviour.
    const p = mountPanel();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.tab-strip-overflow'),
    ).toBeTruthy();
  });

  it('overflow button carries aria attributes', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.tab-strip-overflow');
    expect(btn.getAttribute('aria-haspopup')).toBe('menu');
    expect(btn.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('ChatPanel tab strip overflow — open/close', () => {
  it('menu is closed by default', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(false);
    expect(
      p.shadowRoot.querySelector('.tab-strip-overflow-menu'),
    ).toBeNull();
  });

  it('clicking the overflow button opens the menu', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.tab-strip-overflow');
    btn.click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(true);
    expect(
      p.shadowRoot.querySelector('.tab-strip-overflow-menu'),
    ).toBeTruthy();
  });

  it('aria-expanded reflects open state', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.tab-strip-overflow');
    btn.click();
    await settle(p);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
  });

  it('clicking the button again closes the menu', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.tab-strip-overflow');
    btn.click();
    await settle(p);
    btn.click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(false);
    expect(
      p.shadowRoot.querySelector('.tab-strip-overflow-menu'),
    ).toBeNull();
  });

  it('outside click dismisses the menu', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(true);
    document.body.click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(false);
  });

  it('click inside the menu does not dismiss', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const menu = p.shadowRoot.querySelector(
      '.tab-strip-overflow-menu',
    );
    menu.click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(true);
  });

  it('Escape dismisses the menu', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(true);
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(false);
  });
});

describe('ChatPanel tab strip overflow — menu items', () => {
  it('renders one item per tab in insertion order', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    seedLabeledTab(p, 'agent-1', 'Agent 1');
    seedLabeledTab(p, 'agent-2', 'Agent 2');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const items = p.shadowRoot.querySelectorAll(
      '.tab-strip-overflow-item',
    );
    expect(items.length).toBe(4);
    expect(items[0].getAttribute('data-tab-id')).toBe('main');
    expect(items[1].getAttribute('data-tab-id')).toBe('agent-0');
    expect(items[2].getAttribute('data-tab-id')).toBe('agent-1');
    expect(items[3].getAttribute('data-tab-id')).toBe('agent-2');
  });

  it('item labels match the strip button labels', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0: refactor auth');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const item = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-0"]',
    );
    expect(item.textContent.trim()).toBe(
      'Agent 0: refactor auth',
    );
  });

  it('main item renders as "Main"', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const mainItem = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="main"]',
    );
    expect(mainItem.textContent.trim()).toBe('Main');
  });

  it('active item gets the .active class', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p._activeTabId = 'agent-0';
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const mainItem = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="main"]',
    );
    const agentItem = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-0"]',
    );
    expect(mainItem.classList.contains('active')).toBe(false);
    expect(agentItem.classList.contains('active')).toBe(true);
  });

  it('items carry role="menuitem"', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const items = p.shadowRoot.querySelectorAll(
      '.tab-strip-overflow-item',
    );
    for (const item of items) {
      expect(item.getAttribute('role')).toBe('menuitem');
    }
  });
});

describe('ChatPanel tab strip overflow — jump', () => {
  it('clicking an item flips _activeTabId', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    seedLabeledTab(p, 'agent-1', 'Agent 1');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const item = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-1"]',
    );
    item.click();
    await settle(p);
    expect(p._activeTabId).toBe('agent-1');
  });

  it('clicking an item closes the menu', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const item = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-0"]',
    );
    item.click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(false);
  });

  it('clicking dispatches active-tab-changed', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('active-tab-changed', listener);
    try {
      p.shadowRoot
        .querySelector(
          '.tab-strip-overflow-item[data-tab-id="agent-0"]',
        )
        .click();
      await settle(p);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        tabId: 'agent-0',
        previousTabId: 'main',
      });
    } finally {
      p.removeEventListener('active-tab-changed', listener);
    }
  });

  it('clicking the already-active item is a no-op', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('active-tab-changed', listener);
    try {
      p.shadowRoot
        .querySelector(
          '.tab-strip-overflow-item[data-tab-id="main"]',
        )
        .click();
      await settle(p);
      expect(listener).not.toHaveBeenCalled();
      expect(p._tabStripOverflowOpen).toBe(false);
    } finally {
      p.removeEventListener('active-tab-changed', listener);
    }
  });
});

describe('ChatPanel tab strip overflow — cleanup', () => {
  it('disconnect releases document listeners', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    expect(p._tabStripOverflowOpen).toBe(true);
    p.remove();
    expect(() => document.body.click()).not.toThrow();
    expect(() =>
      document.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'Escape',
          bubbles: true,
        }),
      ),
    ).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Tab close button (D21 Phase B3)
// ---------------------------------------------------------------------------

describe('ChatPanel tab close — rendering', () => {
  // No per-tab ✕ affordance is rendered, and now there is nothing behind one
  // either. Three groups stood below this: "behavior" and "guards" drove
  // `_onTabClose` directly, and "close-tab backend wiring" asserted it fired
  // `close_agent_context` to free the agent's server-side scope. All three
  // went with the writable agent tabs in `a0cb83b` — `tabs.js` records that
  // no UI gesture ever bound to the primitive, and that both remaining kinds
  // of tab sweep themselves: browsed transcripts on a session change, a live
  // subagent's tab with the turn that spawned it. Neither holds a backend
  // scope to release. What is still worth pinning is that the button is
  // absent, which is what this group does
  // (specs5/5-webapp/subagent-browser.md § Tab Lifetime).
  it('main tab does not render a close button', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(mainBtn.querySelector('.tab-close')).toBeNull();
  });

  it('agent tabs do not render a close button', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(agentBtn.querySelector('.tab-close')).toBeNull();
  });

  it('no close button anywhere in the tab strip', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    seedLabeledTab(p, 'agent-1', 'Agent 1');
    p.requestUpdate();
    await settle(p);
    const closes = p.shadowRoot.querySelectorAll('.tab-close');
    expect(closes.length).toBe(0);
  });

  it('overflow menu items have no close affordance', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-overflow')
      .click();
    await settle(p);
    const item = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-0"]',
    );
    expect(item.querySelector('.tab-close')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tooltip enrichment
// ---------------------------------------------------------------------------
//
// "Mode storage" stood alongside this, asserting that an `agentsSpawned`
// payload's `mode` field landed in `panel._tabModes`. That broadcast has no
// consumer since `a0cb83b`, so nothing writes the map any more — it is still
// read, by the tab chips and the LED tooltip, and so is permanently empty.
// The tests went rather than being rewritten because there is no longer a
// path that fills it; see the note in the open-work list about retiring the
// map itself.

describe('ChatPanel tab tooltip enrichment', () => {
  it('main tab tooltip is just the label', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 0');
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(mainBtn.getAttribute('title')).toBe('Main');
  });

  it('agent tab without mode shows bare label', async () => {
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 00: refactor');
    p.requestUpdate();
    await settle(p);
    const btn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-0"]',
    );
    expect(btn.getAttribute('title')).toBe(
      'Agent 00: refactor',
    );
  });

  it('overflow menu items carry the label as their tooltip', async () => {
    // Four tests stood here pinning a `(code)` / `(doc+xref)` mode segment
    // on tab and overflow tooltips, seeded through a helper that wrote
    // `panel._tabModes` by hand. Production stopped filling that map when
    // `a0cb83b` removed the spawn protocol, so the segment could not appear
    // in a real session and the tests were describing a shape no session
    // could produce. The map is gone; what survives is the tooltip itself,
    // and the overflow menu is the path the others did not cover.
    const p = mountPanel();
    await settle(p);
    seedLabeledTab(p, 'agent-0', 'Agent 00: refactor');
    p.requestUpdate();
    await settle(p);
    p.shadowRoot.querySelector('.tab-strip-overflow').click();
    await settle(p);
    const item = p.shadowRoot.querySelector(
      '.tab-strip-overflow-item[data-tab-id="agent-0"]',
    );
    expect(item.getAttribute('title')).toBe('Agent 00: refactor');
  });
});

// ---------------------------------------------------------------------------
// The send path, per tab
// ---------------------------------------------------------------------------
//
// This section used to be "C2b — per-tab text routing via agent_tag": every
// test asserted the tab's id landed in `chat_streaming`'s 6th argument, so a
// send from an agent tab reached that tab's own `ContextManager`.
//
// There is no agent_tag any more, and nothing for it to route to. One CLI
// session holds one turn; a `Task` call's subagent output is attributed by its
// `tool_use_id` (blocks.js), not by opening a second stream. `chat_streaming`
// takes five arguments — `(request_id, message, files, images, viewer)`. The
// two reasoning arguments went with it (the CLI decides when to think), and
// phase 3 deleted the state and handlers behind them.
//
// What survives is the part that was never about the tag: a send reads the
// *active* tab. Its file selection goes out, its state records the turn. That
// property still holds with several tabs open, and it is what these tests pin
// alongside the arity itself — a positional shape is easy to break quietly,
// since a stale extra argument is simply ignored by the RPC layer.

describe('ChatPanel send path', () => {
  async function setupWithAgentTab() {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'frontend-trivial';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'frontend-trivial');
    return { panel: p, started, agentTabId };
  }

  it('sends the four arguments chat_streaming takes', async () => {
    // A third `files` argument — the picker's checkbox list, taken from
    // the sending tab — sat between the message and the images until
    // CC-21. Arity is pinned because every argument here is positional:
    // a dropped one silently shifts `images` into `viewer`.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hello from main';
    await p._send();
    await settle(p);
    expect(started).toHaveBeenCalledOnce();
    const args = started.mock.calls[0];
    expect(args).toHaveLength(4);
    expect(args[0]).toBe(p._tabs.get('main').currentRequestId);
    expect(args[1]).toBe('hello from main');
    expect(args[2]).toEqual([]);
    // viewer framing — explicitly null rather than omitted, so the arity
    // stays unambiguous for the phase that fills it in.
    expect(args[3]).toBeNull();
  });

  // A test named "the selection list comes from the active tab" lived
  // here. It seeded a different `selectedFiles` on main and on an agent
  // tab, sent from the agent tab, and asserted the agent's list went
  // out. There is no per-tab file list under CC-21; the neighbouring
  // "the turn is recorded on the sending tab only" still covers the
  // part that mattered — that a send reads the *active* tab's state.

  it('the turn is recorded on the sending tab only', async () => {
    const { panel, started, agentTabId } =
      await setupWithAgentTab();
    panel._activeTabId = agentTabId;
    await settle(panel);
    panel._input = 'hello from agent';
    await panel._send();
    await settle(panel);
    const agent = panel._tabs.get(agentTabId);
    expect(agent.currentRequestId).toBe(started.mock.calls[0][0]);
    expect(agent.streaming).toBe(true);
    const main = panel._tabs.get('main');
    expect(main.currentRequestId).toBeNull();
    expect(main.streaming).toBe(false);
  });

  it('each tab sends under its own request id', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._tabs.set('frontend-trivial', p._makeTabState());
    p._tabs.set('backend-auth', p._makeTabState());
    p._tabLabels.set('frontend-trivial', 'frontend-trivial');
    p._tabLabels.set('backend-auth', 'backend-auth');
    p._activeTabId = 'frontend-trivial';
    await settle(p);
    p._input = 'from frontend';
    await p._send();
    await settle(p);
    p._tabs.get('frontend-trivial').streaming = false;
    p._tabs.get('frontend-trivial').currentRequestId = null;
    p._activeTabId = 'backend-auth';
    await settle(p);
    p._input = 'from backend';
    await p._send();
    await settle(p);
    expect(started).toHaveBeenCalledTimes(2);
    expect(started.mock.calls[0][1]).toBe('from frontend');
    expect(started.mock.calls[1][1]).toBe('from backend');
    expect(started.mock.calls[1][0]).not.toBe(started.mock.calls[0][0]);
    expect(p._tabs.get('backend-auth').currentRequestId).toBe(
      started.mock.calls[1][0],
    );
  });

  it('switching back to main sends from main', async () => {
    // Used to assert on the per-tab selection list that went out as the
    // third argument (CC-21). What it really guards is that the send
    // path re-reads the active tab after a switch back rather than
    // holding a reference to the tab it last sent from.
    const { panel, started, agentTabId } =
      await setupWithAgentTab();
    panel._activeTabId = agentTabId;
    await settle(panel);
    panel._input = 'from agent';
    await panel._send();
    await settle(panel);
    panel._tabs.get(agentTabId).streaming = false;
    panel._tabs.get(agentTabId).currentRequestId = null;
    panel._activeTabId = 'main';
    await settle(panel);
    panel._input = 'from main';
    await panel._send();
    await settle(panel);
    expect(started).toHaveBeenCalledTimes(2);
    expect(started.mock.calls[1][1]).toBe('from main');
    expect(panel._tabs.get('main').currentRequestId).toBe(
      started.mock.calls[1][0],
    );
    // The agent tab kept the request id it sent under.
    expect(started.mock.calls[0][1]).toBe('from agent');
  });
});


// ---------------------------------------------------------------------------
// D1 — Streaming indicator on tab labels
// ---------------------------------------------------------------------------

describe('ChatPanel D1 streaming indicator', () => {
  it('does not render indicator on idle tabs', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p.requestUpdate();
    await settle(p);
    const indicators = p.shadowRoot.querySelectorAll(
      '.tab-streaming-indicator',
    );
    expect(indicators).toHaveLength(0);
  });

  it('renders indicator on streaming tab', async () => {
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._tabs.get(agentTabId).streaming = true;
    p.requestUpdate();
    await settle(p);
    const tabBtn = p.shadowRoot.querySelector(
      `.tab-strip-tab[data-tab-id="${agentTabId}"]`,
    );
    expect(
      tabBtn.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(
      mainBtn.querySelector('.tab-streaming-indicator'),
    ).toBeNull();
  });

  it('renders indicator on active tab too', async () => {
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._tabs.get(agentTabId).streaming = true;
    p._activeTabId = agentTabId;
    p.requestUpdate();
    await settle(p);
    const tabBtn = p.shadowRoot.querySelector(
      `.tab-strip-tab[data-tab-id="${agentTabId}"]`,
    );
    expect(tabBtn.classList.contains('active')).toBe(true);
    expect(
      tabBtn.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
  });

  it('indicator on main tab when main is streaming', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabs.get('main').streaming = true;
    p.requestUpdate();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(
      mainBtn.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
  });

  it('multiple tabs can show indicators simultaneously', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p._tabs.get('turn_abc/agent-00').streaming = true;
    p._tabs.get('turn_abc/agent-01').streaming = true;
    p.requestUpdate();
    await settle(p);
    const indicators = p.shadowRoot.querySelectorAll(
      '.tab-streaming-indicator',
    );
    expect(indicators).toHaveLength(2);
  });

  it('aria-busy reflects streaming state', async () => {
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._tabs.get(agentTabId).streaming = true;
    p.requestUpdate();
    await settle(p);
    const tabBtn = p.shadowRoot.querySelector(
      `.tab-strip-tab[data-tab-id="${agentTabId}"]`,
    );
    expect(tabBtn.getAttribute('aria-busy')).toBe('true');
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(mainBtn.getAttribute('aria-busy')).toBe('false');
  });

  it('indicator appears when send starts on active tab', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    expect(
      p.shadowRoot.querySelectorAll('.tab-streaming-indicator'),
    ).toHaveLength(0);
    p._input = 'hi';
    await p._send();
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(
      mainBtn.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
  });

  it('indicator disappears when stream completes on active tab', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done' },
    });
    await settle(p);
    const mainBtn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="main"]',
    );
    expect(
      mainBtn.querySelector('.tab-streaming-indicator'),
    ).toBeNull();
  });

  it('indicator persists after switching away from streaming tab', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._activeTabId = agentTabId;
    await settle(p);
    p._input = 'work';
    await p._send();
    await settle(p);
    p._activeTabId = 'main';
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      `.tab-strip-tab[data-tab-id="${agentTabId}"]`,
    );
    expect(
      agentBtn.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
  });

  it('indicator disappears when stream completes on inactive tab', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._activeTabId = agentTabId;
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    const reqId = started.mock.calls[0][0];
    p._activeTabId = 'main';
    await settle(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done' },
    });
    await settle(p);
    const agentBtn = p.shadowRoot.querySelector(
      `.tab-strip-tab[data-tab-id="${agentTabId}"]`,
    );
    expect(
      agentBtn.querySelector('.tab-streaming-indicator'),
    ).toBeNull();
  });

  it('indicator renders in overflow menu too', async () => {
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._tabs.get(agentTabId).streaming = true;
    p.requestUpdate();
    await settle(p);
    p.shadowRoot.querySelector('.tab-strip-overflow').click();
    await settle(p);
    const menuItem = p.shadowRoot.querySelector(
      `.tab-strip-overflow-item[data-tab-id="${agentTabId}"]`,
    );
    expect(
      menuItem.querySelector('.tab-streaming-indicator'),
    ).toBeTruthy();
    expect(menuItem.getAttribute('aria-busy')).toBe('true');
  });

  it('indicator is marked aria-hidden', async () => {
    const p = mountPanel();
    await settle(p);
    const agentTabId = 'turn_abc/agent-00';
    p._tabs.set(agentTabId, p._makeTabState());
    p._tabLabels.set(agentTabId, 'Agent 00');
    p._tabs.get(agentTabId).streaming = true;
    p.requestUpdate();
    await settle(p);
    const indicator = p.shadowRoot.querySelector(
      '.tab-streaming-indicator',
    );
    expect(indicator.getAttribute('aria-hidden')).toBe('true');
  });
});

// ---------------------------------------------------------------------------
// D2 — chat-tab keyboard cycling (Alt+` / Alt+Shift+`)
// ---------------------------------------------------------------------------

describe('ChatPanel D2 tab cycling shortcuts', () => {
  function pressAltBacktick(shift = false) {
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: '`',
        altKey: true,
        shiftKey: shift,
        bubbles: true,
        cancelable: true,
      }),
    );
  }

  it('single-tab mode does not intercept the key', async () => {
    const p = mountPanel();
    await settle(p);
    expect(p._activeTabId).toBe('main');
    pressAltBacktick();
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('single-tab mode does not preventDefault', async () => {
    const p = mountPanel();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '`',
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    const spy = vi.spyOn(ev, 'preventDefault');
    document.dispatchEvent(ev);
    await settle(p);
    expect(spy).not.toHaveBeenCalled();
  });

  it('Alt+` cycles to the next tab', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p.requestUpdate();
    await settle(p);
    expect(p._activeTabId).toBe('main');
    pressAltBacktick();
    await settle(p);
    expect(p._activeTabId).toBe('turn_abc/agent-00');
    pressAltBacktick();
    await settle(p);
    expect(p._activeTabId).toBe('turn_abc/agent-01');
  });

  it('Alt+Shift+` cycles to the previous tab', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p._activeTabId = 'turn_abc/agent-01';
    await settle(p);
    pressAltBacktick(true);
    await settle(p);
    expect(p._activeTabId).toBe('turn_abc/agent-00');
    pressAltBacktick(true);
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('Alt+` wraps from last to first', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._activeTabId = 'turn_abc/agent-00';
    await settle(p);
    pressAltBacktick();
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('Alt+Shift+` wraps from first to last', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p.requestUpdate();
    await settle(p);
    pressAltBacktick(true);
    await settle(p);
    expect(p._activeTabId).toBe('turn_abc/agent-01');
  });

  it('preventDefault fires on handled keys', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '`',
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    const spy = vi.spyOn(ev, 'preventDefault');
    document.dispatchEvent(ev);
    await settle(p);
    expect(spy).toHaveBeenCalled();
  });

  it('Ctrl+Alt+` is ignored (WM conflict)', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '`',
      altKey: true,
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(ev);
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('Meta+Alt+` is ignored (macOS conflict)', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '`',
      altKey: true,
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(ev);
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('plain backtick (no Alt) does not cycle', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '`',
      altKey: false,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(ev);
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('Alt+1 does not trigger the shortcut', async () => {
    // Alt+1..4 belong to app-shell's dialog-tab
    // shortcuts. Pinned to prevent accidental conflicts.
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const ev = new KeyboardEvent('keydown', {
      key: '1',
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    const spy = vi.spyOn(ev, 'preventDefault');
    document.dispatchEvent(ev);
    await settle(p);
    expect(p._activeTabId).toBe('main');
    expect(spy).not.toHaveBeenCalled();
  });

  it('dispatches active-tab-changed on cycle', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('active-tab-changed', listener);
    try {
      pressAltBacktick();
      await settle(p);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        tabId: 'turn_abc/agent-00',
        previousTabId: 'main',
      });
    } finally {
      p.removeEventListener('active-tab-changed', listener);
    }
  });

  it('disconnect removes the document listener', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p.requestUpdate();
    await settle(p);
    const activeTabIdBefore = p._activeTabId;
    p.remove();
    pressAltBacktick();
    expect(p._activeTabId).toBe(activeTabIdBefore);
  });

  it('three-tab cycle: main → agent-00 → agent-01 → main', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('turn_abc/agent-00', p._makeTabState());
    p._tabs.set('turn_abc/agent-01', p._makeTabState());
    p._tabLabels.set('turn_abc/agent-00', 'Agent 00');
    p._tabLabels.set('turn_abc/agent-01', 'Agent 01');
    p.requestUpdate();
    await settle(p);
    const sequence = [
      'turn_abc/agent-00',
      'turn_abc/agent-01',
      'main',
      'turn_abc/agent-00',
    ];
    for (const expected of sequence) {
      pressAltBacktick();
      await settle(p);
      expect(p._activeTabId).toBe(expected);
    }
  });
});

// ---------------------------------------------------------------------------
// Per-tab block state
// ---------------------------------------------------------------------------
//
// This section used to cover the URL chips' per-tab snapshot/restore. The
// chips are gone — they fetched URLs into the native engine's context and the
// CLI has WebFetch — but the property they were pinning is not: switching tabs
// must not carry one tab's live turn state onto another's.
//
// `turnBlocks` is what needs that guarantee now. It differs from the chips in
// one way that matters: it is mutated in place rather than snapshotted, so the
// tab state IS the live object and there is no copy step to get wrong. What
// there is to get wrong is sharing — two tabs holding one object would render
// each other's tool cards.

describe('ChatPanel per-tab block state', () => {
  it('each tab gets its own block state', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent-0');
    const main = p._tabs.get('main').turnBlocks;
    const agent = p._tabs.get('agent-0').turnBlocks;
    expect(main).not.toBe(agent);
    expect(main.index).not.toBe(agent.index);
    expect(main.subagents).not.toBe(agent.subagents);
  });

  it('a block on one tab does not appear on another', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent-0');
    const main = p._tabs.get('main').turnBlocks;
    main.blocks.push({ blockId: 'r1:b0', kind: 'text', content: 'main only' });
    main.index.set('r1:b0', main.blocks[0]);
    p._activeTabId = 'agent-0';
    await settle(p);
    expect(p._tabs.get('agent-0').turnBlocks.blocks).toEqual([]);
    // And the accessor follows the active tab rather than the last write.
    expect(p._turnBlocks.blocks).toEqual([]);
  });

  it('the accessor reads through to the active tab', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent-0');
    expect(p._turnBlocks).toBe(p._tabs.get('main').turnBlocks);
    p._activeTabId = 'agent-0';
    await settle(p);
    expect(p._turnBlocks).toBe(p._tabs.get('agent-0').turnBlocks);
  });

  it('switching tabs pre-first-render does not throw', async () => {
    // Defensive — same guarantee the chips section pinned, for the same
    // reason: a tab switch can land before the panel has rendered once.
    publishFakeRpc({});
    const p = mountPanel();
    p._tabs.set('agent-0', p._makeTabState());
    expect(() => {
      p._activeTabId = 'agent-0';
    }).not.toThrow();
  });
});
