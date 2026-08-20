// Tests for webapp/src/file-picker/index.js — read denial, path
// insertion, and event bubbling.
//
// This file was `selection.test.js` until CC-21. It covered the
// checkbox column: a file checkbox emitting `selection-changed`, a
// directory checkbox computing checked/indeterminate from its
// descendants, and shift+click on a checkbox writing a deny rule.
// The checkbox and the selection went; the deny rule stayed and
// moved onto the row itself, so what is left here is the part that
// still exists — with the gesture retargeted from `.checkbox` to
// `.row`.

import {
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import {
  file,
  dir,
  rootOf,
  mountPicker,
  installCleanup,
} from './test-helpers.js';

installCleanup();

describe('FilePicker component', () => {
  describe('file rows', () => {
    it('clicking the file name dispatches file-clicked', async () => {
      const tree = rootOf([file('a.md')]);
      const p = mountPicker({ tree });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('file-clicked', listener);
      p.shadowRoot.querySelector('.row.is-file .name').click();
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        path: 'a.md',
      });
    });

    it('has no checkbox to click', async () => {
      // The column is gone, not hidden. A test asserting on
      // `.checkbox` would pass vacuously against a component that
      // rendered one somewhere else, so assert the absence
      // directly and once.
      const tree = rootOf([
        dir('src', [file('src/a.md')]),
      ]);
      const p = mountPicker({ tree, excludedFiles: new Set() });
      await p.updateComplete;
      expect(
        p.shadowRoot.querySelectorAll('.checkbox'),
      ).toHaveLength(0);
      expect(
        p.shadowRoot.querySelector('input[type="checkbox"]'),
      ).toBeNull();
    });
  });

  describe('directory rows', () => {
    it('plain click toggles expansion', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({ tree });
      await p.updateComplete;
      const row = p.shadowRoot.querySelector('.row.is-dir');
      expect(row.getAttribute('aria-expanded')).toBe('false');
      row.click();
      await p.updateComplete;
      expect(
        p.shadowRoot
          .querySelector('.row.is-dir')
          .getAttribute('aria-expanded'),
      ).toBe('true');
    });

    it('marks all-excluded and some-excluded states', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['src/a.md']),
      });
      await p.updateComplete;
      let row = p.shadowRoot.querySelector('.row.is-dir');
      expect(row.classList.contains('some-excluded')).toBe(true);
      expect(row.classList.contains('all-excluded')).toBe(false);
      p.excludedFiles = new Set(['src/a.md', 'src/b.md']);
      await p.updateComplete;
      row = p.shadowRoot.querySelector('.row.is-dir');
      expect(row.classList.contains('all-excluded')).toBe(true);
      expect(row.classList.contains('some-excluded')).toBe(false);
    });

    it('shows the ✕ badge only when partially denied', async () => {
      // A fully-denied directory is already struck through by the
      // `all-excluded` class; the badge is there to say "something
      // in here is denied and you cannot see it from the closed
      // row".
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['src/a.md']),
      });
      await p.updateComplete;
      expect(
        p.shadowRoot.querySelector('.row.is-dir .excluded-badge'),
      ).toBeTruthy();
      p.excludedFiles = new Set();
      await p.updateComplete;
      expect(
        p.shadowRoot.querySelector('.row.is-dir .excluded-badge'),
      ).toBeNull();
    });
  });

  // ---------------------------------------------------------------
  // Read denial — shift+click on the row (CC-14, retargeted CC-21)
  // ---------------------------------------------------------------

  describe('read denial', () => {
    // Helper — simulate a shift+click on a row. jsdom doesn't
    // honour shiftKey on a synthetic click(), so we dispatch a
    // MouseEvent directly with the modifier set.
    function shiftClick(el) {
      const event = new MouseEvent('click', {
        bubbles: true,
        cancelable: true,
        shiftKey: true,
      });
      el.dispatchEvent(event);
      return event;
    }

    it('excluded file gets the is-excluded class', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['a.md']),
      });
      await p.updateComplete;
      const row = p.shadowRoot.querySelector('.row.is-file');
      expect(row.classList.contains('is-excluded')).toBe(true);
    });

    it('non-excluded file does not get the is-excluded class', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const row = p.shadowRoot.querySelector('.row.is-file');
      expect(row.classList.contains('is-excluded')).toBe(false);
    });

    it('excluded file shows the ✕ badge', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['a.md']),
      });
      await p.updateComplete;
      const badge = p.shadowRoot.querySelector('.excluded-badge');
      expect(badge).toBeTruthy();
      expect(badge.textContent).toContain('✕');
    });

    it('non-excluded file does not show the ✕ badge', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      expect(
        p.shadowRoot.querySelector('.excluded-badge'),
      ).toBeNull();
    });

    it('denied file tooltip says how to allow it again', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['a.md']),
      });
      await p.updateComplete;
      const row = p.shadowRoot.querySelector('.row.is-file');
      const title = row.getAttribute('title');
      expect(title).toContain('denied read');
      expect(title).toContain('shift+click to allow');
    });

    it('normal file tooltip names all three gestures', async () => {
      // The row is now the whole control surface — open, insert,
      // deny — and the tooltip is the only place that says so.
      // If a gesture stops being advertised it stops being
      // discoverable, which is how the checkbox got built in the
      // first place.
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({ tree, excludedFiles: new Set() });
      await p.updateComplete;
      const title = p.shadowRoot
        .querySelector('.row.is-file')
        .getAttribute('title');
      expect(title).toContain('click to open');
      expect(title).toContain('middle-click to insert the path');
      expect(title).toContain('shift+click to deny');
    });

    it('directory tooltip adapts to the denial state', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['src/a.md', 'src/b.md']),
      });
      await p.updateComplete;
      expect(
        p.shadowRoot
          .querySelector('.row.is-dir')
          .getAttribute('title'),
      ).toContain('shift+click to allow all');
      p.excludedFiles = new Set(['src/a.md']);
      await p.updateComplete;
      expect(
        p.shadowRoot
          .querySelector('.row.is-dir')
          .getAttribute('title'),
      ).toContain('denied read on some files');
    });

    it('shift+click on a file row denies it', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-file'));
      expect(listener).toHaveBeenCalledOnce();
      expect(
        listener.mock.calls[0][0].detail.excludedFiles,
      ).toEqual(['a.md']);
    });

    it('shift+click on a denied file row allows it again', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['a.md']),
      });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-file'));
      expect(listener).toHaveBeenCalledOnce();
      expect(
        listener.mock.calls[0][0].detail.excludedFiles,
      ).toEqual([]);
    });

    it('shift+click on a file row does NOT open the file', async () => {
      // The gesture that writes a permission rule must not also
      // navigate — a user denying a file they can't be shown is
      // not asking to be shown it.
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const clicked = vi.fn();
      p.addEventListener('file-clicked', clicked);
      shiftClick(p.shadowRoot.querySelector('.row.is-file'));
      expect(clicked).not.toHaveBeenCalled();
    });

    it('plain click on a file row opens it and denies nothing', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const clicked = vi.fn();
      const exclusion = vi.fn();
      p.addEventListener('file-clicked', clicked);
      p.addEventListener('exclusion-changed', exclusion);
      p.shadowRoot.querySelector('.row.is-file').click();
      expect(clicked).toHaveBeenCalledOnce();
      expect(exclusion).not.toHaveBeenCalled();
    });

    it('shift+click on a directory row denies every descendant', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-dir'));
      expect(listener).toHaveBeenCalledOnce();
      const excluded =
        listener.mock.calls[0][0].detail.excludedFiles;
      expect(excluded.sort()).toEqual(['src/a.md', 'src/b.md']);
    });

    it('shift+click on an all-denied directory allows the subtree', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['src/a.md', 'src/b.md']),
      });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-dir'));
      expect(listener).toHaveBeenCalledOnce();
      expect(
        listener.mock.calls[0][0].detail.excludedFiles,
      ).toEqual([]);
    });

    it('shift+click on a partially-denied directory denies the rest', async () => {
      // Anything short of all-denied flips to fully denied. The
      // alternative — toggling each file independently — would
      // make one gesture on a mixed directory both deny and allow,
      // which is not a thing a user can mean.
      const tree = rootOf([
        dir('src', [file('src/a.md'), file('src/b.md')]),
      ]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(['src/a.md']),
      });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-dir'));
      expect(
        listener.mock.calls[0][0].detail.excludedFiles.sort(),
      ).toEqual(['src/a.md', 'src/b.md']);
    });

    it('shift+click on a directory row does NOT expand it', async () => {
      const tree = rootOf([
        dir('src', [file('src/a.md')]),
      ]);
      const p = mountPicker({ tree, excludedFiles: new Set() });
      await p.updateComplete;
      shiftClick(p.shadowRoot.querySelector('.row.is-dir'));
      await p.updateComplete;
      expect(
        p.shadowRoot
          .querySelector('.row.is-dir')
          .getAttribute('aria-expanded'),
      ).toBe('false');
    });

    it('shift+click on an empty directory is a no-op', async () => {
      const tree = rootOf([dir('empty', [])]);
      const p = mountPicker({ tree, excludedFiles: new Set() });
      await p.updateComplete;
      const listener = vi.fn();
      p.addEventListener('exclusion-changed', listener);
      shiftClick(p.shadowRoot.querySelector('.row.is-dir'));
      expect(listener).not.toHaveBeenCalled();
    });

    it('shift+click calls preventDefault', async () => {
      // Shift+click is the browser's range-select gesture. Left
      // unclaimed it drags a text selection across the tree while
      // the rule is being written.
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const event = shiftClick(
        p.shadowRoot.querySelector('.row.is-file'),
      );
      expect(event.defaultPrevented).toBe(true);
    });

    it('exclusion-changed bubbles across the shadow boundary', async () => {
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({
        tree,
        excludedFiles: new Set(),
      });
      await p.updateComplete;
      const parentListener = vi.fn();
      document.body.addEventListener(
        'exclusion-changed',
        parentListener,
      );
      shiftClick(p.shadowRoot.querySelector('.row.is-file'));
      document.body.removeEventListener(
        'exclusion-changed',
        parentListener,
      );
      expect(parentListener).toHaveBeenCalledOnce();
    });

    it('excludedFiles prop default is an empty Set', async () => {
      // Constructor default — tests without the prop
      // explicitly set shouldn't crash.
      const tree = rootOf([file('a.md', 5)]);
      const p = mountPicker({ tree });
      await p.updateComplete;
      expect(p.excludedFiles).toBeInstanceOf(Set);
      expect(p.excludedFiles.size).toBe(0);
    });

    it('does not mutate the excludedFiles set it was given', async () => {
      // The orchestrator owns the set; the picker computes the
      // next one and dispatches it. Mutating in place would leave
      // the two disagreeing about what was denied with no event to
      // reconcile them.
      const tree = rootOf([file('a.md', 5)]);
      const original = new Set();
      const p = mountPicker({ tree, excludedFiles: original });
      await p.updateComplete;
      shiftClick(p.shadowRoot.querySelector('.row.is-file'));
      expect(original.size).toBe(0);
    });
  });

  describe('bubbling', () => {
    it('file-clicked bubbles out of the shadow root', async () => {
      const tree = rootOf([file('a.md')]);
      const p = mountPicker({ tree });
      await p.updateComplete;
      const parentListener = vi.fn();
      document.body.addEventListener(
        'file-clicked',
        parentListener,
      );
      p.shadowRoot.querySelector('.row.is-file .name').click();
      document.body.removeEventListener(
        'file-clicked',
        parentListener,
      );
      expect(parentListener).toHaveBeenCalledOnce();
    });
  });
});
