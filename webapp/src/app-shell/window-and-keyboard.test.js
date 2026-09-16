// Tests for AppShell — window resize handling and global
// keyboard shortcuts.
//
// Covers:
//   - The RAF-throttled window resize handler that clamps the
//     dialog back into the viewport and relays out the
//     background viewers (Monaco caches layout; the SVG viewer
//     editors don't auto-refit).
//   - Global Alt+digit tab-switch shortcuts (Alt+1..4) and the
//     Alt+M minimize toggle. Alt+Arrow file navigation is
//     covered separately because it uses a different listener
//     (capture-phase on the file-nav grid, not bubble-phase on
//     document).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { installAppShellTestSetup, mountShell } from './test-helpers.js';

describe('AppShell window resize and keyboard shortcuts', () => {
  installAppShellTestSetup();

  describe('window resize', () => {
    beforeEach(() => { localStorage.clear(); });
    afterEach(() => { localStorage.clear(); });

    it('throttles handler to one call per animation frame', async () => {
      // Explicitly include rAF/cAF in the fake-timer set.
      // In vitest 2.x + jsdom, rAF is not wired through
      // setTimeout by default; fake timers won't intercept
      // it unless we ask. Previously this test relied on
      // an implementation detail that no longer holds.
      vi.useFakeTimers({
        toFake: [
          'setTimeout',
          'clearTimeout',
          'setInterval',
          'clearInterval',
          'requestAnimationFrame',
          'cancelAnimationFrame',
        ],
      });
      const shell = mountShell();
      await shell.updateComplete;
      const spy = vi.spyOn(shell, '_handleWindowResize');
      // Fire 5 resize events rapidly. RAF throttle means
      // only one _handleWindowResize call.
      for (let i = 0; i < 5; i += 1) {
        shell._onWindowResize();
      }
      expect(spy).not.toHaveBeenCalled();
      // Flush — with rAF faked, runAllTimers drains the
      // queued callback.
      vi.runAllTimers();
      expect(spy).toHaveBeenCalledTimes(1);
      spy.mockRestore();
    });

    it('calls relayout on both viewers during window resize', async () => {
      vi.useFakeTimers({
        toFake: [
          'setTimeout',
          'clearTimeout',
          'setInterval',
          'clearInterval',
          'requestAnimationFrame',
          'cancelAnimationFrame',
        ],
      });
      const shell = mountShell();
      await shell.updateComplete;
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      const diffRelayout = vi.spyOn(diff, 'relayout');
      const svgRelayout = vi.spyOn(svg, 'relayout');
      shell._onWindowResize();
      // Both throttles are pending; flush.
      vi.runAllTimers();
      // _handleWindowResize scheduled the viewer
      // relayout, which fires on the next RAF tick.
      vi.runAllTimers();
      expect(diffRelayout).toHaveBeenCalled();
      expect(svgRelayout).toHaveBeenCalled();
      diffRelayout.mockRestore();
      svgRelayout.mockRestore();
    });

    it('cancels pending RAF on disconnect', async () => {
      // Same fake-timer extension as the throttle test —
      // cancelAnimationFrame must be faked too, otherwise
      // disconnect can't cancel the pending rAF and the
      // spy fires anyway.
      vi.useFakeTimers({
        toFake: [
          'setTimeout',
          'clearTimeout',
          'setInterval',
          'clearInterval',
          'requestAnimationFrame',
          'cancelAnimationFrame',
        ],
      });
      const shell = mountShell();
      await shell.updateComplete;
      const spy = vi.spyOn(shell, '_handleWindowResize');
      shell._onWindowResize();
      shell.remove();
      // Flush — the cancelled rAF must not fire.
      vi.runAllTimers();
      expect(spy).not.toHaveBeenCalled();
      spy.mockRestore();
    });

    it('leaves undocked position alone when still in bounds', () => {
      const shell = mountShell();
      shell._undockedPos = {
        left: 100, top: 100, width: 500, height: 400,
      };
      shell._handleWindowResize();
      expect(shell._undockedPos).toEqual({
        left: 100, top: 100, width: 500, height: 400,
      });
    });

    it('rescues stranded undocked dialog after viewport shrink', () => {
      const shell = mountShell();
      // Simulate an undocked dialog well beyond the current
      // viewport. jsdom's default innerWidth is 1024.
      shell._undockedPos = {
        left: 5000, top: 0, width: 400, height: 400,
      };
      shell._handleWindowResize();
      expect(shell._undockedPos.left).toBeLessThan(1024);
    });

    it('clamps oversized docked width after viewport shrink', () => {
      const shell = mountShell();
      // jsdom innerWidth ~1024 by default; set a docked
      // width bigger than viewport.
      shell._dockedWidth = 2000;
      shell._undockedPos = null;
      shell._handleWindowResize();
      expect(shell._dockedWidth).toBeLessThanOrEqual(
        window.innerWidth,
      );
    });
  });

  // ---------------------------------------------------------------
  // Global keyboard shortcuts — Alt+1..4 / Alt+M
  // ---------------------------------------------------------------
  //
  // specs4/5-webapp/shell.md § Global Keyboard Shortcuts
  // pins the Alt+digit tab switch and Alt+M minimize
  // toggle. Alt+Arrow is covered by its own describe
  // block ("Alt+Arrow debounce") — that handler is
  // registered capture-phase on the file-nav grid;
  // these shortcuts use bubble-phase on document.

  describe('global keyboard shortcuts', () => {
    /**
     * Dispatch a keydown on document with the given
     * options. Matches how the browser delivers real
     * key events: target is document.body (or
     * wherever focus is), bubbling, cancelable.
     */
    function fireKey(opts) {
      const ev = new KeyboardEvent('keydown', {
        bubbles: true,
        cancelable: true,
        ...opts,
      });
      document.dispatchEvent(ev);
      return ev;
    }

    beforeEach(() => {
      localStorage.clear();
    });
    afterEach(() => {
      localStorage.clear();
    });

    it('Alt+1 switches to files tab', async () => {
      const shell = mountShell();
      shell.activeTab = 'context';
      await shell.updateComplete;
      fireKey({ key: '1', altKey: true });
      expect(shell.activeTab).toBe('files');
    });

    it('Alt+2 switches to context tab', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      fireKey({ key: '2', altKey: true });
      expect(shell.activeTab).toBe('context');
    });

    it('Alt+3 switches to settings tab', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      fireKey({ key: '3', altKey: true });
      expect(shell.activeTab).toBe('settings');
    });

    it('Alt+4 switches to doc-convert when available', async () => {
      const shell = mountShell();
      shell._docConvertAvailable = true;
      await shell.updateComplete;
      fireKey({ key: '4', altKey: true });
      expect(shell.activeTab).toBe('doc-convert');
    });

    it('Alt+4 no-ops when doc-convert is unavailable', async () => {
      const shell = mountShell();
      shell._docConvertAvailable = false;
      shell.activeTab = 'context';
      await shell.updateComplete;
      fireKey({ key: '4', altKey: true });
      // Tab unchanged — the stored preference stays on
      // whatever it was. A silent consume is preferable
      // to switching to a hidden tab with no body.
      expect(shell.activeTab).toBe('context');
    });

    it('Alt+4 consumes the keystroke even when unavailable', async () => {
      // preventDefault must fire so the browser's own
      // Alt+4 binding (Firefox tab-switch) doesn't steal
      // the keystroke when our tab is hidden.
      const shell = mountShell();
      shell._docConvertAvailable = false;
      await shell.updateComplete;
      const ev = fireKey({ key: '4', altKey: true });
      expect(ev.defaultPrevented).toBe(true);
    });

    it('Alt+M toggles minimize', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      expect(shell._minimized).toBe(false);
      fireKey({ key: 'm', altKey: true });
      expect(shell._minimized).toBe(true);
      fireKey({ key: 'm', altKey: true });
      expect(shell._minimized).toBe(false);
    });

    it('Alt+M accepts uppercase M (Caps Lock safe)', async () => {
      // Users with Caps Lock on would otherwise see the
      // shortcut silently fail. Both cases map to the
      // same action.
      const shell = mountShell();
      await shell.updateComplete;
      fireKey({ key: 'M', altKey: true });
      expect(shell._minimized).toBe(true);
    });

    it('preventDefault fires on handled shortcuts', async () => {
      // Each handled shortcut must call preventDefault
      // so browser chrome shortcuts (Firefox uses
      // Alt+digit for tab switching) don't intercept.
      const shell = mountShell();
      shell._docConvertAvailable = true;
      await shell.updateComplete;
      for (const key of ['1', '2', '3', '4', 'm']) {
        const ev = fireKey({ key, altKey: true });
        expect(ev.defaultPrevented).toBe(true);
      }
    });

    it('plain digits without Alt are ignored', async () => {
      // Typing '1' inside the chat textarea shouldn't
      // switch tabs.
      const shell = mountShell();
      shell.activeTab = 'context';
      await shell.updateComplete;
      const ev = fireKey({ key: '1' });
      expect(shell.activeTab).toBe('context');
      expect(ev.defaultPrevented).toBe(false);
    });

    it('Alt+Shift+1 is ignored', async () => {
      // Alt+Shift+digit on macOS is a symbol-entry
      // shortcut ("¡" for Alt+Shift+1). Consuming it
      // would break symbol entry for users who type in
      // Spanish / other layouts that rely on it.
      const shell = mountShell();
      shell.activeTab = 'context';
      await shell.updateComplete;
      const ev = fireKey({
        key: '1', altKey: true, shiftKey: true,
      });
      expect(shell.activeTab).toBe('context');
      expect(ev.defaultPrevented).toBe(false);
    });

    it('Ctrl+Alt+1 is ignored', async () => {
      // Ctrl+Alt+digit is bound by some window managers
      // (GNOME workspace switching). Leave it alone.
      const shell = mountShell();
      shell.activeTab = 'context';
      await shell.updateComplete;
      const ev = fireKey({
        key: '1', altKey: true, ctrlKey: true,
      });
      expect(shell.activeTab).toBe('context');
      expect(ev.defaultPrevented).toBe(false);
    });

    it('Alt+5 switches to the SDK Surface tab', async () => {
      const shell = mountShell();
      shell.activeTab = 'files';
      await shell.updateComplete;
      const ev = fireKey({ key: '5', altKey: true });
      expect(shell.activeTab).toBe('sdk-surface');
      expect(ev.defaultPrevented).toBe(true);
    });

    it('Alt+6 and other unmapped digits are ignored', async () => {
      const shell = mountShell();
      shell.activeTab = 'files';
      await shell.updateComplete;
      const ev = fireKey({ key: '6', altKey: true });
      expect(shell.activeTab).toBe('files');
      expect(ev.defaultPrevented).toBe(false);
    });

    it('Alt+letter other than M is ignored', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      const ev = fireKey({ key: 'a', altKey: true });
      expect(shell._minimized).toBe(false);
      expect(ev.defaultPrevented).toBe(false);
    });

    it('tab switch persists to localStorage', async () => {
      // Regression — _switchTab already persists, but
      // verify the shortcut path doesn't bypass it.
      const shell = mountShell();
      await shell.updateComplete;
      fireKey({ key: '3', altKey: true });
      expect(localStorage.getItem('aic-dc-active-tab'))
        .toBe('settings');
    });

    it('minimize toggle persists to localStorage', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      fireKey({ key: 'm', altKey: true });
      expect(localStorage.getItem('aic-dc-minimized')).toBe('true');
    });

    it('listener removed on disconnect', async () => {
      // After the shell unmounts, document-level Alt+1
      // presses must not mutate its state (and must not
      // throw).
      const shell = mountShell();
      shell.activeTab = 'files';
      await shell.updateComplete;
      shell.remove();
      // State captured before unmount; the post-unmount
      // event shouldn't change it.
      const before = shell.activeTab;
      expect(() =>
        fireKey({ key: '2', altKey: true }),
      ).not.toThrow();
      expect(shell.activeTab).toBe(before);
    });

    // -------------------------------------------------------------
    // While a permission request is on screen
    // -------------------------------------------------------------
    //
    // shell.md § Keyboard Shortcuts: every shortcut is inert while a
    // modal permission dialog is open, because each of them changes
    // something the scrim is covering. Alt+Arrow is included here even
    // though its own semantics live in viewers.test.js — one rule
    // covers every shortcut, and splitting it across two files is how
    // half of it gets dropped.

    describe('while a permission request is open', () => {
      /** Queue a real request on the dialog the shell mounts. */
      function askPermission(over = {}) {
        window.dispatchEvent(new CustomEvent('permission-request', {
          detail: {
            permission_id: 'perm_1',
            tool_use_id: 'toolu_1',
            request_id: 'req_1',
            tool_name: 'Bash',
            display_name: 'Bash',
            tool_class: 'exec',
            gated_by_default: true,
            summary: 'ls -la',
            input: { command: 'ls -la' },
            command: { command: 'ls -la', cwd: '/repo', flags: [] },
            suggested_rules: [],
            expires_at: null,
            localhost_available: true,
            ...over,
          },
        }));
      }

      function fireAltArrow(shell, dir) {
        const ev = new KeyboardEvent('keydown', {
          key: `Arrow${dir}`,
          altKey: true,
          bubbles: true,
          cancelable: true,
        });
        document.dispatchEvent(ev);
        return ev;
      }

      it('Alt+digit does not switch the tab behind the scrim', async () => {
        // The tab would change where the user cannot see it, and they
        // would find themselves somewhere else on answering.
        const shell = mountShell();
        await shell.updateComplete;
        askPermission();
        const ev = fireKey({ key: '2', altKey: true });
        expect(shell.activeTab).toBe('files');
        // Not consumed either: declining a key and swallowing it makes a
        // suppressed shortcut indistinguishable from a broken one.
        expect(ev.defaultPrevented).toBe(false);
      });

      it('Alt+M does not collapse the panel behind it', async () => {
        const shell = mountShell();
        await shell.updateComplete;
        askPermission();
        fireKey({ key: 'm', altKey: true });
        expect(shell._minimized).toBe(false);
      });

      it('Ctrl+Shift+F does not focus a field the user cannot see', async () => {
        // The worst of the four: it moves focus to a search input behind
        // the scrim while focus is meant to be trapped in the dialog, so
        // the next thing typed goes somewhere invisible.
        const shell = mountShell();
        shell.activeTab = 'settings';
        await shell.updateComplete;
        const switched = vi.spyOn(shell, '_switchTab');
        askPermission();
        fireKey({ key: 'F', ctrlKey: true, shiftKey: true });
        expect(switched).not.toHaveBeenCalled();
        switched.mockRestore();
      });

      it('leaves Alt+Arrow for the dialog\'s own diff editor', async () => {
        // A `write` request puts a Monaco diff on screen, where Alt+Arrow
        // is word navigation. The grid handler consumes it capture-phase,
        // so without the guard the caret stays put and a file swaps in
        // the viewer nobody can see.
        const shell = mountShell();
        await shell.updateComplete;
        const nav = shell.shadowRoot.querySelector('aic-file-nav');
        nav.openFile('a.py');
        nav.openFile('b.py');
        await shell.updateComplete;
        const navigate = vi.spyOn(nav, 'navigateDirection');

        askPermission({ tool_class: 'write', tool_name: 'Write' });
        const ev = fireAltArrow(shell, 'Left');

        expect(navigate).not.toHaveBeenCalled();
        // Left unconsumed on purpose — this is the keystroke reaching the
        // editor it was aimed at.
        expect(ev.defaultPrevented).toBe(false);
        navigate.mockRestore();
      });

      it('hands the shortcuts back when the request is answered', async () => {
        // The reason the dialog is asked rather than mirrored: a mirror
        // that misses one of the ways a queue drains disables the
        // keyboard for the rest of the session.
        const shell = mountShell();
        await shell.updateComplete;
        askPermission();
        fireKey({ key: '2', altKey: true });
        expect(shell.activeTab).toBe('files');

        window.dispatchEvent(new CustomEvent('permission-resolved', {
          detail: { permission_id: 'perm_1', action: 'allow' },
        }));
        await shell.updateComplete;

        fireKey({ key: '2', altKey: true });
        expect(shell.activeTab).toBe('context');
      });

      it('leaves them live for a question docked beside the chat', async () => {
        // A docked question has no scrim and the UI behind it is genuinely
        // live, so suppressing the shortcuts would be claiming otherwise.
        // Alt+2 takes the chat off screen, which is what un-docks the
        // question — it re-centres as a modal rather than being ignored.
        const shell = mountShell();
        await shell.updateComplete;
        const panel = shell.shadowRoot.querySelector('.dialog');
        panel.getBoundingClientRect = () => ({
          left: 0, right: 400, top: 0, bottom: 800, width: 400, height: 800,
        });
        Object.defineProperty(window, 'innerWidth', {
          value: 1600, configurable: true, writable: true,
        });
        shell._relayoutViewers();
        await shell.updateComplete;
        const dialog = shell.shadowRoot.querySelector('aic-permission-dialog');
        askPermission({ tool_class: 'interact', tool_name: 'AskUserQuestion' });
        expect(dialog.modal).toBe(false);

        fireKey({ key: '2', altKey: true });
        expect(shell.activeTab).toBe('context');
      });
    });
  });
});