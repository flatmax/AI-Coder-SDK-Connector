// Tests for webapp/src/files-tab.js — read-denial slice.
// Covers: the third checkbox state's sync (picker ↔
// server) now that it writes `Read` deny rules rather
// than an index filter (CC-14), and the absence of the
// L0 confirmation dialog that used to gate it.

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

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
// Exclusion sync (Increment 5)
// ---------------------------------------------------------------------------

describe('FilesTab exclusion sync', () => {
  it('pushes excludedFiles to picker on every tree load', async () => {
    // Initial empty excludedFiles still reaches the picker,
    // so `Set.has()` during render works without guards.
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
    expect(picker.excludedFiles).toBeInstanceOf(Set);
    expect(picker.excludedFiles.size).toBe(0);
  });

  it('calls set_denied_read_files when picker dispatches exclusion-changed', async () => {
    const setExcluded = vi.fn().mockResolvedValue(['a.md']);
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setExcluded,
    });
    const t = mountTab();
    await settle(t);
    // Simulate the picker dispatching the event (same path
    // the real shift+click flow uses).
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.md'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setExcluded).toHaveBeenCalledOnce();
    expect(setExcluded.mock.calls[0][0]).toEqual(['a.md']);
    // One argument. The deny list is repo-wide and
    // authoritative — there is no cache flag to pass and
    // no per-agent variant to route to.
    expect(setExcluded.mock.calls[0].length).toBe(1);
  });

  it('updates internal state and picker prop on exclusion change', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': vi
        .fn()
        .mockResolvedValue(['a.md']),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.md'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(t._excludedFiles.has('a.md')).toBe(true);
    expect(picker.excludedFiles.has('a.md')).toBe(true);
  });

  it('short-circuits redundant exclusion updates', async () => {
    // Re-dispatching the same excluded set should not
    // trigger another server RPC. Mirrors the selection
    // short-circuit and protects against future broadcast
    // loopback when collab-side excluded-state broadcast
    // lands.
    const setExcluded = vi.fn().mockResolvedValue(['a.md']);
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setExcluded,
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.md'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setExcluded).toHaveBeenCalledTimes(1);
    // Same set again → no-op.
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.md'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setExcluded).toHaveBeenCalledTimes(1);
  });

  it('surfaces restricted error as warning toast', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': vi
        .fn()
        .mockResolvedValue({
          error: 'restricted',
          reason: 'Participants cannot change read denial',
        }),
    });
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const t = mountTab();
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.dispatchEvent(
        new CustomEvent('exclusion-changed', {
          detail: { excludedFiles: ['a.md'] },
          bubbles: true,
          composed: true,
        }),
      );
      await settle(t);
      const detail = toastListener.mock.calls.at(-1)[0].detail;
      expect(detail.type).toBe('warning');
      expect(detail.message).toContain('Participants');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('surfaces RPC rejection as error toast', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': vi
        .fn()
        .mockRejectedValue(new Error('exclusion boom')),
    });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const t = mountTab();
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.dispatchEvent(
        new CustomEvent('exclusion-changed', {
          detail: { excludedFiles: ['a.md'] },
          bubbles: true,
          composed: true,
        }),
      );
      await settle(t);
      const detail = toastListener.mock.calls.at(-1)[0].detail;
      expect(detail.type).toBe('error');
      expect(detail.message).toContain('exclusion boom');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
      consoleSpy.mockRestore();
    }
  });

  it('ignores malformed exclusion-changed payloads', async () => {
    const setExcluded = vi.fn().mockResolvedValue([]);
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
      'ClaudeCodeService.set_denied_read_files': setExcluded,
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Defensive — malformed events shouldn't reach the
    // RPC or mutate state.
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: null },
        bubbles: true,
        composed: true,
      }),
    );
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: 'not an array' },
        bubbles: true,
        composed: true,
      }),
    );
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: null,
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setExcluded).not.toHaveBeenCalled();
    expect(t._excludedFiles.size).toBe(0);
  });

  it('tree reload preserves exclusion state', async () => {
    // After a commit (files-modified), the tree reloads
    // but exclusion state shouldn't be wiped. The
    // _pushChildProps path must assign excludedFiles
    // alongside the new tree.
    let callCount = 0;
    const getTree = vi.fn().mockImplementation(() => {
      callCount += 1;
      return Promise.resolve(
        fakeTreeResponse([
          { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
        ]),
      );
    });
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.set_denied_read_files': vi
        .fn()
        .mockResolvedValue(['a.md']),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Exclude a.md.
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.md'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(picker.excludedFiles.has('a.md')).toBe(true);
    // Reload tree.
    pushEvent('files-modified', {});
    await settle(t);
    expect(callCount).toBe(2);
    // Exclusion state survived.
    expect(picker.excludedFiles.has('a.md')).toBe(true);
    expect(t._excludedFiles.has('a.md')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// What replaced the L0-exclude confirmation dialog
// ---------------------------------------------------------------------------
//
// Every user-driven denial used to open a dialog asking
// whether to invalidate the L0 cache now (a ~100K-token
// prefix rewrite) or leave the structural map stale. There
// is no map and no L0 cache, so the dialog went with them
// in conversion phase 3. These tests pin its absence, and
// pin the one honest thing it did say: the rule is not
// instant, because the CLI reads its own settings sources.

describe('FilesTab read denial — no L0 prompt', () => {
  /** Dispatch an exclusion-changed event from the picker. */
  function fireExclusionChanged(tab, paths) {
    const picker = tab.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: paths },
        bubbles: true,
        composed: true,
      }),
    );
  }

  async function setupTab(denyResult) {
    const setDenied = vi.fn().mockResolvedValue(
      denyResult === undefined
        ? {
          denied_read_files: [],
          settings_file: '/repo/.claude/settings.local.json',
          takes_effect:
              "on the CLI's next read of its settings sources",
        }
        : denyResult,
    );
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
            { name: 'b.md', path: 'b.md', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setDenied,
    });
    const t = mountTab();
    await settle(t);
    return { t, setDenied };
  }

  it('shift+click denial applies straight through — no dialog', async () => {
    const { t, setDenied } = await setupTab();
    fireExclusionChanged(t, ['a.md']);
    await settle(t);
    expect(t.shadowRoot.querySelector('.l0-dialog')).toBeNull();
    expect(
      t.shadowRoot.querySelector('.l0-dialog-backdrop'),
    ).toBeNull();
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0]).toEqual(['a.md']);
    expect(t._excludedFiles.has('a.md')).toBe(true);
  });

  it('stores no preference in localStorage', async () => {
    // The pref key existed only to remember an answer to
    // the dialog. Nothing should write it now, including
    // under its old name — a stale value must not resurrect
    // any behaviour.
    const { t } = await setupTab();
    fireExclusionChanged(t, ['a.md']);
    await settle(t);
    expect(
      localStorage.getItem('aic-dc-l0-exclude-pref'),
    ).toBeNull();
  });

  it('reports the takes-effect caveat once per session', async () => {
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const { t } = await setupTab();
      fireExclusionChanged(t, ['a.md']);
      await settle(t);
      const first = toastListener.mock.calls.at(-1)[0].detail;
      expect(first.type).toBe('info');
      expect(first.message).toContain('next read');
      const countAfterFirst = toastListener.mock.calls.length;
      // A second denial is the same caveat. Saying it again
      // on every checkbox tick would train the user to
      // ignore it.
      fireExclusionChanged(t, ['a.md', 'b.md']);
      await settle(t);
      expect(toastListener.mock.calls.length).toBe(countAfterFirst);
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('says nothing when the deny list is emptied', async () => {
    // Clearing every denial is not a denial; there is no
    // "you are now denied N files" to report.
    const { t } = await setupTab();
    fireExclusionChanged(t, ['a.md']);
    await settle(t);
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      fireExclusionChanged(t, []);
      await settle(t);
      expect(toastListener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('context-menu deny writes the rule', async () => {
    // This test also asserted that denying a file deselected
    // it — pointing the agent at a file it may not read was a
    // contradiction the two states could get into. CC-21 left
    // deny-read as the only state a row carries, so there is
    // nothing to reconcile it against.
    const { t, setDenied } = await setupTab();
    t._dispatchExclude('a.md');
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0]).toEqual(['a.md']);
    expect(t._excludedFiles.has('a.md')).toBe(true);
  });

  it('context-menu allow drops the path from the rule', async () => {
    const { t, setDenied } = await setupTab();
    t._excludedFiles = new Set(['a.md', 'b.md']);
    t._dispatchInclude('a.md');
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
    // The RPC replaces the whole list, so the removal is
    // expressed as the surviving list.
    expect(setDenied.mock.calls[0][0]).toEqual(['b.md']);
    expect(t._excludedFiles.has('a.md')).toBe(false);
  });

  it('deny-all over a directory sends one rule set', async () => {
    const { t, setDenied } = await setupTab();
    t._dispatchExcludeAll('');  // empty path = repo root
    await settle(t);
    expect(t.shadowRoot.querySelector('.l0-dialog')).toBeNull();
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0].sort()).toEqual([
      'a.md', 'b.md',
    ]);
  });

  it('allow-all empties the rule set', async () => {
    const { t, setDenied } = await setupTab();
    t._excludedFiles = new Set(['a.md', 'b.md']);
    t._dispatchIncludeAll('');
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0]).toEqual([]);
  });

  it('an agent tab writes the same repo-wide rule', async () => {
    // A per-agent deny set was a filter on a per-agent
    // prompt. SDK subagents share the session's settings
    // sources, so there is one rule set and one RPC.
    const { t, setDenied } = await setupTab();
    t._activeTabId = 'agent-frontend';
    if (!t._excludedFilesByTab.has('agent-frontend')) {
      t._excludedFilesByTab.set('agent-frontend', new Set());
    }
    fireExclusionChanged(t, ['a.md']);
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0]).toEqual(['a.md']);
    expect(setDenied.mock.calls[0].length).toBe(1);
  });

  it('surfaces a non-restricted error from the RPC', async () => {
    // `write_denied_read_files` raises ValueError for a path
    // it refuses (the service turns it into `{error}`). A
    // silent failure here would leave the checkbox ticked
    // over a file the agent can still read.
    const { t } = await setupTab({ error: 'path escapes the repo' });
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      fireExclusionChanged(t, ['../outside.md']);
      await settle(t);
      const detail = toastListener.mock.calls.at(-1)[0].detail;
      expect(detail.type).toBe('error');
      expect(detail.message).toContain('escapes the repo');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });
});
