// The engine chip and the unfinished-engine notice.
//
// Governing spec: specs5/5-webapp/chat.md § Engine Indicator and Notice.
//
// The bug these exist for: on 2026-09-03 a user reported the UI as
// "unusable" — no history browser, no always-allow, no slash commands —
// and every one of those was the capability descriptor hiding a surface
// the master engine could not feed, exactly as AG-9 specifies. The
// application was working. The tests below are about the part that was
// missing: nothing said which engine was running, so a correctly hidden
// surface and a broken build looked identical.
//
// Two rules carry the whole design, and both are asserted here rather
// than described:
//
//   1. The notice keys off `unbuilt`, never off an engine name. An
//      `absent` surface is a real difference between engines and the UI
//      is complete without it; an `unbuilt` one is a feature this project
//      built and has not wired up. Only the second is worth interrupting
//      over, and on the shipped engine the count is zero — which is the
//      test that it is keyed to the right fact.
//   2. The chip appears only when more than one engine is mountable.
//      With one engine there is no question to answer.

import { html, render } from 'lit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resetCapabilities, setCapabilities } from '../engine-capabilities.js';
import {
  alternateEngines,
  dismissEngineNotice,
  engineIsUnfinished,
  engineNoticeKey,
  renderEngineChip,
  renderEngineNotice,
  revealEngineNotice,
  unbuiltSurfaces,
} from './engine-notice.js';

/** A descriptor entry, in the shape `capabilities.descriptor` sends. */
function surface(title, status) {
  return { title, status, supported: status === 'supported', note: '' };
}

/** The Claude column, abbreviated: everything served but image generation. */
const CLAUDE = {
  transcript_history: surface('History browser and transcript rendering',
    'supported'),
  usd_cost: surface('USD cost', 'supported'),
  image_generation: surface('Generated images', 'absent'),
};

/** The Antigravity column, abbreviated: two absent, two unbuilt. */
const ANTIGRAVITY = {
  transcript_history: surface('History browser and transcript rendering',
    'unbuilt'),
  session_mirror: surface('Repo-local verbatim session mirror', 'unbuilt'),
  usd_cost: surface('USD cost', 'absent'),
  image_generation: surface('Generated images', 'supported'),
};

/** A panel stub with only the fields these renderers read. */
function panel(overrides = {}) {
  return {
    _engines: null,
    _engineNoticeForced: false,
    _engineNoticeDismissed: null,
    _engineSwitchPending: false,
    _engineSwitchError: '',
    _onSwitchEngine: vi.fn(),
    ...overrides,
  };
}

/** Render a template into a detached host and return it. */
function draw(template) {
  const host = document.createElement('div');
  render(html`${template}`, host);
  return host;
}

beforeEach(() => resetCapabilities());
afterEach(() => resetCapabilities());

// ---------------------------------------------------------------
// unbuiltSurfaces / engineIsUnfinished — the fact everything keys off
// ---------------------------------------------------------------

describe('unbuiltSurfaces', () => {
  it('is empty before the descriptor has loaded', () => {
    expect(unbuiltSurfaces()).toEqual([]);
    expect(engineIsUnfinished()).toBe(false);
  });

  it('is empty on an engine that serves everything it is asked for', () => {
    setCapabilities(CLAUDE);
    expect(unbuiltSurfaces()).toEqual([]);
    expect(engineIsUnfinished()).toBe(false);
  });

  it('ignores absent surfaces, which are a difference and not a gap', () => {
    setCapabilities({ usd_cost: surface('USD cost', 'absent') });
    expect(unbuiltSurfaces()).toEqual([]);
    expect(engineIsUnfinished()).toBe(false);
  });

  it('reports unbuilt surfaces by their descriptor titles, sorted', () => {
    setCapabilities(ANTIGRAVITY);
    expect(unbuiltSurfaces().map((s) => s.title)).toEqual([
      'History browser and transcript rendering',
      'Repo-local verbatim session mirror',
    ]);
    expect(engineIsUnfinished()).toBe(true);
  });
});

// ---------------------------------------------------------------
// alternateEngines
// ---------------------------------------------------------------

describe('alternateEngines', () => {
  it('is empty when the engine list has not been read', () => {
    expect(alternateEngines(panel())).toEqual([]);
  });

  it('is empty when a malformed answer arrives', () => {
    expect(alternateEngines(panel({ _engines: { active: 'claude' } })))
      .toEqual([]);
  });

  it('excludes the active engine', () => {
    const p = panel({
      _engines: { active: 'claude', mountable: ['antigravity', 'claude'] },
    });
    expect(alternateEngines(p)).toEqual(['antigravity']);
  });
});

// ---------------------------------------------------------------
// The chip
// ---------------------------------------------------------------

describe('renderEngineChip', () => {
  it('renders nothing before the engine list is known', () => {
    expect(draw(renderEngineChip(panel())).textContent.trim()).toBe('');
  });

  it('renders nothing on a single-engine install', () => {
    const p = panel({ _engines: { active: 'claude', mountable: ['claude'] } });
    expect(draw(renderEngineChip(p)).textContent.trim()).toBe('');
  });

  it('names the active engine when a second one is mountable', () => {
    setCapabilities(CLAUDE);
    const p = panel({
      _engines: { active: 'claude', mountable: ['antigravity', 'claude'] },
    });
    const host = draw(renderEngineChip(p));
    expect(host.textContent).toContain('Claude');
    expect(host.querySelector('.engine-unfinished')).toBeNull();
  });

  it('warns on the chip when the engine has unbuilt surfaces', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    const host = draw(renderEngineChip(p));
    expect(host.textContent).toContain('Antigravity');
    expect(host.querySelector('.engine-unfinished')).not.toBeNull();
  });

  it('switches engines on change, without a trip through Settings', () => {
    // The chip *is* the selector now. It used to be a button that opened
    // the notice, and choosing an engine meant finding the Settings tab —
    // two steps away from the place the question is asked.
    setCapabilities(ANTIGRAVITY);
    const asked = [];
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
      _onSwitchEngine: (name) => asked.push(name),
    });
    const select = draw(renderEngineChip(p)).querySelector('select');
    select.value = 'claude';
    select.dispatchEvent(new Event('change'));
    expect(asked).toEqual(['claude']);
  });

  it('shows the server\'s labels rather than a capitalised identifier', () => {
    // Two engines reach the same product and differ only in which account
    // pays. "Antigravity" and "Agy" cannot say that; the server's labels
    // can, and a table here would be the AG-R-4 branch.
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: {
        active: 'agy',
        mountable: ['claude', 'agy'],
        labels: { claude: 'claude', agy: 'antigravity (subscription)' },
      },
    });
    const text = draw(renderEngineChip(p)).textContent;
    expect(text).toContain('antigravity (subscription)');
    expect(text).not.toContain('Agy');
  });

  it('keeps a way back into the notice while the engine has gaps', () => {
    // Dismissal is per engine and per gap set, so without this the user
    // who dismissed it once could never see what is missing again.
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
      _engineNoticeDismissed: 'something',
    });
    draw(renderEngineChip(p)).querySelector('button.engine-gaps').click();
    expect(p._engineNoticeForced).toBe(true);
    expect(p._engineNoticeDismissed).toBeNull();
  });

  it('offers no gaps button on an engine that has none', () => {
    // Otherwise it is furniture rather than a signal.
    setCapabilities(CLAUDE);
    const p = panel({
      _engines: { active: 'claude', mountable: ['claude', 'antigravity'] },
    });
    expect(draw(renderEngineChip(p)).querySelector('button.engine-gaps')).toBeNull();
  });
});

// ---------------------------------------------------------------
// The notice
// ---------------------------------------------------------------

describe('renderEngineNotice', () => {
  it('stays silent on the shipped engine, which has no gaps', () => {
    setCapabilities(CLAUDE);
    const p = panel({
      _engines: { active: 'claude', mountable: ['antigravity', 'claude'] },
    });
    expect(draw(renderEngineNotice(p)).textContent.trim()).toBe('');
  });

  it('stays silent before the engine list is known', () => {
    setCapabilities(ANTIGRAVITY);
    expect(draw(renderEngineNotice(panel())).textContent.trim()).toBe('');
  });

  it('names the engine and every surface it has not built', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    const host = draw(renderEngineNotice(p));
    expect(host.textContent).toContain('running on Antigravity');
    const items = [...host.querySelectorAll('.engine-missing li')]
      .map((li) => li.textContent.trim());
    expect(items).toEqual([
      'History browser and transcript rendering',
      'Repo-local verbatim session mirror',
    ]);
  });

  it('does not list an absent surface, which is not a gap', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    expect(draw(renderEngineNotice(p)).textContent).not.toContain('USD cost');
  });

  it('offers the one alternate engine, and says what switching costs', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    const host = draw(renderEngineNotice(p));
    const button = host.querySelector('.engine-switch');
    expect(button.textContent).toContain('Switch to Claude');
    expect(host.textContent).toContain('Starts a new session');
    button.click();
    expect(p._onSwitchEngine).toHaveBeenCalledWith('claude');
  });

  it('offers no switch when the choice is between more than two', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: {
        active: 'antigravity',
        mountable: ['antigravity', 'claude', 'third'],
      },
    });
    expect(draw(renderEngineNotice(p)).querySelector('.engine-switch'))
      .toBeNull();
  });

  it('disables the switch while one is in flight', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
      _engineSwitchPending: true,
    });
    expect(draw(renderEngineNotice(p)).querySelector('.engine-switch').disabled)
      .toBe(true);
  });

  it('shows why a switch was refused rather than only logging it', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
      _engineSwitchError: 'A turn is still running',
    });
    expect(draw(renderEngineNotice(p)).textContent)
      .toContain('A turn is still running');
  });

  it('goes quiet once dismissed, and comes back when the gaps change', () => {
    setCapabilities(ANTIGRAVITY);
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    dismissEngineNotice(p);
    expect(draw(renderEngineNotice(p)).textContent.trim()).toBe('');

    setCapabilities({
      ...ANTIGRAVITY,
      subagent_tabs: surface('Subagent rows and their own tabs', 'unbuilt'),
    });
    expect(draw(renderEngineNotice(p)).textContent).toContain('Antigravity');
  });

  it('answers the chip on a complete engine rather than staying blank', () => {
    setCapabilities(CLAUDE);
    const p = panel({
      _engines: { active: 'claude', mountable: ['antigravity', 'claude'] },
    });
    revealEngineNotice(p);
    const text = draw(renderEngineNotice(p)).textContent;
    expect(text).toContain('running on Claude');
    expect(text).toContain('Nothing this build offers is missing');
  });
});

// ---------------------------------------------------------------
// engineNoticeKey — dismissal identity
// ---------------------------------------------------------------

describe('engineNoticeKey', () => {
  it('differs between engines with the same gaps', () => {
    setCapabilities(ANTIGRAVITY);
    const a = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    const b = panel({
      _engines: { active: 'third', mountable: ['third', 'claude'] },
    });
    expect(engineNoticeKey(a)).not.toBe(engineNoticeKey(b));
  });

  it('differs when the same engine gains a gap', () => {
    const p = panel({
      _engines: { active: 'antigravity', mountable: ['antigravity', 'claude'] },
    });
    setCapabilities(ANTIGRAVITY);
    const before = engineNoticeKey(p);
    setCapabilities({
      ...ANTIGRAVITY,
      subagent_tabs: surface('Subagent rows and their own tabs', 'unbuilt'),
    });
    expect(engineNoticeKey(p)).not.toBe(before);
  });
});
