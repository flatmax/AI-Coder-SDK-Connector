// Tests for window-event handling: session-changed,
// compaction-event, and lifecycle/cleanup. The chat panel
// listens at the window level for events the AppShell
// translates from JRPC notifications.

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  settle,
} from './test-helpers.js';

// ---------------------------------------------------------------------------
// Session-changed
// ---------------------------------------------------------------------------

describe('ChatPanel session-changed event', () => {
  it('replaces message list on session change', async () => {
    const p = mountPanel({
      messages: [{ role: 'user', content: 'old' }],
    });
    await settle(p);
    pushEvent('session-changed', {
      session_id: 'sess_new',
      messages: [
        { role: 'user', content: 'new one' },
        { role: 'assistant', content: 'new two' },
      ],
    });
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[0].content).toBe('new one');
  });

  it('clears message list for empty sessions', async () => {
    const p = mountPanel({
      messages: [{ role: 'user', content: 'old' }],
    });
    await settle(p);
    pushEvent('session-changed', {
      session_id: 'sess_new',
      messages: [],
    });
    await settle(p);
    expect(p.messages).toEqual([]);
  });

  it('resets streaming state on session change', async () => {
    // If a stream is in flight when the user starts a new
    // session, the UI should move on — no leftover streaming
    // card, no leftover request ID.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._streaming).toBe(true);
    pushEvent('session-changed', {
      session_id: 'sess_new',
      messages: [],
    });
    await settle(p);
    expect(p._streaming).toBe(false);
    expect(p._currentRequestId).toBeNull();
    expect(
      p.shadowRoot.querySelector('.message-card.streaming'),
    ).toBeNull();
  });

  it('preserves system_event flag when loading messages', async () => {
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'Committed abc1234',
          system_event: true,
        },
        { role: 'user', content: 'then I typed this' },
      ],
    });
    await settle(p);
    expect(p.messages[0].system_event).toBe(true);
    expect(p.messages[1].system_event).toBeUndefined();
  });

  it('preserves turn_id from persisted records', async () => {
    // Increment A persists turn_id on every
    // record produced by an agentic turn. Session
    // reload must thread it back so the historical
    // "View agents" affordance works after refresh.
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'spawn agents',
          turn_id: 'turn_abc',
        },
        {
          role: 'assistant',
          content: 'delegated',
          turn_id: 'turn_abc',
        },
        {
          role: 'user',
          content: 'pre-Increment-A record',
        },
      ],
    });
    await settle(p);
    expect(p.messages[0].turn_id).toBe('turn_abc');
    expect(p.messages[1].turn_id).toBe('turn_abc');
    expect('turn_id' in p.messages[2]).toBe(false);
  });

  it('preserves agent_blocks from persisted assistant records', async () => {
    // Per spec specs4/3-llm/history.md § Cross-Turn
    // Agent Reconstruction — assistant records that
    // spawned agents persist the {id, agent_idx}
    // mapping. Session reload threads it back so
    // historical-turn UI can recover the right
    // archive directories.
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        { role: 'user', content: 'go' },
        {
          role: 'assistant',
          content: 'delegated',
          turn_id: 'turn_abc',
          agent_blocks: [
            { id: 'a0', agent_idx: 0 },
            { id: 'a1', agent_idx: 1 },
          ],
        },
      ],
    });
    await settle(p);
    expect(p.messages[1].agent_blocks).toEqual([
      { id: 'a0', agent_idx: 0 },
      { id: 'a1', agent_idx: 1 },
    ]);
  });

  it('omits empty agent_blocks array on reload', async () => {
    // Records that DO have the key but with an
    // empty array (defensive against future
    // backend changes) shouldn't surface a phantom
    // affordance trigger.
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'assistant',
          content: 'no agents',
          turn_id: 'turn_abc',
          agent_blocks: [],
        },
      ],
    });
    await settle(p);
    expect('agent_blocks' in p.messages[0]).toBe(false);
    expect(p.messages[0].turn_id).toBe('turn_abc');
  });

  it('omits non-string turn_id defensively', async () => {
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        { role: 'user', content: 'a', turn_id: 42 },
        { role: 'user', content: 'b', turn_id: '' },
        { role: 'user', content: 'c', turn_id: null },
      ],
    });
    await settle(p);
    expect('turn_id' in p.messages[0]).toBe(false);
    expect('turn_id' in p.messages[1]).toBe(false);
    expect('turn_id' in p.messages[2]).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Multimodal session-changed normalisation
// ---------------------------------------------------------------------------

describe('ChatPanel multimodal session-changed', () => {
  it('extracts images from multimodal content blocks', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'look at this' },
            {
              type: 'image_url',
              image_url: {
                url: 'data:image/png;base64,FROMSESSION',
              },
            },
          ],
        },
      ],
    });
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0].content).toBe('look at this');
    expect(p.messages[0].images).toEqual([
      'data:image/png;base64,FROMSESSION',
    ]);
  });

  it('preserves pre-existing images field if present', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    // Server may send a flattened shape too — string
    // content plus an images field. Preserve it.
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'hi',
          images: ['data:image/png;base64,ALREADY'],
        },
      ],
    });
    await settle(p);
    expect(p.messages[0].images).toEqual([
      'data:image/png;base64,ALREADY',
    ]);
  });

  it('messages without images get no images field', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [{ role: 'user', content: 'plain text' }],
    });
    await settle(p);
    expect(p.messages[0].images).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Restoring a rendered turn
// ---------------------------------------------------------------------------
//
// `history_load` renders a *turn*: an ordered `blocks` list, the files it
// touched, and a footer summary of usage and duration. The restore used to
// keep `{role, content, images, system_event}` and drop the rest, which was
// harmless while the native engine's records held nothing else and is a
// resumed session showing none of the agent's work now.

describe('ChatPanel restoring a rendered turn', () => {
  /** An assistant turn in the shape `history_load` renders. */
  function renderedTurn(overrides = {}) {
    return {
      role: 'assistant',
      content: 'Read the file and fixed it.',
      blocks: [
        {
          block_id: 'b0',
          kind: 'text',
          seq: 0,
          content: 'Read the file and fixed it.',
          done: true,
          agent_id: null,
        },
      ],
      subagents: [],
      files: ['src/foo.py'],
      turn: {
        tool_calls: 2,
        num_turns: 3,
        files_modified: ['src/foo.py'],
        duration_ms: 4200,
        model_usage: {
          'claude-opus-4': { input_tokens: 10, output_tokens: 20 },
        },
      },
      terminalReason: null,
      ...overrides,
    };
  }

  it('keeps the blocks, the files and the footer', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [renderedTurn()] });
    await settle(p);
    const msg = p.messages[0];
    expect(msg.blocks).toHaveLength(1);
    expect(msg.files).toEqual(['src/foo.py']);
    expect(msg.turn.tool_calls).toBe(2);
    expect(msg.turn.duration_ms).toBe(4200);
    expect(msg.subagents).toEqual([]);
  });

  it('renders it as a turn, not as prose', async () => {
    // `blocks` is what makes renderMessage treat the message as a Claude
    // Code turn at all — the footer and the tool cards hang off that one
    // decision, so this is the assertion that the restore is load-bearing.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [renderedTurn()] });
    await settle(p);
    expect(p.shadowRoot.querySelector('.turn-footer')).toBeTruthy();
  });

  it('draws no terminal badge for a browsed turn', async () => {
    // The transcript holds no result entry, so `terminalReason` is null
    // and stays null. A "completed" badge on no evidence is worse than
    // no badge.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [renderedTurn()] });
    await settle(p);
    expect(p.messages[0].terminalReason).toBeUndefined();
    expect(
      p.shadowRoot.querySelector('.terminal-badge'),
    ).toBeNull();
  });

  it('keeps a real terminal reason when there is one', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [renderedTurn({ terminalReason: 'interrupted' })],
    });
    await settle(p);
    expect(p.messages[0].terminalReason).toBe('interrupted');
  });

  it('invents no footer for a turn that has none', async () => {
    // An empty footer would report zeros as if they were measurements.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const turn = renderedTurn();
    delete turn.turn;
    delete turn.files;
    pushEvent('session-changed', { messages: [turn] });
    await settle(p);
    expect(p.messages[0].turn).toBeUndefined();
    expect(p.messages[0].files).toBeUndefined();
  });

  it('keeps a compaction divider read back from disk', async () => {
    // Same shape the live `compact_boundary` broadcast appends, so a
    // divider read from the transcript and one seen as it happened render
    // by the same path.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'Conversation compacted',
          system_event: true,
          compaction: {
            pre_tokens: 150000,
            post_tokens: 20000,
            trigger: 'auto',
          },
        },
      ],
    });
    await settle(p);
    expect(p.messages[0].compaction.pre_tokens).toBe(150000);
    expect(
      p.shadowRoot.querySelector('.compaction-divider'),
    ).toBeTruthy();
  });

  it('does not attribute a compact summary to the user', async () => {
    // The CLI wrote it, about the context it dropped, and the transcript
    // replays it as a user turn because that is how the model receives
    // it. Labelling it "You" would credit the reader with writing it.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'Summary of the conversation so far…',
          compact_summary: true,
        },
      ],
    });
    await settle(p);
    expect(p.messages[0].system_event).toBe(true);
    expect(p.messages[0].compact_summary).toBe(true);
    const label = p.shadowRoot.querySelector('.role-label');
    expect(label.textContent.trim()).toBe('System');
  });

  it('keeps a compact summary out of up-arrow recall', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        { role: 'user', content: 'what I typed' },
        {
          role: 'user',
          content: 'Summary of the conversation so far…',
          compact_summary: true,
        },
      ],
    });
    await settle(p);
    const history = p.shadowRoot.querySelector('ac-input-history');
    // `_entries` is plain strings; the element exposes no public reader.
    const recalled = history._entries || [];
    expect(recalled).toContain('what I typed');
    expect(recalled.some((t) => (t || '').includes('Summary of the'))).toBe(
      false,
    );
  });

  it('restores the same shape from state-loaded', async () => {
    // A reconnect and a resume are the same transcript arriving by two
    // routes; two normalisers would let them disagree.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('state-loaded', { messages: [renderedTurn()] });
    await settle(p);
    expect(p.messages[0].blocks).toHaveLength(1);
    expect(p.messages[0].turn.num_turns).toBe(3);
    expect(p.shadowRoot.querySelector('.turn-footer')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Compaction events — the retired native-engine stages
// ---------------------------------------------------------------------------
//
// Five stages used to arrive on this channel and are now handled by nobody:
// `url_fetch` / `url_ready` from URL curation (CC-9), and `compacting` /
// `compacted` / `compaction_error` from a compaction this app ran over a
// history it owned. The suites that covered them went with them in conversion
// phase 3. What replaces those suites is this one, because the dangerous case
// is not that the branches are gone — it is a stale or replayed broadcast
// finding them still there. `compacted` in particular carried a whole
// replacement message list: honouring one would swap the transcript the user
// is reading for a summary written by an engine that no longer exists.

describe('ChatPanel compaction events — retired stages', () => {
  async function sendAndComplete(panel, text = 'hi') {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = text;
    await panel._send();
    await settle(panel);
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'original answer' },
    });
    await settle(panel);
    return reqId;
  }

  it('ignores every retired stage — no toast, no transcript change', async () => {
    const p = mountPanel();
    const reqId = await sendAndComplete(p, 'original question');
    const before = [...p.messages];
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      for (const event of [
        { stage: 'url_fetch', url: 'github.com/owner/repo' },
        { stage: 'url_ready', url: 'github.com/owner/repo' },
        { stage: 'compacting' },
        { stage: 'compaction_error', error: 'boom' },
      ]) {
        pushEvent('compaction-event', { requestId: reqId, event });
        await settle(p);
      }
      expect(toasts).not.toHaveBeenCalled();
      expect(p.messages).toEqual(before);
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });

  it('will not swap the transcript for a `compacted` payload', async () => {
    const p = mountPanel();
    const reqId = await sendAndComplete(p, 'original question');
    expect(p.messages).toHaveLength(2);
    pushEvent('compaction-event', {
      requestId: reqId,
      event: {
        stage: 'compacted',
        case: 'summarize',
        messages: [
          { role: 'user', content: '[History Summary]\nbrief recap' },
          { role: 'assistant', content: 'Ok, I understand.' },
        ],
      },
    });
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[0].content).toContain('original question');
    expect(p.messages[1].content).toContain('original answer');
  });
});

// ---------------------------------------------------------------------------
// Compaction events — compact_boundary (the Claude Code stage)
// ---------------------------------------------------------------------------
//
// The one stage of this event the new engine actually sends. It reports that
// the CLI compacted its own context; it does not hand us a rewritten
// conversation. So the assertions here are as much about what does NOT happen
// — no replacement, no toast — as about the divider that does appear.

describe('ChatPanel compact_boundary', () => {
  async function sendAndGetId(panel, text = 'hi') {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = text;
    await panel._send();
    await settle(panel);
    return started.mock.calls[0][0];
  }

  function boundary(reqId, payload = {}) {
    pushEvent('compaction-event', {
      requestId: reqId,
      event: {
        stage: 'compact_boundary',
        pre_tokens: 168_200,
        post_tokens: 21_400,
        trigger: 'auto',
        raw: {},
        ...payload,
      },
    });
  }

  it('appends a divider with the before/after counts', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId);
    await settle(p);
    const divider = p.shadowRoot.querySelector('.compaction-divider');
    expect(divider).toBeTruthy();
    expect(divider.textContent).toContain('Context compacted');
    expect(
      divider.querySelector('.compaction-counts').textContent,
    ).toBe('168.2k → 21.4k tokens');
    // Two rules, one label — the label sits in the gap.
    expect(divider.querySelectorAll('.compaction-rule')).toHaveLength(2);
  });

  it('leaves the transcript it interrupted alone', async () => {
    // The whole point. The engine compacted its own context; the messages on
    // this page are ours and are unaffected, so the user's question is still
    // there afterwards with the divider added after it.
    const p = mountPanel();
    const reqId = await sendAndGetId(p, 'my original question');
    expect(p.messages).toHaveLength(1);
    boundary(reqId);
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[0].content).toBe('my original question');
    expect(p.messages[0].compaction).toBeUndefined();
    expect(p.messages[1].compaction).toEqual({
      pre_tokens: 168_200,
      post_tokens: 21_400,
      trigger: 'automatic',
    });
  });

  it('says nothing in a toast', async () => {
    // A boundary is a fact to record, not an interruption to announce. The
    // `pre_compact` hook already toasted that compaction was coming.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      boundary(reqId);
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });

  it('marks the boundary even with no counts to report', async () => {
    // The subtype is untyped on the wire. A CLI that renames or drops the
    // token fields still gets a divider — one that says compaction happened,
    // not one that says `undefined → undefined`.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId, {
      pre_tokens: null,
      post_tokens: undefined,
      trigger: null,
    });
    await settle(p);
    const divider = p.shadowRoot.querySelector('.compaction-divider');
    expect(divider).toBeTruthy();
    expect(divider.textContent).toContain('Context compacted');
    expect(divider.textContent).not.toContain('undefined');
    expect(divider.querySelector('.compaction-counts')).toBeNull();
    expect(divider.querySelector('.compaction-trigger')).toBeNull();
  });

  it('shows an unrecognised trigger verbatim', async () => {
    // `auto` and `manual` get read-aloud labels; anything else is a fact about
    // a CLI we do not fully know, and hiding it would make it undiagnosable.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId, { trigger: 'microcompact' });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.compaction-trigger').textContent.trim(),
    ).toBe('microcompact');
  });

  it('maps the two triggers the CLI names', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId, { trigger: 'manual' });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.compaction-trigger').textContent.trim(),
    ).toBe('manual');
    expect(p.messages.at(-1).compaction.trigger).toBe('manual');
  });

  it('carries a plain-text form for search and copy', async () => {
    // Chat search, the copy button and the history browser all read `content`
    // and know nothing about `compaction`.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId);
    await settle(p);
    expect(p.messages.at(-1).content).toBe(
      'Context compacted (automatic) — 168.2k → 21.4k tokens',
    );
    expect(p.messages.at(-1).system_event).toBe(true);
  });

  it('is a divider, not a message card with a body', async () => {
    // No role label, no toolbar, no markdown body: nobody wrote it, so there
    // is nothing to attribute, copy or reply to.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId);
    await settle(p);
    const divider = p.shadowRoot.querySelector('.compaction-divider');
    expect(divider.querySelector('.role-label')).toBeNull();
    expect(divider.querySelector('.message-toolbar')).toBeNull();
    expect(divider.querySelector('.md-content')).toBeNull();
    // Still a search target — the counts are the sort of thing you scroll
    // back for.
    expect(divider.classList.contains('message-card')).toBe(true);
    expect(divider.dataset.msgIndex).toBe('1');
  });

  it('each boundary in a session gets its own divider', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId);
    boundary(reqId, { pre_tokens: 90_000, post_tokens: 12_000 });
    await settle(p);
    const dividers = p.shadowRoot.querySelectorAll('.compaction-divider');
    expect(dividers).toHaveLength(2);
    expect(
      dividers[1].querySelector('.compaction-counts').textContent,
    ).toBe('90.0k → 12.0k tokens');
  });

  it('the streaming card it interrupted keeps streaming', async () => {
    // Compaction happens mid-turn, so the divider lands above the live card
    // and the turn carries on. The frozen turn is appended after the divider
    // when it completes — the boundary is recorded where it happened relative
    // to the messages that existed, which is the best a per-turn freeze can do.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    boundary(reqId);
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.message-card.streaming'),
    ).toBeTruthy();
    expect(p._currentRequestId).toBe(reqId);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'carried on' },
    });
    await settle(p);
    expect(p.messages.map((m) => Boolean(m.compaction))).toEqual([
      false,
      true,
      false,
    ]);
    expect(p.messages.at(-1).content).toBe('carried on');
  });
});

// ---------------------------------------------------------------------------
// Compaction events — request ID filtering
// ---------------------------------------------------------------------------

describe('ChatPanel compaction events — request ID filtering', () => {
  async function sendAndCompleteStream(panel, text = 'hi') {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = text;
    await panel._send();
    await settle(panel);
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done' },
    });
    await settle(panel);
    return reqId;
  }

  // The filter is now exercised through the only stage that
  // survives, so "accepted" means a divider was appended
  // rather than a toast fired.
  function boundary(reqId) {
    pushEvent('compaction-event', {
      ...(reqId ? { requestId: reqId } : {}),
      event: {
        stage: 'compact_boundary',
        pre_tokens: 168_200,
        post_tokens: 21_400,
        trigger: 'auto',
      },
    });
  }

  it('accepts events for the most recently completed request', async () => {
    // Common case — the CLI compacts after the turn ends, so
    // `_currentRequestId` is null by the time the boundary
    // lands and only `_lastRequestId` can match. This is the
    // normal path, not an edge case.
    const p = mountPanel();
    const reqId = await sendAndCompleteStream(p);
    const before = p.messages.length;
    boundary(reqId);
    await settle(p);
    expect(p.messages.length).toBe(before + 1);
    expect(p.messages.at(-1).compaction).toBeTruthy();
  });

  it('accepts events for the current streaming request', async () => {
    // Rare but possible — a boundary arrives mid-stream.
    // `_currentRequestId` matches.
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    const reqId = started.mock.calls[0][0];
    const before = p.messages.length;
    boundary(reqId);
    await settle(p);
    expect(p.messages.length).toBe(before + 1);
  });

  it('drops events for unknown request IDs', async () => {
    const p = mountPanel();
    await sendAndCompleteStream(p);
    const before = p.messages.length;
    boundary('random-unknown-id');
    await settle(p);
    expect(p.messages.length).toBe(before);
  });

  it('accepts events without a requestId (progress broadcasts)', async () => {
    const p = mountPanel();
    await settle(p);
    const before = p.messages.length;
    boundary(null);
    await settle(p);
    expect(p.messages.length).toBe(before + 1);
  });
});

// ---------------------------------------------------------------------------
// Compaction events — defensive
// ---------------------------------------------------------------------------

describe('ChatPanel compaction events — defensive', () => {
  it('unknown stage is silently ignored', async () => {
    const p = mountPanel();
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { stage: 'future_stage_we_dont_know_about' },
      });
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });

  it('doc_enrichment_* stages are ignored (handled elsewhere)', async () => {
    // Per spec: doc enrichment drives a header progress
    // bar, not a chat toast. Chat panel must not render
    // these even though they come through the same
    // channel.
    const p = mountPanel();
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      for (const stage of [
        'doc_enrichment_queued',
        'doc_enrichment_file_done',
        'doc_enrichment_complete',
        'doc_enrichment_failed',
      ]) {
        pushEvent('compaction-event', {
          event: { stage, file: 'docs/readme.md' },
        });
        await settle(p);
      }
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });

  it('malformed events (no event payload) do not crash', async () => {
    const p = mountPanel();
    await settle(p);
    for (const detail of [{}, { requestId: 'x' }, { event: null }]) {
      pushEvent('compaction-event', detail);
      await settle(p);
    }
    // Still live afterwards — a swallowed exception would
    // leave the handler wired but inert.
    const before = p.messages.length;
    pushEvent('compaction-event', {
      event: {
        stage: 'compact_boundary',
        pre_tokens: 1000,
        post_tokens: 200,
      },
    });
    await settle(p);
    expect(p.messages.length).toBe(before + 1);
  });

  it('event with missing stage field is ignored', async () => {
    const p = mountPanel();
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { url: 'no stage here' },
      });
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });
});

// ---------------------------------------------------------------------------
// User message broadcast echo handling
// ---------------------------------------------------------------------------

describe('ChatPanel user-message event', () => {
  it('ignores echo when we are the sender', async () => {
    const p = mountPanel();
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(p);
    p._input = 'hello';
    await p._send();
    await settle(p);
    pushEvent('user-message', { content: 'hello' });
    await settle(p);
    const userMessages = p.messages.filter(
      (m) => m.role === 'user' && !m.system_event,
    );
    expect(userMessages).toHaveLength(1);
  });

  it('adds the message when we are a passive observer', async () => {
    // No user-initiated request in flight — we're a
    // collaborator seeing another user's prompt.
    const p = mountPanel();
    await settle(p);
    pushEvent('user-message', {
      content: 'message from another client',
    });
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0].content).toBe(
      'message from another client',
    );
  });
});

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

describe('ChatPanel cleanup', () => {
  it('removes event listeners on disconnect', async () => {
    const p = mountPanel();
    await settle(p);
    p.remove();
    pushEvent('stream-chunk', {
      requestId: 'any',
      content: 'should be ignored',
    });
    expect(p._streamingContent).toBe('');
  });

  it('removes compaction-event listener on disconnect', async () => {
    const p = mountPanel();
    await settle(p);
    p.remove();
    const toasts = vi.fn();
    window.addEventListener('ac-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { stage: 'url_fetch', url: 'test' },
      });
      await new Promise((r) => setTimeout(r, 10));
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('ac-toast', toasts);
    }
  });
});

// ---------------------------------------------------------------------------
// Speech-player state sync
// ---------------------------------------------------------------------------

describe('ChatPanel speech-player-state sync', () => {
  it('lights the speaker for the active message index', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hi' }],
    });
    await settle(p);
    pushEvent('speech-player-state', {
      active: true,
      status: 'playing',
      ownerKey: 0,
      index: 0,
      total: 1,
    });
    await settle(p);
    expect(p._speakingMsgIndex).toBe(0);
  });

  it('clears the speaker when playback goes idle', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hi' }],
    });
    await settle(p);
    pushEvent('speech-player-state', {
      active: true,
      status: 'playing',
      ownerKey: 0,
    });
    await settle(p);
    expect(p._speakingMsgIndex).toBe(0);
    pushEvent('speech-player-state', { active: false, ownerKey: null });
    await settle(p);
    expect(p._speakingMsgIndex).toBe(-1);
  });

  it('ignores a non-numeric ownerKey', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hi' }],
    });
    await settle(p);
    pushEvent('speech-player-state', {
      active: true,
      status: 'playing',
      ownerKey: 'something-else',
    });
    await settle(p);
    expect(p._speakingMsgIndex).toBe(-1);
  });

  it('stops listening after disconnect', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hi' }],
    });
    await settle(p);
    p.remove();
    pushEvent('speech-player-state', {
      active: true,
      status: 'playing',
      ownerKey: 0,
    });
    expect(p._speakingMsgIndex).toBe(-1);
  });
});