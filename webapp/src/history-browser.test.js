// Tests for webapp/src/history-browser.js — history browser
// modal component.
//
// Strategy mirrors chat-panel.test.js:
//   - Fake RPC proxy installed via SharedRpc
//   - Manual mount/unmount in afterEach
//   - settle() drains microtasks + rAF so Lit updates settle
//
// Coverage areas:
//   - Initial state (closed renders nothing)
//   - Opening loads sessions
//   - Session list rendering
//   - Session click loads messages
//   - Message preview rendering (user escaped, assistant markdown)
//   - Search input debounce + RPC call
//   - Search mode toggle
//   - Keyboard shortcuts (Escape closes, Escape in search clears first)
//   - Backdrop click closes
//   - Resume and fork call resume_session; delete is two-click
//   - Image pointers resolved one at a time via history_image
//   - session-loaded event fires on successful resume
//   - Stale response handling (generation guards)

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';

import { SharedRpc } from './rpc.js';
import './history-browser.js';
import {
  formatRelativeTime,
  SEARCH_DEBOUNCE_MS,
} from './history-browser.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const _mounted = [];

function mountBrowser(props = {}) {
  const el = document.createElement('ac-history-browser');
  Object.assign(el, props);
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

function publishFakeRpc(methods) {
  const proxy = {};
  for (const [name, impl] of Object.entries(methods)) {
    proxy[name] = async (...args) => {
      const value = await impl(...args);
      return { fake: value };
    };
  }
  SharedRpc.set(proxy);
  return proxy;
}

async function settle(el) {
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
}

afterEach(() => {
  while (_mounted.length) {
    const el = _mounted.pop();
    if (el.isConnected) el.remove();
  }
  SharedRpc.reset();
});

// ---------------------------------------------------------------------------
// formatRelativeTime
// ---------------------------------------------------------------------------

describe('formatRelativeTime', () => {
  it('returns empty for falsy input', () => {
    expect(formatRelativeTime('')).toBe('');
    expect(formatRelativeTime(null)).toBe('');
    expect(formatRelativeTime(undefined)).toBe('');
  });

  it('returns "just now" for recent timestamps', () => {
    const iso = new Date(Date.now() - 30 * 1000).toISOString();
    expect(formatRelativeTime(iso)).toBe('just now');
  });

  it('returns minutes for < 1h old', () => {
    const iso = new Date(Date.now() - 12 * 60 * 1000).toISOString();
    expect(formatRelativeTime(iso)).toBe('12m ago');
  });

  it('returns hours for < 1d old', () => {
    const iso = new Date(
      Date.now() - 3 * 60 * 60 * 1000,
    ).toISOString();
    expect(formatRelativeTime(iso)).toBe('3h ago');
  });

  it('returns days for < 30d old', () => {
    const iso = new Date(
      Date.now() - 5 * 24 * 60 * 60 * 1000,
    ).toISOString();
    expect(formatRelativeTime(iso)).toBe('5d ago');
  });

  it('returns raw string for malformed input', () => {
    expect(formatRelativeTime('not-a-date')).toBe('not-a-date');
  });
});

// ---------------------------------------------------------------------------
// Initial state
// ---------------------------------------------------------------------------

describe('HistoryBrowser initial state', () => {
  it('renders nothing when closed', async () => {
    const el = mountBrowser();
    await el.updateComplete;
    // Host display: none when not [open].
    expect(el.shadowRoot.querySelector('.backdrop')).toBeNull();
  });

  it('renders modal when open', async () => {
    publishFakeRpc({});
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(el.shadowRoot.querySelector('.backdrop')).toBeTruthy();
    expect(el.shadowRoot.querySelector('.modal')).toBeTruthy();
  });

  it('reflects open as an attribute', async () => {
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    expect(el.hasAttribute('open')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Session list loading
// ---------------------------------------------------------------------------

describe('HistoryBrowser session list', () => {
  it('loads sessions when opened', async () => {
    const listSessions = vi.fn().mockResolvedValue([
      {
        session_id: 's1',
        timestamp: new Date().toISOString(),
        message_count: 3,
        preview: 'First session',
        first_role: 'user',
      },
    ]);
    publishFakeRpc({ 'ClaudeCodeService.history_list': listSessions });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(listSessions).toHaveBeenCalledOnce();
    const items = el.shadowRoot.querySelectorAll('.session-item');
    expect(items.length).toBe(1);
    expect(items[0].textContent).toContain('First session');
  });

  it('shows empty state when no sessions', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.empty-list').textContent,
    ).toContain('No sessions');
  });

  it('handles session list RPC error gracefully', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockRejectedValue(new Error('db exploded')),
    });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const el = mountBrowser({ open: true });
      await settle(el);
      // Says what went wrong, and does not say "no sessions yet".
      expect(
        el.shadowRoot.querySelector('.error-note').textContent,
      ).toContain('db exploded');
      expect(el.shadowRoot.querySelector('.empty-list')).toBeNull();
      expect(consoleSpy).toHaveBeenCalled();
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('does not load when RPC is not connected', async () => {
    // No publishFakeRpc — RPC not available.
    const el = mountBrowser({ open: true });
    await settle(el);
    // No crash; empty state or loading state shows.
    expect(el._sessions).toEqual([]);
  });

  it('reloads sessions on re-open', async () => {
    const listSessions = vi.fn().mockResolvedValue([]);
    publishFakeRpc({ 'ClaudeCodeService.history_list': listSessions });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(listSessions).toHaveBeenCalledTimes(1);
    el.open = false;
    await settle(el);
    el.open = true;
    await settle(el);
    expect(listSessions).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// The reads answer a union: a list, or {error}
// ---------------------------------------------------------------------------

describe('HistoryBrowser read failures', () => {
  it('shows why the listing is missing instead of "no sessions"', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue({ error: 'Could not read the session history' }),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.error-note').textContent,
    ).toContain('Could not read the session history');
    expect(el.shadowRoot.querySelector('.empty-list')).toBeNull();
    expect(el._sessions).toEqual([]);
  });

  it('keeps "no sessions yet" for an empty history', async () => {
    // The other half of the union, and the point of telling them
    // apart: an empty list is not a failure.
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.empty-list').textContent,
    ).toContain('No sessions');
    expect(el.shadowRoot.querySelector('.error-note')).toBeNull();
  });

  it('stays quiet when the backend has no history at all', async () => {
    // A stripped-down backend answers "method not found". Nothing is
    // wrong, so nothing is reported.
    publishFakeRpc({});
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(el._listError).toBe('');
    expect(el.shadowRoot.querySelector('.error-note')).toBeNull();
  });

  it('shows why a session would not load, not "empty session"', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([oneSession()]),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue({
        error: 'Session s1 has no readable transcript',
      }),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.error-note').textContent,
    ).toContain('no readable transcript');
    expect(el.shadowRoot.querySelector('.preview-empty')).toBeNull();
  });

  it('clears the load failure when another session is picked', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([
          oneSession(),
          oneSession({ session_id: 's2', preview: 'two' }),
        ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockImplementation((sid) =>
          sid === 's1'
            ? Promise.resolve({ error: 'gone' })
            : Promise.resolve([{ role: 'user', content: 'here' }]),
        ),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await settle(el);
    expect(el._messagesError).toBe('gone');
    items[1].click();
    await settle(el);
    expect(el._messagesError).toBe('');
    expect(
      el.shadowRoot.querySelectorAll('.preview-message').length,
    ).toBe(1);
  });

  it('shows why a search failed instead of "no matches"', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi
        .fn()
        .mockResolvedValue({ error: 'index is rebuilding' }),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const input = el.shadowRoot.querySelector('.search-input');
    input.value = 'needle';
    input.dispatchEvent(new Event('input'));
    await new Promise((r) => setTimeout(r, SEARCH_DEBOUNCE_MS + 50));
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.error-note').textContent,
    ).toContain('index is rebuilding');
  });
});

// ---------------------------------------------------------------------------
// Session selection and message preview
// ---------------------------------------------------------------------------

describe('HistoryBrowser session selection', () => {
  async function setupWithSessions() {
    const listSessions = vi.fn().mockResolvedValue([
      {
        session_id: 's1',
        timestamp: new Date().toISOString(),
        message_count: 2,
        preview: 'Session one',
        first_role: 'user',
      },
      {
        session_id: 's2',
        timestamp: new Date().toISOString(),
        message_count: 5,
        preview: 'Session two',
        first_role: 'user',
      },
    ]);
    const getSession = vi.fn().mockImplementation((sid) => {
      if (sid === 's1') {
        return Promise.resolve([
          { role: 'user', content: 'hello from s1' },
          { role: 'assistant', content: '**bold** reply' },
        ]);
      }
      return Promise.resolve([
        { role: 'user', content: 'from s2' },
      ]);
    });
    publishFakeRpc({
      'ClaudeCodeService.history_list': listSessions,
      'ClaudeCodeService.history_load': getSession,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    return { el, getSession };
  }

  it('loads messages on session click', async () => {
    const { el, getSession } = await setupWithSessions();
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await settle(el);
    expect(getSession).toHaveBeenCalledWith('s1');
    const messages = el.shadowRoot.querySelectorAll('.preview-message');
    expect(messages.length).toBe(2);
  });

  it('marks selected session visually', async () => {
    const { el } = await setupWithSessions();
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await settle(el);
    expect(items[0].classList.contains('selected')).toBe(true);
    expect(items[1].classList.contains('selected')).toBe(false);
  });

  it('clicking the same session twice does not re-fetch', async () => {
    const { el, getSession } = await setupWithSessions();
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await settle(el);
    items[0].click();
    await settle(el);
    expect(getSession).toHaveBeenCalledTimes(1);
  });

  it('renders user content escaped, assistant as markdown', async () => {
    const { el } = await setupWithSessions();
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const messages = el.shadowRoot.querySelectorAll('.preview-message');
    // User message — no markdown rendering (no <strong>
    // even if content had **).
    expect(messages[0].classList.contains('role-user')).toBe(true);
    expect(messages[0].textContent).toContain('hello from s1');
    // Assistant message — markdown renders **bold** as <strong>.
    expect(messages[1].classList.contains('role-assistant')).toBe(true);
    expect(messages[1].querySelector('strong')).toBeTruthy();
  });

  it('shows system event styling for system_event messages', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([
        {
          session_id: 's1',
          timestamp: new Date().toISOString(),
          message_count: 1,
          preview: 'session',
          first_role: 'user',
        },
      ]),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([
        {
          role: 'user',
          content: '**Committed** abc1234',
          system_event: true,
        },
      ]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const msg = el.shadowRoot.querySelector('.preview-message');
    expect(msg.classList.contains('role-system')).toBe(true);
    // System events render as markdown too, so bold appears.
    expect(msg.querySelector('strong')).toBeTruthy();
  });

  it('shows empty-preview placeholder before selection', async () => {
    const { el } = await setupWithSessions();
    expect(
      el.shadowRoot.querySelector('.preview-empty').textContent,
    ).toContain('Select a session');
  });

  it('shows empty-session placeholder for empty sessions', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([
        {
          session_id: 's1',
          timestamp: new Date().toISOString(),
          message_count: 0,
          preview: '(empty)',
          first_role: 'user',
        },
      ]),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.preview-empty').textContent,
    ).toContain('Empty session');
  });

  it('discards stale message responses', async () => {
    // Rapid clicks between sessions — only the latest
    // response should win. Slower responses for earlier
    // clicks must not overwrite the newer selection.
    let resolvers = {};
    const getSession = vi.fn().mockImplementation((sid) => {
      return new Promise((resolve) => {
        resolvers[sid] = resolve;
      });
    });
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([
        {
          session_id: 's1',
          timestamp: new Date().toISOString(),
          message_count: 1,
          preview: 'one',
          first_role: 'user',
        },
        {
          session_id: 's2',
          timestamp: new Date().toISOString(),
          message_count: 1,
          preview: 'two',
          first_role: 'user',
        },
      ]),
      'ClaudeCodeService.history_load': getSession,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const items = el.shadowRoot.querySelectorAll('.session-item');
    // Click s1, then s2 before s1's response arrives.
    items[0].click();
    await el.updateComplete;
    items[1].click();
    await el.updateComplete;
    // s2's response arrives first.
    resolvers.s2([
      { role: 'user', content: 'from s2' },
    ]);
    await settle(el);
    // Now s1's stale response arrives.
    resolvers.s1([
      { role: 'user', content: 'STALE from s1' },
      { role: 'assistant', content: 'stale reply' },
    ]);
    await settle(el);
    // Preview reflects s2, not the stale s1 response.
    const messages = el.shadowRoot.querySelectorAll('.preview-message');
    expect(messages.length).toBe(1);
    expect(messages[0].textContent).toContain('from s2');
  });
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

describe('HistoryBrowser search', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('debounces search RPC by SEARCH_DEBOUNCE_MS', async () => {
    const search = vi.fn().mockResolvedValue([]);
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': search,
    });
    const el = mountBrowser({ open: true });
    // Drain the initial load under fake timers.
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    input.value = 'hello';
    input.dispatchEvent(new Event('input'));
    await el.updateComplete;
    // Before debounce elapses — no call yet.
    expect(search).not.toHaveBeenCalled();
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1);
    await el.updateComplete;
    expect(search).not.toHaveBeenCalled();
    // At the boundary — call fires.
    vi.advanceTimersByTime(1);
    await vi.runAllTimersAsync();
    await el.updateComplete;
    expect(search).toHaveBeenCalledOnce();
    expect(search).toHaveBeenCalledWith('hello');
  });

  it('coalesces rapid typing into a single RPC call', async () => {
    const search = vi.fn().mockResolvedValue([]);
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': search,
    });
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    // Type several characters rapidly.
    for (const v of ['h', 'he', 'hel', 'hell', 'hello']) {
      input.value = v;
      input.dispatchEvent(new Event('input'));
      await el.updateComplete;
      vi.advanceTimersByTime(50); // less than debounce
    }
    // Now let the debounce fire.
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await vi.runAllTimersAsync();
    await el.updateComplete;
    // Single RPC call with the final query.
    expect(search).toHaveBeenCalledOnce();
    expect(search).toHaveBeenCalledWith('hello');
  });

  it('empty query returns to session list mode', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    // Type a query.
    input.value = 'x';
    input.dispatchEvent(new Event('input'));
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await vi.runAllTimersAsync();
    await el.updateComplete;
    expect(el._searchMode).toBe(true);
    // Clear it.
    input.value = '';
    input.dispatchEvent(new Event('input'));
    await el.updateComplete;
    expect(el._searchMode).toBe(false);
  });

  it('whitespace-only query does not enter search mode', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    input.value = '   ';
    input.dispatchEvent(new Event('input'));
    await el.updateComplete;
    expect(el._searchMode).toBe(false);
  });

  it('renders search hits', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([
        {
          session_id: 's1',
          message_id: 'm1',
          role: 'user',
          content_preview: 'the matching content',
          timestamp: new Date().toISOString(),
        },
      ]),
    });
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    input.value = 'match';
    input.dispatchEvent(new Event('input'));
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await vi.runAllTimersAsync();
    await el.updateComplete;
    const hits = el.shadowRoot.querySelectorAll('.search-hit');
    expect(hits.length).toBe(1);
    expect(hits[0].textContent).toContain('the matching content');
  });

  it('clicking a hit selects its session', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([
        {
          session_id: 'target_session',
          message_id: 'm1',
          role: 'user',
          content_preview: 'hit',
          timestamp: new Date().toISOString(),
        },
      ]),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([
        { role: 'user', content: 'target content' },
      ]),
    });
    const el = mountBrowser({ open: true });
    await el.updateComplete;
    await vi.runAllTimersAsync();
    await el.updateComplete;

    const input = el.shadowRoot.querySelector('.search-input');
    input.value = 'match';
    input.dispatchEvent(new Event('input'));
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    await vi.runAllTimersAsync();
    await el.updateComplete;
    el.shadowRoot.querySelector('.search-hit').click();
    await vi.runAllTimersAsync();
    await el.updateComplete;
    expect(el._selectedSessionId).toBe('target_session');
    // Search mode exits.
    expect(el._searchMode).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Close actions
// ---------------------------------------------------------------------------

describe('HistoryBrowser close actions', () => {
  it('close button dispatches close event', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    el.shadowRoot.querySelector('.close-button').click();
    expect(listener).toHaveBeenCalledOnce();
  });

  it('backdrop click dispatches close event', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    const backdrop = el.shadowRoot.querySelector('.backdrop');
    // Simulate a click on the backdrop itself (target ===
    // currentTarget).
    backdrop.dispatchEvent(
      new MouseEvent('click', {
        bubbles: true,
        composed: true,
      }),
    );
    expect(listener).toHaveBeenCalledOnce();
  });

  it('clicking inside the modal does not close it', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    el.shadowRoot.querySelector('.modal').click();
    expect(listener).not.toHaveBeenCalled();
  });

  it('Escape key dispatches close event', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape' }),
    );
    expect(listener).toHaveBeenCalled();
  });

  it('Escape in search input clears query first', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const input = el.shadowRoot.querySelector('.search-input');
    input.value = 'hello';
    input.dispatchEvent(new Event('input'));
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    // Escape from the input.
    input.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
      }),
    );
    await el.updateComplete;
    // Query cleared; modal stays open.
    expect(el._searchQuery).toBe('');
    expect(listener).not.toHaveBeenCalled();
  });

  it('Escape in search input with empty query closes modal', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    const input = el.shadowRoot.querySelector('.search-input');
    input.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
      }),
    );
    expect(listener).toHaveBeenCalledOnce();
  });

  it('does not react to Escape when closed', async () => {
    const el = mountBrowser({ open: false });
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('close', listener);
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape' }),
    );
    expect(listener).not.toHaveBeenCalled();
  });

  it('removes document listener on disconnect', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.remove();
    // Listener removed — no throws, no ghost events.
    expect(() => {
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape' }),
      );
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Resume and fork
// ---------------------------------------------------------------------------

/** One listed session, enough for the footer to act on. */
function oneSession(overrides = {}) {
  return {
    session_id: 's1',
    timestamp: new Date().toISOString(),
    message_count: 1,
    preview: 'one',
    first_role: 'user',
    resumable: true,
    ...overrides,
  };
}

/**
 * The three RPCs the footer path needs: a listing, a load for the
 * preview pane, and the resume itself.
 */
function publishResumeRpc(resume, session = oneSession()) {
  return publishFakeRpc({
    'ClaudeCodeService.history_list': vi
      .fn()
      .mockResolvedValue([session]),
    'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([]),
    'ClaudeCodeService.resume_session': resume,
  });
}

describe('HistoryBrowser resume and fork', () => {
  it('offers fork wherever it offers resume', async () => {
    publishResumeRpc(vi.fn());
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(el.shadowRoot.querySelector('.resume-button')).toBeTruthy();
    expect(el.shadowRoot.querySelector('.fork-button')).toBeTruthy();
  });

  it('disables both buttons until a session is selected', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.resume-button').disabled,
    ).toBe(true);
    expect(el.shadowRoot.querySelector('.fork-button').disabled).toBe(
      true,
    );
  });

  it('enables both buttons after selection', async () => {
    publishResumeRpc(vi.fn());
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.resume-button').disabled,
    ).toBe(false);
    expect(el.shadowRoot.querySelector('.fork-button').disabled).toBe(
      false,
    );
  });

  it('resumes without forking', async () => {
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    publishResumeRpc(resume);
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    el.shadowRoot.querySelector('.resume-button').click();
    await settle(el);
    expect(resume).toHaveBeenCalledWith('s1', false);
  });

  it('forks on the fork button', async () => {
    const resume = vi
      .fn()
      .mockResolvedValue({ session_id: null, forked_from: 's1' });
    publishResumeRpc(resume);
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    el.shadowRoot.querySelector('.fork-button').click();
    await settle(el);
    expect(resume).toHaveBeenCalledWith('s1', true);
  });

  it('dispatches session-loaded with the action taken', async () => {
    publishResumeRpc(vi.fn().mockResolvedValue({ session_id: 's1' }));
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('session-loaded', listener);
    el.shadowRoot.querySelector('.resume-button').click();
    await settle(el);
    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0][0].detail).toEqual({
      session_id: 's1',
      action: 'resumed',
    });
  });

  it('reports a null session id as the fork it asked for', async () => {
    // A fork's real ID is minted by the CLI on the first turn, so the
    // reply carries null. The event still has to name the session the
    // user clicked, or the parent has nothing to act on.
    publishResumeRpc(
      vi.fn().mockResolvedValue({ session_id: null, forked_from: 's1' }),
    );
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const listener = vi.fn();
    el.addEventListener('session-loaded', listener);
    el.shadowRoot.querySelector('.fork-button').click();
    await settle(el);
    expect(listener.mock.calls[0][0].detail).toEqual({
      session_id: 's1',
      action: 'forked',
    });
  });

  it('toasts a refusal instead of loading it', async () => {
    // `{error, reason}` is a refusal the user can act on — a turn is
    // still running, or they are not on localhost. It must not read
    // as a session that opened.
    publishResumeRpc(
      vi.fn().mockResolvedValue({
        error: 'A turn is still running',
        reason: 'turn_in_progress',
      }),
    );
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const loaded = vi.fn();
    el.addEventListener('session-loaded', loaded);
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail);
    window.addEventListener('ac-toast', onToast);
    try {
      el.shadowRoot.querySelector('.resume-button').click();
      await settle(el);
    } finally {
      window.removeEventListener('ac-toast', onToast);
    }
    expect(loaded).not.toHaveBeenCalled();
    expect(toasts).toHaveLength(1);
    expect(toasts[0].message).toBe('A turn is still running');
  });

  it('does not dispatch session-loaded on RPC error', async () => {
    publishResumeRpc(vi.fn().mockRejectedValue(new Error('load failed')));
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const el = mountBrowser({ open: true });
      await settle(el);
      el.shadowRoot.querySelector('.session-item').click();
      await settle(el);
      const listener = vi.fn();
      el.addEventListener('session-loaded', listener);
      el.shadowRoot.querySelector('.resume-button').click();
      await settle(el);
      expect(listener).not.toHaveBeenCalled();
      expect(consoleSpy).toHaveBeenCalled();
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('names the action in flight and disables both buttons', async () => {
    let resolver;
    const resume = vi.fn().mockImplementation(
      () =>
        new Promise((r) => {
          resolver = r;
        }),
    );
    publishResumeRpc(resume);
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    el.shadowRoot.querySelector('.fork-button').click();
    await el.updateComplete;
    expect(
      el.shadowRoot.querySelector('.fork-button').textContent,
    ).toContain('Forking');
    // Both, not just the one clicked: they attach the one engine to
    // one conversation.
    expect(
      el.shadowRoot.querySelector('.resume-button').disabled,
    ).toBe(true);
    expect(el.shadowRoot.querySelector('.fork-button').disabled).toBe(
      true,
    );
    resolver({ session_id: 's1' });
    await settle(el);
  });

  it('ignores duplicate clicks', async () => {
    const resume = vi
      .fn()
      .mockImplementation(
        () => new Promise((r) => setTimeout(() => r({}), 50)),
      );
    publishResumeRpc(resume);
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const btn = el.shadowRoot.querySelector('.resume-button');
    btn.click();
    btn.click();
    btn.click();
    await settle(el);
    await new Promise((r) => setTimeout(r, 100));
    expect(resume).toHaveBeenCalledOnce();
  });

  it('labels a non-resumable session rather than failing on it', async () => {
    const resume = vi.fn();
    publishResumeRpc(resume, oneSession({ resumable: false }));
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.not-resumable'),
    ).toBeTruthy();
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.footer-note').textContent,
    ).toContain('browsable only');
    expect(
      el.shadowRoot.querySelector('.resume-button').disabled,
    ).toBe(true);
    // Clicking it anyway does nothing — the disabled attribute is the
    // UI's story, the guard is the one that has to hold.
    el._onResumeClick(false);
    await settle(el);
    expect(resume).not.toHaveBeenCalled();
  });

  it('treats a row with no resumable field as resumable', async () => {
    // An unknown must not cost the user a session they could open.
    const row = oneSession();
    delete row.resumable;
    publishResumeRpc(vi.fn().mockResolvedValue({ session_id: 's1' }), row);
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(el.shadowRoot.querySelector('.not-resumable')).toBeNull();
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.resume-button').disabled,
    ).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Resuming out from under a half-read conversation
// ---------------------------------------------------------------------------
//
// Resuming replaces the live session, and a fork spares the session being
// opened rather than the one being left. When the reader has not reached the
// end of the live conversation, that is worth a second click
// (specs5/5-webapp/chat.md § Resume Is Not Load). `liveUnread` is the chat
// panel's answer — this modal covers the transcript it is asking about.

describe('HistoryBrowser resume confirmation', () => {
  async function pickSession(resume, props = {}) {
    publishResumeRpc(resume);
    const el = mountBrowser({ open: true, ...props });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  function resumeBtn(el) {
    return el.shadowRoot.querySelector('.resume-button');
  }

  function forkBtn(el) {
    return el.shadowRoot.querySelector('.fork-button');
  }

  it('asks once before replacing a half-read conversation', async () => {
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    const el = await pickSession(resume, { liveUnread: true });
    resumeBtn(el).click();
    await settle(el);
    expect(resume).not.toHaveBeenCalled();
    expect(resumeBtn(el).textContent.trim()).toBe('Resume anyway?');
    expect(resumeBtn(el).title).toContain('Click again');
    resumeBtn(el).click();
    await settle(el);
    expect(resume).toHaveBeenCalledWith('s1', false);
  });

  it('asks before forking too', async () => {
    // A fork leaves the *browsed* session alone. The live one is replaced
    // either way, which is what the question is about.
    const resume = vi.fn().mockResolvedValue({ session_id: null });
    const el = await pickSession(resume, { liveUnread: true });
    forkBtn(el).click();
    await settle(el);
    expect(resume).not.toHaveBeenCalled();
    expect(forkBtn(el).textContent.trim()).toBe('Fork anyway?');
    forkBtn(el).click();
    await settle(el);
    expect(resume).toHaveBeenCalledWith('s1', true);
  });

  it('does not ask when the reader is at the end', async () => {
    // The ordinary case, and the reason this is conditional: a
    // confirmation on every resume is one that stops being read.
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    const el = await pickSession(resume, { liveUnread: false });
    resumeBtn(el).click();
    await settle(el);
    expect(resume).toHaveBeenCalledWith('s1', false);
  });

  it('asks again when the answer would be a different action', async () => {
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    const el = await pickSession(resume, { liveUnread: true });
    resumeBtn(el).click();
    await settle(el);
    forkBtn(el).click();
    await settle(el);
    expect(resume).not.toHaveBeenCalled();
    expect(resumeBtn(el).textContent.trim()).toBe('Resume');
    expect(forkBtn(el).textContent.trim()).toBe('Fork anyway?');
  });

  it('disarms when the selection moves', async () => {
    const resume = vi.fn().mockResolvedValue({ session_id: 's2' });
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([oneSession(), oneSession({ session_id: 's2' })]),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([]),
      'ClaudeCodeService.resume_session': resume,
    });
    const el = mountBrowser({ open: true, liveUnread: true });
    await settle(el);
    const rows = [...el.shadowRoot.querySelectorAll('.session-item')];
    rows[0].click();
    await settle(el);
    resumeBtn(el).click();
    await settle(el);
    expect(resumeBtn(el).textContent.trim()).toBe('Resume anyway?');
    // An armed Resume belongs to the session it was armed on.
    rows[1].click();
    await settle(el);
    expect(resumeBtn(el).textContent.trim()).toBe('Resume');
    resumeBtn(el).click();
    await settle(el);
    expect(resume).not.toHaveBeenCalled();
  });

  it('disarms when the modal closes', async () => {
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    const el = await pickSession(resume, { liveUnread: true });
    resumeBtn(el).click();
    await settle(el);
    el.open = false;
    await settle(el);
    el.open = true;
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(resumeBtn(el).textContent.trim()).toBe('Resume');
  });

  it('disarms after the resume it asked about', async () => {
    // A refusal leaves the modal open on the session the user picked, so
    // the next attempt has to be a fresh question rather than a click that
    // acts immediately.
    const resume = vi.fn().mockResolvedValue({ error: 'A turn is running' });
    const el = await pickSession(resume, { liveUnread: true });
    resumeBtn(el).click();
    await settle(el);
    resumeBtn(el).click();
    await settle(el);
    expect(resume).toHaveBeenCalledOnce();
    expect(resumeBtn(el).textContent.trim()).toBe('Resume');
  });

  it('defaults to not asking', async () => {
    // A parent that never sets the property is a browser that behaves the
    // way it did before the confirmation existed.
    const resume = vi.fn().mockResolvedValue({ session_id: 's1' });
    const el = await pickSession(resume);
    expect(el.liveUnread).toBe(false);
    resumeBtn(el).click();
    await settle(el);
    expect(resume).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// Delete
// ---------------------------------------------------------------------------

/** Collect `ac-toast` details raised while `fn` runs. */
async function withToasts(fn) {
  const toasts = [];
  const onToast = (e) => toasts.push(e.detail);
  window.addEventListener('ac-toast', onToast);
  try {
    await fn();
  } finally {
    window.removeEventListener('ac-toast', onToast);
  }
  return toasts;
}

describe('HistoryBrowser delete', () => {
  async function setupDelete(del, sessions = [oneSession()]) {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue(sessions),
      'ClaudeCodeService.history_load': vi.fn().mockResolvedValue([]),
      'ClaudeCodeService.history_delete': del,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  it('is disabled until a session is selected', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.delete-button').disabled,
    ).toBe(true);
  });

  it('arms on the first click and does not call the RPC', async () => {
    const del = vi.fn().mockResolvedValue({ status: 'deleted' });
    const el = await setupDelete(del);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    expect(del).not.toHaveBeenCalled();
    const btn = el.shadowRoot.querySelector('.delete-button');
    expect(btn.classList.contains('armed')).toBe(true);
    expect(btn.textContent).toContain('permanently');
  });

  it('deletes on the second click', async () => {
    const del = vi
      .fn()
      .mockResolvedValue({ session_id: 's1', status: 'deleted' });
    const el = await setupDelete(del);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    expect(del).toHaveBeenCalledWith('s1');
  });

  it('disarms when the selection moves', async () => {
    // An armed Delete belongs to the session it was armed on. The
    // alternative — re-aiming it at whatever is selected now — is a
    // click that deletes a session the user never armed.
    const del = vi.fn().mockResolvedValue({ status: 'deleted' });
    const el = await setupDelete(del, [
      oneSession(),
      oneSession({ session_id: 's2', preview: 'two' }),
    ]);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    el.shadowRoot.querySelectorAll('.session-item')[1].click();
    await settle(el);
    expect(
      el.shadowRoot
        .querySelector('.delete-button')
        .classList.contains('armed'),
    ).toBe(false);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    expect(del).not.toHaveBeenCalled();
  });

  it('disarms when the modal closes', async () => {
    const el = await setupDelete(vi.fn());
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    el.open = false;
    await settle(el);
    expect(el._confirmDelete).toBeNull();
  });

  it('keeps the row when the server refuses', async () => {
    // The live session is refused rather than half-deleted: the store
    // is a live mirror, so the transcript would come straight back.
    const del = vi.fn().mockResolvedValue({
      error: 'That is the current conversation. Start a new session first.',
      reason: 'session_live',
    });
    const el = await setupDelete(del);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    const toasts = await withToasts(async () => {
      el.shadowRoot.querySelector('.delete-button').click();
      await settle(el);
    });
    expect(toasts).toHaveLength(1);
    expect(toasts[0].message).toContain('Start a new session first');
    expect(toasts[0].type).toBe('warning');
    expect(el._sessions).toHaveLength(1);
    // And it disarms, so the next click is a fresh decision.
    expect(el._confirmDelete).toBeNull();
  });

  it('reports a failed delete rather than dropping the row', async () => {
    const del = vi.fn().mockRejectedValue(new Error('disk is read-only'));
    const el = await setupDelete(del);
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      el.shadowRoot.querySelector('.delete-button').click();
      await settle(el);
      const toasts = await withToasts(async () => {
        el.shadowRoot.querySelector('.delete-button').click();
        await settle(el);
      });
      expect(toasts[0].message).toContain('disk is read-only');
      expect(el._sessions).toHaveLength(1);
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('drops the row on the broadcast, not on the reply', async () => {
    // Every client has the same stale row, including this one, and
    // the broadcast is the only account that reaches all of them.
    const del = vi
      .fn()
      .mockResolvedValue({ session_id: 's1', status: 'deleted' });
    const el = await setupDelete(del);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    el.shadowRoot.querySelector('.delete-button').click();
    await settle(el);
    // The reply alone leaves the list alone.
    expect(el._sessions).toHaveLength(1);
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's1' },
      }),
    );
    await settle(el);
    expect(el._sessions).toEqual([]);
  });

  it('drops a row another client deleted', async () => {
    const el = await setupDelete(vi.fn(), [
      oneSession(),
      oneSession({ session_id: 's2', preview: 'two' }),
    ]);
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's2' },
      }),
    );
    await settle(el);
    expect(el._sessions.map((s) => s.session_id)).toEqual(['s1']);
    // s1 was the selection and is untouched.
    expect(el._selectedSessionId).toBe('s1');
  });

  it('clears the preview when the previewed session goes', async () => {
    const el = await setupDelete(vi.fn());
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's1' },
      }),
    );
    await settle(el);
    expect(el._selectedSessionId).toBeNull();
    expect(el._selectedMessages).toEqual([]);
    expect(
      el.shadowRoot.querySelector('.preview-empty').textContent,
    ).toContain('Select a session');
  });

  it('drops search hits for a deleted session', async () => {
    const el = await setupDelete(vi.fn());
    el._searchHits = [
      { session_id: 's1', content_preview: 'gone' },
      { session_id: 's2', content_preview: 'stays' },
    ];
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's1' },
      }),
    );
    await settle(el);
    expect(el._searchHits.map((h) => h.session_id)).toEqual(['s2']);
  });

  it('ignores a broadcast with no session id', async () => {
    const el = await setupDelete(vi.fn());
    window.dispatchEvent(
      new CustomEvent('session-deleted', { detail: {} }),
    );
    await settle(el);
    expect(el._sessions).toHaveLength(1);
  });

  it('stops listening once unmounted', async () => {
    const el = await setupDelete(vi.fn());
    el.remove();
    // No listener, no state change, and above all no throw from a
    // handler touching a torn-down component.
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's1' },
      }),
    );
    expect(el._sessions).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// State reset on close
// ---------------------------------------------------------------------------

describe('HistoryBrowser close state reset', () => {
  it('clears search state when closed', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
      'ClaudeCodeService.history_search': vi.fn().mockResolvedValue([]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    // Put into search mode manually (skip the debounce
    // machinery for directness).
    el._searchQuery = 'hello';
    el._searchMode = true;
    el._searchHits = [{ session_id: 's1', content_preview: 'x' }];
    el.open = false;
    await settle(el);
    expect(el._searchQuery).toBe('');
    expect(el._searchMode).toBe(false);
    expect(el._searchHits).toEqual([]);
  });

  it('clears selection when closed', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([
        {
          session_id: 's1',
          timestamp: new Date().toISOString(),
          message_count: 1,
          preview: 'one',
          first_role: 'user',
        },
      ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue([{ role: 'user', content: 'x' }]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(el._selectedSessionId).toBe('s1');
    el.open = false;
    await settle(el);
    expect(el._selectedSessionId).toBeNull();
    expect(el._selectedMessages).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Image thumbnails in preview
// ---------------------------------------------------------------------------

describe('HistoryBrowser preview images', () => {
  async function setupWithImage(messages) {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([
          {
            session_id: 's1',
            timestamp: new Date().toISOString(),
            message_count: messages.length,
            preview: 'with image',
            first_role: 'user',
          },
        ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue(messages),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  it('renders thumbnails for messages with images field', async () => {
    // A message carrying its own data URIs — what a live paste hands
    // us in the turn it happened. Loaded history uses pointers
    // instead; both shapes render.
    const el = await setupWithImage([
      {
        role: 'user',
        content: 'look at this',
        images: [
          'data:image/png;base64,AAA',
          'data:image/jpeg;base64,BBB',
        ],
      },
    ]);
    const thumbs = el.shadowRoot.querySelectorAll('.preview-image');
    expect(thumbs.length).toBe(2);
    expect(thumbs[0].src).toContain('base64,AAA');
    expect(thumbs[1].src).toContain('base64,BBB');
  });

  it('renders thumbnails for multimodal content arrays', async () => {
    // When the server sends multimodal content blocks
    // directly (e.g., some callers don't collapse to a
    // top-level images field), the normalizer pulls
    // image_url blocks out.
    const el = await setupWithImage([
      {
        role: 'user',
        content: [
          { type: 'text', text: 'see this' },
          {
            type: 'image_url',
            image_url: { url: 'data:image/png;base64,CCC' },
          },
        ],
      },
    ]);
    const thumbs = el.shadowRoot.querySelectorAll('.preview-image');
    expect(thumbs.length).toBe(1);
    expect(thumbs[0].src).toContain('base64,CCC');
  });

  it('does not render image section when images absent', async () => {
    const el = await setupWithImage([
      { role: 'user', content: 'plain text' },
    ]);
    expect(
      el.shadowRoot.querySelector('.preview-images'),
    ).toBeNull();
  });

  it('renders both text and images together', async () => {
    const el = await setupWithImage([
      {
        role: 'user',
        content: 'look',
        images: ['data:image/png;base64,X'],
      },
    ]);
    // Body carries text.
    const body = el.shadowRoot.querySelector('.preview-body');
    expect(body.textContent).toContain('look');
    // And thumbnail renders.
    expect(el.shadowRoot.querySelector('.preview-image')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Image pointers
// ---------------------------------------------------------------------------

describe('HistoryBrowser image pointers', () => {
  function ref(overrides = {}) {
    return {
      session_id: 's1',
      entry_uuid: 'u1',
      block: 0,
      media_type: 'image/png',
      ...overrides,
    };
  }

  /** Enough turns of the loop for sequential hydration to drain. */
  async function drain(el, rounds = 6) {
    for (let i = 0; i < rounds; i += 1) {
      await new Promise((r) => setTimeout(r, 0));
    }
    await el.updateComplete;
  }

  async function setupWithRefs(messages, image, sessions) {
    const rows =
      sessions ||
      [
        {
          session_id: 's1',
          timestamp: new Date().toISOString(),
          message_count: messages.length,
          preview: 'with image',
          first_role: 'user',
        },
      ];
    const load = vi.fn().mockResolvedValue(messages);
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue(rows),
      'ClaudeCodeService.history_load': load,
      'ClaudeCodeService.history_image': image,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  it('resolves a pointer to a thumbnail', async () => {
    const image = vi
      .fn()
      .mockResolvedValue({ data_uri: 'data:image/png;base64,PNG' });
    const el = await setupWithRefs(
      [{ role: 'user', content: 'look', image_refs: [ref()] }],
      image,
    );
    await drain(el);
    expect(image).toHaveBeenCalledWith('s1', 'u1', 0);
    const thumb = el.shadowRoot.querySelector('.preview-image');
    expect(thumb.src).toContain('base64,PNG');
  });

  it('holds the tile while the pointer is unresolved', async () => {
    // The box is there before the bytes are, so a session full of
    // screenshots does not reflow line by line as they land.
    let release;
    const image = vi.fn(
      () => new Promise((r) => { release = r; }),
    );
    const el = await setupWithRefs(
      [{ role: 'user', content: 'look', image_refs: [ref()] }],
      image,
    );
    expect(
      el.shadowRoot.querySelector('.preview-image-pending'),
    ).toBeTruthy();
    expect(el.shadowRoot.querySelector('.preview-image')).toBeNull();
    release({ data_uri: 'data:image/png;base64,PNG' });
    await drain(el);
    expect(el.shadowRoot.querySelector('.preview-image')).toBeTruthy();
    expect(
      el.shadowRoot.querySelector('.preview-image-pending'),
    ).toBeNull();
  });

  it('does not hold the transcript behind the images', async () => {
    // The text is readable while the bytes are still coming.
    const image = vi.fn(() => new Promise(() => {}));
    const el = await setupWithRefs(
      [{ role: 'user', content: 'look at this', image_refs: [ref()] }],
      image,
    );
    expect(el._loadingMessages).toBe(false);
    expect(
      el.shadowRoot.querySelector('.preview-body').textContent,
    ).toContain('look at this');
  });

  it('marks an image that is gone rather than dropping it', async () => {
    // An image silently absent from a prompt reads as a prompt that
    // never had one — a different conversation from the one that
    // happened.
    const image = vi
      .fn()
      .mockResolvedValue({ error: 'That image is no longer in the transcript' });
    const el = await setupWithRefs(
      [{ role: 'user', content: 'look', image_refs: [ref()] }],
      image,
    );
    await drain(el);
    const tile = el.shadowRoot.querySelector('.preview-image-missing');
    expect(tile).toBeTruthy();
    expect(tile.title).toContain('no longer in the transcript');
    expect(el.shadowRoot.querySelector('.preview-image')).toBeNull();
  });

  it('marks an image whose fetch threw', async () => {
    const image = vi.fn().mockRejectedValue(new Error('socket closed'));
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const el = await setupWithRefs(
        [{ role: 'user', content: 'look', image_refs: [ref()] }],
        image,
      );
      await drain(el);
      expect(
        el.shadowRoot.querySelector('.preview-image-missing').title,
      ).toContain('socket closed');
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('fetches each pointer once, one at a time', async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const image = vi.fn(async (_s, _u, block) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 0));
      inFlight -= 1;
      return { data_uri: `data:image/png;base64,B${block}` };
    });
    const el = await setupWithRefs(
      [
        {
          role: 'user',
          content: 'three',
          image_refs: [
            ref({ block: 0 }),
            ref({ block: 1 }),
            ref({ block: 2 }),
          ],
        },
      ],
      image,
    );
    await drain(el, 12);
    expect(image).toHaveBeenCalledTimes(3);
    expect(maxInFlight).toBe(1);
    const thumbs = [...el.shadowRoot.querySelectorAll('.preview-image')];
    expect(thumbs.map((t) => t.src.slice(-2))).toEqual([
      'B0',
      'B1',
      'B2',
    ]);
  });

  it('caches across a reselect, failures included', async () => {
    const image = vi
      .fn()
      .mockResolvedValueOnce({ data_uri: 'data:image/png;base64,PNG' })
      .mockResolvedValueOnce({ error: 'gone' });
    const el = await setupWithRefs(
      [
        {
          role: 'user',
          content: 'two',
          image_refs: [ref({ block: 0 }), ref({ block: 1 })],
        },
      ],
      image,
    );
    await drain(el, 10);
    expect(image).toHaveBeenCalledTimes(2);
    // Leave and come back: the pointers are unchanged, so nothing is
    // refetched — least of all the failure, which would stall the
    // preview again to reach the same answer.
    el._selectedSessionId = null;
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    await drain(el, 10);
    expect(image).toHaveBeenCalledTimes(2);
    expect(el.shadowRoot.querySelector('.preview-image')).toBeTruthy();
    expect(
      el.shadowRoot.querySelector('.preview-image-missing'),
    ).toBeTruthy();
  });

  it('abandons hydration when the selection moves on', async () => {
    // The generation guard is rechecked every iteration, not once:
    // the loop awaits, and the user is free to click away while it
    // does. Otherwise a slow first image pays for the rest of a
    // session nobody is looking at any more.
    const seen = [];
    let release;
    const image = vi.fn((_s, _u, block) => {
      seen.push(block);
      return new Promise((r) => { release = r; });
    });
    const row = (session_id, preview) => ({
      session_id,
      timestamp: new Date().toISOString(),
      message_count: 1,
      preview,
      first_role: 'user',
    });
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([row('s1', 'one'), row('s2', 'two')]),
      // s2 has no images of its own, so anything fetched after the
      // click could only come from the abandoned loop.
      'ClaudeCodeService.history_load': vi.fn(async (id) =>
        id === 's1'
          ? [
              {
                role: 'user',
                content: 'many',
                image_refs: [
                  ref({ block: 0 }),
                  ref({ block: 1 }),
                  ref({ block: 2 }),
                ],
              },
            ]
          : [{ role: 'user', content: 'no images here' }],
      ),
      'ClaudeCodeService.history_image': image,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelectorAll('.session-item')[0].click();
    await settle(el);
    // Exactly one fetch is in flight and blocked.
    expect(seen).toEqual([0]);

    el.shadowRoot.querySelectorAll('.session-item')[1].click();
    await settle(el);
    // Let the in-flight one land. Its result is cached — the loop
    // just must not go on to ask for blocks 1 and 2.
    release({ data_uri: 'data:image/png;base64,X' });
    await drain(el, 10);
    expect(seen).toEqual([0]);
    expect(
      el.shadowRoot.querySelector('.preview-body').textContent,
    ).toContain('no images here');
  });

  it('ignores a pointer that names nothing fetchable', async () => {
    const image = vi.fn();
    const el = await setupWithRefs(
      [
        {
          role: 'user',
          content: 'broken',
          image_refs: [{ media_type: 'image/png' }],
        },
      ],
      image,
    );
    await drain(el);
    expect(image).not.toHaveBeenCalled();
    // Still a tile: the entry said there was an image there.
    expect(
      el.shadowRoot.querySelector('.preview-image-pending'),
    ).toBeTruthy();
  });

  it('renders data URIs and pointers on the same message', async () => {
    const image = vi
      .fn()
      .mockResolvedValue({ data_uri: 'data:image/png;base64,REF' });
    const el = await setupWithRefs(
      [
        {
          role: 'user',
          content: 'both',
          images: ['data:image/png;base64,LIVE'],
          image_refs: [ref()],
        },
      ],
      image,
    );
    await drain(el);
    const thumbs = [...el.shadowRoot.querySelectorAll('.preview-image')];
    expect(thumbs).toHaveLength(2);
    expect(thumbs[0].src).toContain('LIVE');
    expect(thumbs[1].src).toContain('REF');
  });
});

// ---------------------------------------------------------------------------
// Hover action buttons (copy, paste-to-prompt)
// ---------------------------------------------------------------------------

describe('HistoryBrowser message action buttons', () => {
  async function setupWithMessages(messages) {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([
          {
            session_id: 's1',
            timestamp: new Date().toISOString(),
            message_count: messages.length,
            preview: 'test',
            first_role: 'user',
          },
        ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue(messages),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  function installFakeClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const original = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    return {
      writeText,
      restore() {
        if (original === undefined) {
          delete navigator.clipboard;
        } else {
          Object.defineProperty(navigator, 'clipboard', {
            value: original,
            configurable: true,
          });
        }
      },
    };
  }

  it('renders toolbar with copy and paste buttons per message', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'hello' },
      { role: 'assistant', content: 'hi' },
    ]);
    const toolbars = el.shadowRoot.querySelectorAll('.preview-toolbar');
    expect(toolbars.length).toBe(2);
    for (const tb of toolbars) {
      const btns = tb.querySelectorAll('.preview-action-button');
      expect(btns.length).toBe(2);
    }
  });

  it('copy button writes raw text to clipboard', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const el = await setupWithMessages([
        { role: 'assistant', content: 'use **bold** here' },
      ]);
      // Copy button is the first action button.
      const copyBtn = el.shadowRoot
        .querySelector('.preview-toolbar')
        .querySelectorAll('.preview-action-button')[0];
      copyBtn.click();
      await settle(el);
      expect(writeText).toHaveBeenCalledOnce();
      // Raw markdown source, not rendered HTML.
      expect(writeText).toHaveBeenCalledWith('use **bold** here');
    } finally {
      restore();
    }
  });

  it('copy emits success toast', async () => {
    const { restore } = installFakeClipboard();
    try {
      const el = await setupWithMessages([
        { role: 'user', content: 'hi' },
      ]);
      const toastListener = vi.fn();
      window.addEventListener('ac-toast', toastListener);
      try {
        el.shadowRoot
          .querySelector('.preview-toolbar')
          .querySelectorAll('.preview-action-button')[0]
          .click();
        await settle(el);
        const detail = toastListener.mock.calls.at(-1)[0].detail;
        expect(detail.type).toBe('success');
        expect(detail.message).toMatch(/copied/i);
      } finally {
        window.removeEventListener('ac-toast', toastListener);
      }
    } finally {
      restore();
    }
  });

  it('copy emits warning when clipboard unavailable', async () => {
    // Simulate insecure context — no navigator.clipboard.
    const original = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    });
    try {
      const el = await setupWithMessages([
        { role: 'user', content: 'hi' },
      ]);
      const toastListener = vi.fn();
      window.addEventListener('ac-toast', toastListener);
      try {
        el.shadowRoot
          .querySelector('.preview-toolbar')
          .querySelectorAll('.preview-action-button')[0]
          .click();
        await settle(el);
        const detail = toastListener.mock.calls.at(-1)[0].detail;
        expect(detail.type).toBe('warning');
      } finally {
        window.removeEventListener('ac-toast', toastListener);
      }
    } finally {
      if (original === undefined) {
        delete navigator.clipboard;
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: original,
          configurable: true,
        });
      }
    }
  });

  it('paste button dispatches paste-to-prompt event', async () => {
    const el = await setupWithMessages([
      { role: 'assistant', content: 'paste this' },
    ]);
    const listener = vi.fn();
    el.addEventListener('paste-to-prompt', listener);
    // Paste button is the second action button.
    const pasteBtn = el.shadowRoot
      .querySelector('.preview-toolbar')
      .querySelectorAll('.preview-action-button')[1];
    pasteBtn.click();
    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0][0].detail).toEqual({
      text: 'paste this',
    });
  });

  it('paste-to-prompt bubbles across shadow DOM', async () => {
    // Chat panel listens at document level.
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    const listener = vi.fn();
    document.body.addEventListener('paste-to-prompt', listener);
    try {
      el.shadowRoot
        .querySelector('.preview-toolbar')
        .querySelectorAll('.preview-action-button')[1]
        .click();
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      document.body.removeEventListener(
        'paste-to-prompt',
        listener,
      );
    }
  });

  it('paste closes the modal', async () => {
    // Modal closes so the user sees the chat input.
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    const closeListener = vi.fn();
    el.addEventListener('close', closeListener);
    el.shadowRoot
      .querySelector('.preview-toolbar')
      .querySelectorAll('.preview-action-button')[1]
      .click();
    expect(closeListener).toHaveBeenCalledOnce();
  });

  it('actions work on multimodal messages (text extracted)', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const el = await setupWithMessages([
        {
          role: 'user',
          content: [
            { type: 'text', text: 'hello' },
            {
              type: 'image_url',
              image_url: { url: 'data:image/png;base64,X' },
            },
            { type: 'text', text: 'world' },
          ],
        },
      ]);
      el.shadowRoot
        .querySelector('.preview-toolbar')
        .querySelectorAll('.preview-action-button')[0]
        .click();
      await settle(el);
      expect(writeText).toHaveBeenCalledWith('hello\nworld');
    } finally {
      restore();
    }
  });

  it('copy on empty content is a no-op', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const el = await setupWithMessages([
        {
          role: 'user',
          content: [
            {
              type: 'image_url',
              image_url: { url: 'data:image/png;base64,X' },
            },
          ],
        },
      ]);
      el.shadowRoot
        .querySelector('.preview-toolbar')
        .querySelectorAll('.preview-action-button')[0]
        .click();
      await settle(el);
      expect(writeText).not.toHaveBeenCalled();
    } finally {
      restore();
    }
  });
});

// ---------------------------------------------------------------------------
// Context menu
// ---------------------------------------------------------------------------

describe('HistoryBrowser context menu', () => {
  async function setupWithMessages(messages) {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([
          {
            session_id: 's1',
            timestamp: new Date().toISOString(),
            message_count: messages.length,
            preview: 'test',
            first_role: 'user',
          },
        ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue(messages),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    return el;
  }

  function fireContextMenu(el, x = 100, y = 200) {
    const msg = el.shadowRoot.querySelector('.preview-message');
    const ev = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
    });
    msg.dispatchEvent(ev);
  }

  it('right-click opens the context menu', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'hi' },
    ]);
    expect(el.shadowRoot.querySelector('.context-menu')).toBeNull();
    fireContextMenu(el);
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.context-menu'),
    ).toBeTruthy();
  });

  it('context menu positions at click point', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'hi' },
    ]);
    fireContextMenu(el, 150, 250);
    await settle(el);
    const menu = el.shadowRoot.querySelector('.context-menu');
    expect(menu.style.left).toBe('150px');
    expect(menu.style.top).toBe('250px');
  });

  it('renders four context menu items', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'hi' },
    ]);
    fireContextMenu(el);
    await settle(el);
    const items = el.shadowRoot.querySelectorAll(
      '.context-menu-item',
    );
    expect(items.length).toBe(4);
    // Verify labels.
    const labels = Array.from(items).map((i) =>
      i.textContent.trim(),
    );
    expect(labels[0]).toMatch(/left/i);
    expect(labels[1]).toMatch(/right/i);
    expect(labels[2]).toMatch(/copy/i);
    expect(labels[3]).toMatch(/paste/i);
  });

  it('contextmenu event has preventDefault called', async () => {
    // Stops the browser's native right-click menu from
    // appearing on top of ours.
    const el = await setupWithMessages([
      { role: 'user', content: 'hi' },
    ]);
    const msg = el.shadowRoot.querySelector('.preview-message');
    const ev = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: 10,
      clientY: 10,
    });
    const spy = vi.spyOn(ev, 'preventDefault');
    msg.dispatchEvent(ev);
    expect(spy).toHaveBeenCalled();
  });

  it('Load in Left Panel dispatches load-diff-panel', async () => {
    const el = await setupWithMessages([
      { role: 'assistant', content: 'panel content' },
    ]);
    const listener = vi.fn();
    el.addEventListener('load-diff-panel', listener);
    fireContextMenu(el);
    await settle(el);
    const items = el.shadowRoot.querySelectorAll(
      '.context-menu-item',
    );
    items[0].click(); // Load in Left Panel
    expect(listener).toHaveBeenCalledOnce();
    const detail = listener.mock.calls[0][0].detail;
    expect(detail.content).toBe('panel content');
    expect(detail.panel).toBe('left');
    expect(detail.label).toContain('assistant');
  });

  it('Load in Right Panel dispatches with panel=right', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'text' },
    ]);
    const listener = vi.fn();
    el.addEventListener('load-diff-panel', listener);
    fireContextMenu(el);
    await settle(el);
    const items = el.shadowRoot.querySelectorAll(
      '.context-menu-item',
    );
    items[1].click(); // Load in Right Panel
    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0][0].detail.panel).toBe('right');
  });

  it('load-diff-panel bubbles across shadow DOM', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    const listener = vi.fn();
    document.body.addEventListener('load-diff-panel', listener);
    try {
      fireContextMenu(el);
      await settle(el);
      el.shadowRoot.querySelectorAll('.context-menu-item')[0].click();
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      document.body.removeEventListener(
        'load-diff-panel',
        listener,
      );
    }
  });

  it('Load in Panel does NOT close the modal', async () => {
    // User may want to load both panels from the history
    // browser in succession — don't close after each.
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    const closeListener = vi.fn();
    el.addEventListener('close', closeListener);
    fireContextMenu(el);
    await settle(el);
    el.shadowRoot.querySelectorAll('.context-menu-item')[0].click();
    expect(closeListener).not.toHaveBeenCalled();
  });

  it('Load in Panel closes the context menu', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    expect(el._contextMenu).not.toBeNull();
    el.shadowRoot.querySelectorAll('.context-menu-item')[0].click();
    await settle(el);
    expect(el._contextMenu).toBeNull();
  });

  it('Copy item uses the clipboard path', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const original = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    try {
      const el = await setupWithMessages([
        { role: 'user', content: 'copy me' },
      ]);
      fireContextMenu(el);
      await settle(el);
      el.shadowRoot
        .querySelectorAll('.context-menu-item')[2]
        .click();
      await settle(el);
      expect(writeText).toHaveBeenCalledWith('copy me');
    } finally {
      if (original === undefined) {
        delete navigator.clipboard;
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: original,
          configurable: true,
        });
      }
    }
  });

  it('Copy item closes the context menu', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    el.shadowRoot.querySelectorAll('.context-menu-item')[2].click();
    await settle(el);
    expect(el._contextMenu).toBeNull();
  });

  it('Paste item dispatches paste-to-prompt and closes modal', async () => {
    const el = await setupWithMessages([
      { role: 'assistant', content: 'paste' },
    ]);
    const pasteListener = vi.fn();
    const closeListener = vi.fn();
    el.addEventListener('paste-to-prompt', pasteListener);
    el.addEventListener('close', closeListener);
    fireContextMenu(el);
    await settle(el);
    el.shadowRoot.querySelectorAll('.context-menu-item')[3].click();
    await settle(el);
    expect(pasteListener).toHaveBeenCalledOnce();
    expect(pasteListener.mock.calls[0][0].detail.text).toBe('paste');
    expect(closeListener).toHaveBeenCalledOnce();
  });

  it('clicking outside the menu dismisses it', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    expect(el._contextMenu).not.toBeNull();
    // Click on the backdrop (outside the menu and modal
    // content alike).
    document.body.click();
    await settle(el);
    expect(el._contextMenu).toBeNull();
  });

  it('Escape closes the context menu without closing modal', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    const closeListener = vi.fn();
    el.addEventListener('close', closeListener);
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape' }),
    );
    await settle(el);
    expect(el._contextMenu).toBeNull();
    expect(closeListener).not.toHaveBeenCalled();
  });

  it('Escape after menu already closed closes the modal', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    // No context menu open.
    const closeListener = vi.fn();
    el.addEventListener('close', closeListener);
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape' }),
    );
    expect(closeListener).toHaveBeenCalledOnce();
  });

  it('closing the modal also closes context menu', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    expect(el._contextMenu).not.toBeNull();
    el.shadowRoot.querySelector('.close-button').click();
    await settle(el);
    expect(el._contextMenu).toBeNull();
  });

  it('opening the modal again resets context menu state', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    fireContextMenu(el);
    await settle(el);
    // Force-close via prop.
    el.open = false;
    await settle(el);
    expect(el._contextMenu).toBeNull();
  });

  it('document click listener removed on disconnect', async () => {
    const el = await setupWithMessages([
      { role: 'user', content: 'x' },
    ]);
    el.remove();
    // No crash when a click fires after disconnect.
    expect(() => document.body.click()).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Subagent listing
//
// The only way into a past session's subagent transcripts. A turn read
// back off disk carries no subagent rows — the transcript records each
// subagent under its own id without attributing it to the turn that
// spawned it — so the per-turn "View subagents" affordance has nothing to
// offer here and this listing is what does
// (specs5/5-webapp/subagent-browser.md § Historical Transcripts).
// ---------------------------------------------------------------------------

describe('HistoryBrowser subagent listing', () => {
  const SESSIONS = [
    {
      session_id: 's1',
      timestamp: new Date().toISOString(),
      message_count: 2,
      preview: 'Session one',
      first_role: 'user',
    },
    {
      session_id: 's2',
      timestamp: new Date().toISOString(),
      message_count: 1,
      preview: 'Session two',
      first_role: 'user',
    },
  ];

  function row(over = {}) {
    return {
      agent_id: 'agent_1',
      subpath: 'subagents/agent-agent_1',
      message_count: 4,
      preview: 'Find every call site of authenticate()',
      ...over,
    };
  }

  async function setup(listing, extra = {}) {
    const listSubagents =
      typeof listing === 'function'
        ? vi.fn().mockImplementation(listing)
        : vi.fn().mockResolvedValue(listing);
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue(SESSIONS),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue([{ role: 'user', content: 'hello' }]),
      'ClaudeCodeService.list_subagent_transcripts': listSubagents,
      ...extra,
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    return { el, listSubagents };
  }

  function chips(el) {
    return [...el.shadowRoot.querySelectorAll('.subagent-chip')];
  }

  it('lists the selected session’s subagents', async () => {
    const { el, listSubagents } = await setup([
      row({
        agent_id: 'a1',
        agent_type: 'explore',
        description: 'find auth',
      }),
      row({
        agent_id: 'a2',
        agent_type: 'plan',
        description: 'sketch a fix',
      }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    // The browsed session, explicitly: the panel's default is the live
    // one, which is not what is on screen here.
    expect(listSubagents).toHaveBeenCalledWith('s1');
    const labels = chips(el).map((c) => c.textContent.trim());
    expect(labels[0]).toContain('explore: find auth');
    expect(labels[1]).toContain('plan: sketch a fix');
  });

  it('counts the transcripts in the bar’s title', async () => {
    const { el } = await setup([
      row({ agent_id: 'a1' }),
      row({ agent_id: 'a2' }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.subagents-title').textContent,
    ).toContain('2 subagents');
  });

  it('says "1 subagent" for a single transcript', async () => {
    const { el } = await setup([row({ agent_id: 'a1' })]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.subagents-title').textContent,
    ).toContain('1 subagent');
  });

  it('shows each transcript’s message count', async () => {
    const { el } = await setup([
      row({ agent_id: 'a1', message_count: 7 }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot
        .querySelector('.subagent-chip-count')
        .textContent.trim(),
    ).toBe('7');
  });

  it('falls back to the prompt preview when the metadata is absent', async () => {
    // A session imported from disk rather than mirrored live: the CLI's
    // `agent_metadata` entry never reached the store, so there is no
    // description or type to label the row with.
    const { el } = await setup([
      row({ agent_id: 'a1', preview: 'Search for the token parser' }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(chips(el)[0].textContent).toContain(
      'Search for the token parser',
    );
  });

  it('falls back to the agent id when there is nothing else', async () => {
    const { el } = await setup([
      row({ agent_id: 'agent_xyz', preview: '' }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(chips(el)[0].textContent).toContain('agent_xyz');
  });

  it('skips a row with no agent id', async () => {
    // The id is what opens a transcript, so a row without one is a
    // listing entry with nothing behind it.
    const { el } = await setup([
      row({ agent_id: 'a1' }),
      { subpath: 'subagents/junk', message_count: 2, preview: 'orphan' },
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(chips(el).length).toBe(1);
  });

  it('draws nothing before a session is selected', async () => {
    const { el, listSubagents } = await setup([row()]);
    expect(el.shadowRoot.querySelector('.subagents-bar')).toBeNull();
    expect(listSubagents).not.toHaveBeenCalled();
  });

  it('draws nothing for a session that delegated nothing', async () => {
    const { el } = await setup([]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(el.shadowRoot.querySelector('.subagents-bar')).toBeNull();
  });

  it('shows why an unreadable listing is missing', async () => {
    // `{error}` rather than an empty list, so "delegated nothing" and
    // "could not tell" stay different sentences.
    const { el } = await setup({ error: 'Could not read the subagents' });
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.subagents-error').textContent,
    ).toContain('Could not read the subagents');
  });

  it('says it is still reading rather than saying none', async () => {
    const { el } = await setup(() => new Promise(() => {}));
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelector('.subagents-note').textContent,
    ).toContain('reading');
  });

  it('draws no error when the backend does not expose the method', async () => {
    // A stripped-down backend has no subagents to list, which the
    // listing's absence already says; an error banner would report a
    // problem that is not one.
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue(SESSIONS),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue([{ role: 'user', content: 'hello' }]),
    });
    const el = mountBrowser({ open: true });
    await settle(el);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(el.shadowRoot.querySelector('.subagents-bar')).toBeNull();
    expect(el._subagentsError).toBe('');
  });

  it('does not hold the transcript behind the listing', async () => {
    // Two independent reads. A listing that never answers must not cost
    // the user the conversation.
    const { el } = await setup(() => new Promise(() => {}));
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(
      el.shadowRoot.querySelectorAll('.preview-message').length,
    ).toBe(1);
  });

  it('does not lose the listing to the messages read’s generation', async () => {
    // Both reads start on the same click; each bumping the other's
    // counter would have them discard each other's answers.
    const { el } = await setup([row({ agent_id: 'a1' })]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(chips(el).length).toBe(1);
    expect(
      el.shadowRoot.querySelectorAll('.preview-message').length,
    ).toBe(1);
  });

  it('discards a stale listing response', async () => {
    const resolvers = {};
    const { el } = await setup(
      (sid) =>
        new Promise((resolve) => {
          resolvers[sid] = resolve;
        }),
    );
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await el.updateComplete;
    items[1].click();
    await el.updateComplete;
    // Empty previews so the id is what labels each chip, which is what
    // tells the two answers apart on screen.
    resolvers.s2([row({ agent_id: 'from_s2', preview: '' })]);
    await settle(el);
    resolvers.s1([
      row({ agent_id: 'stale_a', preview: '' }),
      row({ agent_id: 'stale_b', preview: '' }),
    ]);
    await settle(el);
    const labels = chips(el).map((c) => c.textContent);
    expect(labels.length).toBe(1);
    expect(labels[0]).toContain('from_s2');
  });

  it('hands the agent id, its label and the browsed session over', async () => {
    const { el } = await setup([
      row({
        agent_id: 'a1',
        agent_type: 'explore',
        description: 'find auth',
      }),
    ]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const seen = [];
    el.addEventListener('view-subagents-requested', (e) => seen.push(e));
    chips(el)[0].click();
    await settle(el);
    expect(seen.length).toBe(1);
    expect(seen[0].detail).toEqual({
      agents: [{ agent_id: 'a1', label: 'explore: find auth' }],
      session_id: 's1',
    });
    // Composed and bubbling, because the chat panel listens on itself
    // and this element lives inside its shadow root.
    expect(seen[0].bubbles).toBe(true);
    expect(seen[0].composed).toBe(true);
  });

  it('closes the modal on opening a transcript', async () => {
    const { el } = await setup([row({ agent_id: 'a1' })]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    const closed = vi.fn();
    el.addEventListener('close', closed);
    chips(el)[0].click();
    await settle(el);
    // The tab it just asked for is behind this modal.
    expect(closed).toHaveBeenCalledOnce();
  });

  it('replaces the listing when the selection moves', async () => {
    const { el } = await setup((sid) =>
      Promise.resolve([row({ agent_id: `${sid}_agent`, preview: '' })]),
    );
    const items = el.shadowRoot.querySelectorAll('.session-item');
    items[0].click();
    await settle(el);
    expect(chips(el)[0].textContent).toContain('s1_agent');
    items[1].click();
    await settle(el);
    const labels = chips(el).map((c) => c.textContent);
    expect(labels.length).toBe(1);
    expect(labels[0]).toContain('s2_agent');
  });

  it('drops the listing when the selected session is deleted', async () => {
    // Deleting a session takes its subagent directory with it.
    const { el } = await setup([row({ agent_id: 'a1' })]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    expect(chips(el).length).toBe(1);
    window.dispatchEvent(
      new CustomEvent('session-deleted', {
        detail: { session_id: 's1' },
      }),
    );
    await settle(el);
    expect(el.shadowRoot.querySelector('.subagents-bar')).toBeNull();
    expect(el._subagents).toEqual([]);
  });

  it('clears the listing when the modal closes', async () => {
    const { el } = await setup([row({ agent_id: 'a1' })]);
    el.shadowRoot.querySelector('.session-item').click();
    await settle(el);
    el.open = false;
    await settle(el);
    expect(el._subagents).toEqual([]);
    expect(el._subagentsError).toBe('');
  });
});