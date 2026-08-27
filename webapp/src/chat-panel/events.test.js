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

  it('preserves the subagent rows a restored turn carries', async () => {
    // What the rows are for: `agent_id` is the handle
    // `get_subagent_transcript` reads by, so dropping
    // them here would leave a restored turn's
    // delegation unreadable. `_Turn.freeze` writes
    // `subagents: []` today — the transcript does not
    // record which turn spawned which subagent — but
    // the restore is not the place to assume that.
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        { role: 'user', content: 'go' },
        {
          role: 'assistant',
          content: 'delegated',
          turn_id: 'turn_abc',
          subagents: [
            { key: 'task-1', agent_id: 'agent_abc', status: 'completed' },
          ],
        },
      ],
    });
    await settle(p);
    expect(p.messages[1].subagents).toEqual([
      { key: 'task-1', agent_id: 'agent_abc', status: 'completed' },
    ]);
  });

  it('drops the native engine’s agent_blocks mapping', async () => {
    // `{id, agent_idx}` addressed a per-agent archive
    // directory under the native engine. The CLI has
    // no such thing — a subagent is read by its own
    // id — and nothing in the panel consumes the key,
    // so carrying it forward would only preserve a
    // dead pointer in the restored records.
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
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
        // `turn_model_usage`, snake_case, no cost: exactly what
        // `history.py`'s `TurnBuilder.freeze()` produces. A browsed turn
        // carries no cost at all — the CLI's transcript does not record it.
        turn_model_usage: {
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
    const history = p.shadowRoot.querySelector('aic-input-history');
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
// Restoring image pointers
// ---------------------------------------------------------------------------
//
// A prompt's screenshots live in the transcript as base64 (specs5/4-features/
// images.md — the transcript *is* the storage), so `history_load` renders them
// as pointers and the bytes are fetched one at a time through `history_image`.
// A restore that dropped the pointers was a resumed prompt whose screenshots
// were simply gone, with nothing on screen to say so.

describe('ChatPanel restoring image pointers', () => {
  const PNG = 'data:image/png;base64,iVBORw0KGgo=';

  function ref(overrides = {}) {
    return {
      session_id: 's1',
      entry_uuid: 'u1',
      block: 0,
      media_type: 'image/png',
      ...overrides,
    };
  }

  /** A prompt carrying pointers, in the shape `history_load` renders. */
  function prompt(refs) {
    return { role: 'user', content: 'look at this', image_refs: refs };
  }

  /** Extra settle rounds — each pointer resolves on its own microtask. */
  async function drain(panel, rounds = 4) {
    for (let i = 0; i < rounds; i += 1) await settle(panel);
  }

  function tiles(panel) {
    return {
      images: [...panel.shadowRoot.querySelectorAll('img.message-image')],
      pending: [
        ...panel.shadowRoot.querySelectorAll('.message-image-pending'),
      ],
      missing: [
        ...panel.shadowRoot.querySelectorAll('.message-image-missing'),
      ],
    };
  }

  it('carries the pointers through the restore', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await settle(p);
    expect(p.messages[0].image_refs).toEqual([ref()]);
  });

  it('resolves each pointer and draws the image', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    expect(fetchImage).toHaveBeenCalledWith('s1', 'u1', 0);
    expect(tiles(p).images.map((i) => i.getAttribute('src'))).toEqual([PNG]);
  });

  it('holds the tile before the bytes arrive', async () => {
    // The box is the size the image will be, so a prompt with several
    // screenshots does not reflow tile by tile as they land.
    let release;
    const held = new Promise((r) => {
      release = r;
    });
    publishFakeRpc({
      'ClaudeCodeService.history_image': () => held,
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [prompt([ref(), ref({ block: 1 })])],
    });
    await settle(p);
    expect(tiles(p).pending).toHaveLength(2);
    release({ data_uri: PNG });
    await drain(p);
  });

  it('keeps a marked tile for a pointer that cannot be read', async () => {
    // An image silently absent from a prompt reads as a prompt that never
    // had one, which is a different conversation from the one that happened.
    publishFakeRpc({
      'ClaudeCodeService.history_image': async () => ({
        error: 'That entry is gone',
      }),
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    const { missing, images } = tiles(p);
    expect(images).toHaveLength(0);
    expect(missing).toHaveLength(1);
    expect(missing[0].getAttribute('title')).toBe('That entry is gone');
  });

  it('marks the tile when the call itself fails', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_image': async () => {
        throw new Error('socket closed');
      },
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    expect(tiles(p).missing).toHaveLength(1);
  });

  it('fetches one pointer at a time', async () => {
    // Twenty pasted screenshots would otherwise open twenty concurrent
    // RPCs at a backend whose reads are disk-bound anyway.
    const calls = [];
    let release;
    const held = new Promise((r) => {
      release = r;
    });
    publishFakeRpc({
      'ClaudeCodeService.history_image': (session, uuid, block) => {
        calls.push(block);
        return calls.length === 1 ? held : Promise.resolve({ data_uri: PNG });
      },
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [prompt([ref(), ref({ block: 1 }), ref({ block: 2 })])],
    });
    await drain(p);
    expect(calls).toEqual([0]);
    release({ data_uri: PNG });
    await drain(p, 6);
    expect(calls).toEqual([0, 1, 2]);
  });

  it('fetches a pointer once, however often it is restored', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    expect(fetchImage).toHaveBeenCalledTimes(1);
    expect(tiles(p).images).toHaveLength(1);
  });

  it('abandons the fetching when another session is restored', async () => {
    // The loop awaits, and during the await the user is free to resume
    // something else. The rest of that work belongs to nobody.
    const calls = [];
    let release;
    const held = new Promise((r) => {
      release = r;
    });
    publishFakeRpc({
      'ClaudeCodeService.history_image': (session, uuid, block) => {
        calls.push(`${session}:${block}`);
        return calls.length === 1 ? held : Promise.resolve({ data_uri: PNG });
      },
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [prompt([ref(), ref({ block: 1 })])],
    });
    await settle(p);
    expect(calls).toEqual(['s1:0']);
    // A different session lands while the first pointer is still in flight.
    pushEvent('session-changed', {
      messages: [{ role: 'user', content: 'a session with no pictures' }],
    });
    await settle(p);
    release({ data_uri: PNG });
    await drain(p, 6);
    // The second pointer of the abandoned session was never asked for.
    expect(calls).toEqual(['s1:0']);
  });

  it('offers a resolved pointer to the lightbox and the composer', async () => {
    // Re-attaching from a past session is a documented path into the
    // composer, and it works by sending a fresh copy of the bytes.
    publishFakeRpc({
      'ClaudeCodeService.history_image': async () => ({ data_uri: PNG }),
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [prompt([ref()])] });
    await drain(p);
    p.shadowRoot.querySelector('img.message-image').click();
    expect(p._lightboxImage).toBe(PNG);
    p.shadowRoot.querySelector('.message-image-reattach').click();
    expect(p._pendingImages).toEqual([PNG]);
  });

  it('draws a prompt that carries both bytes and pointers', async () => {
    // The live half of a prompt the user pasted into and then resumed.
    publishFakeRpc({
      'ClaudeCodeService.history_image': async () => ({ data_uri: PNG }),
    });
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'both',
          images: ['data:image/png;base64,BBB'],
          image_refs: [ref()],
        },
      ],
    });
    await drain(p);
    expect(tiles(p).images).toHaveLength(2);
  });

  it('hydrates the state-loaded path too', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    pushEvent('state-loaded', { messages: [prompt([ref()])] });
    await drain(p);
    expect(fetchImage).toHaveBeenCalledTimes(1);
    expect(tiles(p).images).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Pointers that arrive after the message
// ---------------------------------------------------------------------------
//
// A collaborator's `userMessage` broadcast carries no pointers: it goes out
// before the turn starts, and a pointer names the transcript entry the image
// lives in, which the CLI writes mid-turn. So they follow as `userMessageImages`
// and are attached to the message the request id names — which is a message
// only a passive observer has, because the sender is holding the bytes it
// pasted (specs5/4-features/images.md § Engine Service Integration).

describe('ChatPanel late image pointers', () => {
  const PNG = 'data:image/png;base64,iVBORw0KGgo=';
  const REQ = '1736956800000-a1b2c3';

  function ref(overrides = {}) {
    return {
      session_id: 's1',
      entry_uuid: 'u1',
      block: 1,
      media_type: 'image/png',
      ...overrides,
    };
  }

  async function drain(panel, rounds = 4) {
    for (let i = 0; i < rounds; i += 1) await settle(panel);
  }

  /** A prompt as a collaborator receives it: text, and a request id. */
  function observed(panel, requestId = REQ, content = 'look at this') {
    pushEvent('user-message', { content, request_id: requestId });
    return settle(panel);
  }

  function pointers(panel, refs, requestId = REQ) {
    pushEvent('user-message-images', {
      requestId,
      data: { image_refs: refs },
    });
    return settle(panel);
  }

  it('attaches them to the message and fetches the bytes', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    await observed(p);
    await pointers(p, [ref()]);
    await drain(p);
    expect(p.messages[0].image_refs).toEqual([ref()]);
    expect(fetchImage).toHaveBeenCalledWith('s1', 'u1', 1);
    expect(
      [...p.shadowRoot.querySelectorAll('img.message-image')],
    ).toHaveLength(1);
  });

  it('leaves the sender’s own message alone', async () => {
    // The sender has the data URI it pasted and fetches nothing. Nothing
    // checks "am I the sender": only the passive path stamps a request id
    // onto a message, so there is nothing here for this event to match.
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({
      'ClaudeCodeService.history_image': fetchImage,
      'ClaudeCodeService.chat_streaming': vi
        .fn()
        .mockResolvedValue({ status: 'started' }),
    });
    const p = mountPanel();
    await settle(p);
    p._pendingImages = ['data:image/png;base64,BBB'];
    p._input = 'look at this';
    await p._send();
    await settle(p);
    await pointers(p, [ref()]);
    await drain(p);
    expect(p.messages[0].image_refs).toBeUndefined();
    expect(fetchImage).not.toHaveBeenCalled();
    // The bytes it pasted are still the one tile it draws.
    expect(
      [...p.shadowRoot.querySelectorAll('img.message-image')],
    ).toHaveLength(1);
  });

  it('picks the message the request id names', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_image': vi
        .fn()
        .mockResolvedValue({ data_uri: PNG }),
    });
    const p = mountPanel();
    await settle(p);
    await observed(p, 'req-first', 'the first prompt');
    await observed(p, 'req-second', 'the second prompt');
    await pointers(p, [ref()], 'req-first');
    await drain(p);
    expect(p.messages[0].image_refs).toEqual([ref()]);
    expect(p.messages[1].image_refs).toBeUndefined();
  });

  it('does not add the same pointer twice', async () => {
    // A re-broadcast would otherwise draw the same screenshot again.
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    await observed(p);
    await pointers(p, [ref()]);
    await drain(p);
    await pointers(p, [ref()]);
    await drain(p);
    expect(p.messages[0].image_refs).toHaveLength(1);
    expect(fetchImage).toHaveBeenCalledTimes(1);
  });

  it('adds a second image announced separately', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_image': vi
        .fn()
        .mockResolvedValue({ data_uri: PNG }),
    });
    const p = mountPanel();
    await settle(p);
    await observed(p);
    await pointers(p, [ref()]);
    await drain(p);
    await pointers(p, [ref({ block: 2, entry_uuid: 'u2' })]);
    await drain(p);
    expect(p.messages[0].image_refs.map((r) => r.block)).toEqual([1, 2]);
  });

  it('ignores an event for a message it does not have', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    await observed(p, 'req-first');
    await pointers(p, [ref()], 'req-other');
    await drain(p);
    expect(p.messages[0].image_refs).toBeUndefined();
    expect(fetchImage).not.toHaveBeenCalled();
  });

  it('ignores a malformed event', async () => {
    const fetchImage = vi.fn().mockResolvedValue({ data_uri: PNG });
    publishFakeRpc({ 'ClaudeCodeService.history_image': fetchImage });
    const p = mountPanel();
    await settle(p);
    await observed(p);
    for (const detail of [
      {},
      { requestId: REQ },
      { requestId: REQ, data: {} },
      { requestId: REQ, data: { image_refs: [] } },
      { requestId: REQ, data: { image_refs: [null, 'nonsense'] } },
      { data: { image_refs: [ref()] } },
    ]) {
      pushEvent('user-message-images', detail);
      await settle(p);
    }
    await drain(p);
    expect(p.messages[0].image_refs).toBeUndefined();
    expect(fetchImage).not.toHaveBeenCalled();
    // Still live afterwards — a swallowed exception would leave the handler
    // wired but inert.
    await pointers(p, [ref()]);
    await drain(p);
    expect(p.messages[0].image_refs).toEqual([ref()]);
  });

  it('stops listening on disconnect', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_image': vi
        .fn()
        .mockResolvedValue({ data_uri: PNG }),
    });
    const p = mountPanel();
    await settle(p);
    await observed(p);
    p.remove();
    await pointers(p, [ref()]);
    expect(p.messages[0].image_refs).toBeUndefined();
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
    window.addEventListener('aic-toast', toasts);
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
      window.removeEventListener('aic-toast', toasts);
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
    // `pre_compact` hook already said compaction was coming, and the progress
    // overlay it drives is what clears itself on this event.
    const p = mountPanel();
    const reqId = await sendAndGetId(p);
    const toasts = vi.fn();
    window.addEventListener('aic-toast', toasts);
    try {
      boundary(reqId);
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('aic-toast', toasts);
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
    window.addEventListener('aic-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { stage: 'future_stage_we_dont_know_about' },
      });
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('aic-toast', toasts);
    }
  });

  it('doc_enrichment_* stages are ignored (handled elsewhere)', async () => {
    // Per spec: doc enrichment drives a header progress
    // bar, not a chat toast. Chat panel must not render
    // these even though they come through the same
    // channel.
    const p = mountPanel();
    const toasts = vi.fn();
    window.addEventListener('aic-toast', toasts);
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
      window.removeEventListener('aic-toast', toasts);
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
    window.addEventListener('aic-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { url: 'no stage here' },
      });
      await settle(p);
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('aic-toast', toasts);
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
    window.addEventListener('aic-toast', toasts);
    try {
      pushEvent('compaction-event', {
        event: { stage: 'url_fetch', url: 'test' },
      });
      await new Promise((r) => setTimeout(r, 10));
      expect(toasts).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('aic-toast', toasts);
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

// ---------------------------------------------------------------------------
// The session-directory size warning
// ---------------------------------------------------------------------------
//
// One sentence, once per server lifetime, on whichever of its two carriers
// notices first: the state snapshot ("checked at startup") or a finished turn
// ("and after each turn" — specs5/3-engine/history.md § Numeric constants).
// Both were delivered and nothing read either, so a repo whose transcripts had
// grown past a gigabyte said so to nobody.

describe('ChatPanel disk warning', () => {
  const WARNING =
    'Mirrored session transcripts are using 1.4 GiB in `.aic-dc/sessions/`. '
    + 'Deleting old sessions from the history browser reclaims the space.';

  function systemCards(panel) {
    return [
      ...panel.shadowRoot.querySelectorAll('.message-card.role-system'),
    ];
  }

  it('says it when the panel’s own snapshot carries it', async () => {
    publishFakeRpc({
      'ClaudeCodeService.get_current_state': async () => ({
        permission_mode: 'default',
        disk_warning: WARNING,
      }),
    });
    const p = mountPanel();
    await settle(p);
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0]).toEqual({
      role: 'user',
      content: WARNING,
      system_event: true,
    });
  });

  it('says nothing when there is nothing to say', async () => {
    // The overwhelmingly common case: a directory under the threshold, or a
    // warning some other client's snapshot already spent.
    publishFakeRpc({
      'ClaudeCodeService.get_current_state': async () => ({
        permission_mode: 'default',
        disk_warning: null,
      }),
    });
    const p = mountPanel();
    await settle(p);
    await settle(p);
    expect(p.messages).toEqual([]);
  });

  it('renders it as a system event, markdown and all', async () => {
    const p = mountPanel();
    await settle(p);
    pushEvent('post-response-complete', {
      requestId: 'req-1',
      data: { disk_warning: WARNING },
    });
    await settle(p);
    const cards = systemCards(p);
    expect(cards).toHaveLength(1);
    // The path it names is a code span in the sentence the service writes.
    expect(cards[0].querySelector('code')?.textContent)
      .toBe('.aic-dc/sessions/');
  });

  it('survives the restore that a state-loaded snapshot does', async () => {
    // Appended before the restore it travels with, the notice would be
    // dropped with the message list the restore replaces — and the server's
    // one-shot is already spent, so there is no second chance at it.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('state-loaded', {
      messages: [{ role: 'user', content: 'from the transcript' }],
      disk_warning: WARNING,
    });
    await settle(p);
    expect(p.messages.map((m) => m.content)).toEqual([
      'from the transcript',
      WARNING,
    ]);
  });

  it('says it even when the snapshot restores nothing', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('state-loaded', { messages: [], disk_warning: WARNING });
    await settle(p);
    expect(p.messages.map((m) => m.content)).toEqual([WARNING]);
  });

  it('says it to a client that is mid-stream', async () => {
    // The streaming guard is there to protect a live conversation from being
    // replaced by a snapshot; it is not a reason to withhold the warning.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._streaming).toBe(true);
    pushEvent('state-loaded', {
      messages: [{ role: 'user', content: 'a snapshot to ignore' }],
      disk_warning: WARNING,
    });
    await settle(p);
    expect(p.messages.map((m) => m.content)).toEqual(['hi', WARNING]);
  });

  it('takes it from any turn, not just this client’s', async () => {
    // The directory is the session's, not the turn's. A collaborator's turn
    // is as good a messenger as our own, so there is no request-id filter.
    const p = mountPanel();
    await settle(p);
    pushEvent('post-response-complete', {
      requestId: 'someone-elses-turn',
      data: { disk_warning: WARNING },
    });
    await settle(p);
    expect(p.messages.map((m) => m.content)).toEqual([WARNING]);
  });

  it('ignores a turn that carries no warning', async () => {
    const p = mountPanel();
    await settle(p);
    for (const detail of [
      { requestId: 'r' },
      { requestId: 'r', data: null },
      { requestId: 'r', data: { files_reindexed: [], context_usage: null } },
      { requestId: 'r', data: { disk_warning: null } },
      { requestId: 'r', data: { disk_warning: '' } },
      { requestId: 'r', data: { disk_warning: { text: WARNING } } },
      {},
    ]) {
      pushEvent('post-response-complete', detail);
      await settle(p);
    }
    expect(p.messages).toEqual([]);
    // Still live afterwards — a swallowed exception would leave the handler
    // wired but inert.
    pushEvent('post-response-complete', {
      requestId: 'r',
      data: { disk_warning: WARNING },
    });
    await settle(p);
    expect(p.messages).toHaveLength(1);
  });

  it('stops listening on disconnect', async () => {
    const p = mountPanel();
    await settle(p);
    p.remove();
    pushEvent('post-response-complete', {
      requestId: 'r',
      data: { disk_warning: WARNING },
    });
    expect(p.messages).toEqual([]);
  });
});