// Tests for chat-panel/session-title.js — the action bar's title chip.
//
// Driven against a stand-in panel and Lit's own `render`, because the
// module's contract is the state it keeps on the panel and the chip it
// draws from that state; the full chat panel adds nothing to either.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from 'lit';

import {
  attachSessionTitle,
  detachSessionTitle,
  renderSessionTitle,
  syncSessionLabel,
} from './session-title.js';

function fakePanel(rpc = {}) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const panel = {
    rpcConnected: true,
    _sessionLabel: null,
    _sessionTitleEditing: false,
    _sessionTitlePending: false,
    updateComplete: Promise.resolve(),
    shadowRoot: host,
    rpcExtract: vi.fn(async (method, ...args) => {
      const impl = rpc[method];
      if (!impl) throw new Error(`unexpected ${method}`);
      return impl(...args);
    }),
    draw() {
      render(renderSessionTitle(panel), host);
      return host;
    },
  };
  return panel;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

function fire(name, detail) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

let panel;

afterEach(() => {
  if (panel) {
    detachSessionTitle(panel);
    panel.shadowRoot.remove();
    panel = null;
  }
});

describe('the title chip', () => {
  it('renders nothing before the session has an ID', () => {
    panel = fakePanel();
    expect(panel.draw().querySelector('.session-title')).toBeNull();
  });

  it('shows the title and the tag', async () => {
    panel = fakePanel({
      'ClaudeCodeService.history_label': async (sid) => ({
        session_id: sid, title: 'Parser rewrite', tag: 'wip',
      }),
    });
    await syncSessionLabel(panel, 's1');
    const chip = panel.draw().querySelector('.session-title');
    expect(chip.textContent).toContain('Parser rewrite');
    expect(chip.querySelector('.session-title-tag').textContent).toBe('wip');
    expect(chip.classList.contains('untitled')).toBe(false);
  });

  it('says untitled when there is no title yet', async () => {
    panel = fakePanel({
      'ClaudeCodeService.history_label': async (sid) => ({
        session_id: sid, title: '', tag: '',
      }),
    });
    await syncSessionLabel(panel, 's1');
    const chip = panel.draw().querySelector('.session-title');
    expect(chip.textContent.trim()).toBe('Untitled session');
    expect(chip.classList.contains('untitled')).toBe(true);
  });

  it('a failed read still shows the chip, untitled', async () => {
    panel = fakePanel({
      'ClaudeCodeService.history_label': async () => {
        throw new Error('down');
      },
    });
    await syncSessionLabel(panel, 's1');
    expect(panel.draw().querySelector('.session-title')).not.toBeNull();
  });

  it('a late answer for a session no longer on screen is dropped', async () => {
    let answer;
    panel = fakePanel({
      'ClaudeCodeService.history_label': (sid) =>
        sid === 's1'
          ? new Promise((r) => { answer = r; })
          : { session_id: sid, title: 'Two', tag: '' },
    });
    const first = syncSessionLabel(panel, 's1');
    await syncSessionLabel(panel, 's2');
    answer({ session_id: 's1', title: 'One', tag: '' });
    await first;
    expect(panel._sessionLabel).toEqual({ session_id: 's2', title: 'Two', tag: '' });
  });
});

describe('renaming from the chip', () => {
  beforeEach(async () => {
    panel = fakePanel({
      'ClaudeCodeService.history_label': async (sid) => ({
        session_id: sid, title: 'Old', tag: '',
      }),
      'ClaudeCodeService.history_rename': vi.fn(async (sid, title) => ({
        session_id: sid, title, tag: '',
      })),
    });
    await syncSessionLabel(panel, 's1');
  });

  async function edit(value, key = 'Enter') {
    panel.draw().querySelector('.session-title').click();
    await flush();
    const input = panel.draw().querySelector('.session-title-input');
    input.value = value;
    input.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    await flush();
  }

  it('a click opens an input holding the title', async () => {
    panel.draw().querySelector('.session-title').click();
    expect(panel.draw().querySelector('.session-title-input').value).toBe('Old');
  });

  it('Enter renames the session on screen', async () => {
    await edit('  New name ');
    expect(panel.rpcExtract).toHaveBeenCalledWith(
      'ClaudeCodeService.history_rename', 's1', 'New name',
    );
    expect(panel._sessionTitleEditing).toBe(false);
  });

  it('Escape, an empty title and an unchanged one do not call', async () => {
    await edit('x', 'Escape');
    await edit('   ');
    await edit('Old');
    expect(panel.rpcExtract).not.toHaveBeenCalledWith(
      'ClaudeCodeService.history_rename', expect.anything(), expect.anything(),
    );
  });

  it('a refusal toasts and keeps the input', async () => {
    panel.rpcExtract.mockImplementationOnce(async () => ({ error: 'no' }));
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail.message);
    window.addEventListener('aic-toast', onToast);
    await edit('Mine');
    window.removeEventListener('aic-toast', onToast);
    expect(toasts).toEqual(['no']);
    expect(panel._sessionTitleEditing).toBe(true);
  });
});

describe('following the session on screen', () => {
  beforeEach(() => {
    panel = fakePanel({
      'ClaudeCodeService.history_label': vi.fn(async (sid) => ({
        session_id: sid, title: sid === 'untitled' ? '' : `T-${sid}`, tag: '',
      })),
    });
    attachSessionTitle(panel);
  });

  it('picks up the session from the connect snapshot', async () => {
    fire('state-loaded', { session_id: 'a' });
    await flush();
    expect(panel._sessionLabel.title).toBe('T-a');
  });

  it('follows the engine init and a resume', async () => {
    fire('session-started', { requestId: 'r', data: { session_id: 'a' } });
    await flush();
    fire('session-changed', { session_id: 'b', action: 'resumed' });
    await flush();
    expect(panel._sessionLabel.title).toBe('T-b');
  });

  it('a new session has no chip until it has an ID', async () => {
    fire('state-loaded', { session_id: 'a' });
    await flush();
    fire('session-changed', { session_id: null, action: 'new' });
    await flush();
    expect(panel._sessionLabel).toBeNull();
  });

  it('a broadcast label for this session replaces the chip', async () => {
    fire('state-loaded', { session_id: 'a' });
    await flush();
    fire('session-labelled', { session_id: 'a', title: 'Renamed', tag: 't' });
    fire('session-labelled', { session_id: 'other', title: 'No', tag: '' });
    expect(panel._sessionLabel).toEqual({ session_id: 'a', title: 'Renamed', tag: 't' });
  });

  it('re-reads after a turn only while untitled', async () => {
    const read = panel.rpcExtract;
    fire('state-loaded', { session_id: 'untitled' });
    await flush();
    fire('stream-complete', {});
    await flush();
    expect(read).toHaveBeenCalledTimes(2);
    fire('state-loaded', { session_id: 'a' });
    await flush();
    fire('stream-complete', {});
    await flush();
    expect(read).toHaveBeenCalledTimes(3);
  });

  it('detaching stops following', async () => {
    detachSessionTitle(panel);
    fire('state-loaded', { session_id: 'a' });
    await flush();
    expect(panel._sessionLabel).toBeNull();
  });
});
