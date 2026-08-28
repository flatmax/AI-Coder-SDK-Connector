// Tests for webapp/src/app-shell/viewers.js — viewer routing
// and Alt+Arrow keyboard navigation with debounce.
//
// Covers:
//   - File extension dispatch between diff-viewer and
//     svg-viewer when a `navigate-file` event arrives.
//   - Visibility toggling and file-list preservation as
//     the active viewer changes.
//   - Alt+Arrow debounce: rapid key sequences coalesce to
//     a single viewer fetch; Alt release flushes early.
//   - The viewer background's left inset, which keeps the
//     side-by-side viewers out from under the docked dialog.
//
// Shared mocks for monaco-editor / svg-pan-zoom and the
// AppShell prototype patches live in
// `./test-helpers.js`. Each describe block keeps its own
// local `settle()` helper because the two suites need
// slightly different drain depths (viewer routing waits
// on child viewer updates; Alt+Arrow drains microtasks
// after fake-timer flushes).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getRepoRoot, setRepoRoot } from '../repo-path.js';
import { installAppShellTestSetup, mountShell } from './test-helpers.js';
import { relayoutViewers, syncViewerInset } from './viewers.js';

describe('AppShell viewer routing and navigation', () => {
  installAppShellTestSetup();

  describe('viewer routing', () => {
    async function settle(shell) {
      await shell.updateComplete;
      await new Promise((r) => setTimeout(r, 0));
      await shell.updateComplete;
      // Let the viewers' own Lit updates settle.
      const diff = shell.shadowRoot.querySelector(
        'aic-diff-viewer',
      );
      const svg = shell.shadowRoot.querySelector(
        'aic-svg-viewer',
      );
      if (diff) await diff.updateComplete;
      if (svg) await svg.updateComplete;
    }

    it('renders both viewers in the background layer', async () => {
      const shell = mountShell();
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      expect(diff).toBeTruthy();
      expect(svg).toBeTruthy();
    });

    it('diff viewer is visible by default', async () => {
      const shell = mountShell();
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      expect(diff.classList.contains('viewer-visible')).toBe(true);
      expect(svg.classList.contains('viewer-hidden')).toBe(true);
    });

    it('navigate-file to .py routes to diff viewer', async () => {
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'src/main.py' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff.hasOpenFiles).toBe(true);
      expect(diff._file.path).toBe('src/main.py');
    });

    it('navigate-file to .svg routes to svg viewer', async () => {
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'docs/flow.svg' },
        }),
      );
      await settle(shell);
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(svg.hasOpenFiles).toBe(true);
      expect(svg._files[0].path).toBe('docs/flow.svg');
      // Diff viewer didn't receive it — no file in the
      // single-file slot.
      expect(diff.hasOpenFiles).toBe(false);
      expect(diff._file).toBe(null);
    });

    it('opening an .svg flips active viewer to svg', async () => {
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'diagram.svg' },
        }),
      );
      await settle(shell);
      expect(shell._activeViewer).toBe('svg');
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      expect(svg.classList.contains('viewer-visible')).toBe(true);
      expect(diff.classList.contains('viewer-hidden')).toBe(true);
    });

    it('switching between .py and .svg toggles visibility', async () => {
      const shell = mountShell();
      await settle(shell);
      // Open .py — diff visible.
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'a.py' },
        }),
      );
      await settle(shell);
      expect(shell._activeViewer).toBe('diff');
      // Open .svg — svg visible.
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'b.svg' },
        }),
      );
      await settle(shell);
      expect(shell._activeViewer).toBe('svg');
      // Back to .py — diff visible again.
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'c.py' },
        }),
      );
      await settle(shell);
      expect(shell._activeViewer).toBe('diff');
    });

    it('both viewers preserve their file lists across visibility toggles', async () => {
      // Key point: switching between .py and .svg doesn't
      // close the viewer that becomes hidden. Its tabs
      // remain intact. Matters for Phase 3.1's Monaco
      // instances, which are expensive to create.
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'a.py' },
        }),
      );
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'b.svg' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      // Diff viewer holds its single active file; SVG
      // viewer still uses the multi-file model.
      expect(diff._file?.path).toBe('a.py');
      expect(svg._files).toHaveLength(1);
      expect(svg._files[0].path).toBe('b.svg');
    });

    it('navigate-file with empty path is ignored', async () => {
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      expect(diff.hasOpenFiles).toBe(false);
      expect(svg.hasOpenFiles).toBe(false);
    });

    it('navigate-file with no detail is ignored', async () => {
      const shell = mountShell();
      await settle(shell);
      window.dispatchEvent(new CustomEvent('navigate-file'));
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff.hasOpenFiles).toBe(false);
    });

    it('opens an absolute path the engine reported as a repo path', async () => {
      // Claude Code's file tools take absolute paths, so a tool card's file
      // chip carries one. Every Repo RPC takes a repo-relative path and
      // refuses an absolute one, which is what a chip click used to earn.
      const shell = mountShell();
      setRepoRoot('/home/dev/my-repo');
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '/home/dev/my-repo/tests/test_thing.py' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff._file.path).toBe('tests/test_thing.py');
    });

    it('persists and registers the relative path, not the absolute one', async () => {
      // The absolute path used to be what got remembered as the last-open
      // file and handed to the nav grid, so one bad click survived a reload.
      const shell = mountShell();
      setRepoRoot('/home/dev/my-repo');
      await settle(shell);
      const saved = vi.spyOn(shell, '_saveLastOpenFile');
      const nav = { openFile: vi.fn() };
      vi.spyOn(shell, '_getFileNav').mockReturnValue(nav);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '/home/dev/my-repo/src/main.js' },
        }),
      );
      await settle(shell);
      expect(saved).toHaveBeenCalledWith('src/main.js');
      expect(nav.openFile).toHaveBeenCalledWith('src/main.js');
    });

    it('leaves a path outside the repo as it found it', async () => {
      // It has no repo-relative name. Rewriting it would ask for a
      // different file; the backend refusing it is the correct outcome.
      const shell = mountShell();
      setRepoRoot('/home/dev/my-repo');
      await settle(shell);
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '/etc/hosts.py' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff._file.path).toBe('/etc/hosts.py');
    });

    it('takes the root from the state snapshot', async () => {
      // The whole chain the fix depends on: the backend sends the absolute
      // root once, the shell keeps it, and the next chip click is openable.
      const shell = mountShell();
      shell.call = {
        'ClaudeCodeService.get_current_state': async () => ({
          repo_name: 'my-repo',
          repo_root: '/home/dev/my-repo',
        }),
      };
      await shell._fetchCurrentState();
      await settle(shell);
      expect(getRepoRoot()).toBe('/home/dev/my-repo');
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '/home/dev/my-repo/src/main.js' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff._file.path).toBe('src/main.js');
    });

    it('leaves an absolute path alone before the root is known', async () => {
      // No snapshot yet: unchanged is the old behaviour, and measuring
      // against an empty root would produce a wrong path.
      const shell = mountShell();
      await settle(shell);
      expect(getRepoRoot()).toBe('');
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: '/home/dev/my-repo/a.py' },
        }),
      );
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      expect(diff._file.path).toBe('/home/dev/my-repo/a.py');
    });

    it('forwards line and searchText to the viewer', async () => {
      // The stub accepts these and ignores them, but the
      // shell must pass them through so Phase 3.1's real
      // implementation can use them without any shell-side
      // changes.
      const shell = mountShell();
      await settle(shell);
      // Spy on the viewer's openFile to inspect args.
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const spy = vi.spyOn(diff, 'openFile');
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: {
            path: 'src/foo.py',
            line: 42,
            searchText: 'my anchor',
          },
        }),
      );
      await settle(shell);
      expect(spy).toHaveBeenCalledWith({
        path: 'src/foo.py',
        line: 42,
        searchText: 'my anchor',
      });
    });

    it('unsubscribes from navigate-file on disconnect', async () => {
      const shell = mountShell();
      await settle(shell);
      shell.remove();
      // After disconnect, dispatching navigate-file must
      // not affect state on the disconnected element.
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'ghost.py' },
        }),
      );
      // No crash; viewer inside the removed shell hasn't
      // been re-attached so we can't check its state
      // directly, but the lack of exception is the
      // contract.
      expect(shell.isConnected).toBe(false);
    });
  });

  describe('in-session viewport memory', () => {
    async function settle(shell) {
      await shell.updateComplete;
      await new Promise((r) => setTimeout(r, 0));
      await shell.updateComplete;
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      if (diff) await diff.updateComplete;
    }

    /**
     * Give the diff viewer a fake active file plus the
     * viewport-reading API the shell's capture path calls.
     * The monaco mock in test-helpers returns a modified
     * editor whose getScrollTop is hard-coded to 0, so we
     * override _getModifiedEditor to report a real scroll.
     */
    function stubViewportState(diff, {
      path,
      scrollTop = 0,
      lineNumber = 1,
      previewOpen = false,
      previewScrollTop = 0,
    }) {
      diff._file = { path, original: '', modified: '', savedContent: '' };
      diff._getModifiedEditor = () => ({
        getScrollTop: () => scrollTop,
        getScrollLeft: () => 0,
        getPosition: () => ({ lineNumber, column: 1 }),
        setScrollTop() {},
        setScrollLeft() {},
        setPosition() {},
      });
      diff.isPreviewOpen = () => previewOpen;
      diff.getPreviewScrollTop = () => previewScrollTop;
    }

    it('navigate-file records the outgoing file viewport', async () => {
      // The bug this covers: only the Alt+Arrow path used
      // to capture viewport state, so leaving a file by any
      // other route (preview link click, picker, search
      // hit) left nothing to restore on the way back.
      const shell = mountShell();
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      stubViewportState(diff, {
        path: 'docs/guide.md',
        scrollTop: 640,
        lineNumber: 31,
        previewOpen: true,
        previewScrollTop: 275,
      });
      // Click a source link out of the preview — this is
      // the exact event onPreviewClick dispatches.
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'src/app.js' },
        }),
      );
      await settle(shell);
      const stored = shell._diffViewportMemory?.get('docs/guide.md');
      expect(stored).toBeTruthy();
      expect(stored.diff.scrollTop).toBe(640);
      expect(stored.diff.lineNumber).toBe(31);
      expect(stored.preview).toEqual({ open: true, scrollTop: 275 });
    });

    it('Alt+Arrow back to a link-visited md file restores preview and scroll', async () => {
      // Full round trip: markdown in preview mode → click a
      // .js link → Alt+Left back. Preview must reopen and
      // both scroll surfaces must be restored.
      const shell = mountShell();
      await settle(shell);
      const nav = shell.shadowRoot.querySelector('aic-file-nav');
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      // Seed the grid the way real navigation would: the md
      // file is opened, then the link target.
      nav.openFile('docs/guide.md');
      stubViewportState(diff, {
        path: 'docs/guide.md',
        scrollTop: 640,
        lineNumber: 31,
        previewOpen: true,
        previewScrollTop: 275,
      });
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'src/app.js' },
        }),
      );
      await settle(shell);
      // Now on the .js file, with no preview.
      stubViewportState(diff, { path: 'src/app.js', scrollTop: 12 });
      const setPreviewMode = vi.fn();
      const restorePreviewScrollTop = vi.fn();
      diff.setPreviewMode = setPreviewMode;
      diff.restorePreviewScrollTop = restorePreviewScrollTop;
      diff.openFile = vi.fn(async () => {});
      diff._waitForDiffReady = () => Promise.resolve();
      // Alt+Left walks back to the markdown node.
      shell._altArrowPending = 'docs/guide.md';
      shell._flushAltArrowPending();
      await settle(shell);
      // Two frames: restoreViewport's rAF poll, then
      // finishPreview's own deferral.
      await new Promise((r) => requestAnimationFrame(() => r()));
      await new Promise((r) => requestAnimationFrame(() => r()));
      expect(diff.openFile).toHaveBeenCalledWith({
        path: 'docs/guide.md',
      });
      // Preview reopens before the scroll writes — the
      // pane width changes, so Monaco must lay out against
      // the split layout the offsets were captured in.
      expect(setPreviewMode).toHaveBeenCalledWith(true);
      expect(restorePreviewScrollTop).toHaveBeenCalledWith(275);
    });

    it('viewport memory is bounded and evicts least-recently-touched', async () => {
      const shell = mountShell();
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const { VIEWPORT_MEMORY_LIMIT } = await import('./viewport.js');
      // Walk through one more file than the cap allows.
      for (let i = 0; i <= VIEWPORT_MEMORY_LIMIT; i += 1) {
        stubViewportState(diff, { path: `f${i}.py`, scrollTop: i + 1 });
        window.dispatchEvent(
          new CustomEvent('navigate-file', {
            detail: { path: `f${i + 1}.py` },
          }),
        );
        await settle(shell);
      }
      const memory = shell._diffViewportMemory;
      expect(memory.size).toBe(VIEWPORT_MEMORY_LIMIT);
      // The very first entry aged out; the newest survives.
      expect(memory.has('f0.py')).toBe(false);
      expect(memory.has(`f${VIEWPORT_MEMORY_LIMIT}.py`)).toBe(true);
    });

    it('refresh navigations do not overwrite remembered state', async () => {
      // doReopenLastFile dispatches with _refresh: true
      // while the viewer is still pre-restore. Capturing
      // then would race the localStorage restore for the
      // same path.
      const shell = mountShell();
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      stubViewportState(diff, { path: 'a.md', scrollTop: 500 });
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'b.py' },
        }),
      );
      await settle(shell);
      expect(
        shell._diffViewportMemory.get('a.md').diff.scrollTop,
      ).toBe(500);
      // A refresh dispatch while a.md shows scroll 0 must
      // leave the remembered 500 alone.
      stubViewportState(diff, { path: 'a.md', scrollTop: 0 });
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: { path: 'a.md', _refresh: true },
        }),
      );
      await settle(shell);
      expect(
        shell._diffViewportMemory.get('a.md').diff.scrollTop,
      ).toBe(500);
    });
  });

  describe('Alt+Arrow debounce', () => {
    async function settle(shell) {
      await shell.updateComplete;
      await new Promise((r) => setTimeout(r, 0));
      await shell.updateComplete;
    }

    /**
     * Dispatch an Alt+Arrow keydown on the document,
     * simulating a user holding Alt and pressing the
     * given arrow direction.
     */
    function fireAltArrow(direction) {
      const keyMap = {
        left: 'ArrowLeft',
        right: 'ArrowRight',
        up: 'ArrowUp',
        down: 'ArrowDown',
      };
      const ev = new KeyboardEvent('keydown', {
        key: keyMap[direction],
        altKey: true,
        bubbles: true,
        cancelable: true,
      });
      document.dispatchEvent(ev);
    }

    function fireAltRelease() {
      const ev = new KeyboardEvent('keyup', {
        key: 'Alt',
        bubbles: true,
      });
      document.dispatchEvent(ev);
    }

    it('rapid arrow sequence produces one viewer fetch, not N', async () => {
      // The core win of the debounce: holding Alt+Right
      // through a 10-node path should produce ONE
      // openFile call for the final target, not ten.
      // Tests the _altArrowTimer + _flushAltArrowPending
      // pair directly — bypasses the real timer by
      // faking it.
      //
      // Fake timers are installed AFTER seeding the shell
      // and grid, because `settle` awaits a setTimeout(0)
      // that would otherwise never fire under fake timers.
      const shell = mountShell();
      await settle(shell);
      // Seed the grid with a chain of nodes so arrow
      // presses have targets. openFile on the grid is
      // synchronous; each call creates a new node
      // adjacent to the current.
      const nav = shell.shadowRoot.querySelector('aic-file-nav');
      nav.openFile('a.py');
      nav.openFile('b.py');
      nav.openFile('c.py');
      nav.openFile('d.py');
      await settle(shell);
      // Spy on the diff viewer's openFile.
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const openSpy = vi.spyOn(diff, 'openFile');
      // NOW install fake timers — only the debounce
      // setTimeout needs to be controlled from here on.
      vi.useFakeTimers();
      try {
        // Navigate back through the chain. After seeding,
        // the current node is d.py; three left-arrows
        // walk back to a.py.
        fireAltArrow('left');
        fireAltArrow('left');
        fireAltArrow('left');
        // Before the debounce window elapses, no viewer
        // fetch has been dispatched.
        expect(openSpy).not.toHaveBeenCalled();
        // Flush the debounce timer, then drain any
        // follow-up microtasks from the internal
        // updateComplete.then chain.
        await vi.advanceTimersByTimeAsync(250);
      } finally {
        vi.useRealTimers();
      }
      await settle(shell);
      // Exactly one openFile dispatch, for the final
      // position after the three lefts.
      expect(openSpy).toHaveBeenCalledTimes(1);
    });

    it('Alt release flushes pending fetch immediately', async () => {
      // Releasing Alt mid-debounce must fire the pending
      // fetch right away, otherwise the HUD fades out
      // before the viewer updates.
      const shell = mountShell();
      await settle(shell);
      const nav = shell.shadowRoot.querySelector('aic-file-nav');
      nav.openFile('a.py');
      nav.openFile('b.py');
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const openSpy = vi.spyOn(diff, 'openFile');
      vi.useFakeTimers();
      try {
        fireAltArrow('left');
        expect(openSpy).not.toHaveBeenCalled();
        fireAltRelease();
        // Release cancels the debounce timer and flushes
        // synchronously via _flushAltArrowPending, but
        // the actual openFile call happens inside an
        // updateComplete.then() microtask. Drain.
        await vi.runAllTimersAsync();
      } finally {
        vi.useRealTimers();
      }
      await settle(shell);
      expect(openSpy).toHaveBeenCalledTimes(1);
    });

    it('arrow within debounce window resets the timer', async () => {
      // Subsequent arrow within the debounce window
      // resets the timer (coalesces). Only the final
      // keystroke's target gets dispatched.
      const shell = mountShell();
      await settle(shell);
      const nav = shell.shadowRoot.querySelector('aic-file-nav');
      nav.openFile('a.py');
      nav.openFile('b.py');
      nav.openFile('c.py');
      await settle(shell);
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      const openSpy = vi.spyOn(diff, 'openFile');
      vi.useFakeTimers();
      try {
        // First arrow, wait 100ms (inside window).
        fireAltArrow('left');
        await vi.advanceTimersByTimeAsync(100);
        expect(openSpy).not.toHaveBeenCalled();
        // Second arrow resets the timer.
        fireAltArrow('left');
        // Another 150ms — would have fired the first
        // arrow's debounce (total 250ms), but the second
        // reset means we need a full 200ms from the
        // second arrow.
        await vi.advanceTimersByTimeAsync(150);
        expect(openSpy).not.toHaveBeenCalled();
        // Push past the second timer.
        await vi.advanceTimersByTimeAsync(100);
      } finally {
        vi.useRealTimers();
      }
      await settle(shell);
      expect(openSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe('viewer background inset', () => {
    /**
     * Give the dialog the rect it would have if layout ran.
     * jsdom reports zeros for every element, and
     * `syncViewerInset` treats a zero-width rect as "no
     * strip to reserve", so without a stub every case here
     * would pass for the wrong reason.
     *
     * `left`/`width` are the inputs; `right` is derived the
     * way a real rect derives it, because that's the value
     * the inset is taken from.
     */
    function stubDialogRect(shell, { left, width }) {
      const dialog = shell.shadowRoot.querySelector('.dialog');
      dialog.getBoundingClientRect = () => ({
        left, top: 0, width, height: 800,
        right: left + width, bottom: 800,
        x: left, y: 0,
        toJSON() {},
      });
      return dialog;
    }

    function insetOf(shell) {
      return shell.shadowRoot
        .querySelector('.viewer-background')
        .style.getPropertyValue('--viewer-inset-left');
    }

    it('reserves the docked dialog width', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      // 400 wide plus the 1px border-right: the measured
      // right edge is what the viewers must clear, which is
      // why the inset is read from the rect rather than
      // recomputed from _dockedWidth.
      stubDialogRect(shell, { left: 0, width: 401 });
      expect(syncViewerInset(shell)).toBe(true);
      expect(insetOf(shell)).toBe('401px');
    });

    it('rounds a fractional right edge', async () => {
      // A percentage width on an odd viewport gives the
      // dialog a sub-pixel right edge; an unrounded value
      // would leave a hairline of the left pane under the
      // dialog's border.
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 400.6 });
      syncViewerInset(shell);
      expect(insetOf(shell)).toBe('401px');
    });

    it('clears the inset when the dialog is undocked', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 401 });
      syncViewerInset(shell);
      expect(insetOf(shell)).toBe('401px');
      // A floating dialog covers the viewer wherever the
      // user dropped it — insetting for it would make the
      // content jump on every drag.
      shell._undockedPos = {
        left: 120, top: 60, width: 500, height: 400,
      };
      await shell.updateComplete;
      expect(
        shell.shadowRoot
          .querySelector('.dialog')
          .classList.contains('floating'),
      ).toBe(true);
      expect(insetOf(shell)).toBe('0px');
    });

    it('clears the inset when the dialog is minimized', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 401 });
      syncViewerInset(shell);
      expect(insetOf(shell)).toBe('401px');
      // Minimized collapses to the tab strip, so the whole
      // viewport goes back to the viewer.
      shell._minimized = true;
      await shell.updateComplete;
      expect(insetOf(shell)).toBe('0px');
    });

    it('ignores a dialog whose rect has left the edge', async () => {
      // Mid-drag the dialog's inline left moves before the
      // `floating` class is guaranteed to be in place. A rect
      // that no longer starts at the viewport edge isn't
      // occluding a full-height strip on the left.
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 200, width: 401 });
      syncViewerInset(shell);
      expect(insetOf(shell)).toBe('0px');
    });

    it('reports no change when the inset is unchanged', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 401 });
      expect(syncViewerInset(shell)).toBe(true);
      // Callers relayout on `true`; a repeat sync must not
      // trigger a refit of viewBoxes and Monaco layout.
      expect(syncViewerInset(shell)).toBe(false);
    });

    it('commits the inset before the viewers relayout', async () => {
      // Ordering matters: the viewers measure their own
      // containers, so a relayout that ran before the
      // background resized would fit to the old width.
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 401 });
      const seen = [];
      const svg = shell.shadowRoot.querySelector('aic-svg-viewer');
      const diff = shell.shadowRoot.querySelector('aic-diff-viewer');
      svg.relayout = () => seen.push(insetOf(shell));
      diff.relayout = () => seen.push(insetOf(shell));
      relayoutViewers(shell);
      expect(seen).toEqual(['401px', '401px']);
    });

    it('relayouts the viewers when the docked width changes', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      stubDialogRect(shell, { left: 0, width: 401 });
      const scheduleSpy = vi.spyOn(shell, '_scheduleViewerRelayout');
      shell._dockedWidth = 600;
      await shell.updateComplete;
      expect(insetOf(shell)).toBe('401px');
      expect(scheduleSpy).toHaveBeenCalled();
      // A re-render that can't move the dialog's right edge
      // must not force a layout read.
      scheduleSpy.mockClear();
      shell.activeTab = 'files';
      await shell.updateComplete;
      expect(scheduleSpy).not.toHaveBeenCalled();
    });
  });
});