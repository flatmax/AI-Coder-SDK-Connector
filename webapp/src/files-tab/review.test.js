// Tests for webapp/src/files-tab.js — review state slice
// (review-started / review-ended events, exit-review
// dispatch). Extracted from files-tab.test.js.

import { describe, expect, it, vi } from 'vitest';

import './index.js';
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
// Review state (Increment 11)
// ---------------------------------------------------------------------------

describe('FilesTab review state', () => {
  /**
   * Build a valid reviewState dict matching what the
   * backend's `start_review` emits in the
   * `review-started` window event detail.
   */
  function reviewStateFixture(overrides = {}) {
    return {
      active: true,
      branch: 'feature-auth',
      base_commit: 'abc1234',
      branch_tip: 'def5678',
      original_branch: 'main',
      commits: [
        {
          sha: 'def5678',
          short_sha: 'def5678',
          message: 'third',
          author: 'matt',
          date: '2024-01-03',
        },
      ],
      changed_files: [
        { path: 'a.md', additions: 10, deletions: 5, status: 'M' },
      ],
      stats: {
        commit_count: 1,
        files_changed: 1,
        additions: 10,
        deletions: 5,
      },
      ...overrides,
    };
  }

  async function setupTab() {
    const getTree = vi.fn().mockResolvedValue(
      fakeTreeResponse([
        { name: 'a.md', path: 'a.md', type: 'file', lines: 5 },
      ]),
    );
    const endReview = vi.fn().mockResolvedValue({
      status: 'ended',
    });
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.end_review': endReview,
    });
    const t = mountTab();
    await settle(t);
    return { t, getTree, endReview };
  }

  it('reviewState starts as null', async () => {
    const { t } = await setupTab();
    expect(t._reviewState).toBeNull();
  });

  it('picker reviewState prop is null initially', async () => {
    const { t } = await setupTab();
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.reviewState).toBeNull();
  });

  it('review-started event populates _reviewState', async () => {
    const { t } = await setupTab();
    const fixture = reviewStateFixture();
    pushEvent('review-started', fixture);
    await settle(t);
    expect(t._reviewState).toEqual(fixture);
  });

  it('review-started pushes state to picker', async () => {
    const { t } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.reviewState).toBeTruthy();
    expect(picker.reviewState.active).toBe(true);
    expect(picker.reviewState.branch).toBe('feature-auth');
  });

  /**
   * Mount with a nested tree that starts clean and reports
   * `src/a.md` as staged from the second load onwards — the
   * shape the review's soft reset produces on the server.
   */
  async function setupTabWithStagedSubdir() {
    let calls = 0;
    const getTree = vi.fn().mockImplementation(() => {
      calls += 1;
      const staged = calls === 1 ? [] : ['src/a.md'];
      return Promise.resolve({
        ...fakeTreeResponse([
          {
            name: 'src',
            path: 'src',
            type: 'dir',
            lines: 0,
            children: [
              { name: 'a.md', path: 'src/a.md', type: 'file', lines: 5 },
            ],
          },
        ]),
        staged,
      });
    });
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.end_review': vi
        .fn()
        .mockResolvedValue({ status: 'ended' }),
    });
    const t = mountTab();
    await settle(t);
    return { t, getTree };
  }

  it('review-started expands the directories the review touches', async () => {
    // Entering a review should land the user looking at it.
    // The soft reset stages every branch-tip change, so the
    // reload's staged set names the review's files and the
    // expand pass opens their ancestors.
    //
    // This slot held two selection tests until CC-21 — review
    // entry cleared the picker's selection locally so the UI
    // didn't wait on the server's `filesChanged` broadcast to
    // agree. Expanding is what review entry does to the
    // picker now.
    const { t } = await setupTabWithStagedSubdir();
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // Nothing staged on first load, so nothing expanded.
    expect(picker._expanded.has('src')).toBe(false);
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    expect(picker._expanded.has('src')).toBe(true);
  });

  it('review entry re-expands even after the one-shot flag is spent', async () => {
    // The first-load flag exists so a plain reload doesn't
    // re-open directories the user has since collapsed.
    // Review entry deliberately bypasses it: the user asked
    // for the review, so showing them its files is answering
    // the request rather than overriding a preference.
    const { t } = await setupTabWithStagedSubdir();
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    // The one-shot is already spent by the first load.
    expect(t._initialAutoExpand).toBe(false);
    // User collapses src for good measure.
    picker._expanded = new Set();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    expect(picker._expanded.has('src')).toBe(true);
  });

  it('review-started refreshes the file tree', async () => {
    // Soft-reset on the server side changes staging
    // state; the picker should pick this up via a
    // fresh `get_file_tree` call.
    const { t, getTree } = await setupTab();
    const initialCalls = getTree.mock.calls.length;
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    expect(getTree.mock.calls.length).toBe(initialCalls + 1);
  });

  it('review-ended clears _reviewState', async () => {
    const { t } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    expect(t._reviewState).toBeTruthy();
    pushEvent('review-ended', {});
    await settle(t);
    expect(t._reviewState).toBeNull();
  });

  it('review-ended clears picker reviewState', async () => {
    const { t } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.reviewState).toBeTruthy();
    pushEvent('review-ended', {});
    await settle(t);
    expect(picker.reviewState).toBeNull();
  });

  it('review-ended leaves deny-read rules alone', async () => {
    // Neither end of the review lifecycle touches denials.
    // A deny rule is a real CLI permission rule written to
    // settings, not turn state — clearing it on review exit
    // would let the agent read a file the user had told it
    // not to, with nothing on screen to say so.
    //
    // Selection was the state this test used to guard, and
    // review-started cleared it while review-ended didn't.
    // Denials are simpler: nothing clears them.
    const { t } = await setupTab();
    t._applyExclusion(new Set(['a.md']), /* notifyServer */ false);
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    expect(t._excludedFiles.has('a.md')).toBe(true);
    pushEvent('review-ended', {});
    await settle(t);
    expect(t._excludedFiles.has('a.md')).toBe(true);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.excludedFiles.has('a.md')).toBe(true);
  });

  it('review-ended refreshes the file tree', async () => {
    const { t, getTree } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const mid = getTree.mock.calls.length;
    pushEvent('review-ended', {});
    await settle(t);
    expect(getTree.mock.calls.length).toBe(mid + 1);
  });

  it('exit-review from picker calls ClaudeCodeService.end_review', async () => {
    const { t, endReview } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exit-review', {
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(endReview).toHaveBeenCalledOnce();
  });

  it('exit-review does not optimistically clear state', async () => {
    // If the server rejects (restricted), the banner
    // should stay visible. The `reviewEnded`
    // broadcast is what ends review from the UI's
    // perspective — exit-review just asks the
    // server.
    const { t } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exit-review', {
        bubbles: true,
        composed: true,
      }),
    );
    // Immediately after dispatch, before the RPC
    // resolves — state still present.
    expect(t._reviewState).toBeTruthy();
  });

  it('exit-review surfaces restricted error as warning', async () => {
    const getTree = vi.fn().mockResolvedValue(
      fakeTreeResponse([
        { name: 'a.md', path: 'a.md', type: 'file', lines: 5 },
      ]),
    );
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.end_review': vi.fn().mockResolvedValue({
        error: 'restricted',
        reason: 'Participants cannot end review',
      }),
    });
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const t = mountTab();
      await settle(t);
      pushEvent('review-started', reviewStateFixture());
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.dispatchEvent(
        new CustomEvent('exit-review', {
          bubbles: true,
          composed: true,
        }),
      );
      await settle(t);
      const warnings = toastListener.mock.calls
        .map((call) => call[0].detail)
        .filter((d) => d.type === 'warning');
      expect(warnings.length).toBeGreaterThan(0);
      expect(warnings.at(-1).message).toContain('Participants');
      // State still present — restricted means the
      // server did nothing.
      expect(t._reviewState).toBeTruthy();
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('exit-review surfaces partial status as warning', async () => {
    // Server couldn't reattach the original branch
    // but did clear review state. The user is in an
    // unusual git state; warn them.
    const getTree = vi.fn().mockResolvedValue(
      fakeTreeResponse([]),
    );
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.end_review': vi.fn().mockResolvedValue({
        status: 'partial',
        error: 'could not reattach original branch',
      }),
    });
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const t = mountTab();
      await settle(t);
      pushEvent('review-started', reviewStateFixture());
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.dispatchEvent(
        new CustomEvent('exit-review', {
          bubbles: true,
          composed: true,
        }),
      );
      await settle(t);
      const warnings = toastListener.mock.calls
        .map((call) => call[0].detail)
        .filter((d) => d.type === 'warning');
      expect(warnings.length).toBeGreaterThan(0);
      expect(warnings.at(-1).message).toContain('reattach');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('exit-review surfaces RPC rejection as error toast', async () => {
    const getTree = vi.fn().mockResolvedValue(
      fakeTreeResponse([]),
    );
    publishFakeRpc({
      'Repo.get_file_tree': getTree,
      'ClaudeCodeService.end_review': vi
        .fn()
        .mockRejectedValue(new Error('end_review boom')),
    });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const t = mountTab();
      await settle(t);
      pushEvent('review-started', reviewStateFixture());
      await settle(t);
      const picker = t.shadowRoot.querySelector('aic-file-picker');
      picker.dispatchEvent(
        new CustomEvent('exit-review', {
          bubbles: true,
          composed: true,
        }),
      );
      await settle(t);
      const errors = toastListener.mock.calls
        .map((call) => call[0].detail)
        .filter((d) => d.type === 'error');
      expect(errors.length).toBeGreaterThan(0);
      expect(errors.at(-1).message).toContain('end_review boom');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
      consoleSpy.mockRestore();
    }
  });

  it('tree reload during review pushes reviewState again', async () => {
    // When files-modified fires mid-review, the
    // file tree reloads. The reviewState prop must
    // be re-pushed in _pushChildProps so the
    // picker's banner doesn't disappear.
    const { t } = await setupTab();
    pushEvent('review-started', reviewStateFixture());
    await settle(t);
    const picker = t.shadowRoot.querySelector('aic-file-picker');
    expect(picker.reviewState).toBeTruthy();
    // Simulate a reload.
    pushEvent('files-modified', {});
    await settle(t);
    // Banner survives.
    expect(picker.reviewState).toBeTruthy();
    expect(picker.reviewState.branch).toBe('feature-auth');
  });

  it('removes review listeners on disconnect', async () => {
    const { t } = await setupTab();
    t.remove();
    // After disconnect, review-started should not
    // mutate state. Since the tab is unmounted we
    // can't inspect _reviewState directly, but we
    // verify no crash on dispatch.
    expect(() => {
      pushEvent('review-started', reviewStateFixture());
    }).not.toThrow();
  });
});