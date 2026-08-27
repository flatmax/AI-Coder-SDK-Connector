// Tests for webapp/src/app-shell/index.js — toast layer,
// server-push callbacks, tab switching, and the one-shot
// enrichment-unavailable warning toast.
//
// Covers:
//   - Toast layer (event subscription, auto-dismiss, defaults,
//     unsubscribe on disconnect)
//   - Server-push callbacks (streamChunk, streamComplete,
//     navigateFile, sessionDeleted) dispatching window events
//   - Tab switching via _switchTab
//   - Enrichment-unavailable one-shot warning toast, including
//     localStorage suppression across reloads and tolerance for
//     storage errors
//
// Test infrastructure (monaco/svg-pan-zoom mocks, JRPCClient
// stub, prototype patches for child-tab RPCs) lives in
// ./test-helpers.js. installAppShellTestSetup() registers the
// beforeEach/afterEach pair that every AppShell test file needs.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installAppShellTestSetup, mountShell } from './test-helpers.js';

describe('AppShell events and toasts', () => {
  installAppShellTestSetup();

  describe('toast system', () => {
    it('dispatches window event → shows toast', async () => {
      const shell = mountShell();
      window.dispatchEvent(new CustomEvent('aic-toast', {
        detail: { message: 'File saved', type: 'success' },
      }));
      await shell.updateComplete;
      expect(shell.toasts.length).toBe(1);
      expect(shell.toasts[0].message).toBe('File saved');
      expect(shell.toasts[0].type).toBe('success');
    });

    it('auto-dismisses after 3 seconds', () => {
      vi.useFakeTimers();
      const shell = mountShell();
      shell._showToast('Hi', 'info');
      expect(shell.toasts.length).toBe(1);
      vi.advanceTimersByTime(2999);
      expect(shell.toasts.length).toBe(1);
      vi.advanceTimersByTime(2);
      expect(shell.toasts.length).toBe(0);
    });

    it('ignores events with no message', async () => {
      const shell = mountShell();
      window.dispatchEvent(new CustomEvent('aic-toast', { detail: {} }));
      await shell.updateComplete;
      expect(shell.toasts.length).toBe(0);
    });

    it('defaults type to info when not specified', () => {
      const shell = mountShell();
      window.dispatchEvent(new CustomEvent('aic-toast', {
        detail: { message: 'No type here' },
      }));
      expect(shell.toasts[0].type).toBe('info');
    });

    it('unsubscribes from toast events on disconnect', () => {
      const shell = mountShell();
      shell.remove();
      window.dispatchEvent(new CustomEvent('aic-toast', {
        detail: { message: 'Should not appear' },
      }));
      // Element was removed, so its internal state is gone.
      // This test just confirms the removal doesn't error.
      expect(shell.toasts.length).toBe(0);
    });
  });

  describe('server-push callbacks', () => {
    it('streamChunk dispatches window event', () => {
      // `chunk`, not `content`: the engine's payload is a block-keyed
      // object — `{block_id, seq, content, done}` — and calling the whole
      // thing `content` when `content` is one of its fields read as if the
      // shell were unwrapping it. It forwards it whole.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('stream-chunk', listener);
      const chunk = {
        block_id: '1736956800000-a1b2c3:b0',
        seq: 0,
        content: 'hello',
        done: false,
      };
      shell.streamChunk('req-1', chunk);
      expect(listener).toHaveBeenCalledOnce();
      const event = listener.mock.calls[0][0];
      expect(event.detail).toEqual({ requestId: 'req-1', chunk });
      window.removeEventListener('stream-chunk', listener);
    });

    it('streamComplete dispatches window event', () => {
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('stream-complete', listener);
      shell.streamComplete('req-1', { response: 'ok' });
      expect(listener).toHaveBeenCalledOnce();
      window.removeEventListener('stream-complete', listener);
    });

    it('turnUsage dispatches the running counter with its request id', () => {
      // Turn-scoped like the rest of the streaming channel: the payload says
      // how many tokens, and only the request id says whose turn they were.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('turn-usage', listener);
      const usage = {
        turn_model_usage: {
          'claude-opus-5': { input_tokens: 900, output_tokens: 100 },
        },
      };
      expect(shell.turnUsage('req-1', usage)).toBe(true);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener.mock.calls[0][0].detail).toEqual({
        requestId: 'req-1',
        usage,
      });
      window.removeEventListener('turn-usage', listener);
    });

    it('navigateFile flags remote origin', () => {
      // Collaboration echo-prevention — remote-originated
      // navigation must be distinguishable from local.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('navigate-file', listener);
      shell.navigateFile({ path: 'src/foo.py' });
      const event = listener.mock.calls[0][0];
      expect(event.detail._remote).toBe(true);
      expect(event.detail.path).toBe('src/foo.py');
      window.removeEventListener('navigate-file', listener);
    });

    it('filesChanged is no longer a callback', () => {
      // The backend pushed the authoritative selected-files list
      // here and the shell re-dispatched it as `files-changed` for
      // the picker to apply. Nothing sends it since CC-21 — and
      // because `ExposeClass` publishes whatever public methods a
      // class has, a leftover receiver would keep advertising an
      // RPC the frontend can't act on. `filesModified` (a
      // different event, still live) is the neighbour it is easy
      // to confuse this with.
      const shell = mountShell();
      expect(shell.filesChanged).toBeUndefined();
    });

    it('userMessageImages carries the request id and the pointers', () => {
      // The follow-up to `userMessage`, which goes out before the CLI has
      // written the entry a pointer names. Turn-scoped, so the request id
      // arrives first and says which message the pointers belong to.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('user-message-images', listener);
      const data = {
        image_refs: [
          {
            session_id: 's1',
            entry_uuid: 'u1',
            block: 1,
            media_type: 'image/png',
          },
        ],
      };
      shell.userMessageImages('req-1', data);
      const event = listener.mock.calls[0][0];
      expect(event.detail).toEqual({ requestId: 'req-1', data });
      window.removeEventListener('user-message-images', listener);
    });

    it('permissionDeadline dispatches a session-wide window event', () => {
      // Its own event rather than a re-sent `permissionRequest`: the dialog
      // on screen is updated in place, because rebuilding it would restart
      // the settling interval and lose a half-typed deny reason.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('permission-deadline', listener);
      const data = {
        permission_id: 'perm-1',
        request_id: 'req-1',
        expires_at: '2026-08-14T00:00:30Z',
        localhost_available: false,
      };
      shell.permissionDeadline(data);
      const event = listener.mock.calls[0][0];
      expect(event.detail).toEqual(data);
      window.removeEventListener('permission-deadline', listener);
    });

    it('modelChanged dispatches a session-wide window event', () => {
      // Session-wide, not turn-scoped: no request id, because the model in
      // force outlives the turn that changed it. The window that made the
      // call already flipped its own control on the RPC reply — this event
      // is what every *other* window has instead, and without it they would
      // go on naming the model the session started on.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('model-changed', listener);
      expect(shell.modelChanged({ model: 'haiku', by: 'user' })).toBe(true);
      const event = listener.mock.calls[0][0];
      expect(event.detail).toEqual({ model: 'haiku', by: 'user' });
      window.removeEventListener('model-changed', listener);
    });

    it('sessionDeleted dispatches window event with the session id', () => {
      // Reaches the client that asked for the delete as well as the
      // ones that did not: a session list still offering the row is a
      // click that can only fail.
      const shell = mountShell();
      const listener = vi.fn();
      window.addEventListener('session-deleted', listener);
      shell.sessionDeleted({ session_id: 's1' });
      const event = listener.mock.calls[0][0];
      expect(event.detail.session_id).toBe('s1');
      window.removeEventListener('session-deleted', listener);
    });

    it('callbacks return true for jrpc-oo ack', () => {
      const shell = mountShell();
      expect(shell.streamChunk('r', 'c')).toBe(true);
      expect(shell.streamComplete('r', {})).toBe(true);
      expect(shell.sessionDeleted({ session_id: 's1' })).toBe(true);
      expect(shell.userMessageImages('r', { image_refs: [] })).toBe(true);
    });
  });

  describe('tab switching', () => {
    // `onTabVisible` is fired on a microtask after the render
    // that moves `.active`, so tests have to let both settle.
    async function settleSwitch(shell) {
      await shell.updateComplete;
      await Promise.resolve();
      await Promise.resolve();
    }

    it('changes activeTab via _switchTab', () => {
      const shell = mountShell();
      shell._switchTab('context');
      expect(shell.activeTab).toBe('context');
      shell._switchTab('settings');
      expect(shell.activeTab).toBe('settings');
    });

    it('tells the revealed tab that it is on screen', async () => {
      // The context tab refuses to refetch while hidden — a
      // breakdown costs a control request to the CLI — and marks
      // itself stale instead. It cannot see the class change that
      // reveals it, so the shell has to say so or the stale badge
      // never clears.
      const shell = mountShell();
      await shell.updateComplete;
      const tab = shell.shadowRoot.querySelector('aic-context-usage-tab');
      const spy = vi.spyOn(tab, 'onTabVisible');
      shell._switchTab('context');
      await settleSwitch(shell);
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it('does not tell a tab it is visible when it is being hidden', async () => {
      const shell = mountShell();
      shell._switchTab('context');
      await settleSwitch(shell);
      const tab = shell.shadowRoot.querySelector('aic-context-usage-tab');
      const spy = vi.spyOn(tab, 'onTabVisible');
      shell._switchTab('settings');
      await settleSwitch(shell);
      expect(spy).not.toHaveBeenCalled();
    });

    it('no-ops for a tab with no visibility hook', async () => {
      const shell = mountShell();
      shell._switchTab('files');
      await settleSwitch(shell);
      expect(shell.activeTab).toBe('files');
    });

    it('clears the context tab stale badge on the way in', async () => {
      // End to end: a turn completes while the tab is hidden, the
      // tab marks itself stale, and switching to it refreshes.
      const shell = mountShell();
      await shell.updateComplete;
      const tab = shell.shadowRoot.querySelector('aic-context-usage-tab');
      const refresh = vi
        .spyOn(tab, '_refresh')
        .mockImplementation(async () => {});
      window.dispatchEvent(
        new CustomEvent('stream-complete', {
          detail: { requestId: 'r1', result: { response: 'ok' } },
        }),
      );
      await settleSwitch(shell);
      expect(tab._stale).toBe(true);
      refresh.mockClear();
      shell._switchTab('context');
      await settleSwitch(shell);
      expect(refresh).toHaveBeenCalledTimes(1);
      expect(tab._stale).toBe(false);
    });
  });

  // ---------------------------------------------------------------
  // request-dialog-tab, and the section inside the tab
  // ---------------------------------------------------------------
  //
  // A routed slash command names `tab:context#session`, because naming
  // the tab alone lands on whatever section its reader was last on —
  // and MCP status is not on Usage at all. The shell's job is only to
  // pass the section on; the tab decides what it means.

  describe('request-dialog-tab', () => {
    async function settleSwitch(shell) {
      await shell.updateComplete;
      await Promise.resolve();
      await Promise.resolve();
    }

    function request(detail) {
      window.dispatchEvent(
        new CustomEvent('request-dialog-tab', { detail }),
      );
    }

    it('switches to the tab the event names', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      request({ tab: 'context' });
      await settleSwitch(shell);
      expect(shell.activeTab).toBe('context');
    });

    it('asks the tab for the section the event names', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      const tab = shell.shadowRoot.querySelector('aic-context-usage-tab');
      const spy = vi.spyOn(tab, 'showSection');
      request({ tab: 'context', section: 'session' });
      await settleSwitch(shell);
      expect(spy).toHaveBeenCalledWith('session');
    });

    it('asks for nothing when no section is named', async () => {
      // The back-arrow buttons on every tab body fire this event with a
      // tab and nothing else, and they must not disturb the section.
      const shell = mountShell();
      await shell.updateComplete;
      const tab = shell.shadowRoot.querySelector('aic-context-usage-tab');
      const spy = vi.spyOn(tab, 'showSection');
      request({ tab: 'context' });
      await settleSwitch(shell);
      expect(spy).not.toHaveBeenCalled();
    });

    it('survives a tab with no section hook', async () => {
      // The files tab has none. Deliberately not the settings tab,
      // which grew one for `/model`.
      const shell = mountShell();
      await shell.updateComplete;
      request({ tab: 'files', section: 'model' });
      await settleSwitch(shell);
      expect(shell.activeTab).toBe('files');
    });

    it('asks the settings tab for its model panel', async () => {
      // `/model` names `tab:settings#model`. The shell does not know
      // that the settings tab answers a section by scrolling rather
      // than by picking a segment, and should not.
      const shell = mountShell();
      await shell.updateComplete;
      const tab = shell.shadowRoot.querySelector('aic-settings-tab');
      const spy = vi.spyOn(tab, 'showSection');
      request({ tab: 'settings', section: 'model' });
      await settleSwitch(shell);
      expect(spy).toHaveBeenCalledWith('model');
    });

    it('survives a section the settings tab does not have', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      request({ tab: 'settings', section: 'nope' });
      await settleSwitch(shell);
      expect(shell.activeTab).toBe('settings');
    });

    it('ignores an event naming no tab', async () => {
      const shell = mountShell();
      shell._switchTab('context');
      await settleSwitch(shell);
      request({ section: 'session' });
      await settleSwitch(shell);
      expect(shell.activeTab).toBe('context');
    });
  });

  // ---------------------------------------------------------------
  // Enrichment unavailable — one-shot toast
  // ---------------------------------------------------------------
  //
  // When the backend reports
  // `enrichment_status === "unavailable"` (KeyBERT probe failed
  // or model load failed), the shell shows a one-shot warning
  // toast pointing users at `pip install 'aic-dc[docs]'`. The
  // toast fires from two places:
  //
  //   - `_fetchCurrentState` — initial state snapshot on
  //     connect / reconnect.
  //   - `_onModeChanged` — mid-session modeChanged broadcast
  //     when the backend transitions to unavailable after
  //     startup.
  //
  // Suppressed after first display via a localStorage flag,
  // which persists across reloads. The condition is effectively
  // permanent for the session — repeated toasts would be noise.

  describe('enrichment unavailable toast', () => {
    const STORAGE_KEY = 'aic-dc-enrichment-unavailable-shown';

    beforeEach(() => {
      localStorage.clear();
    });
    afterEach(() => {
      localStorage.clear();
    });

    it('fires on modeChanged with unavailable status', () => {
      const shell = mountShell();
      shell._onModeChanged({
        detail: {
          mode: 'code',
          enrichment_status: 'unavailable',
        },
      });
      expect(shell.toasts.length).toBe(1);
      expect(shell.toasts[0].type).toBe('warning');
      expect(shell.toasts[0].message)
        .toContain('aic-dc[docs]');
    });

    it('does not fire for other enrichment_status values', () => {
      const shell = mountShell();
      for (const status of ['pending', 'building', 'ready']) {
        shell._onModeChanged({
          detail: {
            mode: 'code',
            enrichment_status: status,
          },
        });
      }
      expect(shell.toasts.length).toBe(0);
    });

    it('does not fire when enrichment_status field is absent', () => {
      // Older backends omit the field entirely. The handler
      // must silently pass — no toast, no exception.
      const shell = mountShell();
      shell._onModeChanged({
        detail: { mode: 'code' },
      });
      expect(shell.toasts.length).toBe(0);
    });

    it('sets localStorage suppression flag after first fire', () => {
      const shell = mountShell();
      shell._onModeChanged({
        detail: { enrichment_status: 'unavailable' },
      });
      expect(localStorage.getItem(STORAGE_KEY)).toBe('true');
    });

    it('suppresses repeats within a session', () => {
      const shell = mountShell();
      // First broadcast — toast appears.
      shell._onModeChanged({
        detail: { enrichment_status: 'unavailable' },
      });
      expect(shell.toasts.length).toBe(1);
      // Second broadcast — no new toast.
      shell._onModeChanged({
        detail: { enrichment_status: 'unavailable' },
      });
      expect(shell.toasts.length).toBe(1);
    });

    it('suppresses repeats across reloads via localStorage', () => {
      // Simulate a prior session that already showed the toast.
      localStorage.setItem(STORAGE_KEY, 'true');
      const shell = mountShell();
      shell._onModeChanged({
        detail: { enrichment_status: 'unavailable' },
      });
      expect(shell.toasts.length).toBe(0);
    });

    it('direct helper call matches the event-driven path', () => {
      const shell = mountShell();
      shell._maybeShowEnrichmentUnavailableToast('unavailable');
      expect(shell.toasts.length).toBe(1);
      expect(shell.toasts[0].type).toBe('warning');
    });

    it('direct helper no-ops for non-unavailable values', () => {
      const shell = mountShell();
      shell._maybeShowEnrichmentUnavailableToast('pending');
      shell._maybeShowEnrichmentUnavailableToast('building');
      shell._maybeShowEnrichmentUnavailableToast('ready');
      shell._maybeShowEnrichmentUnavailableToast(undefined);
      shell._maybeShowEnrichmentUnavailableToast(null);
      expect(shell.toasts.length).toBe(0);
    });

    it('preserves other modeChanged side effects', () => {
      // The enrichment-status check must not interfere with
      // mode handling. A single event carrying both fields
      // should update mode state AND fire the toast.
      const shell = mountShell();
      shell._mode = 'code';
      shell._onModeChanged({
        detail: {
          mode: 'doc',
          enrichment_status: 'unavailable',
        },
      });
      expect(shell._mode).toBe('doc');
      expect(shell.toasts.length).toBe(1);
    });

    it('ignores a cross_ref_enabled field it is sent', () => {
      // Retired in conversion phase 4 — both indexes are
      // always available as tools. A payload from an older
      // backend must not resurrect the state it named.
      const shell = mountShell();
      shell._onModeChanged({
        detail: { mode: 'doc', cross_ref_enabled: true },
      });
      expect(shell._mode).toBe('doc');
      expect(shell._crossRefEnabled).toBeUndefined();
    });

    it('survives localStorage errors on read', () => {
      // Private-browsing modes can throw on getItem. The
      // helper must swallow and proceed — one duplicate
      // toast across reloads is better than failing silently.
      const shell = mountShell();
      const origGet = Storage.prototype.getItem;
      Storage.prototype.getItem = () => {
        throw new Error('quota');
      };
      try {
        shell._maybeShowEnrichmentUnavailableToast('unavailable');
        expect(shell.toasts.length).toBe(1);
      } finally {
        Storage.prototype.getItem = origGet;
      }
    });

    it('survives localStorage errors on write', () => {
      const shell = mountShell();
      const origSet = Storage.prototype.setItem;
      Storage.prototype.setItem = () => {
        throw new Error('quota');
      };
      try {
        shell._maybeShowEnrichmentUnavailableToast('unavailable');
        // Toast still displayed even though persistence failed.
        expect(shell.toasts.length).toBe(1);
      } finally {
        Storage.prototype.setItem = origSet;
      }
    });
  });
});