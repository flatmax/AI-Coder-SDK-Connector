// Tests for the files-tab first-load auto-expand rule: the
// ancestor directories of every changed file (modified ∪ staged ∪
// untracked ∪ deleted) open on the first successful tree load, so
// work in progress is visible without walking the tree.
//
// This file was `selection-sync.test.js` until CC-21, and most of
// it was about the other half of that rule — the changed files
// were also auto-*selected*, pushed to the server with
// `set_selected_files`, and re-applied from inbound
// `filesChanged` broadcasts. Selection is gone, and with it the
// outbound RPC and the broadcast. What survives is the honest
// half: expanding a directory says "there is something here to
// look at", which is all git knowing a file changed licenses
// anyone to say.

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

describe('FilesTab first-load auto-expand', () => {
  it('expands ancestor directories of changed files', async () => {
    // A user opening the app with pending changes wants to SEE
    // the changed files in the tree, not have them hidden inside
    // collapsed dirs.
    const getTree = vi.fn().mockResolvedValue({
      tree: {
        name: 'repo',
        path: '',
        type: 'dir',
        lines: 0,
        children: [
          {
            name: 'src',
            path: 'src',
            type: 'dir',
            lines: 0,
            children: [
              {
                name: 'inner',
                path: 'src/inner',
                type: 'dir',
                lines: 0,
                children: [
                  {
                    name: 'deep.md',
                    path: 'src/inner/deep.md',
                    type: 'file',
                    lines: 1,
                  },
                ],
              },
            ],
          },
        ],
      },
      modified: ['src/inner/deep.md'],
      staged: [],
      untracked: [],
      deleted: [],
      diff_stats: {},
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Both ancestors expanded.
    expect(picker._expanded.has('src')).toBe(true);
    expect(picker._expanded.has('src/inner')).toBe(true);
    // The file itself is NOT in the expanded set (it's
    // a file, not a directory).
    expect(picker._expanded.has('src/inner/deep.md')).toBe(false);
  });

  it('unions all four change categories', async () => {
    // Modified, staged, untracked and deleted all count as work
    // in progress worth showing. Clean files' ancestors are not
    // opened — that is the whole point of the rule.
    const getTree = vi.fn().mockResolvedValue({
      tree: {
        name: 'repo',
        path: '',
        type: 'dir',
        lines: 0,
        children: [
          ...['m', 's', 'u', 'd', 'clean'].map((k) => ({
            name: k,
            path: k,
            type: 'dir',
            lines: 0,
            children: [
              {
                name: 'f.md',
                path: `${k}/f.md`,
                type: 'file',
                lines: 1,
              },
            ],
          })),
        ],
      },
      modified: ['m/f.md'],
      staged: ['s/f.md'],
      untracked: ['u/f.md'],
      deleted: ['d/f.md'],
      diff_stats: {},
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker._expanded.has('m')).toBe(true);
    expect(picker._expanded.has('s')).toBe(true);
    expect(picker._expanded.has('u')).toBe(true);
    expect(picker._expanded.has('d')).toBe(true);
    expect(picker._expanded.has('clean')).toBe(false);
  });

  it('expands ancestors for every changed file', async () => {
    // Two files in different subtrees — both paths'
    // ancestors get expanded.
    const getTree = vi.fn().mockResolvedValue({
      tree: {
        name: 'repo',
        path: '',
        type: 'dir',
        lines: 0,
        children: [
          {
            name: 'src',
            path: 'src',
            type: 'dir',
            lines: 0,
            children: [
              {
                name: 'a.md',
                path: 'src/a.md',
                type: 'file',
                lines: 1,
              },
            ],
          },
          {
            name: 'tests',
            path: 'tests',
            type: 'dir',
            lines: 0,
            children: [
              {
                name: 'b.md',
                path: 'tests/b.md',
                type: 'file',
                lines: 1,
              },
            ],
          },
        ],
      },
      modified: ['src/a.md'],
      untracked: ['tests/b.md'],
      staged: [],
      deleted: [],
      diff_stats: {},
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker._expanded.has('src')).toBe(true);
    expect(picker._expanded.has('tests')).toBe(true);
  });

  it('a changed top-level file expands nothing', async () => {
    // A file at repo root has no ancestor dirs to
    // expand (other than root itself, which isn't a
    // collapsible node). The expanded set stays empty.
    const getTree = vi.fn().mockResolvedValue({
      tree: {
        name: 'repo',
        path: '',
        type: 'dir',
        lines: 0,
        children: [
          { name: 'a.md', path: 'a.md', type: 'file', lines: 1 },
        ],
      },
      modified: ['a.md'],
      staged: [],
      untracked: [],
      deleted: [],
      diff_stats: {},
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker._expanded.size).toBe(0);
  });

  it('preserves user-expanded directories', async () => {
    // The expansion set is a UNION with any prior user
    // action. Shouldn't happen in practice (picker is
    // empty before first load), but the contract is
    // clear: we add, never replace.
    const getTree = vi.fn().mockResolvedValue({
      tree: {
        name: 'repo',
        path: '',
        type: 'dir',
        lines: 0,
        children: [
          {
            name: 'src',
            path: 'src',
            type: 'dir',
            lines: 0,
            children: [
              { name: 'a.md', path: 'src/a.md', type: 'file', lines: 1 },
            ],
          },
          {
            name: 'docs',
            path: 'docs',
            type: 'dir',
            lines: 0,
            children: [
              { name: 'b.md', path: 'docs/b.md', type: 'file', lines: 1 },
            ],
          },
        ],
      },
      modified: ['src/a.md'],
      staged: [],
      untracked: [],
      deleted: [],
      diff_stats: {},
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await t.updateComplete;
    // Pre-seed picker expansion (simulating an
    // impossible-in-practice race, just to pin the
    // union contract).
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker._expanded = new Set(['docs']);
    await settle(t);
    // After auto-expand, both docs (user) and src
    // (auto-expand) are expanded.
    expect(picker._expanded.has('docs')).toBe(true);
    expect(picker._expanded.has('src')).toBe(true);
  });

  it('runs exactly once per component lifetime', async () => {
    // The second tree load (from files-modified, e.g. after a
    // commit) must not re-expand. A user who deliberately
    // collapsed a noisy directory would see it spring back open
    // otherwise — exactly the "can't get rid of this"
    // frustration the flag prevents.
    let callCount = 0;
    const getTree = vi.fn().mockImplementation(() => {
      callCount += 1;
      return Promise.resolve({
        tree: {
          name: 'repo',
          path: '',
          type: 'dir',
          lines: 0,
          children: [
            {
              name: 'src',
              path: 'src',
              type: 'dir',
              lines: 0,
              children: [
                {
                  name: 'a.md',
                  path: 'src/a.md',
                  type: 'file',
                  lines: 1,
                },
              ],
            },
          ],
        },
        modified: ['src/a.md'],
        staged: [],
        untracked: [],
        deleted: [],
        diff_stats: {},
      });
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker._expanded.has('src')).toBe(true);
    // User collapses it again.
    picker.shadowRoot
      .querySelector('.row.is-dir:not(.is-root)')
      .click();
    await settle(t);
    expect(picker._expanded.has('src')).toBe(false);
    // Simulate a reload (e.g. a commit fires files-modified).
    // The second load must NOT re-expand — the flag is already
    // flipped.
    pushEvent('files-modified', {});
    await settle(t);
    expect(callCount).toBe(2);
    expect(picker._expanded.has('src')).toBe(false);
  });

  it('flag flips synchronously before the auto-expand runs', async () => {
    // Defensive — a re-entrant _loadFileTree call
    // during the auto-expand step (impossible today but
    // cheap to protect against) must not double-fire.
    // We verify by checking the flag state after mount
    // completion — always false, regardless of what
    // happens during the call.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    expect(t._initialAutoExpand).toBe(false);
  });

  it('skipped entirely when tree load fails', async () => {
    // Error path — tree RPC rejects, toast fires, no
    // auto-expand runs. The flag stays true so a
    // subsequent successful reload can still do the
    // initial expansion.
    const getTree = vi
      .fn()
      .mockRejectedValue(new Error('load failed'));
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const t = mountTab();
      await settle(t);
      // Flag UNTOUCHED — the auto-expand code is
      // downstream of the failed await and never ran.
      expect(t._initialAutoExpand).toBe(true);
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('runs on the next successful load after initial failure', async () => {
    // The flag stays true across failed loads; a
    // successful reload triggered by files-modified
    // picks up the auto-expand.
    let callCount = 0;
    const getTree = vi.fn().mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.reject(new Error('transient'));
      }
      return Promise.resolve({
        tree: {
          name: 'repo',
          path: '',
          type: 'dir',
          lines: 0,
          children: [
            {
              name: 'src',
              path: 'src',
              type: 'dir',
              lines: 0,
              children: [
                {
                  name: 'a.md',
                  path: 'src/a.md',
                  type: 'file',
                  lines: 1,
                },
              ],
            },
          ],
        },
        modified: ['src/a.md'],
        staged: [],
        untracked: [],
        deleted: [],
        diff_stats: {},
      });
    });
    publishFakeRpc({ 'Repo.get_file_tree': getTree });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const t = mountTab();
      await settle(t);
      // First load failed → flag still true.
      expect(t._initialAutoExpand).toBe(true);
      // Trigger reload.
      pushEvent('files-modified', {});
      await settle(t);
      // Second load succeeded → auto-expand ran.
      expect(t._initialAutoExpand).toBe(false);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      expect(picker._expanded.has('src')).toBe(true);
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('does not push the changed files anywhere', async () => {
    // The rule used to also auto-*select* the changed files and
    // send them to the server, which is what made it a claim
    // about intent rather than about visibility. Registering the
    // dead RPC as a spy pins that nothing resurrected the call.
    const setSelected = vi.fn().mockResolvedValue({ ok: true });
    publishFakeRpc({
      'ClaudeCodeService.set_selected_files': setSelected,
      'Repo.get_file_tree': vi.fn().mockResolvedValue({
        tree: {
          name: 'repo',
          path: '',
          type: 'dir',
          lines: 0,
          children: [
            {
              name: 'src',
              path: 'src',
              type: 'dir',
              lines: 0,
              children: [
                { name: 'a.md', path: 'src/a.md', type: 'file', lines: 1 },
              ],
            },
          ],
        },
        modified: ['src/a.md'],
        staged: [],
        untracked: [],
        deleted: [],
        diff_stats: {},
      }),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Expansion happened...
    expect(picker._expanded.has('src')).toBe(true);
    // ...and stopped there.
    expect(setSelected).not.toHaveBeenCalled();
  });
});
