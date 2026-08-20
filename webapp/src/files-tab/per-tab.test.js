// Tests for webapp/src/files-tab — per-tab deny-read storage,
// the active-tab-changed handler, and the one thing that is NOT
// per-tab: the deny-read RPC itself.
//
// This file pinned per-tab *selection* alongside deny-read until
// CC-21 dropped the selection channel. What is left is the half
// that describes a real per-tab claim — the picker shows the
// active tab's struck-through rows — plus the tests that pin the
// deliberate asymmetry: a deny rule lands in the settings sources
// every SDK subagent inherits, so it is repo-wide by
// construction and there is no agent-scoped variant to route to.

import { describe, expect, it, vi } from 'vitest';

import './index.js';
import {
  mountTab,
  publishFakeRpc,
  settle,
  fakeTreeResponse,
  installCleanup,
} from './test-helpers.js';

installCleanup();

// ---------------------------------------------------------------------------
// Per-tab deny-read structure (D21 Phase A4)
// ---------------------------------------------------------------------------

// These tests pin the Map-based storage contract directly —
// the Map exists with exactly one `"main"` entry on
// construction, `_activeTabId` defaults to `"main"`, and
// `_excludedFiles` reads/writes route through the Map
// without disturbing existing single-tab behaviour. The
// `active-tab-changed` handler is wired to update
// `_activeTabId` and push the new tab's deny set to the
// picker. Single-tab operation (Phase A scope) never
// actually switches tabs, but the plumbing is pinned so
// Phase C's spawn path doesn't re-touch this component.

describe('FilesTab per-tab deny-read — structure', () => {
  it('constructs with a Map containing only "main"', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    expect(t._excludedFilesByTab).toBeInstanceOf(Map);
    expect(t._excludedFilesByTab.size).toBe(1);
    expect(t._excludedFilesByTab.has('main')).toBe(true);
  });

  it('_activeTabId defaults to "main"', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    expect(t._activeTabId).toBe('main');
  });

  it('main tab entry starts as an empty Set', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    const mainSet = t._excludedFilesByTab.get('main');
    expect(mainSet).toBeInstanceOf(Set);
    expect(mainSet.size).toBe(0);
  });

  it('_excludedFiles getter reads from the active tab slot', async () => {
    // Mutate the Map directly; the getter reflects the
    // change. Pins that reads go through the Map, not a
    // shadow field on `this`.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    const mainSet = t._excludedFilesByTab.get('main');
    mainSet.add('direct.md');
    expect(t._excludedFiles.has('direct.md')).toBe(true);
  });

  it('_excludedFiles setter writes to the active tab slot', async () => {
    // Assign via the setter; the Map entry reflects
    // the new Set.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t._excludedFiles = new Set(['via-setter.md']);
    const mainSet = t._excludedFilesByTab.get('main');
    expect(mainSet.has('via-setter.md')).toBe(true);
  });

  it('_excludedFiles setter wraps non-Set inputs defensively', async () => {
    // `_applyExclusion` always passes Set instances, but
    // the setter accepts iterables too (paranoia against
    // a future refactor that passes an array by
    // accident).
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t._excludedFiles = ['from-array.md'];
    const mainSet = t._excludedFilesByTab.get('main');
    expect(mainSet).toBeInstanceOf(Set);
    expect(mainSet.has('from-array.md')).toBe(true);
  });

  it('getter lazy-creates missing tab entries', async () => {
    // Defensive — if `_activeTabId` is flipped to a
    // key that has no Map entry (shouldn't happen in
    // production but worth pinning), the getter
    // creates an empty Set on demand rather than
    // returning undefined.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t._activeTabId = 'some-orphan-tab';
    expect(t._excludedFilesByTab.has('some-orphan-tab')).toBe(false);
    const fresh = t._excludedFiles;
    expect(fresh).toBeInstanceOf(Set);
    expect(fresh.size).toBe(0);
    // And the Map now has the entry.
    expect(t._excludedFilesByTab.has('some-orphan-tab')).toBe(true);
  });

  it('main-tab behaviour unchanged — _applyExclusion round-trips', async () => {
    // Sanity check that the per-tab refactor didn't
    // break the existing deny flow. Assign via
    // `_applyExclusion`, read back via getter, verify
    // the Map entry matches.
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
        .mockResolvedValue({ denied_read_files: ['a.md'] }),
    });
    const t = mountTab();
    await settle(t);
    t._applyExclusion(new Set(['a.md']), /* notifyServer */ false);
    expect(t._excludedFiles.has('a.md')).toBe(true);
    const mainSet = t._excludedFilesByTab.get('main');
    expect(mainSet.has('a.md')).toBe(true);
  });
});

describe('FilesTab active-tab-changed handler', () => {
  /**
   * Dispatch an `active-tab-changed` event on the
   * window with the given detail. The chat panel is
   * the usual originator in production (via its
   * `_activeTabId` setter), but for A4 tests we fire
   * directly on `window` since the chat panel never
   * actually switches tabs in Phase A.
   */
  function fireActiveTabChanged(tabId, previousTabId = 'main') {
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', {
        detail: { tabId, previousTabId },
      }),
    );
  }

  it('updates _activeTabId on event', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    expect(t._activeTabId).toBe('main');
    fireActiveTabChanged('agent-0');
    await settle(t);
    expect(t._activeTabId).toBe('agent-0');
  });

  it('creates Map entry for new tab on first switch', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    expect(t._excludedFilesByTab.has('agent-0')).toBe(false);
    fireActiveTabChanged('agent-0');
    await settle(t);
    expect(t._excludedFilesByTab.has('agent-0')).toBe(true);
    expect(t._excludedFilesByTab.get('agent-0')).toBeInstanceOf(Set);
    expect(t._excludedFilesByTab.get('agent-0').size).toBe(0);
  });

  it('pushes new tab deny set to picker', async () => {
    // Seed an agent tab with a pre-existing deny set
    // (simulating Phase C spawning behaviour), then
    // switch to it. The picker's `excludedFiles` prop
    // should reflect the new tab's set.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t._excludedFilesByTab.set('agent-0', new Set(['agent-file.py']));
    fireActiveTabChanged('agent-0');
    await settle(t);
    const picker = t.shadowRoot.querySelector('ac-file-picker');
    expect(picker.excludedFiles.has('agent-file.py')).toBe(true);
    // And the main tab's file (from any prior state)
    // shouldn't leak through.
    expect(picker.excludedFiles.size).toBe(1);
  });

  it('switching back restores previous tab deny set', async () => {
    // Simulate a round-trip: main has one denial,
    // switch to agent-0 (empty), switch back to main.
    // The picker should show main's denial again.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    // Seed main tab's deny set.
    t._excludedFilesByTab.get('main').add('main-file.md');
    // Switch to agent-0.
    fireActiveTabChanged('agent-0');
    await settle(t);
    let picker = t.shadowRoot.querySelector('ac-file-picker');
    expect(picker.excludedFiles.size).toBe(0);
    // Switch back to main.
    fireActiveTabChanged('main', 'agent-0');
    await settle(t);
    picker = t.shadowRoot.querySelector('ac-file-picker');
    expect(picker.excludedFiles.has('main-file.md')).toBe(true);
  });

  it('no-op when event tabId matches current', async () => {
    // Spam the event with the current tab ID — should
    // not touch the picker (spy on requestUpdate).
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    const picker = t.shadowRoot.querySelector('ac-file-picker');
    const spy = vi.spyOn(picker, 'requestUpdate');
    fireActiveTabChanged('main', 'main');
    fireActiveTabChanged('main', 'main');
    await settle(t);
    expect(spy).not.toHaveBeenCalled();
  });

  it('ignores malformed events (missing tabId)', async () => {
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    // Various malformed shapes — none of them should
    // flip _activeTabId away from 'main'.
    window.dispatchEvent(new CustomEvent('active-tab-changed'));
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', { detail: {} }),
    );
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', {
        detail: { tabId: 42 },
      }),
    );
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', {
        detail: { tabId: '' },
      }),
    );
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', { detail: null }),
    );
    await settle(t);
    expect(t._activeTabId).toBe('main');
  });

  it('removes listener on disconnect', async () => {
    // After unmount, the event must not crash or
    // update state. _activeTabId should stay frozen.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
    });
    const t = mountTab();
    await settle(t);
    t.remove();
    expect(() => {
      fireActiveTabChanged('agent-0');
    }).not.toThrow();
  });

  it('deny writes target the active tab only', async () => {
    // Switch to agent-0, apply a denial, verify the
    // agent's Map entry updates and main's stays empty.
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(fakeTreeResponse([])),
      'ClaudeCodeService.set_denied_read_files': vi
        .fn()
        .mockResolvedValue({ denied_read_files: [] }),
    });
    const t = mountTab();
    await settle(t);
    fireActiveTabChanged('agent-0');
    await settle(t);
    t._applyExclusion(new Set(['a.py']), false);
    expect(
      t._excludedFilesByTab.get('agent-0').has('a.py'),
    ).toBe(true);
    expect(t._excludedFilesByTab.get('main').size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Deny-read RPC routing
// ---------------------------------------------------------------------------
//
// The per-tab Map above is a display concern. The RPC is not
// per-tab and must not become so: a deny rule lands in the
// settings sources every SDK subagent inherits, so a per-agent
// variant would be a promise the permission layer cannot keep.
// Every tab hits the one ClaudeCodeService.set_denied_read_files.
//
// A parallel set of tests pinned agent-scoped *selection*
// routing (LLMService.set_agent_selected_files, plus its
// agent-not-found and restricted toasts) until CC-21. Both the
// RPC and its callers are gone; the restricted-error path that
// remains is exclusion's own, covered in exclusion.test.js.

describe('FilesTab deny-read RPC routing', () => {
  /**
   * Fire active-tab-changed to flip the orchestrator's
   * _activeTabId. Same helper shape the A4 block uses.
   */
  function fireActiveTabChanged(tabId, previousTabId = 'main') {
    window.dispatchEvent(
      new CustomEvent('active-tab-changed', {
        detail: { tabId, previousTabId },
      }),
    );
  }

  it('agent-tab read denial does NOT route per-agent', async () => {
    // The one thing this test has to pin is that an agent tab
    // does not look for an agent-scoped RPC: if it did, the
    // denial would fail silently against a service that has no
    // such method.
    const setDenied = vi
      .fn()
      .mockResolvedValue({ denied_read_files: ['a.py'] });
    const setAgentExcl = vi.fn().mockResolvedValue([]);
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.py', path: 'a.py', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setDenied,
      'LLMService.set_agent_excluded_index_files': setAgentExcl,
    });
    const t = mountTab();
    await settle(t);
    fireActiveTabChanged('backend-auth');
    await settle(t);
    const picker = t.shadowRoot.querySelector('ac-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.py'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
    expect(setDenied.mock.calls[0][0]).toEqual(['a.py']);
    expect(setAgentExcl).not.toHaveBeenCalled();
  });

  it('main tab read denial calls set_denied_read_files', async () => {
    const setDenied = vi
      .fn()
      .mockResolvedValue({ denied_read_files: ['a.py'] });
    publishFakeRpc({
      'Repo.get_file_tree': vi
        .fn()
        .mockResolvedValue(
          fakeTreeResponse([
            { name: 'a.py', path: 'a.py', type: 'file', lines: 1 },
          ]),
        ),
      'ClaudeCodeService.set_denied_read_files': setDenied,
    });
    const t = mountTab();
    await settle(t);
    // Stay on main.
    const picker = t.shadowRoot.querySelector('ac-file-picker');
    picker.dispatchEvent(
      new CustomEvent('exclusion-changed', {
        detail: { excludedFiles: ['a.py'] },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(t);
    expect(setDenied).toHaveBeenCalledOnce();
  });
});
