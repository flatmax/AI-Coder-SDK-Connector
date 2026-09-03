// Action-bar contents and tab-scoped visibility.
//
// Phase 2 emptied most of this bar. The controls it used to gate per tab —
// mode toggle (💻/📄), cross-reference toggle (🔀), new-session (✨), history
// (📜) — all drove `LLMService`, and the chat path no longer reaches it. They
// were removed rather than left inert in the commit that repointed the path,
// because a control whose visible state does not describe what the engine will
// do is exactly the failure the permission dialog exists to prevent.
//
// Phase 5 gave ✨ and 📜 a real engine to drive and they came back; the rest
// have nothing to come back to. So this file pins three things:
//
//   1. **The controls with no successor stay removed.** A test that only
//      checked the new selector would pass just as well if someone re-added a
//      dead 🔀 button.
//
//   2. **The session group is tab-scoped.** ✨ and 📜 belong to the live
//      conversation, so they are absent on an agent tab and on a historical
//      read-only one, where restarting "the session" would restart the one
//      behind the tab the user is reading.
//
//   3. **The permission-mode selector is always reachable.** Every tab, every
//      search mode. It is the one action-bar control that changes what the next
//      tool call does, and a user who cannot see the current mode cannot know
//      whether the next edit will ask.
//
// The `/` palette button and search bar were never gated and still are not.

import { afterEach, describe, expect, it } from 'vitest';

import {
  resetCapabilities,
  setCapabilities,
} from '../engine-capabilities.js';

import {
  mountPanel,
  seedLabeledTab,
  settle,
  _mounted,
} from './test-helpers.js';

/** Controls phase 2 removed that phase 5 did not bring back. */
const REMOVED_CONTROLS = [
  ['.mode-toggle', 'code/doc index switch — the preset selector, CC-12'],
  ['.crossref-btn', 'cross-reference toggle — never; retired in phase 4'],
  ['.reasoning-toggle', 'reasoning flags chat_streaming no longer takes'],
  ['aic-url-chips', 'URL fetching — the CLI has WebFetch'],
];

/** The session group, back in phase 5 against `ClaudeCodeService`. */
const SESSION_CONTROLS = ['.new-session-button', '.history-button'];

describe('ChatPanel action bar — what it carries', () => {
  describe('controls phase 2 removed', () => {
    for (const [selector, why] of REMOVED_CONTROLS) {
      it(`does not render ${selector} (${why})`, async () => {
        const panel = mountPanel();
        await settle(panel);
        expect(panel.shadowRoot.querySelector(selector)).toBeFalsy();
      });
    }

    it('does not render them on an agent tab either', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      for (const [selector] of REMOVED_CONTROLS) {
        expect(panel.shadowRoot.querySelector(selector)).toBeFalsy();
      }
    });
  });

  describe('the session group', () => {
    it('renders on the main tab', async () => {
      const panel = mountPanel();
      await settle(panel);
      for (const selector of SESSION_CONTROLS) {
        expect(panel.shadowRoot.querySelector(selector)).toBeTruthy();
      }
    });

    it('is absent on an agent tab', async () => {
      // A subagent transcript has no session of its own to restart, and the
      // ✨ on it would restart the conversation that spawned it.
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      for (const selector of SESSION_CONTROLS) {
        expect(panel.shadowRoot.querySelector(selector)).toBeFalsy();
      }
    });

    it('is absent on a historical read-only tab', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'historical:t_123/agent-0', 'Agent 0');
      panel._tabs.get('historical:t_123/agent-0').readOnly = true;
      panel._activeTabId = 'historical:t_123/agent-0';
      await settle(panel);
      for (const selector of SESSION_CONTROLS) {
        expect(panel.shadowRoot.querySelector(selector)).toBeFalsy();
      }
    });

    it('collapses out of the way of file search', async () => {
      const panel = mountPanel();
      panel._searchMode = 'file';
      await settle(panel);
      for (const selector of SESSION_CONTROLS) {
        expect(panel.shadowRoot.querySelector(selector)).toBeFalsy();
      }
    });

    it('is inside a search-collapsible group, unlike the selector', async () => {
      const panel = mountPanel();
      await settle(panel);
      for (const selector of SESSION_CONTROLS) {
        const btn = panel.shadowRoot.querySelector(selector);
        expect(btn.closest('.search-collapsible')).toBeTruthy();
      }
    });
  });

  describe('permission-mode selector', () => {
    it('renders on the main tab', async () => {
      const panel = mountPanel();
      await settle(panel);
      const group = panel.shadowRoot.querySelector('.permission-mode');
      expect(group).toBeTruthy();
      expect(group.querySelector('select.permission-mode-select')).toBeTruthy();
    });

    it('renders on an agent tab', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      expect(
        panel.shadowRoot.querySelector('.permission-mode-select'),
      ).toBeTruthy();
    });

    it('renders on a historical read-only tab', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'historical:t_123/agent-0', 'Agent 0');
      panel._tabs.get('historical:t_123/agent-0').readOnly = true;
      panel._activeTabId = 'historical:t_123/agent-0';
      await settle(panel);
      expect(
        panel.shadowRoot.querySelector('.permission-mode-select'),
      ).toBeTruthy();
    });

    it('survives file-search mode', async () => {
      // The bar's other groups collapse when the search field expands, and
      // the ✨/📜 pair goes with them. The selector is deliberately outside
      // every `.search-collapsible` group so expanding search cannot hide
      // the safety posture.
      const panel = mountPanel();
      panel._searchMode = 'file';
      await settle(panel);
      const group = panel.shadowRoot.querySelector('.permission-mode');
      expect(group).toBeTruthy();
      expect(group.closest('.search-collapsible')).toBeNull();
    });

    it('is the first control in the bar', async () => {
      const panel = mountPanel();
      await settle(panel);
      const bar = panel.shadowRoot.querySelector('.action-bar');
      expect(bar.firstElementChild.classList.contains('permission-mode')).toBe(
        true,
      );
    });

    it('stays reachable across a tab switch', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      expect(
        panel.shadowRoot.querySelector('.permission-mode-select'),
      ).toBeTruthy();

      panel._activeTabId = 'main';
      await settle(panel);
      expect(
        panel.shadowRoot.querySelector('.permission-mode-select'),
      ).toBeTruthy();
    });
  });

  describe('controls that were never gated', () => {
    it('renders the search bar on an agent tab', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      expect(panel.shadowRoot.querySelector('.search-bar')).toBeTruthy();
    });

    it('renders the slash-palette button on an agent tab', async () => {
      const panel = mountPanel();
      seedLabeledTab(panel, 'agent-0', 'Agent 0');
      panel._activeTabId = 'agent-0';
      await settle(panel);
      expect(
        panel.shadowRoot.querySelector('.slash-palette-button'),
      ).toBeTruthy();
    });
  });

  // AG-9 — the 📜 button is hidden on an engine that keeps no transcript
  // history. Hidden at the entry point rather than inside the dialog: a
  // browser that opens to explain it has nothing to show is a click that
  // can only disappoint, and the button is the honest place to say no.

  describe('controls gated on the engine', () => {
    afterEach(() => resetCapabilities());

    it('hides the history button on an engine with no transcripts', async () => {
      resetCapabilities();
      setCapabilities({
        transcript_history: { supported: false, status: 'unbuilt', note: '' },
      });
      const panel = mountPanel();
      await settle(panel);
      expect(panel.shadowRoot.querySelector('.history-button')).toBeFalsy();
    });

    it('keeps the new-session button, which every engine has', async () => {
      // Starting a fresh conversation is not a capability — it is what
      // every engine does. Taking it away with the history button would
      // be hiding a working control because a neighbouring one is gone.
      resetCapabilities();
      setCapabilities({
        transcript_history: { supported: false, status: 'unbuilt', note: '' },
      });
      const panel = mountPanel();
      await settle(panel);
      expect(panel.shadowRoot.querySelector('.new-session-button')).toBeTruthy();
    });

    it('draws it before the descriptor lands, and on an engine that has it', async () => {
      resetCapabilities();
      const panel = mountPanel();
      await settle(panel);
      expect(panel.shadowRoot.querySelector('.history-button')).toBeTruthy();
    });
  });

  // -------------------------------------------------------------
  // The engine chip — the answer to "why did the button go away?"
  // -------------------------------------------------------------
  //
  // The tests above pin that the history button hides on an engine with
  // no transcripts. That is correct and it is what a user reported as the
  // application being broken, because nothing in the bar said which
  // engine was running. The chip is the other half: hiding stays, and it
  // stops being anonymous.

  describe('engine chip', () => {
    afterEach(() => resetCapabilities());

    it('is absent on a single-engine install, which has nothing to ask',
      async () => {
        const panel = mountPanel();
        panel._engines = { active: 'claude', mountable: ['claude'] };
        await settle(panel);
        expect(panel.shadowRoot.querySelector('.engine-chip')).toBeFalsy();
      });

    it('names the running engine when a second one is mountable', async () => {
      const panel = mountPanel();
      panel._engines = {
        active: 'claude', mountable: ['antigravity', 'claude'],
      };
      await settle(panel);
      const chip = panel.shadowRoot.querySelector('.engine-chip');
      expect(chip).toBeTruthy();
      expect(chip.textContent).toContain('Claude');
    });

    it('stands beside the gap it explains when a surface is hidden',
      async () => {
        resetCapabilities();
        setCapabilities({
          transcript_history: { supported: false, status: 'unbuilt', note: '' },
        });
        const panel = mountPanel();
        panel._engines = {
          active: 'antigravity', mountable: ['antigravity', 'claude'],
        };
        await settle(panel);
        expect(panel.shadowRoot.querySelector('.history-button')).toBeFalsy();
        const chip = panel.shadowRoot.querySelector('.engine-chip');
        expect(chip.textContent).toContain('Antigravity');
        expect(chip.classList.contains('engine-unfinished')).toBe(true);
      });
  });
});
