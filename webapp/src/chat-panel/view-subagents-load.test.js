// Tests for the `view-subagents-requested` handler — the one that turns
// agent ids into read-only tabs by reading each transcript off disk.
//
// Coverage:
//   - The read: `get_subagent_transcript(agent_id, session_id)`, one tab per
//     agent, labelled by description, activated as soon as the first lands
//   - The normalizer: a transcript's turns keep their blocks and footer,
//     because this is `history_load`'s renderer and the same restore path
//   - Unreadable transcripts: the reason renders in place of the messages
//     and the tab stays (specs5/5-webapp/subagent-browser.md § Empty States)
//   - Live subagents are skipped — their tab in the strip is the better view
//   - Staleness: a session resume mid-read abandons the tabs still to come
//   - The read-only gate: no input surface on a transcript tab

import { describe, expect, it, vi } from 'vitest';

import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  seedTab,
  settle,
} from './test-helpers.js';

const TRANSCRIPT = [
  { role: 'user', content: 'find the auth call sites' },
  {
    role: 'assistant',
    content: 'Found four.',
    blocks: [
      {
        block_id: 'b0',
        kind: 'text',
        seq: 0,
        content: 'Found four.',
        done: true,
        agent_id: null,
      },
    ],
    subagents: [],
    files: ['src/auth.py'],
    turn: { tool_calls: 3, num_turns: 1, files_modified: ['src/auth.py'] },
    terminalReason: null,
  },
];

function ask(panel, agents, sessionId) {
  panel.dispatchEvent(
    new CustomEvent('view-subagents-requested', {
      detail: sessionId ? { agents, session_id: sessionId } : { agents },
      bubbles: true,
      composed: true,
    }),
  );
}

const agent = (id, label) => ({ agent_id: id, label });

describe('view-subagents handler — reading the transcript', () => {
  it('reads each agent by id and lands them in the strip', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    ask(p, [
      agent('agent_abc', 'explore: find auth call sites'),
      agent('agent_def', 'general: write the tests'),
    ]);
    await settle(p);
    expect(read.mock.calls.map((c) => c[0])).toEqual([
      'agent_abc',
      'agent_def',
    ]);
    expect(Array.from(p._tabs.keys())).toEqual([
      'main',
      'historical:agent_abc',
      'historical:agent_def',
    ]);
  });

  it('passes the session the panel is attached to', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-started', { data: { session_id: 'sess_1' } });
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(read).toHaveBeenCalledWith('agent_abc', 'sess_1');
  });

  it('prefers a session named in the request', async () => {
    // The history browser opens a subagent of a session that is not live;
    // the panel's own session id would be the wrong one to read.
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-started', { data: { session_id: 'sess_live' } });
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')], 'sess_old');
    await settle(p);
    expect(read).toHaveBeenCalledWith('agent_abc', 'sess_old');
  });

  it('labels the tab with the description, not the id', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: find auth call sites')]);
    await settle(p);
    expect(p._tabLabels.get('historical:agent_abc')).toBe(
      '📜 explore: find auth call sites',
    );
  });

  it('falls back to the id when the row had no description', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', '')]);
    await settle(p);
    expect(p._tabLabels.get('historical:agent_abc')).toBe('📜 agent_abc');
  });

  it('marks the tab read-only', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(p._tabs.get('historical:agent_abc').readOnly).toBe(true);
  });

  it('activates the first transcript and no later one', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    expect(p._activeTabId).toBe('main');
    ask(p, [
      agent('agent_abc', 'explore: x'),
      agent('agent_def', 'general: y'),
    ]);
    await settle(p);
    expect(p._activeTabId).toBe('historical:agent_abc');
  });

  it('keeps a transcript turn as a turn, not as prose', async () => {
    // Same renderer as `history_load`, so the same restore: dropping the
    // blocks here would show a subagent's work as a paragraph of text.
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    const messages = p._tabs.get('historical:agent_abc').messages;
    expect(messages).toHaveLength(2);
    expect(messages[1].blocks).toHaveLength(1);
    expect(messages[1].turn.tool_calls).toBe(3);
    expect(p.shadowRoot.querySelector('.turn-footer')).toBeTruthy();
  });
});

describe('view-subagents handler — unreadable transcripts', () => {
  it('shows the service’s reason in place of the messages', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi.fn().mockResolvedValue({
        error: 'Subagent agent_abc has no readable transcript',
      }),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    const tab = p._tabs.get('historical:agent_abc');
    expect(tab).toBeTruthy();
    expect(tab.messages).toHaveLength(1);
    expect(tab.messages[0].content).toContain('no readable transcript');
    expect(tab.messages[0].system_event).toBe(true);
  });

  it('keeps the tab when the read fails outright', async () => {
    // The row in Main is evidence the subagent ran; a tab that vanished on
    // click would read as "nothing happened".
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockRejectedValue(new Error('socket closed')),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    const tab = p._tabs.get('historical:agent_abc');
    expect(tab).toBeTruthy();
    expect(tab.messages[0].content).toContain('socket closed');
    expect(p._activeTabId).toBe('historical:agent_abc');
  });

  it('says so when the transcript came back empty', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(
      p._tabs.get('historical:agent_abc').messages[0].content,
    ).toContain('no readable transcript');
  });

  it('reads the rest after one comes back unreadable', async () => {
    const read = vi
      .fn()
      .mockResolvedValueOnce({ error: 'transcript not found' })
      .mockResolvedValueOnce(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    ask(p, [
      agent('agent_abc', 'explore: x'),
      agent('agent_def', 'general: y'),
    ]);
    await settle(p);
    expect(p._tabs.has('historical:agent_abc')).toBe(true);
    expect(p._tabs.get('historical:agent_def').messages).toHaveLength(2);
  });
});

describe('view-subagents handler — what it refuses to do', () => {
  it('skips a subagent whose live tab is in the strip', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent_abc');
    ask(p, [
      agent('agent_abc', 'explore: x'),
      agent('agent_def', 'general: y'),
    ]);
    await settle(p);
    expect(read.mock.calls.map((c) => c[0])).toEqual(['agent_def']);
    expect(p._tabs.has('historical:agent_abc')).toBe(false);
  });

  it('toasts and reads nothing when every subagent is still live', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent_abc');
    const toasts = [];
    p._emitToast = (message, type) => toasts.push([message, type]);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(read).not.toHaveBeenCalled();
    expect(toasts).toEqual([
      ['That subagent is still active in the tab strip', 'info'],
    ]);
  });

  it('reads a repeated agent id once', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x'), agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(read).toHaveBeenCalledOnce();
  });

  it('ignores a malformed request', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    p.dispatchEvent(new CustomEvent('view-subagents-requested'));
    p.dispatchEvent(
      new CustomEvent('view-subagents-requested', { detail: {} }),
    );
    p.dispatchEvent(
      new CustomEvent('view-subagents-requested', { detail: { agents: [] } }),
    );
    ask(p, [{ label: 'no id here' }]);
    await settle(p);
    expect(read).not.toHaveBeenCalled();
  });
});

describe('view-subagents handler — the strip is not a pile', () => {
  it('clears the previous turn’s transcripts on the next click', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    ask(p, [agent('agent_def', 'general: y')]);
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual([
      'main',
      'historical:agent_def',
    ]);
  });

  it('leaves live tabs alone', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'agent_live');
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    ask(p, [agent('agent_def', 'general: y')]);
    await settle(p);
    expect(p._tabs.has('agent_live')).toBe(true);
  });

  it('drops them when the user resumes another session', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(p._activeTabId).toBe('historical:agent_abc');
    pushEvent('session-changed', { messages: [] });
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main']);
    expect(p._activeTabId).toBe('main');
  });

  it('abandons the reads still to come when that happens', async () => {
    // The await spans user actions: a resume landing between two reads must
    // not have the second one arrive into a strip that has moved on.
    let release;
    const gate = new Promise((r) => {
      release = r;
    });
    let call = 0;
    const read = vi.fn(async () => {
      call += 1;
      if (call === 2) await gate;
      return TRANSCRIPT;
    });
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    ask(p, [
      agent('agent_abc', 'explore: x'),
      agent('agent_def', 'general: y'),
    ]);
    await settle(p);
    expect(p._tabs.has('historical:agent_abc')).toBe(true);
    pushEvent('session-changed', { messages: [] });
    await settle(p);
    release();
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main']);
  });
});

describe('view-subagents handler — no channel to a subagent', () => {
  it('offers no input surface at all on a transcript tab', async () => {
    // Absent, not disabled: a greyed-out textarea implies a channel that
    // might open under some condition (subagent-browser.md § Tab Content).
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(p.shadowRoot.querySelector('.input-textarea')).toBeNull();
    expect(p.shadowRoot.querySelector('.send-button')).toBeNull();
    expect(p.shadowRoot.querySelector('ac-input-history')).toBeNull();
    expect(
      p.shadowRoot.querySelector('.read-only-note').textContent,
    ).toContain('no channel to a subagent');
  });

  it('keeps the safety posture and the LED row on show', async () => {
    // What the agent is allowed to do, and which conversation is live, are
    // true on every tab — so the action bar and the LEDs stay.
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    expect(p.shadowRoot.querySelector('.permission-mode-select')).toBeTruthy();
    expect(p.shadowRoot.querySelector('.led-strip')).toBeTruthy();
  });

  it('brings the input surface back on Main', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    p._activeTabId = 'main';
    await settle(p);
    expect(p.shadowRoot.querySelector('.input-textarea')).toBeTruthy();
    expect(p.shadowRoot.querySelector('.read-only-note')).toBeNull();
  });

  it('sends nothing from a transcript tab', async () => {
    // The guard behind the missing textarea, for a send arriving by some
    // other route — a shortcut, or a tab switch racing a keystroke.
    const chat = vi.fn().mockResolvedValue({ ok: true });
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
      'ClaudeCodeService.chat_streaming': chat,
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    const toasts = [];
    p._emitToast = (message, type) => toasts.push([message, type]);
    p._input = 'are you there?';
    await p._send();
    expect(chat).not.toHaveBeenCalled();
    expect(toasts).toEqual([
      [
        'Read-only transcript — there is no channel to a subagent. ' +
          'Switch to Main to send a message.',
        'warning',
      ],
    ]);
  });

  it('sends again from Main', async () => {
    const chat = vi.fn().mockResolvedValue({ ok: true });
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': vi
        .fn()
        .mockResolvedValue(TRANSCRIPT),
      'ClaudeCodeService.chat_streaming': chat,
    });
    const p = mountPanel();
    await settle(p);
    ask(p, [agent('agent_abc', 'explore: x')]);
    await settle(p);
    p._activeTabId = 'main';
    await settle(p);
    p._input = 'carry on';
    await p._send();
    expect(chat).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// From the history browser
// ---------------------------------------------------------------------------

describe('view-subagents handler — the history browser’s route in', () => {
  it('opens a transcript the browser hands up, for the session it names', async () => {
    // The whole seam in one test: the browser lives in the panel's shadow
    // root, so the event it dispatches has to be composed to reach the
    // panel's listener, and the session id has to travel with it — the
    // panel's own session is not the one being browsed.
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({
      'ClaudeCodeService.get_subagent_transcript': read,
      'ClaudeCodeService.history_list': vi.fn().mockResolvedValue([
        {
          session_id: 'sess_old',
          timestamp: new Date().toISOString(),
          message_count: 2,
          preview: 'a conversation from last week',
          first_role: 'user',
        },
      ]),
      'ClaudeCodeService.history_load': vi
        .fn()
        .mockResolvedValue([{ role: 'user', content: 'hello' }]),
      'ClaudeCodeService.list_subagent_transcripts': vi
        .fn()
        .mockResolvedValue([
          {
            agent_id: 'agent_abc',
            subpath: 'subagents/agent-agent_abc',
            message_count: 3,
            preview: 'find the auth call sites',
            agent_type: 'explore',
            description: 'find auth call sites',
          },
        ]),
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-started', { data: { session_id: 'sess_live' } });
    await settle(p);

    p.shadowRoot.querySelector('.history-button').click();
    await settle(p);
    const browser = p.shadowRoot.querySelector('ac-history-browser');
    await browser.updateComplete;
    await settle(p);
    browser.shadowRoot.querySelector('.session-item').click();
    await browser.updateComplete;
    await settle(p);
    browser.shadowRoot.querySelector('.subagent-chip').click();
    await settle(p);

    expect(read).toHaveBeenCalledWith('agent_abc', 'sess_old');
    expect(p._tabLabels.get('historical:agent_abc')).toBe(
      '📜 explore: find auth call sites',
    );
    expect(p._activeTabId).toBe('historical:agent_abc');
    // The modal closes: the tab it asked for is behind it.
    expect(p._historyOpen).toBe(false);
  });
});
