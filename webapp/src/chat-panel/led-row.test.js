// Tests for the LED row module.
//
// `getLedState` and `formatLedTooltip` are pure
// functions and tested in isolation. `renderLedRow`
// is exercised through a mounted ChatPanel because
// it depends on `panel._tabs` and `panel._activeTabId`
// — wiring those up by hand re-implements the panel.

import { describe, expect, it } from 'vitest';

import {
  formatLedTooltip,
  getLedState,
  renderLedRow,
} from './led-row.js';
import {
  mountPanel,
  seedLabeledTab,
  settle,
} from './test-helpers.js';

// ---------------------------------------------------------------
// getLedState — pure
// ---------------------------------------------------------------

describe('getLedState', () => {
  it('streaming wins over outcome', () => {
    expect(
      getLedState({
        streaming: true,
        lastEditOutcome: { status: 'error', failureReason: 'x' },
      }),
    ).toBe('cyan');
    expect(
      getLedState({
        streaming: true,
        lastEditOutcome: { status: 'clean', appliedCount: 3 },
      }),
    ).toBe('cyan');
  });

  it('clean outcome → green', () => {
    expect(
      getLedState({
        streaming: false,
        lastEditOutcome: {
          status: 'clean',
          appliedCount: 0,
          failureReason: null,
        },
      }),
    ).toBe('green');
  });

  it('error outcome → red', () => {
    expect(
      getLedState({
        streaming: false,
        lastEditOutcome: {
          status: 'error',
          appliedCount: 1,
          failureReason: 'a.py: anchor missing',
        },
      }),
    ).toBe('red');
  });

  it('no outcome and not streaming → idle', () => {
    // Nothing has run in this tab yet. It used to read cyan, on the reasoning
    // that a tab only existed because `agentsSpawned` had created it for a
    // stream already in flight; `a0cb83b` removed that protocol, and what is
    // left hitting this branch is Main on a freshly loaded page. Every
    // finished turn writes an outcome, so "neither" is now only ever "nothing
    // yet" — which is not running.
    expect(
      getLedState({ streaming: false, lastEditOutcome: null }),
    ).toBe('idle');
    expect(
      getLedState({
        streaming: false,
        lastEditOutcome: undefined,
      }),
    ).toBe('idle');
  });
});

// ---------------------------------------------------------------
// formatLedTooltip — pure
// ---------------------------------------------------------------

describe('formatLedTooltip', () => {
  // A `mode` argument sat between the id and the state, appending `(code)` /
  // `(doc+xref)`. It came from the spawn protocol's per-agent modes, whose
  // only writer `a0cb83b` removed — so every caller passed the empty string
  // and the segment never drew. The cases that existed solely to pin the
  // parenthesised form went with the argument.
  it('cyan reads as running', () => {
    expect(
      formatLedTooltip('frontend-trivial', 'cyan', null),
    ).toBe('frontend-trivial: running');
  });

  it('idle says so rather than falling through to the failure branch', () => {
    // Everything unrecognised lands on the red wording, so an unnamed idle
    // state would report a freshly loaded page's Main tab as "failed".
    expect(formatLedTooltip('Main', 'idle', null)).toBe('Main: idle');
  });

  it('green with applied count plural', () => {
    expect(
      formatLedTooltip('agent-0', 'green', {
        status: 'clean',
        appliedCount: 3,
        failureReason: null,
      }),
    ).toBe('agent-0: completed (3 edits applied)');
  });

  it('green with applied count singular', () => {
    expect(
      formatLedTooltip('agent-0', 'green', {
        status: 'clean',
        appliedCount: 1,
        failureReason: null,
      }),
    ).toBe('agent-0: completed (1 edit applied)');
  });

  it('green with zero edits', () => {
    expect(
      formatLedTooltip('agent-0', 'green', {
        status: 'clean',
        appliedCount: 0,
        failureReason: null,
      }),
    ).toBe('agent-0: completed (0 edits applied)');
  });

  it('green tolerates missing outcome', () => {
    expect(
      formatLedTooltip('agent-0', 'green', null),
    ).toBe('agent-0: completed (0 edits applied)');
  });

  it('red surfaces failure reason', () => {
    expect(
      formatLedTooltip('agent-1', 'red', {
        status: 'error',
        appliedCount: 0,
        failureReason: 'a.py: anchor not found',
      }),
    ).toBe('agent-1: a.py: anchor not found');
  });

  it('red falls back to "failed" when reason missing', () => {
    expect(
      formatLedTooltip('agent-1', 'red', {
        status: 'error',
        appliedCount: 0,
        failureReason: null,
      }),
    ).toBe('agent-1: failed');
    expect(formatLedTooltip('agent-1', 'red', null)).toBe('agent-1: failed');
  });
});

// ---------------------------------------------------------------
// renderLedRow — DOM shape
// ---------------------------------------------------------------

describe('renderLedRow — visibility', () => {
  it('main tab only → strip carries one main dot', async () => {
    const p = mountPanel();
    await settle(p);
    const strip = p.shadowRoot.querySelector('.led-strip');
    expect(strip).not.toBeNull();
    const dots = strip.querySelectorAll('.led-dot');
    expect(dots.length).toBe(1);
    expect(dots[0].dataset.ledTabId).toBe('main');
  });

  it('one agent tab → main + agent dot', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    await settle(p);
    const strip = p.shadowRoot.querySelector('.led-strip');
    expect(strip).not.toBeNull();
    const dots = strip.querySelectorAll('.led-dot');
    expect(dots.length).toBe(2);
    expect(dots[0].dataset.ledTabId).toBe('main');
    expect(dots[1].dataset.ledTabId).toBe('a0');
  });

  it('multiple agent tabs → main + each agent in insertion order', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    seedLabeledTab(p, 'a1', 'Agent 01');
    seedLabeledTab(p, 'a2', 'Agent 02');
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots.length).toBe(4);
    expect(dots[0].dataset.ledTabId).toBe('main');
    expect(dots[1].dataset.ledTabId).toBe('a0');
    expect(dots[2].dataset.ledTabId).toBe('a1');
    expect(dots[3].dataset.ledTabId).toBe('a2');
  });

  it('main dot carries the led-main marker class', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[0].classList.contains('led-main')).toBe(true);
    expect(dots[1].classList.contains('led-main')).toBe(false);
  });
});

describe('renderLedRow — state classes', () => {
  it('streaming agent → cyan dot', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    p._tabs.get('a0').streaming = true;
    p.requestUpdate();
    await settle(p);
    // dots[0] is main, dots[1] is the agent.
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].classList.contains('led-cyan')).toBe(true);
    expect(dots[1].dataset.ledState).toBe('cyan');
  });

  it('clean outcome → green dot', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    const tab = p._tabs.get('a0');
    tab.streaming = false;
    tab.lastEditOutcome = {
      status: 'clean',
      appliedCount: 2,
      failureReason: null,
    };
    p.requestUpdate();
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].classList.contains('led-green')).toBe(true);
    expect(dots[1].dataset.ledState).toBe('green');
  });

  it('error outcome → red dot', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    const tab = p._tabs.get('a0');
    tab.streaming = false;
    tab.lastEditOutcome = {
      status: 'error',
      appliedCount: 0,
      failureReason: 'a.py: not found',
    };
    p.requestUpdate();
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].classList.contains('led-red')).toBe(true);
    expect(dots[1].dataset.ledState).toBe('red');
  });

  it('no outcome & not streaming draws idle, not a spinner', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    const tab = p._tabs.get('a0');
    tab.streaming = false;
    tab.lastEditOutcome = null;
    p.requestUpdate();
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].classList.contains('led-idle')).toBe(true);
    expect(dots[1].classList.contains('led-cyan')).toBe(false);
  });

  it('mixed states render independently', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    seedLabeledTab(p, 'a1', 'Agent 01');
    seedLabeledTab(p, 'a2', 'Agent 02');
    p._tabs.get('a0').streaming = true;
    p._tabs.get('a1').streaming = false;
    p._tabs.get('a1').lastEditOutcome = {
      status: 'clean',
      appliedCount: 1,
      failureReason: null,
    };
    p._tabs.get('a2').streaming = false;
    p._tabs.get('a2').lastEditOutcome = {
      status: 'error',
      appliedCount: 0,
      failureReason: 'oops',
    };
    p.requestUpdate();
    await settle(p);
    // dots[0] is main; agents follow at indices 1..3.
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].dataset.ledState).toBe('cyan');
    expect(dots[2].dataset.ledState).toBe('green');
    expect(dots[3].dataset.ledState).toBe('red');
  });
});

describe('renderLedRow — active marker', () => {
  it('active agent tab dot gets active class', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    seedLabeledTab(p, 'a1', 'Agent 01');
    p._activeTabId = 'a1';
    await settle(p);
    // dots[0] is main, dots[1] is a0, dots[2] is a1.
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[0].classList.contains('active')).toBe(false);
    expect(dots[1].classList.contains('active')).toBe(false);
    expect(dots[2].classList.contains('active')).toBe(true);
  });

  it('main active → main dot carries active marker', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    seedLabeledTab(p, 'a1', 'Agent 01');
    expect(p._activeTabId).toBe('main');
    await settle(p);
    const actives = p.shadowRoot.querySelectorAll('.led-dot.active');
    expect(actives.length).toBe(1);
    expect(actives[0].dataset.ledTabId).toBe('main');
  });
});

describe('renderLedRow — tooltip wiring', () => {
  it('streaming tab gets a running tooltip', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    p._tabs.get('a0').streaming = true;
    p.requestUpdate();
    await settle(p);
    // dots[0] is main, dots[1] is the agent.
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].title).toBe('a0: running');
    expect(dots[1].getAttribute('aria-label')).toBe('a0: running');
  });

  it('clean tab tooltip carries applied count', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    const tab = p._tabs.get('a0');
    tab.streaming = false;
    tab.lastEditOutcome = {
      status: 'clean',
      appliedCount: 4,
      failureReason: null,
    };
    p.requestUpdate();
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].title).toBe('a0: completed (4 edits applied)');
  });

  it('error tab tooltip carries diagnostic', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    const tab = p._tabs.get('a0');
    tab.streaming = false;
    tab.lastEditOutcome = {
      status: 'error',
      appliedCount: 0,
      failureReason: 'a.py: anchor not found',
    };
    p.requestUpdate();
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[1].title).toBe('a0: a.py: anchor not found');
  });

  it('main dot tooltip uses Main label, not the raw id', async () => {
    const p = mountPanel();
    await settle(p);
    const dot = p.shadowRoot.querySelector('.led-dot');
    expect(dot.dataset.ledTabId).toBe('main');
    // A panel that has just mounted has run nothing. This asserted
    // "Main: running" for as long as the cyan fallback stood, which is exactly
    // what a refreshed page reported over an idle engine.
    expect(dot.title).toBe('Main: idle');
  });
});

describe('renderLedRow — click activates tab', () => {
  it('clicking agent dot flips _activeTabId', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    seedLabeledTab(p, 'a1', 'Agent 01');
    expect(p._activeTabId).toBe('main');
    await settle(p);
    // dots[0] is main, dots[1] is a0, dots[2] is a1.
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    dots[2].click();
    await settle(p);
    expect(p._activeTabId).toBe('a1');
  });

  it('clicking main dot returns focus to main', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    p._activeTabId = 'a0';
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    expect(dots[0].dataset.ledTabId).toBe('main');
    dots[0].click();
    await settle(p);
    expect(p._activeTabId).toBe('main');
  });

  it('clicking already-active dot is a no-op', async () => {
    const p = mountPanel();
    seedLabeledTab(p, 'a0', 'Agent 00');
    p._activeTabId = 'a0';
    await settle(p);
    const dots = p.shadowRoot.querySelectorAll('.led-dot');
    dots[1].click();
    await settle(p);
    expect(p._activeTabId).toBe('a0');
  });
});

// ---------------------------------------------------------------
// Direct call surface — exercise renderLedRow without
// mounting the panel. Confirms the empty-row sentinel
// returns a Lit template that produces no DOM.
// ---------------------------------------------------------------

describe('renderLedRow — direct call', () => {
  it('renders a main-only strip when no agents exist', () => {
    const fakePanel = {
      _tabs: new Map([['main', { streaming: false }]]),
      _activeTabId: 'main',
    };
    const result = renderLedRow(fakePanel);
    expect(result).toBeTruthy();
    // The template carries the strip wrapper; the dot
    // for main goes in via a values slot and isn't
    // visible in `.strings`. The wrapper's class
    // attribute is present in the static text, which
    // lets us distinguish "strip rendered" from "empty
    // fragment" without standing up a full DOM.
    expect(result.strings.join('')).toContain('led-strip');
  });

  it('returns truly empty template when _tabs is empty', () => {
    const fakePanel = {
      _tabs: new Map(),
      _activeTabId: 'main',
    };
    const result = renderLedRow(fakePanel);
    expect(result).toBeTruthy();
    expect(result.strings.join('')).toBe('');
  });
});