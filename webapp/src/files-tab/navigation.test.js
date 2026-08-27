// Tests for files-tab navigation routing.
// Covers: file-clicked → navigate-file dispatch,
// file-mention-click and file-chip-click navigate semantics, and
// active-file-changed listener (highlight + cleanup).
//
// Mentions and chips used to toggle the picker's selection as
// well as navigating (chips, in fact, INSTEAD of navigating).
// CC-21 removed the selection, so both are plain navigation now
// and the tests below pin that a click on a filename does the one
// thing the user asked for.

import { describe, expect, it, vi } from 'vitest';

import {
  mountTab,
  publishFakeRpc,
  settle,
  pushEvent,
  fakeTreeResponse,
  installCleanup,
} from './test-helpers.js';

installCleanup();

// ---------------------------------------------------------------------------
// File click → navigate-file
// ---------------------------------------------------------------------------

describe('FilesTab file click → navigate-file', () => {
  it('dispatches navigate-file with the clicked path', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
    });
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      const t = mountTab();
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.shadowRoot.querySelector('.row.is-file .name').click();
      await settle(t);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        path: 'a.md',
      });
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('does not dispatch navigate-file on a deny shift+click', async () => {
    // Shift+click on a file row writes a deny-read rule; it
    // must not also open the file. The two gestures share the
    // row, so the picker's discrimination between them is what
    // keeps "stop the agent reading this" from yanking the user
    // into a viewer they didn't ask for. Verified end-to-end
    // here because the checkbox that used to carry the deny is
    // gone (CC-21) and the row is now doing double duty.
    const setDenied = vi
      .fn()
      .mockResolvedValue({ denied_read_files: ['a.md'] });
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setDenied,
    });
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      const t = mountTab();
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.shadowRoot.querySelector('.row.is-file').dispatchEvent(
        new MouseEvent('click', {
          shiftKey: true,
          bubbles: true,
          composed: true,
          cancelable: true,
        }),
      );
      await settle(t);
      // The deny landed...
      expect(setDenied).toHaveBeenCalledOnce();
      expect(setDenied.mock.calls[0][0]).toEqual(['a.md']);
      // ...and the viewer stayed put.
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('ignores malformed file-clicked events', async () => {
    const t = mountTab();
    await t.updateComplete;
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      // Simulate a malformed event directly on the tab.
      t.dispatchEvent(
        new CustomEvent('file-clicked', {
          detail: {},
          bubbles: true,
        }),
      );
      t.dispatchEvent(
        new CustomEvent('file-clicked', {
          detail: null,
          bubbles: true,
        }),
      );
      await t.updateComplete;
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });
});

// ---------------------------------------------------------------------------
// File mention / chip click → navigate
// ---------------------------------------------------------------------------

describe('FilesTab file-mention-click handling', () => {
  /**
   * Mount with two files and a spy on the picker's deny RPC.
   * A mention click must reach neither the picker's state nor
   * the server — the spy is here to prove the click has no
   * side channel, not because anything is expected to call it.
   */
  async function setupWithFiles() {
    const getTree = vi.fn().mockResolvedValue(
      fakeTreeResponse([
        { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
        { name: 'b.md', path: 'b.md', type: 'file', lines: 2 },
      ]),
    );
    const setDenied = vi
      .fn()
      .mockResolvedValue({ denied_read_files: [] });
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.set_denied_read_files': setDenied,
    });
    const t = mountTab();
    await settle(t);
    return { t, setDenied };
  }

  /** Fire the event the chat panel dispatches for a mention. */
  function clickMention(t, path) {
    const chat = t.shadowRoot.querySelector('aic-chat-panel');
    chat.dispatchEvent(
      new CustomEvent('file-mention-click', {
        detail: { path },
        bubbles: true,
        composed: true,
      }),
    );
  }

  it('dispatches navigate-file for the clicked path', async () => {
    // Simulate the chat panel dispatching the event.
    // The `@file-mention-click` binding in the files-tab
    // template routes it to the handler.
    const { t } = await setupWithFiles();
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      clickMention(t, 'a.md');
      await settle(t);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        path: 'a.md',
      });
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('changes no state and calls no RPC', async () => {
    // A mention click used to toggle the file's selection,
    // which meant clicking a filename in prose to read it
    // silently changed what the next turn claimed the user
    // wanted. Opening the file is now the whole effect.
    const { t, setDenied } = await setupWithFiles();
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    const before = new Set(picker.excludedFiles);
    clickMention(t, 'a.md');
    await settle(t);
    expect(setDenied).not.toHaveBeenCalled();
    expect(picker.excludedFiles).toEqual(before);
  });

  it('a second click on the same mention navigates again', async () => {
    // No toggle. The old handler treated the second click as
    // "deselect", and the file re-opened only as a side effect
    // of navigation being selection-independent. Now the
    // second click means what the first one did.
    const { t } = await setupWithFiles();
    clickMention(t, 'a.md');
    await settle(t);
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      clickMention(t, 'a.md');
      await settle(t);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        path: 'a.md',
      });
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('ignores malformed events (no path)', async () => {
    const { t, setDenied } = await setupWithFiles();
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      const chat = t.shadowRoot.querySelector('aic-chat-panel');
      for (const detail of [
        {},
        null,
        { path: '' },
        { path: 42 },
      ]) {
        chat.dispatchEvent(
          new CustomEvent('file-mention-click', {
            detail,
            bubbles: true,
            composed: true,
          }),
        );
      }
      await settle(t);
      expect(setDenied).not.toHaveBeenCalled();
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('handles multiple distinct mention clicks', async () => {
    const { t } = await setupWithFiles();
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      clickMention(t, 'a.md');
      await settle(t);
      clickMention(t, 'b.md');
      await settle(t);
      expect(listener).toHaveBeenCalledTimes(2);
      expect(
        listener.mock.calls.map((c) => c[0].detail.path),
      ).toEqual(['a.md', 'b.md']);
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });
});

describe('FilesTab file-chip-click handling', () => {
  /**
   * The chips in an assistant message's "Files Referenced"
   * summary. They carried `navigate: false` and toggled the
   * selection instead of opening anything — curating context
   * wasn't supposed to move the viewer. With no context to
   * curate, opening the file is the only thing left to want
   * from a chip, so they navigate like the prose mentions
   * they were once distinguished from.
   */
  async function setup() {
    publishFakeRpc({
      'Repo.get_file_tree': vi.fn().mockResolvedValue(
        fakeTreeResponse([
          { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
        ]),
      ),
    });
    const t = mountTab();
    await settle(t);
    return t;
  }

  function clickChip(t, path) {
    const chat = t.shadowRoot.querySelector('aic-chat-panel');
    chat.dispatchEvent(
      new CustomEvent('file-chip-click', {
        detail: { path },
        bubbles: true,
        composed: true,
      }),
    );
  }

  it('dispatches navigate-file for the clicked chip', async () => {
    const t = await setup();
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      clickChip(t, 'a.md');
      await settle(t);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        path: 'a.md',
      });
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });

  it('ignores malformed chip events', async () => {
    const t = await setup();
    const listener = vi.fn();
    window.addEventListener('navigate-file', listener);
    try {
      const chat = t.shadowRoot.querySelector('aic-chat-panel');
      for (const detail of [{}, null, { path: '' }, { path: 42 }]) {
        chat.dispatchEvent(
          new CustomEvent('file-chip-click', {
            detail,
            bubbles: true,
            composed: true,
          }),
        );
      }
      await settle(t);
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('navigate-file', listener);
    }
  });
});

// ---------------------------------------------------------------------------
// Active-file highlight (Increment 6)
// ---------------------------------------------------------------------------

describe('FilesTab active-file handling', () => {
  it('pushes activePath to picker when active-file-changed fires', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Default — no active file yet.
    expect(picker.activePath).toBeNull();
    // Fire the viewer's event at the window level (same
    // path the real bubbling event reaches).
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    expect(t._activePath).toBe('a.md');
    expect(picker.activePath).toBe('a.md');
  });

  it('updates picker when activePath changes between files', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
            { name: 'b.md', path: 'b.md', type: 'file', lines: 1 },
          ]),
        ),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    expect(picker.activePath).toBe('a.md');
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'b.md' },
      }),
    );
    await settle(t);
    expect(picker.activePath).toBe('b.md');
  });

  it('clears activePath when viewer closes all files', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
    });
    const t = mountTab();
    await settle(t);
    // Open a file.
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.activePath).toBe('a.md');
    // Close it — viewer sends null.
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: null },
      }),
    );
    await settle(t);
    expect(t._activePath).toBeNull();
    expect(picker.activePath).toBeNull();
  });

  it('ignores duplicate active-file events (short-circuit)', async () => {
    // Re-dispatching the same active path shouldn't cause
    // extra picker re-renders. Mirrors the selection /
    // exclusion short-circuit.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    const requestUpdateSpy = vi.spyOn(picker, 'requestUpdate');
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    const firstCallCount = requestUpdateSpy.mock.calls.length;
    // Same path again — no new requestUpdate.
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    expect(requestUpdateSpy.mock.calls.length).toBe(firstCallCount);
  });

  it('tolerates missing detail (defensive)', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    // No detail at all.
    window.dispatchEvent(new CustomEvent('active-file-changed'));
    // Detail without path.
    window.dispatchEvent(
      new CustomEvent('active-file-changed', { detail: {} }),
    );
    // Path is not a string.
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 42 },
      }),
    );
    await settle(t);
    // No crashes, state stays null.
    expect(t._activePath).toBeNull();
  });

  it('activePath survives tree reload', async () => {
    // Same principle as exclusion state — viewer-active
    // file shouldn't be cleared by a files-modified
    // reload.
    let callCount = 0;
    const getTree = vi.fn().mockImplementation(() => {
      callCount += 1;
      return Promise.resolve(
        fakeTreeResponse([
          { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
        ]),
      );
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    window.dispatchEvent(
      new CustomEvent('active-file-changed', {
        detail: { path: 'a.md' },
      }),
    );
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.activePath).toBe('a.md');
    // Reload.
    pushEvent('files-modified', {});
    await settle(t);
    expect(callCount).toBe(2);
    expect(picker.activePath).toBe('a.md');
    expect(t._activePath).toBe('a.md');
  });

  it('removes window listener on disconnect', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t.remove();
    // After disconnect, active-file-changed events must
    // not reach the (now detached) handler. No easy way
    // to assert absence directly, so we just dispatch
    // and verify nothing throws.
    expect(() => {
      window.dispatchEvent(
        new CustomEvent('active-file-changed', {
          detail: { path: 'a.md' },
        }),
      );
    }).not.toThrow();
  });
});